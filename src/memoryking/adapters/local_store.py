"""SQLite implementation of P02. One owner; all policy/content transactions serialize.

The SQL here is deliberately adapter-local. Future backends must reproduce the contracts,
not translate SQLite locking pragmas or assume ORM portability proves correctness.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Self, cast

from memoryking.adapters.local_contract import validate_capture
from memoryking.adapters.local_files import (
    Control,
    Ownership,
    atomic,
    private_dir,
    private_file,
    sync_dir,
)
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.local import (
    Clock,
    Decision,
    LocalError,
    Released,
    SystemClock,
    temporal_valid,
)

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


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", value):
        raise LocalError("invalid_identifier")
    return value


def uid(prefix: str) -> str:
    return prefix + "_" + secrets.token_hex(16)


def paths(root: Path, control: Path) -> None:
    if root.resolve() == control.resolve() or any(
        a.resolve() in b.resolve().parents for a, b in ((root, control), (control, root))
    ):
        raise LocalError("control_must_be_independent")


def connect(root: Path) -> sqlite3.Connection:
    for name in ("store.sqlite", "store.sqlite-wal", "store.sqlite-shm"):
        path = root / name
        if path.exists() or path.is_symlink():
            private_file(path)
    db = sqlite3.connect(root / "store.sqlite", isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA fullfsync=ON")
    db.execute("PRAGMA checkpoint_fullfsync=ON")
    db.execute("PRAGMA secure_delete=ON")
    return db


class LocalStore:
    dashboard: Any = None

    def __init__(
        self,
        root: Path,
        control: Path,
        *,
        clock: Clock | None = None,
        fault: Callable[[str], None] | None = None,
    ):
        paths(root, control)
        private_dir(root)
        private_dir(control)
        self.root, self.control_path = root, control
        self.owner = Ownership(root)
        try:
            self.control_owner = Ownership(control)
        except Exception:
            self.owner.close()
            raise
        self.clock = clock or SystemClock()
        self.fault = fault or (lambda _: None)
        self.lock = threading.RLock()
        self.healthy = True
        self.retrieval_cache: dict[str, Any] = {}
        try:
            self.control = Control(control)
            self.anchor = self.control.read_anchor()
            self.db: Any = connect(root)
            self._load()
            serial = int(
                self.db.execute("SELECT value FROM meta WHERE key='commit_serial'").fetchone()[0]
            )
            if serial < self.anchor["commit_serial"]:
                raise LocalError("recovery_required")
            if (
                self.anchor["state"] != "active"
                or self.anchor["incarnation"] != self.incarnation
                or self.anchor["store"] != self.store_id
            ):
                raise LocalError("recovery_required")
            for tenant, exposed in self.anchor["frontiers"].items():
                row = self.db.execute(
                    "SELECT position FROM tenants WHERE tenant=?", (tenant,)
                ).fetchone()
                if row is None or row[0] < exposed:
                    raise LocalError("recovery_required")
            self._replay_journal()
            self.last_utc = self.anchor.get("last_utc", 0)
            self.gc()
        except Exception:
            if hasattr(self, "db"):
                self.db.close()
            self.control_owner.close()
            self.owner.close()
            raise

    def _load(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            self.db.executescript(Path(__file__).with_name("knowledge_schema.sql").read_text())
            # Next migration is applied below.
        if version in {1, 2}:
            self.db.executescript(Path(__file__).with_name("retrieval_schema.sql").read_text())
        if version in {1, 2, 3}:
            self.db.executescript(Path(__file__).with_name("working_schema.sql").read_text())
        if version in {1, 2, 3, 4}:
            self.db.executescript(Path(__file__).with_name("handoff_schema.sql").read_text())
        if version in {1, 2, 3, 4, 5}:
            self.db.executescript(Path(__file__).with_name("repository_schema.sql").read_text())
        if version in {1, 2, 3, 4, 5, 6}:
            self.db.executescript(Path(__file__).with_name("optional_schema.sql").read_text())
        if version in {1, 2, 3, 4, 5, 6, 7}:
            self.db.executescript(Path(__file__).with_name("experience_schema.sql").read_text())
        elif version != 8:
            raise LocalError("schema_unsupported")
        meta = dict(self.db.execute("SELECT key,value FROM meta"))
        self.store_id, self.incarnation = meta["store"], meta["incarnation"]
        if self.store_id != self.control.store:
            raise LocalError("recovery_required")

    @classmethod
    def initialize(
        cls, root: Path, control: Path, tenant: str = "main", principal: str = "owner"
    ) -> Path:
        identifier(tenant)
        identifier(principal)
        paths(root, control)
        if root.exists() or control.exists():
            raise LocalError("initialization_requires_new_directories")
        private_dir(root, create=True)
        private_dir(control, create=True)
        private_dir(root / "blobs", create=True)
        store, incarnation = uid("store"), uid("hist")
        atomic(
            control / "identity.json",
            canonical_bytes({"store": store, "key": secrets.token_hex(32)}),
        )
        atomic(control / "journal", b"")
        atomic(root / "store.sqlite", b"")
        db = connect(root)
        try:
            db.executescript(Path(__file__).with_name("local_schema.sql").read_text())
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "INSERT INTO meta VALUES (?,?)",
                [
                    ("store", store),
                    ("incarnation", incarnation),
                    ("journal", "0"),
                    ("commit_serial", "0"),
                ],
            )
            db.execute("INSERT INTO tenants(tenant) VALUES (?)", (tenant,))
            db.execute(
                "INSERT INTO grants VALUES (?,?,?,?,?,?)",
                (tenant, principal, "*", json.dumps(sorted(CAPABILITIES)), None, None),
            )
            db.execute(
                "INSERT INTO projections VALUES (?,?,?,?,?,?)",
                (tenant, incarnation, "audit_probe", "v1", 1, 0),
            )
            db.commit()
            db.executescript(Path(__file__).with_name("knowledge_schema.sql").read_text())
            db.executescript(Path(__file__).with_name("retrieval_schema.sql").read_text())
            db.executescript(Path(__file__).with_name("working_schema.sql").read_text())
            db.executescript(Path(__file__).with_name("handoff_schema.sql").read_text())
            db.executescript(Path(__file__).with_name("repository_schema.sql").read_text())
            db.executescript(Path(__file__).with_name("optional_schema.sql").read_text())
            db.executescript(Path(__file__).with_name("experience_schema.sql").read_text())
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            db.close()
        controller = Control(control)
        controller.anchor(
            {
                "store": store,
                "incarnation": incarnation,
                "state": "active",
                "frontiers": {},
                "journal_sequence": 0,
                "journal_head": "0" * 64,
                "last_utc": 0,
                "commit_serial": 0,
            }
        )
        token = controller.sign(
            {
                "kind": "credential",
                "store": store,
                "incarnation": incarnation,
                "tenant": tenant,
                "principal": principal,
            }
        )
        atomic(control / "owner.credential", token.encode())
        sync_dir(root)
        sync_dir(control.parent)
        return control / "owner.credential"

    def close(self) -> None:
        self.db.close()
        self.control_owner.close()
        self.owner.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _now(self) -> Decision:
        now = self.clock.utc_ms()
        if now < self.last_utc:
            raise LocalError("clock_untrusted")
        self.last_utc = now
        return Decision(now, self.clock.monotonic_ms(), now + 5000)

    @contextmanager
    def transaction(self) -> Iterator[Decision]:
        with self.lock:
            self.owner.check()
            if not self.healthy:
                raise LocalError("recovery_required")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                decision = self._now()
                yield decision
                self.fault("before_commit")
                decision.check(self.clock)
                if (
                    int(
                        self.db.execute(
                            "SELECT value FROM meta WHERE key='commit_serial'"
                        ).fetchone()[0]
                    )
                    >= MAX_COUNTER
                ):
                    raise LocalError("counter_exhausted")
                self.db.execute(
                    "UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='commit_serial'"
                )
                self.db.commit()
                self.fault("after_commit")
                decision.check(self.clock)
            except BaseException:
                self.db.rollback()
                if self.anchor["journal_sequence"] > int(
                    self.db.execute("SELECT value FROM meta WHERE key='journal'").fetchone()[0]
                ):
                    self.healthy = False
                raise

    def _token(self, kind: str, **fields: Any) -> str:
        return self.control.sign(
            {"kind": kind, "store": self.store_id, "incarnation": self.incarnation, **fields}
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

    def _expose(self) -> None:
        """Fsync outside-DB evidence before any acknowledgment/handle leaves the process."""
        with self.lock:
            self._expose_locked()

    def _expose_locked(self) -> None:
        if not self.healthy:
            raise LocalError("recovery_required")
        try:
            self.fault("before_anchor")
            self.anchor["frontiers"] = dict(self.db.execute("SELECT tenant,position FROM tenants"))
            self.anchor["last_utc"] = self.last_utc
            self.anchor["commit_serial"] = int(
                self.db.execute("SELECT value FROM meta WHERE key='commit_serial'").fetchone()[0]
            )
            self.control.anchor(self.anchor)
            self.fault("after_anchor")
        except BaseException:
            self.healthy = False
            raise

    def namespace(self, credential: str, project: str) -> str:
        with self.transaction() as decision:
            identity = self._auth(credential, "capture", project, decision.evaluated_ms)
            token = self._token(
                "namespace",
                tenant=identity["tenant"],
                principal=identity["principal"],
                id=uid("ns"),
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
                    result = self._capture_result(tenant, occurrence["id"], occurrence["position"])
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
                    self._enqueue(tenant, position, obs, who["principal"])
                    result = self._capture_result(tenant, obs, position)
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

    def _visible(self, who: dict[str, Any], row: sqlite3.Row, at: int) -> bool:
        if row["deleted"]:
            return False
        try:
            self._subject_auth(
                self._token("credential", tenant=who["tenant"], principal=who["principal"]),
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
                sql += " AND id IN (SELECT id FROM lexical WHERE tenant=? AND lexical MATCH ?)"
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
        if not capabilities or not set(capabilities) <= CAPABILITIES - {"admin"}:
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
            return record
        except BaseException:
            self.healthy = False
            raise

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
                "INSERT OR REPLACE INTO grants VALUES (?,?,?,?,?,?)",
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
                "INSERT OR IGNORE INTO retired_sources VALUES (?,?,?,?)",
                (tenant, *action["source_identity"]),
            )
            self.db.execute(
                "UPDATE observations SET deleted=1,blob=NULL,readers=NULL,revision=revision+1 WHERE tenant=? AND id=?",
                (tenant, obs),
            )
            self.db.execute("DELETE FROM lexical WHERE tenant=? AND id=?", (tenant, obs))
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
            self.db.execute("DELETE FROM receipts WHERE tenant=?", (tenant,))
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
        else:
            raise LocalError("journal_invalid")
        if kind == "purge":
            from memoryking.adapters.knowledge_store import Knowledge

            Knowledge(self).purge_dependencies(tenant, action["observation"])
            # Conservatively erase all tenant rendered copies, including dependent promotions.
            self.db.execute(
                "UPDATE rendered_artifacts SET state='purged',body=NULL,request_json=NULL,diagnostics=NULL,dependencies='[]' WHERE tenant=?",
                (tenant,),
            )
            self.db.execute(
                "UPDATE checkpoints SET state='purged',body=NULL,dependencies='[]' WHERE tenant=?",
                (tenant,),
            )
            self.db.execute(
                "UPDATE retries SET result=NULL WHERE tenant=? AND namespace IN ('working-v1','handoff-v1')",
                (tenant,),
            )
            self.db.execute("UPDATE handoffs SET state='purged' WHERE tenant=?", (tenant,))
            # Outcomes and frozen experiments may retain quotations or judgments about
            # the purged material. Conservative tenant erasure avoids a derived copy.
            self.db.execute(
                "UPDATE experience_records SET body=NULL,dependencies='[]',readers=NULL WHERE tenant=?",
                (tenant,),
            )
            self.db.execute(
                "UPDATE ranking_state SET configuration=NULL,version=version+1,reconciliation=0 WHERE tenant=?",
                (tenant,),
            )
            self.db.execute(
                "UPDATE retries SET result=NULL WHERE tenant=? AND namespace='experience-v1'",
                (tenant,),
            )
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

    def _replay_journal(self) -> None:
        records = self.control.journal(self.anchor)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for record in records:
                self._apply(record, self.clock.utc_ms())
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if records:
            self.anchor["journal_sequence"] = records[-1]["sequence"]
            self.anchor["journal_head"] = hashlib.sha256(
                self.control.sign(records[-1]).encode()
            ).hexdigest()
            self.control.anchor(self.anchor)

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
        return int(count)

    def gc(self) -> int:
        with self.lock:
            live = {
                (row[0], row[1])
                for row in self.db.execute(
                    "SELECT tenant,blob FROM observations WHERE blob IS NOT NULL"
                )
            }
            removed = 0
            for directory in (self.root / "blobs").iterdir():
                private_dir(directory)
                for path in directory.iterdir():
                    private_file(path)
                    if (directory.name, path.name) not in live:
                        path.unlink()
                        removed += 1
                sync_dir(directory)
            return removed

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
                    "INSERT OR REPLACE INTO projection_items VALUES (?,?,?,?,?,?,?)",
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
            }
        self._expose()
        return result

    def reindex(self, credential: str) -> None:
        with self.transaction() as decision:
            who = self._auth(credential, "admin", "*", decision.evaluated_ms)
            self._reindex(who["tenant"])
        self._expose()

    def _reindex(self, tenant: str) -> None:
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

    def backup(self, destination: Path) -> dict[str, Any]:
        """Offline administrative method; intentionally absent from the agent socket."""
        with self.lock:
            self.owner.check()
            if (
                destination.exists()
                or destination.resolve() in (self.root.resolve(), self.control_path.resolve())
                or any(
                    p.resolve() in destination.resolve().parents
                    for p in (self.root, self.control_path)
                )
            ):
                raise LocalError("backup_requires_new_external_directory")
            private_dir(destination, create=True)
            atomic(destination / "store.sqlite", b"")
            target = sqlite3.connect(destination / "store.sqlite")
            try:
                self.db.backup(target)
            finally:
                target.close()
            with (destination / "store.sqlite").open("rb") as stream:
                os.fsync(stream.fileno())
            private_dir(destination / "blobs", create=True)
            blobs: list[str] = []
            for tenant, digest in self.db.execute(
                "SELECT DISTINCT tenant,blob FROM observations WHERE blob IS NOT NULL"
            ):
                relative = f"blobs/{tenant}/{digest}"
                private_dir((destination / relative).parent, create=True)
                raw = (self.root / relative).read_bytes()
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise LocalError("blob_corrupt")
                atomic(destination / relative, raw)
                blobs.append(relative)
            manifest = {
                "schema": 8,
                "store": self.store_id,
                "incarnation": self.incarnation,
                "frontiers": dict(self.db.execute("SELECT tenant,position FROM tenants")),
                "database_sha256": hashlib.sha256(
                    (destination / "store.sqlite").read_bytes()
                ).hexdigest(),
                "blobs": sorted(blobs),
            }
            atomic(destination / "manifest", self.control.sign(manifest).encode())
            sync_dir(destination)
            sync_dir(destination.parent)
            return manifest

    @classmethod
    def restore(
        cls,
        root: Path,
        control: Path,
        backup: Path,
        *,
        fault: Callable[[str], None] | None = None,
        clock: Clock | None = None,
        lost_anchor_journal_head: str | None = None,
    ) -> Path:
        """Explicit offline fresh-history activation. Missing anchor/journal fails closed."""
        paths(root, control)
        private_dir(root)
        private_dir(control)
        private_dir(backup)
        owner, controller_owner = Ownership(root), None
        hook = fault or (lambda _: None)
        try:
            controller_owner = Ownership(control)
            controller = Control(control)
            if lost_anchor_journal_head is not None:
                if (control / "anchor").exists() or not re.fullmatch(
                    r"[a-f0-9]{64}", lost_anchor_journal_head
                ):
                    raise LocalError("recovery_checkpoint_invalid")
                # Operator supplies the current head from an independently retained
                # checkpoint. Deriving it from this possibly rolled-back journal is unsafe.
                anchor = {
                    "store": controller.store,
                    "incarnation": "lost",
                    "state": "restoring",
                    "frontiers": {},
                    "commit_serial": 0,
                    "last_utc": 0,
                    "journal_sequence": 0,
                    "journal_head": "0" * 64,
                }
                records = controller.journal(anchor)
                head = (
                    hashlib.sha256(controller.sign(records[-1]).encode()).hexdigest()
                    if records
                    else "0" * 64
                )
                if head != lost_anchor_journal_head:
                    raise LocalError("recovery_checkpoint_invalid")
                anchor.update(journal_sequence=len(records), journal_head=head)
            else:
                anchor = controller.read_anchor()
            controller.journal(anchor)
            manifest = controller.verify_document(backup / "manifest")
            if (
                manifest.get("schema") not in {1, 2, 3, 4, 5, 6, 7, 8}
                or manifest.get("store") != controller.store
            ):
                raise LocalError("backup_invalid")
            data = (backup / "store.sqlite").read_bytes()
            if hashlib.sha256(data).hexdigest() != manifest["database_sha256"]:
                raise LocalError("backup_invalid")
            for relative in manifest["blobs"]:
                if not re.fullmatch(r"blobs/[A-Za-z0-9_-]{1,96}/[a-f0-9]{64}", relative):
                    raise LocalError("backup_invalid")
                if (
                    hashlib.sha256((backup / relative).read_bytes()).hexdigest()
                    != relative.rsplit("/", 1)[1]
                ):
                    raise LocalError("backup_invalid")
            anchor["state"] = "restoring"
            anchor["reconciliation_required"] = True
            anchor["history_loss"] = lost_anchor_journal_head is not None
            controller.anchor(anchor)
            hook("restore_fenced")
            for suffix in ("-wal", "-shm"):
                (root / ("store.sqlite" + suffix)).unlink(missing_ok=True)
            atomic(root / "store.sqlite", data)
            for relative in manifest["blobs"]:
                private_dir((root / relative).parent, create=True)
                atomic(root / relative, (backup / relative).read_bytes())
            # Build an unavailable instance under the already-held ownership locks.
            recovery = cls.__new__(cls)
            recovery.root, recovery.control_path = root, control
            recovery.owner, recovery.control_owner = owner, controller_owner
            recovery.control, recovery.anchor = controller, anchor
            recovery.clock, recovery.lock = clock or SystemClock(), threading.RLock()
            recovery.db = connect(root)
            try:
                recovery._load()
                recovery._replay_journal()
                recovery.incarnation = uid("hist")
                recovery.db.execute("BEGIN IMMEDIATE")
                recovery.db.execute(
                    "UPDATE meta SET value=? WHERE key='incarnation'", (recovery.incarnation,)
                )
                recovery.db.execute("UPDATE retries SET result=NULL")
                recovery.db.execute("DELETE FROM receipts")
                recovery.db.execute(
                    "UPDATE rendered_artifacts SET state='retired',body=NULL,request_json=NULL,diagnostics=NULL,dependencies='[]'"
                )
                recovery.db.execute(
                    "UPDATE checkpoints SET state='retired',body=NULL,dependencies='[]'"
                )
                recovery.db.execute("UPDATE handoffs SET state='retired'")
                recovery._retire_optional()
                for (tenant,) in recovery.db.execute("SELECT tenant FROM tenants").fetchall():
                    recovery._reindex(tenant)
                recovery.db.commit()
                recovery.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                recovery.gc()
                hook("restore_before_activation")
                anchor.update(
                    {
                        "state": "active",
                        "incarnation": recovery.incarnation,
                        "frontiers": dict(
                            recovery.db.execute("SELECT tenant,position FROM tenants")
                        ),
                        "last_utc": recovery.clock.utc_ms(),
                        "commit_serial": int(
                            recovery.db.execute(
                                "SELECT value FROM meta WHERE key='commit_serial'"
                            ).fetchone()[0]
                        ),
                    }
                )
                # Only the independent OS owner can recover administrative credentials.
                admin = recovery.db.execute(
                    "SELECT tenant,principal FROM grants WHERE project='*' ORDER BY tenant,principal LIMIT 1"
                ).fetchone()
                if admin is None:
                    raise LocalError("recovery_admin_missing")
                token = recovery._token("credential", tenant=admin[0], principal=admin[1])
                atomic(control / "owner.credential", token.encode())
                controller.anchor(anchor)
            finally:
                recovery.db.close()
            return control / "owner.credential"
        finally:
            owner.close()
            if controller_owner:
                controller_owner.close()

    def _retire_optional(self) -> None:
        """A restored history cannot reuse projected vectors, leases or adapter bases."""
        self.db.execute("UPDATE ranking_state SET configuration=NULL,version=version+1")
        self.db.execute("DELETE FROM adapter_bases")
        self.db.execute("DELETE FROM embedding_items")
        self.db.execute("DELETE FROM embedding_jobs")
        self.db.execute(
            "UPDATE embedding_partitions SET incarnation=?,state='disabled',generation=1,scheduled=0,frontier=0",
            (self.incarnation,),
        )
