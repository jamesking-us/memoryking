"""Backend-agnostic store operations behind the storage port.

Authorization, capture, reads, grants, purge policy, idempotent retries, jobs, context
and status live here once, written in portable SQL against a dialect. A backend (SQLite
in `adapters.local_store`, PostgreSQL in `adapters.postgres_store`) supplies the
connection, the transaction discipline, the control tree and token signing. Neither
backend subclasses the other.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Self, cast

from memoryking.adapters.local_contract import validate_capture
from memoryking.application import lexical_index
from memoryking.application.blob_store import BlobStore
from memoryking.application.control_log import ControlLog
from memoryking.application.retention import (
    CHECKPOINT_BYTES_LIMIT,
    CHECKPOINT_LIMIT,
    EXPERIENCE_LIMIT,
    HANDOFF_LIMIT,
    HANDOFF_VIEW_LIMIT,
    expire_retries,
    index_receipt,
    live_checkpoints,
    live_experience,
    live_handoff_views,
    live_handoffs,
    release_all,
    release_jobs,
    release_receipts,
)
from memoryking.application.storage import Dialect, insert_ignore, upsert
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.local import (
    USER_AGENT_CAPABILITIES,
    USER_OWNER_CAPABILITIES,
    Clock,
    Decision,
    LocalError,
    Released,
    is_user_project,
    project_owner,
    temporal_valid,
    user_project,
)

Row = Any


CAPABILITIES = {
    "capture",
    "read",
    "export",
    "jobs",
    "admin",
    "review",
    "policy",
    "attest",
    "task_constraint",
    "checkpoint",
    "handoff",
    "inference",
    "repository",
}


RETRY_MS = 30 * 24 * 60 * 60 * 1000
# Instance lease: a holder renews at most this often, and another holder is refused until the
# renewal is this old. See "Instance lease" in docs/deployment.md.
LEASE_RENEW_MS = 15_000
LEASE_EXPIRY_MS = 60_000


QUEUE_LIMIT = 1000
DRAIN_BUDGET_MS = 2000  # one drain pass stays well inside the five-second transaction deadline
# The reindex call's own slice of the rebuild, and the size of store small enough to be rebuilt
# inside it; the rest is batched housekeeping (see `reindex`).
REINDEX_CALL_MS = 200
REINDEX_INLINE_LIMIT = 2000
# Work waiting on the queue. A pending job with a nonzero fence was reset by a reindex or
# restore (or already attempted): it is backlog the drain rebuilds, not growth to refuse.
QUEUED_JOBS = "SELECT count(*) FROM jobs WHERE tenant=? AND state IN ('leased','pending') AND (state='leased' OR fence=0)"


CONCURRENCY_LIMIT = 4


MAX_ATTEMPTS = 5


MAX_COUNTER = 9_007_199_254_740_991


ARTIFACT_LIMIT = 1024


ARTIFACT_BYTES_LIMIT = 134217728


INTENT_LIMIT = 4096


INTENT_SCOPE_LIMIT = 32


USAGE_LIMIT = 100000


OPEN_INTENT_STATES = ("intent_recorded", "dispatching", "unknown")


CAPACITY_LIMITS = {
    "jobs": QUEUE_LIMIT,
    "rendered_artifacts": ARTIFACT_LIMIT,
    "rendered_bytes": ARTIFACT_BYTES_LIMIT,
    "operation_intents": INTENT_LIMIT,
    "processing_usage": USAGE_LIMIT,
    "checkpoints": CHECKPOINT_LIMIT,
    "checkpoint_bytes": CHECKPOINT_BYTES_LIMIT,
    "handoffs": HANDOFF_LIMIT,
    "handoff_views": HANDOFF_VIEW_LIMIT,
    "experience_records": EXPERIENCE_LIMIT,
}


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", value):
        raise LocalError("invalid_identifier")
    return value


def uid(prefix: str) -> str:
    return prefix + "_" + secrets.token_hex(16)


def lease_holder(control_path: Path, instance_id: str | None) -> str:
    """Who holds a store: a hash of the operator's `--instance-id`, else of the control tree.

    Without an id the control directory's device and inode stand for the tree, so processes
    sharing one control tree on one volume compute the same holder, and a copy of the tree
    (a different inode) does not. Device and inode mean nothing across hosts or containers,
    so a deployment on more than one host must set the id.
    """
    if instance_id is not None:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", instance_id):
            raise LocalError("invalid_request")
        material = b"instance:" + instance_id.encode()
    else:
        info = os.stat(control_path)
        material = f"tree:{info.st_dev}:{info.st_ino}".encode()
    return hashlib.sha256(b"memoryking-lease:" + material).hexdigest()


class Visibility:
    """Per-transaction answers `_visible` would otherwise recompute for every candidate."""

    def __init__(self) -> None:
        self.checked: set[str] = set()  # observation ids whose hold state was fetched in bulk
        self.held: set[str] = set()
        self.projects: dict[tuple[str, str, str, int], bool] = {}  # read grant per project


class _Followed:
    """What one purge has retired so far, shared by the per-type follow-through methods."""

    def __init__(self, depends: Callable[[Any], bool], purged: set[str]) -> None:
        self.depends, self.purged = depends, purged
        self.retired = 0  # experience records retired: ranking is disabled when any was
        self.checkpoints: set[str] = set()
        self.artifacts: set[str] = set()


class StoreCore:
    """What every backend shares; the attributes below are supplied by the backend."""

    # Set by a store bound to one tenant (the shared profile); None for the local store.
    tenant: str | None = None

    db: Any
    dialect: Dialect
    control: ControlLog  # the signed journal, its anchor and the operations log
    control_path: Path
    lease: str  # the holder this process claims the instance lease as
    root: Path
    blobs: BlobStore  # where observation bodies live (the file store, or a test double)
    clock: Clock
    lock: threading.RLock
    store_id: str
    incarnation: str
    healthy: bool
    fault: Any
    counters: Any
    timings: Any
    retrieval_cache: dict[str, Any]
    anchor: dict[str, Any]
    last_utc: int
    # Daemon-side policies default off; the serving backend sets them from its flags.
    backup_policy: Any | None = None
    embedder: Any | None = None
    reranker: Any | None = None
    summary_idle_ms = 0
    _last_housekeeping = 0
    _layout_done = False
    # Set by the serving daemon: its (limit, in use) figures for the metrics endpoint.
    concurrency_gauge: Callable[[], dict[str, tuple[int, int]]] | None = None
    _lexical_pending: bool | None = None
    _lexical_rate: float | None = None  # observations per second over recent rebuild batches
    _last_summary_scan = 0

    # The operator's egress policy; None means the local recorder is the only provider.
    # A class default so every store, including PostgreSQL, reports a revision.
    egress_policy: Any | None = None

    dashboard: Any = None

    # 0 anchors every committing transaction before release; see `_expose_locked`.
    anchor_interval_ms: int = 0

    # Held by one briefing's transaction, never across transactions: what a transaction has
    # already established about holds and a principal's read grant per project. A policy
    # change takes the exclusive gate, so neither can move while a briefing holds the gate.
    visibility: Visibility | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        raise NotImplementedError

    @contextmanager
    def transaction(self) -> Iterator[Decision]:
        """A backend opens its transaction and yields the authorization instant."""
        raise NotImplementedError
        yield  # pragma: no cover

    def _expose(self) -> None:
        """A backend durably exposes committed evidence before releasing bytes."""
        raise NotImplementedError

    def flush_anchor(self, *, force: bool = False) -> None:
        """Write anchor state `_expose` left for later; a backend that has none does nothing."""

    def gc(self) -> int:
        """A backend removes unreferenced blobs and expired results in its own way."""
        raise NotImplementedError

    def _now(self) -> Decision:
        now = self.clock.utc_ms()
        if now < self.last_utc:
            raise LocalError("clock_untrusted")
        self.last_utc = now
        return Decision(now, self.clock.monotonic_ms(), now + 5000)

    def _generation(self, tenant: str, principal: str) -> int:
        """The principal's current credential generation; 0 until one is advanced."""
        row = self.db.execute(
            "SELECT generation FROM credential_generations WHERE tenant=? AND principal=?",
            (tenant, principal),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def _advance_generation(self, tenant: str, principal: str) -> None:
        """Retire every credential issued for the principal so far (policy; replayed on restore)."""
        self.db.execute(
            upsert("credential_generations"),
            (tenant, principal, self._generation(tenant, principal) + 1),
        )

    def _token(self, kind: str, **fields: Any) -> str:
        if kind == "credential":
            # Generation 0 is omitted, so a credential that was never rotated is byte-identical
            # to one issued before generations existed. An internal caller that mints a
            # credential for a principal gets the current generation unless it names one.
            generation = fields.pop("generation", None)
            if generation is None:
                generation = self._generation(fields["tenant"], fields["principal"])
            if generation:
                fields["generation"] = generation
        return str(
            self.control.sign(
                {"kind": kind, "store": self.store_id, "incarnation": self.incarnation, **fields}
            )
        )

    def _decode(self, token: str, kind: str) -> dict[str, Any]:
        body = self.control.verify(token)
        if body.get("store") != self.store_id or body.get("kind") != kind:
            raise LocalError("invalid_token")
        if body.get("incarnation") != self.incarnation:
            raise LocalError("history_changed")
        return body

    def _auth(self, token: str, capability: str, project: str, at: int) -> dict[str, Any]:
        identity = self._decode(token, "credential")
        # A user scope is private to the user and the agents that act for them: a `*` grant,
        # administrators' included, never reaches it.
        rows = self.db.execute(
            "SELECT * FROM grants WHERE tenant=? AND principal=? AND project=?"
            if is_user_project(project)
            else "SELECT * FROM grants WHERE tenant=? AND principal=? AND project IN (?, '*')",
            (identity["tenant"], identity["principal"], project),
        ).fetchall()
        if not any(
            (
                capability in json.loads(row["capabilities"])
                or "admin" in json.loads(row["capabilities"])
            )
            and temporal_valid(at, row["starts"], row["expires"])
            for row in rows
        ):
            # Which capability an operation needs is public (the reference lists it); naming
            # it tells the caller nothing about what the store holds.
            raise LocalError("denied", detail={"required_capability": capability})
        # After the grant check, so a principal without a current grant is refused exactly as
        # before; only a credential that would otherwise be accepted learns it was replaced.
        generation = identity.get("generation", 0)
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation != self._generation(identity["tenant"], identity["principal"])
        ):
            # Revoked, rotated or replaced: only the principal's current credential is accepted.
            raise LocalError("credential_replaced")
        return identity

    def user_scope(self, who: dict[str, Any], capability: str, at: int) -> str | None:
        """The user scope this principal acts for with `capability` now, if any.

        A principal acts for at most one user, so the answer is unambiguous. Decided from the
        principal's own grants, so it never names a scope the caller cannot already use. A
        delegated credential (a narrowed token for one project) acts for no user."""
        if who.get("delegation"):
            return None
        for row in self.db.execute(
            "SELECT project,capabilities,starts,expires FROM grants WHERE tenant=? AND principal=? AND substr(project,1,6)=? ORDER BY project",
            (who["tenant"], who["principal"], user_project("")),
        ).fetchall():
            capabilities = row["capabilities"]
            if isinstance(capabilities, str):
                capabilities = json.loads(capabilities)
            if capability in capabilities and temporal_valid(at, row["starts"], row["expires"]):
                return cast(str, row["project"])
        return None

    def user_authority(self, token: str, project: str, at: int) -> dict[str, Any]:
        """Authority over a user scope's membership and contents: the user, or an administrator.

        An administrator can administer (link, unlink, purge) but cannot read; see `_auth`."""
        try:
            return self._auth(token, "admin", "*", at)
        except LocalError as exc:
            if exc.code != "denied":
                raise
        who = self._auth(token, "policy", project, at)
        if who["principal"] != project_owner(project):
            raise LocalError("denied")
        return who

    def _subject_auth(self, token: str, capability: str, project: str, at: int) -> dict[str, Any]:
        """Internal intersection check; never an alternate transport credential entry point.

        The subject may be another principal, so a refusal never says which capability they
        lack."""
        try:
            return self._auth(token, capability, project, at)
        except LocalError as exc:
            raise LocalError(exc.code) from None

    def namespace(self, credential: str, project: str, name: str | None = None) -> str:
        """A capture namespace token.

        A named namespace is deterministic for the principal, so a retry with the same
        key from any process replays or conflicts exactly as documented. An unnamed
        namespace is fresh and random.
        """
        with self.transaction() as decision:
            identity = self._auth(credential, "capture", project, decision.evaluated_ms)
            token = self._token(
                "namespace",
                tenant=identity["tenant"],
                principal=identity["principal"],
                id="nsn_" + identifier(name) if name is not None else uid("ns"),
            )
        self._expose()
        return token

    def _meta_int(self, key: str) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return int(row[0]) if row is not None else 0

    def _control_guard(self) -> Any:
        """What serializes control-tree writers beyond this process; the shared store overrides."""
        return nullcontext()

    def compact_operations(self, directory: Path) -> int:
        """Drop operations-log segments that no retained backup needs; returns how many went.

        `directory` holds the retained backups. A segment goes only when it is sealed, lies at
        or below the operations position of the OLDEST backup there (so restoring any retained
        backup still finds the intents), and every operation in it is resolved and reconciled
        in this incarnation. With no backup there, nothing is dropped. The identities a dropped
        segment consumed stay consumed (see `OperationsLog.compact`).
        """
        from memoryking.adapters.operations_log import oldest_coverage

        coverage = oldest_coverage(self.control, directory)
        if coverage <= 0:
            return 0
        current = self.incarnation

        def resolved(identities: Any) -> bool:
            for tenant, identity in identities:
                row = self.db.execute(
                    "SELECT state,incarnation,reconciled_incarnation FROM operation_intents WHERE tenant=? AND id=?",
                    (tenant, identity),
                ).fetchone()
                if (
                    row is None
                    or row["state"] not in ("succeeded", "failed")
                    or (row["reconciled_incarnation"] or row["incarnation"]) != current
                ):
                    return False
            return True

        with self.transaction(), self._control_guard():
            return int(self.control.operations.compact(self.anchor, coverage, resolved))

    def _lease_writable(self) -> bool:
        """Whether this store's database role may write the lease row (see `_hold_lease`)."""
        return True

    def _hold_lease(self, at: int, *, writer: bool = True) -> None:
        """Claim, renew or be refused the instance lease for this store (see `lease_holder`).

        The same holder always passes, so a restart after a crash is never blocked and several
        processes sharing one control tree co-hold. Another holder is refused while the
        recorded renewal is younger than LEASE_EXPIRY_MS. Renewal is written at most every
        LEASE_RENEW_MS, and only by a transaction that may write; a tenant with no row claims
        it on its first writing transaction. A transaction whose role may not write the lease
        (the shared worker) compares like a reader and leaves claiming and renewal to the next
        runtime writer.
        """
        key = self.tenant or "*"
        row = self.db.execute(
            "SELECT holder,renewed_ms FROM instance_lease WHERE tenant=?", (key,)
        ).fetchone()
        if row is None:
            if writer and self._lease_writable():
                self.db.execute(
                    "INSERT INTO instance_lease(tenant,holder,renewed_ms) VALUES (?,?,?)",
                    (key, self.lease, at),
                )
            return
        if hmac.compare_digest(str(row["holder"]).encode(), self.lease.encode()):
            if writer and at - row["renewed_ms"] >= LEASE_RENEW_MS and self._lease_writable():
                self.db.execute("UPDATE instance_lease SET renewed_ms=? WHERE tenant=?", (at, key))
            return
        if at - row["renewed_ms"] < LEASE_EXPIRY_MS:
            raise LocalError("control_lease_held")
        if writer and self._lease_writable():
            self.db.execute(
                "UPDATE instance_lease SET holder=?,renewed_ms=? WHERE tenant=?",
                (self.lease, at, key),
            )

    def _event(self, tenant: str, kind: str, subject: str, at: int, actor: str = "system") -> int:
        if (
            self.db.execute("SELECT position FROM tenants WHERE tenant=?", (tenant,)).fetchone()[0]
            >= MAX_COUNTER
        ):
            raise LocalError("counter_exhausted")
        self.db.execute("UPDATE tenants SET position=position+1 WHERE tenant=?", (tenant,))
        position: int = self.db.execute(
            "SELECT position FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()[0]
        self.db.execute(
            "INSERT INTO events(tenant,position,event_id,kind,subject,origin,at_ms,actor) VALUES (?,?,?,?,?,?,?,?)",
            (tenant, position, uid("evt"), kind, subject, self.incarnation, at, actor),
        )
        return position

    def _blob(self, tenant: str, content: bytes) -> str:
        digest = self.blobs.put(identifier(tenant), content)
        self.fault("after_blob")
        return digest

    def capture(
        self, credential: str, namespace: str, key: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        identifier(key)
        validate_capture(request)
        readers = request["readers"]
        digest = canonical_hash(request)
        with self.transaction() as decision:
            who = self._auth(credential, "capture", request["project"], decision.evaluated_ms)
            ns = self._decode(namespace, "namespace")
            if any(ns[x] != who[x] for x in ("tenant", "principal")):
                raise LocalError("denied")
            tenant = who["tenant"]
            lookup = (tenant, self.incarnation, ns["id"], who["principal"], "capture", key)
            retry = self.db.execute(
                "SELECT * FROM retries WHERE tenant=? AND incarnation=? AND namespace=? AND principal=? AND operation=? AND key=?",
                lookup,
            ).fetchone()
            if retry is not None:
                if retry["hash"] != digest:
                    raise LocalError("idempotency_conflict")
                if retry["result"] is None or decision.evaluated_ms >= retry["expires"]:
                    raise LocalError("idempotency_result_expired")
                result = cast(dict[str, Any], json.loads(retry["result"]))
            else:
                if self.db.execute(
                    "SELECT 1 FROM retired_sources WHERE tenant=? AND principal=? AND source=? AND source_event=?",
                    (tenant, who["principal"], request["source"], request["source_event"]),
                ).fetchone():
                    raise LocalError("source_retired")
                occurrence = self.db.execute(
                    "SELECT * FROM observations WHERE tenant=? AND principal=? AND source=? AND source_event=?",
                    (tenant, who["principal"], request["source"], request["source_event"]),
                ).fetchone()
                if occurrence is not None:
                    if occurrence["deleted"]:
                        raise LocalError("source_retired")
                    if occurrence["payload_hash"] != digest:
                        raise LocalError("source_conflict")
                    from memoryking.adapters.trust import Trust

                    result = {
                        **self._capture_result(tenant, occurrence["id"], occurrence["position"]),
                        "held": Trust(self).held(tenant, occurrence["id"]),
                    }
                else:
                    if request.get("supersedes"):
                        from memoryking.adapters.corrections import Corrections

                        Corrections(self).earlier(who, request, decision.evaluated_ms)
                    count = self.db.execute(QUEUED_JOBS, (tenant,)).fetchone()[0]
                    if count >= QUEUE_LIMIT:
                        raise LocalError("queue_full")
                    blob = self._blob(tenant, request["text"].encode())
                    lexical_index.ensure(self.db, tenant)
                    obs = uid("obs")
                    position = self._event(
                        tenant, "observation_captured", obs, decision.evaluated_ms, who["principal"]
                    )
                    self.db.execute(
                        "INSERT INTO observations(tenant,id,project,principal,source,source_event,payload_hash,blob,readers,position,origin,classification,observed_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            tenant,
                            obs,
                            request["project"],
                            who["principal"],
                            request["source"],
                            request["source_event"],
                            digest,
                            blob,
                            json.dumps(readers),
                            position,
                            self.incarnation,
                            request["classification"],
                            request["observed_at_ms"],
                        ),
                    )
                    lexical_index.add(self.db, tenant, request["project"], obs, request["text"])
                    self._index_anchors(tenant, obs, request["text"])
                    if request.get("supersedes"):
                        from memoryking.adapters.corrections import Corrections

                        Corrections(self).link(who, request, obs, position)
                    from memoryking.adapters.trust import Trust

                    trust = Trust(self)
                    held = trust.level(tenant, request["project"], request["source"]) == "untrusted"
                    if held:
                        # Recorded exactly like any capture, but invisible to every read
                        # until an admin releases it; nothing is derived from it meanwhile.
                        trust.hold(
                            tenant,
                            request["project"],
                            obs,
                            request["source"],
                            decision.evaluated_ms,
                        )
                    self._enqueue(tenant, position, obs, who["principal"])
                    result = {**self._capture_result(tenant, obs, position), "held": held}
                self.db.execute(
                    "INSERT INTO retries(tenant,incarnation,namespace,principal,operation,key,hash,result,observation,expires) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        *lookup,
                        digest,
                        json.dumps(result),
                        result["observation"],
                        decision.evaluated_ms + RETRY_MS,
                    ),
                )
        with self.lock:
            self._expose()
        return result

    def _index_anchors(self, tenant: str, observation: str, text: str) -> int:
        """Record the exact names the text carries; deterministic, bounded, no model."""
        from memoryking.domain.anchors import extract

        found = extract(text)
        for kind, anchor in found:
            self.db.execute(
                insert_ignore("anchors", "tenant,id,anchor,kind"),
                (tenant, observation, anchor, kind),
            )
        return len(found)

    def _capture_result(self, tenant: str, observation: str, position: int) -> dict[str, Any]:
        return {
            "observation": observation,
            "position": position,
            "incarnation": self.incarnation,
            "watermark": self._token("watermark", tenant=tenant, position=position),
            "handle": self._token("observation", tenant=tenant, id=observation),
        }

    def _enqueue(self, tenant: str, position: int, obs: str | None, principal: str) -> None:
        epoch = self.db.execute(
            "SELECT purge_epoch FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()[0]
        generation = self.db.execute(
            "SELECT generation FROM projections WHERE tenant=?", (tenant,)
        ).fetchone()[0]
        self.db.execute(
            "INSERT INTO jobs(tenant,id,position,observation,incarnation,generation,state,purge_epoch,revision,principal) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                tenant,
                uid("job"),
                position,
                obs,
                self.incarnation,
                generation,
                "pending" if obs else "noop",
                epoch,
                1,
                principal,
            ),
        )

    def _visible(self, who: dict[str, Any], row: Row, at: int) -> bool:
        if row["deleted"]:
            return False
        memo = self.visibility
        # A held capture is on nobody's reading list until an admin releases it.
        if memo is not None and row["id"] in memo.checked:
            held = row["id"] in memo.held
        else:
            held = (
                self.db.execute(
                    "SELECT 1 FROM observation_holds WHERE tenant=? AND id=? AND state='held'",
                    (row["tenant"], row["id"]),
                ).fetchone()
                is not None
            )
        if held:
            return False
        key = (who["tenant"], who["principal"], row["project"], at)
        allowed = None if memo is None else memo.projects.get(key)
        if allowed is None:
            try:
                self._subject_auth(
                    self._token("credential", tenant=who["tenant"], principal=who["principal"]),
                    "read",
                    row["project"],
                    at,
                )
                allowed = True
            except LocalError:
                allowed = False
            if memo is not None:
                memo.projects[key] = allowed
        if not allowed:
            return False
        readers = json.loads(row["readers"])
        return readers is None or who["principal"] in readers

    def observation_rows(self, tenant: str, ids: list[str]) -> dict[str, Row]:
        """The rows of these observations and, when a briefing keeps a visibility memo, which
        of them are held: one `IN` query per hundred ids each, however many candidates."""
        rows: dict[str, Row] = {}
        memo = self.visibility
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]
            marks = ",".join("?" for _ in chunk)
            for row in self.db.execute(
                "SELECT * FROM observations WHERE tenant=? AND id IN (" + marks + ")",
                (tenant, *chunk),
            ).fetchall():
                rows[row["id"]] = row
            if memo is not None:
                memo.checked.update(chunk)
                memo.held.update(
                    held[0]
                    for held in self.db.execute(
                        "SELECT id FROM observation_holds WHERE tenant=? AND state='held' AND id IN ("
                        + marks
                        + ")",
                        (tenant, *chunk),
                    ).fetchall()
                )
        return rows

    def read(
        self,
        credential: str,
        project: str,
        *,
        observation: str | None = None,
        query: str | None = None,
        cursor: str | None = None,
        export: bool = False,
        temporal_requirements: list[tuple[int | None, int | None]] | None = None,
    ) -> Released:
        """Bounded capture inspection/export; ranked retrieval belongs to P04.

        temporal_requirements is an internal seam for future constraint/verification
        checks, not a caller-supplied policy API. All use the one persisted instant.
        """
        identifier(project)
        with self.transaction() as decision:
            who = self._auth(
                credential, "export" if export else "read", project, decision.evaluated_ms
            )
            if temporal_requirements and not all(
                temporal_valid(decision.evaluated_ms, a, b) for a, b in temporal_requirements
            ):
                raise LocalError("temporal_requirement_failed")
            after = 0
            if cursor:
                resume = self._decode(cursor, "cursor")
                if (
                    any(resume[x] != who[x] for x in ("tenant", "principal"))
                    or resume["project"] != project
                    or resume["query"] != query
                    or resume["export"] != export
                ):
                    raise LocalError("denied")
                after = resume["position"]
            args: list[Any] = [who["tenant"], project, after]
            sql = "SELECT o.* FROM observations o WHERE tenant=? AND project=? AND position>? AND deleted=0"
            if observation:
                sql += " AND id=?"
                handle = self._decode(observation, "observation")
                if handle["tenant"] != who["tenant"]:
                    raise LocalError("denied")
                args.append(handle["id"])
            if query:
                if len(query) > 256:
                    raise LocalError("invalid_query")
                sql += (
                    " AND id IN (SELECT id FROM lexical WHERE tenant=? AND "
                    + self.dialect.lexical_match()
                    + ")"
                )
                args.extend([who["tenant"], '"' + query.replace('"', '""') + '"'])
            rows = self.db.execute(sql + " ORDER BY position LIMIT 32", args).fetchall()
            items: list[dict[str, Any]] = []
            last = after
            for row in rows:
                last = row["position"]
                if self._visible(who, row, decision.evaluated_ms):
                    raw = self.blobs.get(who["tenant"], row["blob"])
                    items.append(
                        {
                            "id": row["id"],
                            "authority": "observed_episode",
                            "assurance": "actor_reported",
                            "handle": self._token(
                                "observation", tenant=who["tenant"], id=row["id"]
                            ),
                            "source": row["source"],
                            "source_event": row["source_event"],
                            "principal": row["principal"],
                            "project": row["project"],
                            "text": raw.decode(),
                            "position": row["position"],
                            "origin": row["origin"],
                            "classification": row["classification"],
                            "observed_at_ms": row["observed_at_ms"],
                        }
                    )
                    if sum(len(i["text"].encode()) for i in items) >= 131072:
                        break
            if observation and not items:
                raise LocalError("unavailable")
            receipt = uid("rcpt")
            resume_token = (
                self._token(
                    "cursor",
                    tenant=who["tenant"],
                    principal=who["principal"],
                    project=project,
                    position=last,
                    query=query,
                    export=export,
                )
                if rows
                else None
            )
            result = {
                "items": items,
                "cursor": resume_token,
                "receipt": receipt,
                "incarnation": self.incarnation,
                "evaluated_ms": decision.evaluated_ms,
                "deadline_ms": decision.deadline_ms,
            }
            epochs = self.db.execute(
                "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
            ).fetchone()
            self.db.execute(
                "INSERT INTO receipts(tenant,id,principal,incarnation,evaluated,deadline,policy_epoch,purge_epoch,response_hash,observation_ids) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    receipt,
                    who["principal"],
                    self.incarnation,
                    decision.evaluated_ms,
                    decision.deadline_ms,
                    *epochs,
                    canonical_hash(result),
                    json.dumps([i["id"] for i in items]),
                ),
            )
            index_receipt(self.db, who["tenant"], receipt)
            self.fault("after_validation")
        with self.lock:
            self._expose()
        return Released(canonical_bytes(result), decision, self.clock)

    def grant(
        self,
        credential: str,
        principal: str,
        project: str,
        capabilities: list[str],
        *,
        starts: int | None = None,
        expires: int | None = None,
        rotate: bool = False,
        for_user: str | None = None,
    ) -> str:
        """Grant (or restate) access and return the principal's credential.

        With `rotate`, the principal's credential generation advances in the same journaled
        decision, so every credential issued before it stops working. With `for_user` (a
        user principal, or `@self` for the caller) the principal also acts for that user:
        it gets the agent capabilities in the user's scope, one more journaled grant.
        A grant on a user scope itself (`_user_<principal>`) is the same record.
        """
        identifier(principal)
        identifier(project)
        scoped = is_user_project(project)
        if scoped:
            allowed = (
                USER_OWNER_CAPABILITIES
                if principal == project_owner(project)
                else USER_AGENT_CAPABILITIES
            )
            if not capabilities or not set(capabilities) <= allowed:
                raise LocalError("invalid_capabilities")
            identifier(project_owner(project))
        elif not capabilities or not set(capabilities) <= CAPABILITIES - {"admin"}:
            raise LocalError("invalid_capabilities")
        if for_user is not None and for_user != "@self":
            identifier(user_project(for_user))
        if starts is not None and expires is not None and starts >= expires:
            raise LocalError("invalid_interval")
        with self.transaction() as decision:
            at = decision.evaluated_ms
            who = (
                self.user_authority(credential, project, at)
                if scoped
                else self._auth(credential, "admin", "*", at)
            )
            owner = who["principal"] if for_user == "@self" else for_user
            if scoped:
                self._link_checked(who, principal, project, at)
            # Replacement can reduce authority, so every change goes to the current journal.
            action = {
                "kind": "grant",
                "actor": who["principal"],
                "tenant": who["tenant"],
                "principal": principal,
                "project": project,
                "capabilities": capabilities,
                "starts": starts,
                "expires": expires,
            }
            if rotate:
                action["rotate"] = True
            record = self._journal(action)
            self._apply(record, at)
            if owner is not None and not scoped:
                self._link_user(
                    credential, who, principal, owner, capabilities, starts, expires, at
                )
            token = self._token("credential", tenant=who["tenant"], principal=principal)
        self._expose()
        return token

    def _link_checked(self, who: dict[str, Any], principal: str, project: str, at: int) -> None:
        """A principal acts for one user only: refuse a second scope for the same principal."""
        other = self.db.execute(
            "SELECT 1 FROM grants WHERE tenant=? AND principal=? AND substr(project,1,6)=? AND project<>?",
            (who["tenant"], principal, user_project(""), project),
        ).fetchone()
        if other is not None:
            raise LocalError("user_link_conflict")

    def _link_user(
        self,
        credential: str,
        who: dict[str, Any],
        principal: str,
        owner: str,
        requested: list[str],
        starts: int | None,
        expires: int | None,
        at: int,
    ) -> None:
        """Make `principal` act for `owner`, creating the owner's own scope grant first.

        The link carries only what the credential was issued with, and only what an agent may
        hold in a user scope; a credential with neither `capture` nor `read` acts for nobody."""
        project = user_project(owner)
        allowed = USER_OWNER_CAPABILITIES if principal == owner else USER_AGENT_CAPABILITIES
        capabilities = sorted(set(requested) & allowed)
        if not capabilities:
            return
        # Authority over that scope is checked for this very call, not inherited from the
        # grant it accompanies: only an administrator or the user may link anyone to it.
        self.user_authority(credential, project, at)
        self._link_checked(who, principal, project, at)
        steps: list[tuple[str, list[str], int | None, int | None]] = []
        if (
            principal != owner
            and self.db.execute(
                "SELECT 1 FROM grants WHERE tenant=? AND principal=? AND project=?",
                (who["tenant"], owner, project),
            ).fetchone()
            is None
        ):
            steps.append((owner, sorted(USER_OWNER_CAPABILITIES), None, None))
        steps.append((principal, capabilities, starts, expires))
        for name, capabilities, start, end in steps:
            record = self._journal(
                {
                    "kind": "grant",
                    "actor": who["principal"],
                    "tenant": who["tenant"],
                    "principal": name,
                    "project": project,
                    "capabilities": capabilities,
                    "starts": start,
                    "expires": end,
                }
            )
            self._apply(record, at)

    def grants(self, credential: str) -> list[dict[str, Any]]:
        """Every grant in the caller's tenant, for operators; needs admin rights."""
        with self.transaction() as decision:
            who = self._auth(credential, "admin", "*", decision.evaluated_ms)
            at = decision.evaluated_ms
            items = []
            for row in self.db.execute(
                "SELECT principal,project,capabilities,starts,expires FROM grants WHERE tenant=? ORDER BY principal,project",
                (who["tenant"],),
            ).fetchall():
                capabilities = row["capabilities"]
                if isinstance(capabilities, str):
                    capabilities = json.loads(capabilities)
                items.append(
                    {
                        "principal": row["principal"],
                        "project": row["project"],
                        "capabilities": sorted(capabilities),
                        "starts": row["starts"],
                        "expires": row["expires"],
                        "valid_now": temporal_valid(at, row["starts"], row["expires"]),
                    }
                )
        self._expose()
        return items

    def revoke(self, credential: str, principal: str, project: str) -> None:
        identifier(principal)
        identifier(project)
        self._policy(credential, {"kind": "revoke", "principal": principal, "project": project})

    def purge(self, credential: str, observation: str) -> None:
        self._policy(credential, {"kind": "purge", "observation": identifier(observation)})
        # Only the purged observation's own file is looked at: no sweep of the blob directory.
        self.collect_purged()

    def readiness(self) -> dict[str, bool]:
        raise NotImplementedError

    def extra_gauges(self) -> list[Any]:
        raise NotImplementedError

    def _purged_blobs(self) -> list[tuple[str, str]]:
        if "_purged" not in self.__dict__:
            self._purged: list[tuple[str, str]] = []
        return self._purged

    def _mark_gc_pending(self) -> None:
        """Record in the database that blob files may be waiting to be deleted, in the same
        transaction as the purge, so a crash before the delete is found at the next start."""

    def _gc_settled(self) -> None:
        """Nothing is waiting any more (the counterpart of `_mark_gc_pending`)."""

    def _delete_unreferenced(self, items: list[tuple[str, str]]) -> int:
        removed = 0
        with self.lock:
            for tenant, digest in dict.fromkeys(items):
                if (
                    self.db.execute(
                        "SELECT 1 FROM observations WHERE tenant=? AND blob=? LIMIT 1",
                        (tenant, digest),
                    ).fetchone()
                    is None
                ):
                    removed += self.blobs.delete(tenant, digest)
            self.blobs.sync()
        return removed

    def collect_purged(self) -> int:
        """Delete the files of observations purged since the last call that no other
        observation still names. Content is immutable and addressed by hash, so this is the
        whole cleanup: `gc` stays as the explicit full sweep for orphans left by a crash."""
        items, self._purged = self._purged_blobs(), []
        if not items:
            return 0
        try:
            removed = self._delete_unreferenced(items)
        except BaseException:
            self._purged_blobs().extend(items)
            raise
        if not self._purged_blobs():
            self._gc_settled()
        return removed

    def _policy(self, credential: str, action: dict[str, Any]) -> None:
        with self.transaction() as decision:
            at = decision.evaluated_ms
            tenant = self._decode(credential, "credential")["tenant"]
            project = action.get("project")
            if action["kind"] == "purge":
                found = self.db.execute(
                    "SELECT project FROM observations WHERE tenant=? AND id=?",
                    (tenant, action["observation"]),
                ).fetchone()
                project = found[0] if found is not None else None
            who = (
                # In a user scope the user decides, and an administrator may still purge it.
                self.user_authority(credential, project, at)
                if isinstance(project, str) and is_user_project(project)
                else self._auth(credential, "admin", "*", at)
            )
            action["tenant"] = who["tenant"]
            action["actor"] = who["principal"]
            record = self._journal(action)
            self._apply(record, decision.evaluated_ms)
        self._expose()

    def _journal(self, action: dict[str, Any]) -> dict[str, Any]:
        if action["kind"] == "purge":
            row = self.db.execute(
                "SELECT principal,source,source_event FROM observations WHERE tenant=? AND id=?",
                (action["tenant"], action["observation"]),
            ).fetchone()
            if row is None:
                raise LocalError("unavailable")
            action["source_identity"] = list(row)
        try:
            if action["kind"] == "operation":
                # Operation intents live in their own compactable, hash-chained log.
                record = self.control.operations.append(self.anchor, action)
            else:
                record = self.control.append(self.anchor, action)
            self.fault("after_journal")
            return record
        except LocalError as exc:
            if exc.code != "journal_record_limit":
                # Nothing was written for a refused oversized record; the store stays usable.
                self.healthy = False
            raise
        except BaseException:
            self.healthy = False
            raise

    def _purge_dependents(self, tenant: str, observation: str) -> None:
        """Erase derived copies along recorded dependencies.

        Rendered artifacts, checkpoints, handoffs and experience records record the
        observations and claims they were built from, so only those built from the purged
        source or its dependent claims are retired. Replay results carry no dependency
        index and are retired tenant-wide; ranking is disabled when any experience record
        was retired, because its evaluation may have scored the purged material.
        """
        from memoryking.adapters.knowledge_store import Knowledge

        purged = {observation}
        purged.update(
            row[0]
            for row in self.db.execute(
                "SELECT revision FROM evidence_spans WHERE tenant=? AND observation=?",
                (tenant, observation),
            ).fetchall()
        )
        Knowledge(self).purge_dependencies(tenant, observation)

        def depends(raw: Any) -> bool:
            try:
                deps = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                return True
            if not isinstance(deps, list):
                return True
            return any(isinstance(d, dict) and d.get("id") in purged for d in deps)

        # A fixed point: whatever is retired here joins the purged set, so anything
        # derived from it (a summary of a purged checkpoint, an atlas page over a
        # handoff view, an artifact over a retired experience record) goes too.
        followed = _Followed(depends, purged)
        changed = True
        while changed:
            changed = False
            for follow in self._FOLLOW.values():
                changed |= getattr(self, follow)(tenant, followed)
        retired, purged_checkpoints, purged_artifacts = (
            followed.retired,
            followed.checkpoints,
            followed.artifacts,
        )
        # Session summaries hold verbatim checkpoint text: erase every summary of a
        # purged checkpoint or whose artifact was purged.
        for identifier in purged_checkpoints:
            self.db.execute(
                "DELETE FROM session_summaries WHERE tenant=? AND checkpoint=?",
                (tenant, identifier),
            )
        for identifier in purged_artifacts:
            self.db.execute(
                "DELETE FROM session_summaries WHERE tenant=? AND id=?", (tenant, identifier)
            )
        self.db.execute(
            "UPDATE retries SET result=NULL WHERE tenant=? AND namespace IN ('working-v1','handoff-v1','experience-v1')",
            (tenant,),
        )
        if retired:
            self.db.execute(
                "UPDATE ranking_state SET configuration=NULL,version=version+1,reconciliation=0 WHERE tenant=?",
                (tenant,),
            )

    # The record types a purge follows (those with a dependency list), each with the method that
    # retires its rows; tests/test_records.py asserts this is the registry's set.
    _FOLLOW = {
        "checkpoints": "_follow_checkpoints",
        "experience_records": "_follow_experience",
        "rendered_artifacts": "_follow_artifacts",
    }

    def _follow_checkpoints(self, tenant: str, followed: "_Followed") -> bool:
        changed = False
        for row in self.db.execute(
            "SELECT id,dependencies FROM checkpoints WHERE tenant=? AND state!='purged'",
            (tenant,),
        ).fetchall():
            if followed.depends(row["dependencies"]):
                self.db.execute(
                    "UPDATE checkpoints SET state='purged',body=NULL,dependencies='[]' WHERE tenant=? AND id=?",
                    (tenant, row["id"]),
                )
                followed.purged.update(
                    handoff[0]
                    for handoff in self.db.execute(
                        "SELECT id FROM handoffs WHERE tenant=? AND checkpoint=?",
                        (tenant, row["id"]),
                    ).fetchall()
                )
                self.db.execute(
                    "UPDATE handoffs SET state='purged' WHERE tenant=? AND checkpoint=?",
                    (tenant, row["id"]),
                )
                followed.purged.add(row["id"])
                followed.checkpoints.add(row["id"])
                changed = True
        return changed

    def _follow_experience(self, tenant: str, followed: "_Followed") -> bool:
        changed = False
        for row in self.db.execute(
            "SELECT id,dependencies FROM experience_records WHERE tenant=? AND body IS NOT NULL",
            (tenant,),
        ).fetchall():
            if followed.depends(row["dependencies"]):
                self.db.execute(
                    "UPDATE experience_records SET body=NULL,dependencies='[]',readers=NULL WHERE tenant=? AND id=?",
                    (tenant, row["id"]),
                )
                followed.purged.add(row["id"])
                followed.retired += 1
                changed = True
        return changed

    def _follow_artifacts(self, tenant: str, followed: "_Followed") -> bool:
        changed = False
        for row in self.db.execute(
            "SELECT id,dependencies FROM rendered_artifacts WHERE tenant=? AND state='retained'",
            (tenant,),
        ).fetchall():
            if followed.depends(row["dependencies"]):
                self.db.execute(
                    "UPDATE rendered_artifacts SET state='purged',body=NULL,request_json=NULL,diagnostics=NULL,dependencies='[]' WHERE tenant=? AND id=?",
                    (tenant, row["id"]),
                )
                followed.purged.add(row["id"])
                followed.artifacts.add(row["id"])
                changed = True
        return changed

    def _apply(self, record: dict[str, Any], at: int) -> None:
        # A record advances the position of the log it came from: operation records have a
        # sequence of their own, so one never hides behind the journal's.
        applied_key = "operations" if record.get("log") == "operations" else "journal"
        if record["sequence"] <= self._meta_int(applied_key):
            return
        action = record["action"]
        tenant = action["tenant"]
        if self.db.execute("SELECT 1 FROM tenants WHERE tenant=?", (tenant,)).fetchone() is None:
            raise LocalError("journal_tenant_missing")
        kind = action["kind"]
        if kind == "grant":
            self.db.execute(
                upsert("grants"),
                (
                    tenant,
                    action["principal"],
                    action["project"],
                    json.dumps(action["capabilities"]),
                    action["starts"],
                    action["expires"],
                ),
            )
            if action.get("rotate"):
                self._advance_generation(tenant, action["principal"])
        elif kind == "revoke":
            held = self.db.execute(
                "SELECT 1 FROM grants WHERE tenant=? AND principal=? AND project=?",
                (tenant, action["principal"], action["project"]),
            ).fetchone()
            self.db.execute(
                "DELETE FROM grants WHERE tenant=? AND principal=? AND project=?",
                (tenant, action["principal"], action["project"]),
            )
            if held is not None:
                left = [
                    row[0]
                    for row in self.db.execute(
                        "SELECT project FROM grants WHERE tenant=? AND principal=?",
                        (tenant, action["principal"]),
                    ).fetchall()
                ]
                # Links to a user's scope are not grants of their own: an agent whose project
                # access ended no longer acts for anyone, so they end with it.
                if all(
                    is_user_project(p) and project_owner(p) != action["principal"] for p in left
                ):
                    self.db.execute(
                        "DELETE FROM grants WHERE tenant=? AND principal=?",
                        (tenant, action["principal"]),
                    )
                    # The principal lost its last grant: a later grant must not revive old
                    # credentials.
                    self._advance_generation(tenant, action["principal"])
        elif kind == "purge":
            obs = action["observation"]
            self.db.execute(
                insert_ignore("retired_sources", "tenant,principal,source,source_event"),
                (tenant, *action["source_identity"]),
            )
            held = self.db.execute(
                "SELECT blob FROM observations WHERE tenant=? AND id=?", (tenant, obs)
            ).fetchone()
            if held is not None and held[0] is not None:
                # The file is deleted after the commit, if no other observation names it.
                self._purged_blobs().append((tenant, held[0]))
                self._mark_gc_pending()
            self.db.execute(
                "UPDATE observations SET deleted=1,blob=NULL,readers=NULL,revision=revision+1 WHERE tenant=? AND id=?",
                (tenant, obs),
            )
            lexical_index.remove(self.db, tenant, obs)
            self.db.execute("DELETE FROM anchors WHERE tenant=? AND id=?", (tenant, obs))
            self.db.execute(
                "DELETE FROM projection_items WHERE tenant=? AND observation=?", (tenant, obs)
            )
            self.db.execute(
                "DELETE FROM embedding_items WHERE tenant=? AND observation=?", (tenant, obs)
            )
            self.db.execute(
                "DELETE FROM embedding_vectors WHERE tenant=? AND kind='observation' AND subject=?",
                (tenant, obs),
            )
            self.db.execute(
                "UPDATE embedding_jobs SET state='noop',fence=fence+1 WHERE tenant=? AND observation=?",
                (tenant, obs),
            )
            self.db.execute("DELETE FROM adapter_bases WHERE tenant=?", (tenant,))
            self.db.execute(
                "UPDATE retries SET result=NULL WHERE tenant=? AND observation=?", (tenant, obs)
            )
            self.db.execute(
                "UPDATE jobs SET state='noop',fence=fence+1 WHERE tenant=? AND observation=?",
                (tenant, obs),
            )
            # Receipts that list the source are deleted; the epoch bump retires the rest.
            self.db.execute(
                "DELETE FROM receipts WHERE tenant=? AND id IN (SELECT receipt FROM"
                " receipt_observations WHERE tenant=? AND observation=?)",
                (tenant, tenant, obs),
            )
            self.db.execute(
                "UPDATE tenants SET purge_epoch=purge_epoch+1 WHERE tenant=?", (tenant,)
            )
            from memoryking.adapters.corrections import Corrections

            Corrections(self).purged(tenant, obs, action.get("actor", "system"), at)
        elif kind == "ranking":
            from memoryking.adapters.ranking import Ranking

            Ranking(self).apply_policy(action, at)
        elif kind == "procedure":
            from memoryking.adapters.experience import Experience

            Experience(self).apply_policy(action, at)
        elif kind == "knowledge":
            from memoryking.adapters.knowledge_store import Knowledge

            Knowledge(self).apply_policy(action, at)
        elif kind == "operation":
            from memoryking.adapters.working import Working

            Working(self).apply_operation(action)
        elif kind in {"trust", "hold"}:
            from memoryking.adapters.trust import Trust

            Trust(self).apply_policy(action, at)
        elif kind == "unreviewed_tier":
            from memoryking.adapters.unreviewed import Unreviewed

            Unreviewed(self).apply_policy(action, at)
        elif kind in {"pin", "retirement"}:
            from memoryking.adapters.corrections import Corrections

            Corrections(self).apply_policy(action, at)
        else:
            raise LocalError("journal_invalid")
        if kind == "purge":
            self._purge_dependents(tenant, action["observation"])
        if kind != "operation":
            self.db.execute(
                "UPDATE tenants SET policy_epoch=policy_epoch+1 WHERE tenant=?", (tenant,)
            )
        position = self._event(
            tenant,
            kind,
            action.get("observation", action.get("operation", action.get("principal", "policy"))),
            at,
            action.get("actor", "system"),
        )
        self._enqueue(tenant, position, None, "system")
        self.db.execute(
            "UPDATE meta SET value=? WHERE key=?", (str(record["sequence"]), applied_key)
        )

    def expire_results(self) -> int:
        # A tenant-bound store (shared profile) states its tenant in every statement rather
        # than relying on row-level security alone; the local store is single-tenant
        # housekeeping over every tenant it holds. Every statement names its tenant so the
        # per-tenant expiry indexes apply.
        count = 0
        with self.transaction() as decision:
            now = decision.evaluated_ms
            tenants = (
                self.db.execute("SELECT tenant FROM tenants WHERE tenant=?", (self.tenant,))
                if self.tenant
                else self.db.execute("SELECT tenant FROM tenants")
            ).fetchall()
            for (tenant,) in tenants:
                count += expire_retries(self.db, tenant, now)
                self.db.execute(
                    "UPDATE rendered_artifacts SET state='expired',body=NULL,request_json=NULL,diagnostics=NULL,dependencies='[]' WHERE tenant=? AND state='retained' AND expires<=?",
                    (tenant, now),
                )
                # Usage rows keep their 30-day retention; expired rows release capacity.
                self.db.execute(
                    "DELETE FROM processing_usage WHERE tenant=? AND expires<=?", (tenant, now)
                )
                # Access receipts are evidence for 30 days; the dashboard writes one per poll.
                release_receipts(self.db, tenant, now - RETRY_MS)
                # Egress audit records outlive purge (the delivery happened) but not retention.
                self.db.execute(
                    "DELETE FROM egress_audit WHERE tenant=? AND expires<=?", (tenant, now)
                )
                self.db.execute(
                    "DELETE FROM session_summaries WHERE tenant=? AND expires<=?", (tenant, now)
                )
                # Old checkpoint revisions, orphaned hand-off views, superseded and aged
                # experience revisions, finished jobs and artifact tombstones are released
                # here, and at write time when a bound is reached.
                release_all(self.db, self.dialect, tenant, self.incarnation, now)
        return count

    def housekeeping(self, interval_ms: int = 3_600_000) -> bool:
        """Expire results, artifacts, usage rows and receipts at most once per interval.

        The serving daemon calls this between requests so retention never depends on an
        operator remembering to run `gc`.
        """
        now = self.clock.utc_ms()
        if self._lexical_pending is None:
            with self.lock:
                self._lexical_pending = (
                    self.db.execute(
                        "SELECT 1 FROM lexical_state WHERE state='rebuilding' LIMIT 1"
                    ).fetchone()
                    is not None
                )
        if self._lexical_pending:
            self.rebuild_lexical()
        if not self._layout_done:
            # Flat blob files move into their shard directories a little at a time.
            with self.lock:
                self._layout_done = self.blobs.maintain()
        if self.backup_policy is not None:
            self.backup_policy.maybe_run(self, now)
        if self.summary_idle_ms and now - self._last_summary_scan >= 60_000:
            self._last_summary_scan = now
            self.idle_summaries(now)
        if now - self._last_housekeeping < interval_ms:
            return False
        self._last_housekeeping = now
        self.expire_results()
        return True

    def idle_summaries(self, now: int | None = None) -> int:
        """Summaries for streams idle past the threshold, as the owner, when it may read them."""
        from memoryking.adapters.working import Working

        credential_file = self.control_path / "owner.credential"
        if not self.summary_idle_ms or not credential_file.is_file():
            return 0
        try:
            credential = credential_file.read_text().strip()
            # The daemon calls this on its service thread while request threads share the
            # one connection: the scan's reads must not interleave with their statements.
            with self.lock:
                return Working(self).idle_summaries(
                    credential, self.summary_idle_ms, self.clock.utc_ms() if now is None else now
                )
        except (OSError, LocalError):
            return 0

    def claim(self, credential: str, project: str) -> dict[str, Any] | None:
        with self.transaction() as decision:
            who = self._auth(credential, "jobs", project, decision.evaluated_ms)
            tenant, at = who["tenant"], decision.evaluated_ms
            self.db.execute(
                "UPDATE jobs SET state='dead',fence=fence+1 WHERE tenant=? AND state='leased' AND lease_until<=? AND attempts>=?",
                (tenant, at, MAX_ATTEMPTS),
            )
            active = self.db.execute(
                "SELECT count(*) FROM jobs WHERE tenant=? AND state='leased' AND lease_until>?",
                (tenant, at),
            ).fetchone()[0]
            if active >= CONCURRENCY_LIMIT:
                raise LocalError("concurrency_limit")
            row = self.db.execute(
                "SELECT j.* FROM jobs j JOIN observations o ON j.tenant=o.tenant AND j.observation=o.id WHERE j.tenant=? AND o.project=? AND o.deleted=0 AND j.attempts<? AND ((j.state='pending' AND j.available<=?) OR (j.state='leased' AND j.lease_until<=?)) ORDER BY j.position LIMIT 1",
                (tenant, project, MAX_ATTEMPTS, at, at),
            ).fetchone()
            if row is None:
                return None
            fence = row["fence"] + 1
            self.db.execute(
                "UPDATE jobs SET state='leased',attempts=attempts+1,fence=?,lease_until=?,purge_epoch=(SELECT purge_epoch FROM tenants WHERE tenant=?) WHERE tenant=? AND id=?",
                (fence, at + 30000, tenant, tenant, row["id"]),
            )
            result = {
                "job": row["id"],
                "lease": self._token(
                    "lease",
                    tenant=tenant,
                    principal=who["principal"],
                    job=row["id"],
                    fence=fence,
                    generation=row["generation"],
                ),
                "lease_until_ms": at + 30000,
            }
        self._expose()
        return result

    def finish(self, credential: str, project: str, lease: str, outcome: str) -> None:
        if outcome not in {"complete", "retry", "cancel"}:
            raise LocalError("invalid_request")
        with self.transaction() as decision:
            who = self._auth(credential, "jobs", project, decision.evaluated_ms)
            token = self._decode(lease, "lease")
            if any(token[k] != who[k] for k in ("tenant", "principal")):
                raise LocalError("denied")
            row = self.db.execute(
                "SELECT * FROM jobs WHERE tenant=? AND id=?", (who["tenant"], token["job"])
            ).fetchone()
            if (
                row is None
                or row["state"] != "leased"
                or row["fence"] != token["fence"]
                or row["generation"] != token["generation"]
                or row["lease_until"] <= decision.evaluated_ms
            ):
                raise LocalError("stale_lease")
            obs = self.db.execute(
                "SELECT * FROM observations WHERE tenant=? AND id=?",
                (who["tenant"], row["observation"]),
            ).fetchone()
            epoch = self.db.execute(
                "SELECT purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
            ).fetchone()[0]
            if (
                obs is None
                or obs["deleted"]
                or obs["revision"] != row["revision"]
                or obs["project"] != project
            ):
                raise LocalError("stale_source")
            self._subject_auth(
                self._token("credential", tenant=who["tenant"], principal=row["principal"]),
                "capture",
                project,
                decision.evaluated_ms,
            )
            if row["purge_epoch"] != epoch:
                # A different observation's purge forces a fresh lease with current metadata.
                raise LocalError("stale_source")
            if outcome == "complete":
                raw = self.blobs.get(who["tenant"], obs["blob"])
                self.db.execute(
                    upsert("projection_items"),
                    (
                        who["tenant"],
                        obs["id"],
                        self.incarnation,
                        row["generation"],
                        obs["revision"],
                        obs["blob"],
                        len(raw),
                    ),
                )
            state = (
                "complete"
                if outcome == "complete"
                else "cancelled"
                if outcome == "cancel"
                else "dead"
                if row["attempts"] >= MAX_ATTEMPTS
                else "pending"
            )
            delay = min(60000, (2 ** row["attempts"]) * 1000) + secrets.randbelow(1000)
            self.db.execute(
                "UPDATE jobs SET state=?,fence=fence+1,available=? WHERE tenant=? AND id=?",
                (state, decision.evaluated_ms + delay, who["tenant"], row["id"]),
            )
            self._advance(who["tenant"])
        self._expose()

    def _advance(self, tenant: str) -> None:
        projection = self.db.execute(
            "SELECT * FROM projections WHERE tenant=?", (tenant,)
        ).fetchone()
        frontier = projection["frontier"]
        maximum = self.db.execute(
            "SELECT position FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()[0]
        while frontier < maximum:
            rows = self.db.execute(
                "SELECT state FROM jobs WHERE tenant=? AND position=? AND incarnation=? AND generation=?",
                (tenant, frontier + 1, self.incarnation, projection["generation"]),
            ).fetchall()
            if not rows or any(row[0] not in {"complete", "noop"} for row in rows):
                break
            frontier += 1
        if frontier != projection["frontier"]:
            self.db.execute("UPDATE projections SET frontier=? WHERE tenant=?", (frontier, tenant))
            release_jobs(self.db, tenant, chunk=4096, budget_s=0.05)

    def _context_details(
        self, who: dict[str, Any], capabilities: set[str], project: str, at: int
    ) -> dict[str, Any]:
        return {"capabilities": sorted(capabilities), "delegation_chain": []}

    def egress_revision(self) -> str:
        """The policy revision every conversation binds to; a change retires them all."""
        policy = self.egress_policy
        return "local-recording-v1" if policy is None else str(policy.binding)

    def context(self, credential: str, project: str) -> Released:
        """Read-scoped identity discovery; aggregate/admin status remains separate."""
        identifier(project)
        receipt = uid("rcpt")
        with self.transaction() as decision:
            at = decision.evaluated_ms
            who = self._auth(credential, "read", project, at)
            epochs = self.db.execute(
                "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
            ).fetchone()
            capabilities: set[str] = set()
            for row in self.db.execute(
                "SELECT * FROM grants WHERE tenant=? AND principal=? AND project IN (?,?)",
                (who["tenant"], who["principal"], project, "*"),
            ):
                if temporal_valid(at, row["starts"], row["expires"]):
                    capabilities.update(json.loads(row["capabilities"]))
            if "admin" in capabilities:
                capabilities.update(CAPABILITIES)
            raw = canonical_bytes(
                {
                    "store": self.store_id,
                    "tenant": who["tenant"],
                    "principal": who["principal"],
                    "project": project,
                    "incarnation": self.incarnation,
                    "authorization_epoch": epochs[0],
                    "purge_epoch": epochs[1],
                    "egress_policy_revision": self.egress_revision(),
                    **self._context_details(who, capabilities, project, at),
                    "evaluated_ms": at,
                    "deadline_ms": decision.deadline_ms,
                    "receipt": receipt,
                }
            )
            self.db.execute(
                "INSERT INTO receipts(tenant,id,principal,incarnation,evaluated,deadline,policy_epoch,purge_epoch,response_hash,observation_ids) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    receipt,
                    who["principal"],
                    self.incarnation,
                    at,
                    decision.deadline_ms,
                    *epochs,
                    hashlib.sha256(raw).hexdigest(),
                    "[]",
                ),
            )
            self.fault("after_validation")
        self._expose()
        return Released(raw, decision, self.clock, receipt)

    def status(self, credential: str, project: str, watermark: str | None = None) -> dict[str, Any]:
        with self.transaction() as decision:
            who = self._auth(credential, "admin", "*", decision.evaluated_ms)
            tenant = who["tenant"]
            self._advance(tenant)
            projection = dict(
                self.db.execute("SELECT * FROM projections WHERE tenant=?", (tenant,)).fetchone()
            )
            position = self.db.execute(
                "SELECT position FROM tenants WHERE tenant=?", (tenant,)
            ).fetchone()[0]
            satisfied = None
            if watermark:
                target = self._decode(watermark, "watermark")
                if target["tenant"] != tenant:
                    raise LocalError("denied")
                satisfied = projection["frontier"] >= target["position"]
            result = {
                "store": self.store_id,
                "incarnation": self.incarnation,
                "position": position,
                "projection": projection,
                "satisfied": satisfied,
                "jobs": dict(
                    self.db.execute(
                        "SELECT state,count(*) FROM jobs WHERE tenant=? GROUP BY state", (tenant,)
                    )
                ),
                "capacity": self._capacity(tenant),
                "lexical": lexical_index.describe(self.db, tenant, self._lexical_rate),
                "semantic": self._semantic(tenant, position),
                # Counts only: an administrator sees that user memory exists, never what it says.
                "user_scopes": dict(
                    zip(
                        ("scopes", "observations"),
                        (
                            int(n)
                            for n in self.db.execute(
                                "SELECT count(DISTINCT project),count(*) FROM observations WHERE tenant=? AND deleted=0 AND substr(project,1,6)=?",
                                (tenant, user_project("")),
                            ).fetchone()
                        ),
                        strict=True,
                    )
                ),
            }
        self._expose()
        return result

    def _semantic(self, tenant: str, position: int) -> dict[str, Any]:
        """The semantic index per project: whether a model is loaded, how far indexing has
        got and how many vectors it holds. Counts only; no text."""
        model = getattr(self.embedder, "name", None)
        partitions = []
        for row in self.db.execute(
            "SELECT project,fingerprint,state,frontier,generation FROM embedding_partitions WHERE tenant=? AND incarnation=? ORDER BY project,fingerprint",
            (tenant, self.incarnation),
        ).fetchall():
            vectors = int(
                self.db.execute(
                    "SELECT count(*) FROM embedding_vectors WHERE tenant=? AND project=? AND fingerprint=? AND generation=?",
                    (tenant, row["project"], row["fingerprint"], row["generation"]),
                ).fetchone()[0]
            )
            current = self.embedder is not None and row["fingerprint"] == self.embedder.fingerprint
            partitions.append(
                {
                    "project": row["project"],
                    "state": row["state"],
                    "current_model": current,
                    "vectors": vectors,
                    "indexed_position": row["frontier"],
                    "percent_indexed": 100
                    if row["frontier"] >= position
                    else int(100 * row["frontier"] / max(1, position)),
                }
            )
        return {"model": model, "loaded": self.embedder is not None, "partitions": partitions}

    def _capacity(self, tenant: str) -> dict[str, dict[str, Any]]:
        """Utilisation of every bounded per-tenant resource; warning at 80 percent."""

        def count(statement: str) -> int:
            return int(self.db.execute(statement, (tenant,)).fetchone()[0])

        artifacts, artifact_bytes = self.db.execute(
            "SELECT count(*),coalesce(sum(length(body)),0) FROM rendered_artifacts WHERE tenant=? AND state='retained'",
            (tenant,),
        ).fetchone()
        checkpoints, checkpoint_bytes = live_checkpoints(self.db, self.dialect, tenant)
        used = {
            "jobs": count(QUEUED_JOBS),
            "rendered_artifacts": int(artifacts),
            "rendered_bytes": int(artifact_bytes),
            "operation_intents": count(
                "SELECT count(*) FROM operation_intents WHERE tenant=? AND state IN ('intent_recorded','dispatching','unknown')"
            ),
            "processing_usage": count("SELECT count(*) FROM processing_usage WHERE tenant=?"),
            "checkpoints": checkpoints,
            "checkpoint_bytes": checkpoint_bytes,
            "handoffs": live_handoffs(self.db, tenant, self.clock.utc_ms()),
            "handoff_views": live_handoff_views(self.db, tenant),
            "experience_records": live_experience(self.db, tenant, self.incarnation),
        }
        return {
            name: {"used": used[name], "limit": limit, "warning": used[name] * 5 >= limit * 4}
            for name, limit in CAPACITY_LIMITS.items()
        }

    def drain(self, limit: int = 256) -> int:
        """Complete pending audit_probe jobs in-process.

        The serving daemon calls this between requests, so a default installation never
        needs a separate worker to keep accepting captures. Explicit claim/finish workers
        remain valid for the same jobs.
        """
        completed = 0
        with self.lock:
            # A plain read keeps idle polling free of write transactions and fsyncs.
            try:
                idle = (
                    self.healthy
                    and self.db.execute(
                        "SELECT 1 FROM tenants t WHERE EXISTS (SELECT 1 FROM jobs j WHERE j.tenant=t.tenant AND j.state='pending' AND j.available<=?) LIMIT 1",
                        (self.clock.utc_ms(),),
                    ).fetchone()
                    is None
                )
            except LocalError as exc:
                if exc.code != "database_transaction_required":
                    raise
                idle = False  # Backends that gate reads check inside the transaction.
            if idle:
                return 0
        with self.transaction() as decision:
            at = decision.evaluated_ms
            for tenant_row in self.db.execute(
                "SELECT tenant FROM tenants ORDER BY tenant"
            ).fetchall():
                tenant = tenant_row[0]
                rows = self.db.execute(
                    "SELECT j.id,j.generation,o.id AS observation,o.blob,o.revision FROM jobs j JOIN observations o ON j.tenant=o.tenant AND j.observation=o.id WHERE j.tenant=? AND j.state='pending' AND j.available<=? AND j.attempts<? AND o.deleted=0 ORDER BY j.position LIMIT ?",
                    (tenant, at, MAX_ATTEMPTS, limit - completed),
                ).fetchall()
                for row in rows:
                    if self.clock.monotonic_ms() - decision.monotonic_ms > DRAIN_BUDGET_MS:
                        limit = completed  # the rest waits for the next pass
                        break
                    try:
                        raw = self.blobs.get(tenant, row["blob"])
                    except (LocalError, OSError):
                        # One unreadable blob costs only its own job an attempt (with the
                        # worker backoff, dead at MAX_ATTEMPTS); the pass continues.
                        attempts = self.db.execute(
                            "SELECT attempts FROM jobs WHERE tenant=? AND id=?", (tenant, row["id"])
                        ).fetchone()[0]
                        self.db.execute(
                            "UPDATE jobs SET state=?,fence=fence+1,attempts=attempts+1,available=? WHERE tenant=? AND id=?",
                            (
                                "dead" if attempts + 1 >= MAX_ATTEMPTS else "pending",
                                at + min(60000, (2 ** (attempts + 1)) * 1000),
                                tenant,
                                row["id"],
                            ),
                        )
                        continue
                    self.db.execute(
                        upsert("projection_items"),
                        (
                            tenant,
                            row["observation"],
                            self.incarnation,
                            row["generation"],
                            row["revision"],
                            row["blob"],
                            len(raw),
                        ),
                    )
                    self.db.execute(
                        "UPDATE jobs SET state='complete',fence=fence+1,attempts=attempts+1 WHERE tenant=? AND id=?",
                        (tenant, row["id"]),
                    )
                    completed += 1
                if rows:
                    self._advance(tenant)
                if completed >= limit:
                    break
        if completed:
            self._expose()
        return completed

    def reindex(self, credential: str) -> dict[str, Any]:
        """Start the rebuild of the projections and the lexical index.

        The call's own work does not grow with the corpus: it moves the projection
        generation on and marks the lexical index as rebuilding. A store of at most
        `REINDEX_INLINE_LIMIT` observations also runs a short first slice, so a small store
        finishes at once. Everything proportional to the corpus runs afterwards in bounded
        batches, each its own transaction and resumable from the stored cursor: the daemon's
        housekeeping, or `rebuild_lexical` called by an operator waiting on it. Each batch
        also resets the projection jobs of the positions it covers."""
        with self.transaction() as decision:
            who = self._auth(credential, "admin", "*", decision.evaluated_ms)
            tenant = who["tenant"]
            self._begin_projection_reset(tenant)
            lexical_index.begin(self.db, tenant)
            small = (
                self.db.execute(
                    "SELECT count(*) FROM (SELECT 1 FROM observations WHERE tenant=? AND deleted=0 LIMIT ?) AS found",
                    (tenant, REINDEX_INLINE_LIMIT + 1),
                ).fetchone()[0]
                <= REINDEX_INLINE_LIMIT
            )
        self._lexical_pending = True
        self._expose()
        if small:
            self.rebuild_lexical(tenant, wall_ms=REINDEX_CALL_MS, step_ms=REINDEX_CALL_MS)
        with self.transaction():
            result = lexical_index.describe(self.db, tenant, self._lexical_rate, counts=False)
        return result

    def rebuild_lexical(
        self, tenant: str | None = None, wall_ms: int = 1500, step_ms: int = lexical_index.STEP_MS
    ) -> bool:
        """Advance any unfinished lexical rebuild by whole batches until the wall budget is
        spent; True when no tenant in scope has work left. Each batch is its own transaction."""
        started = time.monotonic()
        scope = tenant or self.tenant
        while True:
            with self.transaction():
                names = (
                    [scope]
                    if scope
                    else [r[0] for r in self.db.execute("SELECT tenant FROM tenants").fetchall()]
                )
                pending = [
                    name
                    for name in names
                    if lexical_index.state(self.db, name)[0] == lexical_index.REBUILDING
                ]
                if not pending:
                    self._lexical_pending = False
                    return True
                name = pending[0]
                batch_started = time.monotonic()
                counted: dict[str, int] = {"documents": 0}
                lexical_index.step(
                    self.db,
                    name,
                    lambda row, n=name: lexical_index.blob_text(self.blobs, row, n),
                    budget_ms=step_ms,
                    reset=lambda low, high, n=name: self._reset_jobs(n, low, high),
                    stats=counted,
                )
            took = time.monotonic() - batch_started
            if counted["documents"] and took > 0:
                sample = counted["documents"] / took
                self._lexical_rate = (
                    sample
                    if self._lexical_rate is None
                    else 0.7 * self._lexical_rate + 0.3 * sample
                )
            if (time.monotonic() - started) * 1000 >= wall_ms:
                return False

    def _reindex(self, tenant: str) -> None:
        """Whole rebuild inside the caller's transaction (restore, where nothing else runs)."""
        self._reset_projections(tenant)
        lexical_index.begin(self.db, tenant)
        while not lexical_index.step(
            self.db,
            tenant,
            lambda row: lexical_index.blob_text(self.blobs, row, tenant),
            budget_ms=10**9,
        ):
            pass

    def _begin_projection_reset(self, tenant: str) -> None:
        """Move the projection generation on; jobs and items follow in batches.

        Constant work. Jobs of the old generation are reset by `_reset_jobs` as the rebuild
        passes their positions, and the frontier cannot advance past one until it is, because
        it only counts jobs of the current generation."""
        self.db.execute(
            "UPDATE projections SET incarnation=?,generation=generation+1,frontier=0 WHERE tenant=?",
            (self.incarnation, tenant),
        )

    def _reset_jobs(self, tenant: str, low: int, high: int) -> None:
        """Reset the jobs at event positions in (low, high] to the current generation, and
        drop the projection items of the observations there (they are rebuilt by the drain)."""
        generation = self.db.execute(
            "SELECT generation FROM projections WHERE tenant=?", (tenant,)
        ).fetchone()[0]
        epoch = self.db.execute(
            "SELECT purge_epoch FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()[0]
        self.db.execute(
            "DELETE FROM projection_items WHERE tenant=? AND observation IN (SELECT id FROM observations WHERE tenant=? AND position>? AND position<=?)",
            (tenant, tenant, low, high),
        )
        self.db.execute(
            "UPDATE jobs SET incarnation=?,generation=?,fence=fence+1,attempts=0,lease_until=0,available=0,purge_epoch=?,state=CASE WHEN observation IS NULL OR observation IN (SELECT id FROM observations WHERE tenant=? AND position>? AND position<=? AND deleted=1) THEN 'noop' ELSE 'pending' END WHERE tenant=? AND position>? AND position<=? AND generation<>?",
            (self.incarnation, generation, epoch, tenant, low, high, tenant, low, high, generation),
        )
        # Finished jobs behind the frontier are deleted (see retention), so the rebuild creates the
        # missing ones in this range: one per event position, pending while its observation is live.
        last = low
        while True:
            missing = self.db.execute(
                "SELECT e.position,o.id AS observation,o.deleted,o.revision,o.principal FROM events e"
                " LEFT JOIN jobs j ON j.tenant=e.tenant AND j.position=e.position"
                " LEFT JOIN observations o ON o.tenant=e.tenant AND o.position=e.position"
                " WHERE e.tenant=? AND e.position>? AND e.position<=? AND j.id IS NULL"
                " ORDER BY e.position LIMIT 5000",
                (tenant, last, high),
            ).fetchall()
            if not missing:
                break
            for row in missing:
                live = row["observation"] is not None and not row["deleted"]
                self.db.execute(
                    "INSERT INTO jobs(tenant,id,position,observation,incarnation,generation,state,fence,purge_epoch,revision,principal) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        tenant,
                        uid("job"),
                        row["position"],
                        row["observation"],
                        self.incarnation,
                        generation,
                        "pending" if live else "noop",
                        1,
                        epoch,
                        row["revision"] or 1,
                        row["principal"] or "system",
                    ),
                )
            last = missing[-1]["position"]

    def _reset_projections(self, tenant: str) -> None:
        self.db.execute("DELETE FROM projection_items WHERE tenant=?", (tenant,))
        self.db.execute(
            "UPDATE projections SET incarnation=?,generation=generation+1,frontier=0 WHERE tenant=?",
            (self.incarnation, tenant),
        )
        generation = self.db.execute(
            "SELECT generation FROM projections WHERE tenant=?", (tenant,)
        ).fetchone()[0]
        epoch = self.db.execute(
            "SELECT purge_epoch FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()[0]
        self.db.execute(
            "UPDATE jobs SET incarnation=?,generation=?,fence=fence+1,attempts=0,lease_until=0,available=0,purge_epoch=?,state=CASE WHEN observation IS NULL OR observation IN (SELECT id FROM observations WHERE tenant=? AND deleted=1) THEN 'noop' ELSE 'pending' END WHERE tenant=?",
            (self.incarnation, generation, epoch, tenant, tenant),
        )
        # Finished jobs behind the frontier are deleted, so the rebuild creates the ones that
        # are missing: one per event position, pending while its observation is live.
        last = 0
        while True:
            missing = self.db.execute(
                "SELECT e.position,o.id AS observation,o.deleted,o.revision,o.principal FROM events e"
                " LEFT JOIN jobs j ON j.tenant=e.tenant AND j.position=e.position"
                " LEFT JOIN observations o ON o.tenant=e.tenant AND o.position=e.position"
                " WHERE e.tenant=? AND e.position>? AND j.id IS NULL ORDER BY e.position LIMIT 5000",
                (tenant, last),
            ).fetchall()
            if not missing:
                break
            for row in missing:
                live = row["observation"] is not None and not row["deleted"]
                self.db.execute(
                    "INSERT INTO jobs(tenant,id,position,observation,incarnation,generation,state,fence,purge_epoch,revision,principal) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        tenant,
                        uid("job"),
                        row["position"],
                        row["observation"],
                        self.incarnation,
                        generation,
                        "pending" if live else "noop",
                        1,
                        epoch,
                        row["revision"] or 1,
                        row["principal"] or "system",
                    ),
                )
            last = missing[-1]["position"]
