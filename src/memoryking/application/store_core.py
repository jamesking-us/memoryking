"""Backend-agnostic store operations behind the storage port.

Authorization, capture, reads, grants, purge policy, idempotent retries, jobs, context
and status live here once, written in portable SQL against a dialect. A backend (SQLite
in `adapters.local_store`, PostgreSQL in `adapters.postgres_store`) supplies the
connection, the transaction discipline, the control tree and token signing. Neither
backend subclasses the other.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self, cast

from memoryking.adapters.local_contract import validate_capture
from memoryking.adapters.local_files import atomic, private_dir, private_file, sync_dir
from memoryking.application.storage import Dialect, insert_ignore, upsert
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.local import Clock, Decision, LocalError, Released, temporal_valid

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


QUEUE_LIMIT = 1000


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
}


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", value):
        raise LocalError("invalid_identifier")
    return value


def uid(prefix: str) -> str:
    return prefix + "_" + secrets.token_hex(16)


class StoreCore:
    """What every backend shares; the attributes below are supplied by the backend."""

    capabilities = CAPABILITIES
    db: Any
    dialect: Dialect
    control: Any
    control_path: Path
    root: Path
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
    _last_summary_scan = 0

    # The operator's egress policy; None means the local recorder is the only provider.
    # A class default so every store, including PostgreSQL, reports a revision.
    egress_policy: Any | None = None

    dashboard: Any = None

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

    def gc(self) -> int:
        """A backend removes unreferenced blobs and expired results in its own way."""
        raise NotImplementedError

    def _now(self) -> Decision:
        now = self.clock.utc_ms()
        if now < self.last_utc:
            raise LocalError("clock_untrusted")
        self.last_utc = now
        return Decision(now, self.clock.monotonic_ms(), now + 5000)

    def _token(self, kind: str, **fields: Any) -> str:
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
        return cast(dict[str, Any], body)

    def _auth(self, token: str, capability: str, project: str, at: int) -> dict[str, Any]:
        identity = self._decode(token, "credential")
        rows = self.db.execute(
            "SELECT * FROM grants WHERE tenant=? AND principal=? AND project IN (?, '*')",
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
            raise LocalError("denied")
        return identity

    def _subject_auth(self, token: str, capability: str, project: str, at: int) -> dict[str, Any]:
        """Internal intersection check; never an alternate transport credential entry point."""
        return self._auth(token, capability, project, at)

    def _subject_token(self, who: dict[str, Any]) -> str:
        return self._token("credential", tenant=who["tenant"], principal=who["principal"])

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
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
            (tenant, position, uid("evt"), kind, subject, self.incarnation, at, actor),
        )
        return position

    def _blob(self, tenant: str, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        directory = self.root / "blobs" / identifier(tenant)
        private_dir(directory, create=True)
        sync_dir(directory.parent)
        path = directory / digest
        if not path.exists():
            atomic(path, content)
        else:
            private_file(path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise LocalError("blob_corrupt")
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
                    count = self.db.execute(
                        "SELECT count(*) FROM jobs WHERE tenant=? AND state IN ('pending','leased')",
                        (tenant,),
                    ).fetchone()[0]
                    if count >= QUEUE_LIMIT:
                        raise LocalError("queue_full")
                    blob = self._blob(tenant, request["text"].encode())
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
                    self.db.execute(
                        "INSERT INTO lexical VALUES (?,?,?)", (tenant, obs, request["text"])
                    )
                    self._index_anchors(tenant, obs, request["text"])
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
                    "INSERT INTO retries VALUES (?,?,?,?,?,?,?,?,?,?)",
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
                insert_ignore("anchors", 4),
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
        # A held capture is on nobody's reading list until an admin releases it.
        if (
            self.db.execute(
                "SELECT 1 FROM observation_holds WHERE tenant=? AND id=? AND state='held'",
                (row["tenant"], row["id"]),
            ).fetchone()
            is not None
        ):
            return False
        try:
            self._subject_auth(
                self._subject_token(who),
                "read",
                row["project"],
                at,
            )
        except LocalError:
            return False
        readers = json.loads(row["readers"])
        return readers is None or who["principal"] in readers

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
                    path = self.root / "blobs" / who["tenant"] / row["blob"]
                    private_file(path)
                    raw = path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != row["blob"]:
                        raise LocalError("blob_corrupt")
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
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
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
    ) -> str:
        identifier(principal)
        identifier(project)
        if not capabilities or not set(capabilities) <= self.capabilities - {"admin"}:
            raise LocalError("invalid_capabilities")
        if starts is not None and expires is not None and starts >= expires:
            raise LocalError("invalid_interval")
        with self.transaction() as decision:
            who = self._auth(credential, "admin", "*", decision.evaluated_ms)
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
            record = self._journal(action)
            self._apply(record, decision.evaluated_ms)
        self._expose()
        return self._token("credential", tenant=who["tenant"], principal=principal)

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
        self.gc()

    def _policy(self, credential: str, action: dict[str, Any]) -> None:
        with self.transaction() as decision:
            who = self._auth(credential, "admin", "*", decision.evaluated_ms)
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
            record = self.control.append(self.anchor, action)
            self.fault("after_journal")
            return cast(dict[str, Any], record)
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
        retired = 0
        purged_checkpoints: set[str] = set()
        purged_artifacts: set[str] = set()
        changed = True
        while changed:
            changed = False
            for row in self.db.execute(
                "SELECT id,dependencies FROM checkpoints WHERE tenant=? AND state!='purged'",
                (tenant,),
            ).fetchall():
                if depends(row["dependencies"]):
                    self.db.execute(
                        "UPDATE checkpoints SET state='purged',body=NULL,dependencies='[]' WHERE tenant=? AND id=?",
                        (tenant, row["id"]),
                    )
                    purged.update(
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
                    purged.add(row["id"])
                    purged_checkpoints.add(row["id"])
                    changed = True
            for row in self.db.execute(
                "SELECT id,dependencies FROM experience_records WHERE tenant=? AND body IS NOT NULL",
                (tenant,),
            ).fetchall():
                if depends(row["dependencies"]):
                    self.db.execute(
                        "UPDATE experience_records SET body=NULL,dependencies='[]',readers=NULL WHERE tenant=? AND id=?",
                        (tenant, row["id"]),
                    )
                    purged.add(row["id"])
                    retired += 1
                    changed = True
            for row in self.db.execute(
                "SELECT id,dependencies FROM rendered_artifacts WHERE tenant=? AND state='retained'",
                (tenant,),
            ).fetchall():
                if depends(row["dependencies"]):
                    self.db.execute(
                        "UPDATE rendered_artifacts SET state='purged',body=NULL,request_json=NULL,diagnostics=NULL,dependencies='[]' WHERE tenant=? AND id=?",
                        (tenant, row["id"]),
                    )
                    purged.add(row["id"])
                    purged_artifacts.add(row["id"])
                    changed = True
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

    def _apply(self, record: dict[str, Any], at: int) -> None:
        sequence = int(self.db.execute("SELECT value FROM meta WHERE key='journal'").fetchone()[0])
        if record["sequence"] <= sequence:
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
        elif kind == "revoke":
            self.db.execute(
                "DELETE FROM grants WHERE tenant=? AND principal=? AND project=?",
                (tenant, action["principal"], action["project"]),
            )
        elif kind == "purge":
            obs = action["observation"]
            self.db.execute(
                insert_ignore("retired_sources", 4),
                (tenant, *action["source_identity"]),
            )
            self.db.execute(
                "UPDATE observations SET deleted=1,blob=NULL,readers=NULL,revision=revision+1 WHERE tenant=? AND id=?",
                (tenant, obs),
            )
            self.db.execute("DELETE FROM lexical WHERE tenant=? AND id=?", (tenant, obs))
            self.db.execute("DELETE FROM anchors WHERE tenant=? AND id=?", (tenant, obs))
            self.db.execute(
                "DELETE FROM projection_items WHERE tenant=? AND observation=?", (tenant, obs)
            )
            self.db.execute(
                "DELETE FROM embedding_items WHERE tenant=? AND observation=?", (tenant, obs)
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
                "DELETE FROM receipts WHERE tenant=? AND observation_ids LIKE ?",
                (tenant, '%"' + obs + '"%'),
            )
            self.db.execute(
                "UPDATE tenants SET purge_epoch=purge_epoch+1 WHERE tenant=?", (tenant,)
            )
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
        self.db.execute("UPDATE meta SET value=? WHERE key='journal'", (str(record["sequence"]),))

    def expire_results(self) -> int:
        with self.transaction() as decision:
            count = self.db.execute(
                "UPDATE retries SET result=NULL WHERE result IS NOT NULL AND expires<=?",
                (decision.evaluated_ms,),
            ).rowcount
            self.db.execute(
                "UPDATE rendered_artifacts SET state='expired',body=NULL,request_json=NULL,diagnostics=NULL,dependencies='[]' WHERE state='retained' AND expires<=?",
                (decision.evaluated_ms,),
            )
            # Usage rows keep their 30-day retention; expired rows release capacity.
            self.db.execute(
                "DELETE FROM processing_usage WHERE expires<=?", (decision.evaluated_ms,)
            )
            # Access receipts are evidence for 30 days; the dashboard writes one per poll.
            self.db.execute(
                "DELETE FROM receipts WHERE evaluated<=?", (decision.evaluated_ms - RETRY_MS,)
            )
            # Egress audit records outlive purge (the delivery happened) but not retention.
            self.db.execute("DELETE FROM egress_audit WHERE expires<=?", (decision.evaluated_ms,))
            self.db.execute(
                "DELETE FROM session_summaries WHERE expires<=?", (decision.evaluated_ms,)
            )
        return int(count)

    def housekeeping(self, interval_ms: int = 3_600_000) -> bool:
        """Expire results, artifacts, usage rows and receipts at most once per interval.

        The serving daemon calls this between requests so retention never depends on an
        operator remembering to run `gc`.
        """
        now = self.clock.utc_ms()
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
                path = self.root / "blobs" / who["tenant"] / obs["blob"]
                private_file(path)
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != obs["blob"]:
                    raise LocalError("blob_corrupt")
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
        self.db.execute("UPDATE projections SET frontier=? WHERE tenant=?", (frontier, tenant))

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
                capabilities.update(self.capabilities)
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
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
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
            }
        self._expose()
        return result

    def _capacity(self, tenant: str) -> dict[str, dict[str, Any]]:
        """Utilisation of every bounded per-tenant resource; warning at 80 percent."""

        def count(statement: str) -> int:
            return int(self.db.execute(statement, (tenant,)).fetchone()[0])

        artifacts, artifact_bytes = self.db.execute(
            "SELECT count(*),coalesce(sum(length(body)),0) FROM rendered_artifacts WHERE tenant=? AND state='retained'",
            (tenant,),
        ).fetchone()
        used = {
            "jobs": count(
                "SELECT count(*) FROM jobs WHERE tenant=? AND state IN ('pending','leased')"
            ),
            "rendered_artifacts": int(artifacts),
            "rendered_bytes": int(artifact_bytes),
            "operation_intents": count(
                "SELECT count(*) FROM operation_intents WHERE tenant=? AND state IN ('intent_recorded','dispatching','unknown')"
            ),
            "processing_usage": count("SELECT count(*) FROM processing_usage WHERE tenant=?"),
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
                        "SELECT 1 FROM jobs WHERE state='pending' AND available<=? LIMIT 1",
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
                    path = self.root / "blobs" / tenant / row["blob"]
                    try:
                        private_file(path)
                        raw = path.read_bytes()
                        if hashlib.sha256(raw).hexdigest() != row["blob"]:
                            raise LocalError("blob_corrupt")
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

    def reindex(self, credential: str) -> None:
        with self.transaction() as decision:
            who = self._auth(credential, "admin", "*", decision.evaluated_ms)
            self._reindex(who["tenant"])
        self._expose()

    def _reindex(self, tenant: str) -> None:
        self.db.execute("DELETE FROM projection_items WHERE tenant=?", (tenant,))
        # Anchors are derived from the retained text, so a reindex rebuilds them too:
        # stores created before anchors existed gain them without a re-capture.
        self.db.execute("DELETE FROM anchors WHERE tenant=?", (tenant,))
        for row in self.db.execute(
            "SELECT l.id,l.text FROM lexical l JOIN observations o ON o.tenant=l.tenant AND o.id=l.id WHERE l.tenant=? AND o.deleted=0",
            (tenant,),
        ).fetchall():
            self._index_anchors(tenant, row["id"], row["text"])
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
