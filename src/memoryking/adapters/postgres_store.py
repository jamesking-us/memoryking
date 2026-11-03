"""Request-bound PostgreSQL storage using the reviewed relational service operations.

SQL, gates, pool lifecycle and recovery evidence are native shared implementations.
A store is bound to one signed tenant/principal; transport must never reuse it for
another authenticated identity. All final decisions run on the primary.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from memoryking.adapters.local_files import private_dir, private_file, sync_dir
from memoryking.adapters.local_store import (
    CAPABILITIES,
    MAX_COUNTER,
    LocalStore,
    identifier,
    paths,
    uid,
)
from memoryking.adapters.postgres_sql import PostgresSQL
from memoryking.adapters.shared_control import SharedControl
from memoryking.application.storage import PostgresDialect
from memoryking.application.store_core import StoreCore
from memoryking.domain.local import Clock, Decision, LocalError, SystemClock
from memoryking.observability_metrics import Metrics


class PostgresStore(StoreCore):
    control: SharedControl

    def dense_exact(
        self, statement: str, parameters: tuple[Any, ...], request: dict[str, Any], limit: int
    ) -> dict[str, Any] | None:
        """Explicit optional pgvector engine, with a materialized eligibility boundary.

        No approximate index exists. Float32 cosine can differ by a rounding unit
        from the portable integer-vector calculation; the receipt names the engine.
        """
        import json

        extension = self.db.execute(
            "SELECT e.extversion,n.nspname FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n ON e.extnamespace=n.oid WHERE e.extname='vector'"
        ).fetchone()
        if extension is None or tuple(extension) != ("0.8.7", "memoryking_vector"):
            return None
        prefix = "WITH eligible AS MATERIALIZED (" + statement + ") "
        count = self.db.execute(prefix + "SELECT count(*) FROM eligible", parameters).fetchone()[0]
        if count > limit:
            return {"availability": "scan_limit", "candidate_limit_reached": True}
        rows = self.db.execute(
            prefix
            + ", scored AS (SELECT observation,LEAST(1000000,GREATEST(-1000000,ROUND((1-(vector_json::memoryking_vector.vector OPERATOR(memoryking_vector.<=>) ?::memoryking_vector.vector))*1000000)))::bigint AS score FROM eligible) SELECT observation,score FROM scored WHERE score>=? ORDER BY score DESC,observation LIMIT 257",
            (
                *parameters,
                json.dumps(request["vector"]),
                request.get("minimum_similarity_ppm", 200000),
            ),
        ).fetchall()
        return {
            "scores": {row["observation"]: row["score"] for row in rows[:256]},
            "eligible_vectors": count,
            "candidate_limit_reached": len(rows) > 256,
        }

    def __init__(
        self,
        pool: Any,
        root: Path,
        control: Path,
        tenant: str,
        credential: str,
        *,
        clock: Clock | None = None,
        fault: Callable[[str], None] | None = None,
        release_only: bool = False,
        audience: str = "memoryking-shared",
        request_deadline: float | None = None,
    ):
        """`request_deadline` is a monotonic instant: checkout, the checkout checks and
        every statement in this store's transactions stop at it, so a saturated pool or
        a slow query fails as `service_busy` or `service_unavailable` within the caller's
        own budget instead of past it."""
        identifier(tenant)
        paths(root, control)
        private_dir(root)
        private_dir(control)
        private_dir(control / tenant)
        self.root, self.control_path = root, control / tenant
        self.control = SharedControl(self.control_path)
        self.anchor = self.control.read_anchor()
        self.store_id, self.incarnation = self.control.store, self.anchor["incarnation"]
        self.tenant = tenant
        self.audience = identifier(audience)
        self.credential = credential
        identity = self._decode(credential, "credential")
        if identity.get("tenant") != tenant:
            raise LocalError("denied")
        self.principal = identifier(identity["principal"])
        self.clock = clock or SystemClock()
        self.fault = fault or (lambda _: None)
        self.lock = threading.RLock()
        self.dialect = PostgresDialect()
        self.healthy = True
        self.retrieval_cache: dict[str, Any] = {}
        self._last_housekeeping = 0
        self.counters = Metrics()
        self.timings = threading.local()
        self.embedder: Any | None = None
        self.last_utc = self.anchor["last_utc"]
        self.release_only = release_only
        self.active = False
        self.metrics: list[dict[str, float]] = []
        self.committed: dict[str, Any] | None = None
        self.pool = pool
        self.request_deadline = request_deadline
        from memoryking.adapters.postgres_pool import acquire, check

        remaining = None if request_deadline is None else request_deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise LocalError("service_busy")
        self.connection = acquire(pool, timeout=remaining)
        try:
            if request_deadline is not None:
                remaining_ms = max(1, int((request_deadline - time.monotonic()) * 1000))
                self.connection.execute(
                    "SELECT set_config('statement_timeout',%s,false)", (str(remaining_ms),)
                )
            # getconn() does not run the pool's checkout check callback.
            check(self.connection)
            version = self.connection.execute(
                "SELECT value FROM memoryking.shared_meta WHERE key='schema'"
            ).fetchone()
            if version is None or version[0] != "7":
                raise LocalError("schema_unsupported")
            self.db = PostgresSQL(self.connection, lambda: self.active)
        except BaseException as exc:
            pool.putconn(self.connection)
            if request_deadline is not None and (
                time.monotonic() >= request_deadline or getattr(exc, "sqlstate", None) == "57014"
            ):
                raise LocalError("service_unavailable") from exc
            raise

    def close(self) -> None:
        if self.connection is not None:
            self.connection.rollback()
            self.pool.putconn(self.connection)
            self.connection = None

    def _auth(self, token: str, capability: str, project: str, at: int) -> dict[str, Any]:
        identity = self._decode(token, "credential")
        if identity.get("tenant") != self.tenant or identity.get("principal") != self.principal:
            raise LocalError("denied")
        delegation = identity.get("delegation")
        if delegation:
            if (
                project != delegation["project"]
                or capability not in delegation["capabilities"]
                or at >= delegation["expires"]
            ):
                raise LocalError("denied")
            parent = self._token("credential", tenant=self.tenant, principal=delegation["by"])
            StoreCore._auth(self, parent, capability, project, at)
        return super()._auth(token, capability, project, at)

    def _token(self, kind: str, **fields: Any) -> str:
        if kind == "credential":
            fields["audience"] = self.audience
        return super()._token(kind, **fields)

    def _decode(self, token: str, kind: str) -> dict[str, Any]:
        identity = super()._decode(token, kind)
        if kind == "credential" and identity.get("audience") != self.audience:
            raise LocalError("credential_audience_mismatch")
        return identity

    def _subject_auth(self, token: str, capability: str, project: str, at: int) -> dict[str, Any]:
        identity = self._decode(token, "credential")
        if identity.get("tenant") != self.tenant:
            raise LocalError("denied")
        if identity.get("principal") == self.principal:
            return self._auth(self.credential, capability, project, at)
        return LocalStore._auth(self, token, capability, project, at)

    def _context_details(
        self, who: dict[str, Any], capabilities: set[str], project: str, at: int
    ) -> dict[str, Any]:
        allowed = []
        for capability in sorted(capabilities):
            try:
                self._auth(self.credential, capability, project, at)
                allowed.append(capability)
            except LocalError:
                pass
        return {"capabilities": allowed, "delegation_chain": who.get("delegation_chain", [])}

    def delegate(self, principal: str, project: str, capabilities: list[str], ttl_ms: int) -> str:
        identifier(principal)
        identifier(project)
        if (
            not capabilities
            or not set(capabilities) <= CAPABILITIES - {"admin"}
            or not isinstance(ttl_ms, int)
            or isinstance(ttl_ms, bool)
            or not 1 <= ttl_ms <= 3600000
        ):
            raise LocalError("invalid_delegation")
        with self.transaction() as decision:
            actor = self._decode(self.credential, "credential")
            if actor.get("delegation"):
                raise LocalError("delegation_depth_exceeded")
            subject = self._token("credential", tenant=self.tenant, principal=principal)
            for capability in capabilities:
                self._auth(self.credential, capability, project, decision.evaluated_ms)
                LocalStore._auth(self, subject, capability, project, decision.evaluated_ms)
            delegation = {
                "id": uid("delegation"),
                "by": self.principal,
                "project": project,
                "capabilities": sorted(set(capabilities)),
                "expires": decision.evaluated_ms + ttl_ms,
            }
            token = self._token(
                "credential",
                tenant=self.tenant,
                principal=principal,
                delegation=delegation,
                delegation_chain=[delegation["id"]],
            )
        self._expose()
        return token

    def _settings(self) -> None:
        self.connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        self.connection.execute("SET LOCAL search_path=memoryking,pg_catalog")
        remaining_ms = (
            5000
            if self.request_deadline is None
            else min(5000, int((self.request_deadline - time.monotonic()) * 1000))
        )
        if remaining_ms <= 0:
            raise LocalError("service_busy")
        for setting in ("lock_timeout", "statement_timeout"):
            self.connection.execute("SELECT set_config(%s,%s,true)", (setting, str(remaining_ms)))
        self.connection.execute("SELECT set_config('memoryking.tenant',%s,true)", (self.tenant,))
        self.connection.execute(
            "SELECT set_config('memoryking.principal',%s,true)", (self.principal,)
        )

    def _validate_anchor(self) -> list[dict[str, Any]]:
        with self.control.locked():
            self.anchor = self.control.read_anchor()
            records = self.control.journal(self.anchor)
        meta = dict(self.db.execute("SELECT key,value FROM meta"))
        position = self.db.execute(
            "SELECT position FROM tenants WHERE tenant=?", (self.tenant,)
        ).fetchone()
        if (
            self.anchor["state"] != "active"
            or meta.get("store") != self.store_id
            or meta.get("incarnation") != self.incarnation
            or self.anchor["incarnation"] != self.incarnation
            or int(meta["commit_serial"]) < self.anchor["commit_serial"]
            or position is None
            or position[0] < self.anchor["frontiers"].get(self.tenant, 0)
        ):
            raise LocalError("recovery_required")
        self.last_utc = max(self.last_utc, self.anchor["last_utc"])
        return [record for record in records if record["sequence"] > int(meta["journal"])]

    @contextmanager
    def transaction(self) -> Iterator[Decision]:
        with self.lock:
            if not self.healthy or self.active or self.connection is None:
                raise LocalError("recovery_required")
            started = time.monotonic()
            locked: float | None = None
            clock_started: float | None = None
            clock_locked: float | None = None
            committed_ok = False
            try:
                with self.connection.transaction():
                    self._settings()
                    self.active = True
                    mode = "SHARE" if self.release_only else "UPDATE"
                    row = self.connection.execute(
                        f"SELECT tenant FROM memoryking.tenant_gates WHERE tenant=%s FOR {mode}",
                        (self.tenant,),
                    ).fetchone()
                    if row is None:
                        raise LocalError("denied")
                    locked = time.monotonic()
                    pending = self._validate_anchor()
                    if pending and self.release_only:
                        raise LocalError("journal_replay_required")
                    decision = self._now()
                    for record in pending:
                        self._apply(record, decision.evaluated_ms)
                    if pending:
                        with self.control.locked():
                            anchor = self.control.read_anchor()
                            anchor["journal_sequence"] = pending[-1]["sequence"]
                            anchor["journal_head"] = hashlib.sha256(
                                self.control.sign(pending[-1]).encode()
                            ).hexdigest()
                            self.control.anchor(anchor)
                            self.anchor = anchor
                    yield decision
                    self.fault("before_commit")
                    decision.check(self.clock)
                    clock_started = time.monotonic()
                    serial = self.connection.execute(
                        "SELECT memoryking.next_commit_serial(%s,%s)", (self.tenant, MAX_COUNTER)
                    ).fetchone()
                    if serial is None or serial[0] is None:
                        raise LocalError("counter_exhausted")
                    clock_locked = time.monotonic()
                    position = self.db.execute(
                        "SELECT position FROM tenants WHERE tenant=?", (self.tenant,)
                    ).fetchone()[0]
                    decision.check(self.clock)
                    committed = {
                        "commit_serial": int(serial[0]),
                        "position": position,
                        "last_utc": self.last_utc,
                    }
                self.committed = committed
                committed_ok = True
                self.fault("after_commit")
                decision.check(self.clock)
            finally:
                self.active = False
                metric = {
                    "transaction_ms": (time.monotonic() - started) * 1000,
                    "committed": float(committed_ok),
                }
                if locked is not None:
                    metric["gate_wait_ms"] = (locked - started) * 1000
                if clock_started is not None and clock_locked is not None:
                    metric["clock_wait_ms"] = (clock_locked - clock_started) * 1000
                self.metrics.append(metric)
                spans = getattr(self.timings, "items", None)
                if spans is not None:
                    spans.append(
                        {
                            "lock_wait_ms": metric.get("gate_wait_ms", 0.0),
                            "transaction_ms": metric["transaction_ms"],
                        }
                    )

    def _event(self, tenant: str, kind: str, subject: str, at: int, actor: str = "system") -> int:
        if self.release_only or tenant != self.tenant:
            raise LocalError("exclusive_gate_required")
        return super()._event(tenant, kind, subject, at, actor)

    def _journal(self, action: dict[str, Any]) -> dict[str, Any]:
        if self.release_only or action["tenant"] != self.tenant:
            raise LocalError("exclusive_gate_required")
        with self.control.locked():
            self.anchor = self.control.read_anchor()
            return super()._journal(action)

    def _expose(self) -> None:
        if not self.healthy or self.committed is None:
            raise LocalError("recovery_required")
        started = time.monotonic()
        try:
            with self.control.locked():
                anchor = self.control.read_anchor()
                if anchor["state"] != "active" or anchor["incarnation"] != self.incarnation:
                    raise LocalError("history_changed")
                self.fault("before_anchor")
                anchor["frontiers"][self.tenant] = max(
                    anchor["frontiers"].get(self.tenant, 0), self.committed["position"]
                )
                for key in ("commit_serial", "last_utc"):
                    anchor[key] = max(anchor[key], self.committed[key])
                self.control.anchor(anchor)
                self.anchor = anchor
                self.fault("after_anchor")
            if self.metrics:
                self.metrics[-1]["anchor_ms"] = (time.monotonic() - started) * 1000
        except BaseException:
            self.healthy = False
            raise

    def gc(self) -> int:
        # The exclusive gate prevents deleting a durable staging blob before adoption.
        if self.release_only:
            raise LocalError("exclusive_gate_required")
        with self.transaction():
            live = {
                row[0]
                for row in self.db.execute(
                    "SELECT blob FROM observations WHERE tenant=? AND blob IS NOT NULL",
                    (self.tenant,),
                )
            }
            directory = self.root / "blobs" / self.tenant
            removed = 0
            if directory.exists():
                private_dir(directory)
                for path in directory.iterdir():
                    private_file(path)
                    if path.name not in live:
                        path.unlink()
                        removed += 1
                sync_dir(directory)
        return removed

    def backup(self, destination: Path) -> dict[str, Any]:
        raise LocalError("shared_backup_command_required")

    @classmethod
    def restore(cls, *args: Any, **kwargs: Any) -> Any:
        raise LocalError("shared_restore_command_required")

    @classmethod
    def initialize(cls, *args: Any, **kwargs: Any) -> Any:
        raise LocalError("shared_migration_command_required")
