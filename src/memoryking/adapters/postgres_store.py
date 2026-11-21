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

from memoryking.adapters import blobs as blobfiles
from memoryking.adapters.local_files import private_dir, private_file
from memoryking.adapters.local_store import (
    CAPABILITIES,
    MAX_COUNTER,
    LocalStore,
    identifier,
    paths,
    uid,
)
from memoryking.adapters.postgres_sql import PostgresSQL, take_gate
from memoryking.adapters.shared_control import SharedControl
from memoryking.application.storage import PostgresDialect
from memoryking.application.store_core import StoreCore, lease_holder
from memoryking.domain.local import Clock, Decision, LocalError, SystemClock
from memoryking.observability_metrics import Metrics


class _ReplayPending(Exception):
    """Unapplied journal records were found; leave the transaction and apply them first."""

    def __init__(self, records: list[dict[str, Any]]):
        super().__init__()
        self.records = records


# Journal replay is not part of any request's five seconds: a long purge needs longer than a
# request's budget. Each statement may run this long (milliseconds); the record count is
# bounded by the journal, and each record commits on its own.
JOURNAL_APPLY_STATEMENT_MS = 900_000


# When the anchor of each tenant directory was last written by this process (the opt-in
# `anchor_interval_ms`, see `_expose`).
ANCHOR_WRITTEN: dict[str, float] = {}


class PostgresStore(StoreCore):
    control: SharedControl
    tenant: str  # always bound; the local store leaves StoreCore.tenant None

    def native_index(self) -> str | None:
        """The pgvector version when the operator enabled it and built the converted table."""
        from memoryking.adapters.postgres_admin import PGVECTOR_VERSIONS

        extension = self.db.execute(
            "SELECT e.extversion,n.nspname FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n ON e.extnamespace=n.oid WHERE e.extname='vector'"
        ).fetchone()
        if extension is None or extension[1] != "memoryking_vector":
            return None
        if extension[0] not in PGVECTOR_VERSIONS:
            return None
        built = self.db.execute("SELECT to_regclass('memoryking.embedding_native') IS NOT NULL")
        return str(extension[0]) if built.fetchone()[0] else None

    def dense_shortlist(
        self,
        version: str,
        partition: Any,
        vector: list[int],
        eligibility: tuple[Any, ...],
        shortlist: int,
    ) -> list[Any]:
        """Approximate nearest neighbours by the HNSW index, then the reader's eligibility.

        The index only names candidates; tenant, project, deletion, audience and source
        freshness are applied to them in this statement, and the caller rescores what
        survives exactly from the stored vectors. A filtered approximate search can return
        fewer rows than the reader could see, which the caller detects and answers with the
        exact scan instead (see dense_search)."""
        self.db.execute("SELECT set_config('hnsw.ef_search',?,true)", (str(shortlist),))
        if tuple(int(part) for part in version.split(".")) >= (0, 8, 0):
            # Keep scanning the index until enough rows pass the filter.
            self.db.execute("SELECT set_config('hnsw.iterative_scan','relaxed_order',true)")
            self.db.execute("SELECT set_config('hnsw.max_scan_tuples','100000',true)")
        literal = "[" + ",".join(repr(value / 1_000_000) for value in vector) + "]"
        key = (partition["tenant"], partition["project"], partition["fingerprint"])
        rows = self.db.execute(
            "WITH cand AS MATERIALIZED (SELECT n.subject,n.part FROM embedding_native n "
            "WHERE n.tenant=? AND n.project=? AND n.fingerprint=? AND n.generation=? AND n.kind='observation' "
            "ORDER BY n.vec OPERATOR(memoryking_vector.<=>) ?::memoryking_vector.vector LIMIT ?) "
            "SELECT v.subject,v.vector FROM cand c "
            "JOIN embedding_vectors v ON v.tenant=? AND v.project=? AND v.fingerprint=? AND v.generation=? AND v.kind='observation' AND v.subject=c.subject AND v.part=c.part "
            "JOIN observations o ON v.tenant=o.tenant AND v.subject=o.id AND v.project=o.project AND v.source_revision=o.revision AND v.blob_hash=o.blob "
            "WHERE v.incarnation=? AND o.deleted=0 AND +o.position<=? "
            "AND (o.readers='null' OR EXISTS(SELECT 1 FROM json_each(o.readers) WHERE value=?))",
            (
                *key,
                partition["generation"],
                literal,
                shortlist,
                *key,
                partition["generation"],
                self.incarnation,
                eligibility[1],  # recorded position
                eligibility[0],  # reader
            ),
        ).fetchall()
        return list(rows)

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
        append: bool = False,
        replay_pending: bool = False,
        audience: str = "memoryking-shared",
        request_deadline: float | None = None,
        instance_id: str | None = None,
    ):
        """`request_deadline` is a monotonic instant: checkout, the checkout checks and
        every statement in this store's transactions stop at it, so a saturated pool or
        a slow query fails as `service_busy` or `service_unavailable` within the caller's
        own budget instead of past it."""
        identifier(tenant)
        paths(root, control)
        private_dir(root)
        private_dir(control)
        try:
            private_dir(control / tenant)
        except FileNotFoundError:
            # A tenant that does not exist answers exactly like a credential that does not
            # verify for one that does, so the response never reveals which tenants exist.
            raise LocalError("invalid_token") from None
        self.root, self.control_path = root, control / tenant
        self.lease = lease_holder(self.control_path, instance_id)
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
        # A delegated credential acts as its issuer for this subject; every event and receipt
        # the request writes records the subject (the transaction supplies it as a setting).
        self.on_behalf_of: str | None = (identity.get("delegation") or {}).get("on_behalf_of")
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
        self.reranker: Any | None = None
        self.last_utc = self.anchor["last_utc"]
        self.release_only = release_only
        # An appending request (a capture and the drain after it) shares the gate with briefings
        # and with other appends, which a per-tenant lock serializes among themselves. It may
        # record events but not journal a policy decision, delete files or purge.
        self.append = append
        # A release-only request normally refuses while journal records are unapplied (a writer
        # applies them). A briefing keeps the behaviour it had under the exclusive gate: it
        # applies them first, in transactions of their own, then starts over on the share gate.
        self.replay_pending = replay_pending
        self.active = False
        self.metrics: list[dict[str, float]] = []
        self.committed: dict[str, Any] | None = None
        self.serial_seen = 0  # the database's commit serial when this transaction began
        self.pool = pool
        self.request_deadline = request_deadline
        from memoryking.adapters.postgres_admin import schema_compatible, schema_refusal
        from memoryking.adapters.postgres_pool import acquire, checked

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
            # getconn() does not run the pool's checkout check callback. The schema version is
            # read with the checks: it moves only when someone migrates, and a connection that
            # has just been checked has just been told.
            ran = checked(self.connection)
            cached = getattr(self.connection, "memoryking_schema", None)
            if cached is None or ran:
                version = self.connection.execute(
                    "SELECT value FROM memoryking.shared_meta WHERE key='schema'"
                ).fetchone()
                cached = None if version is None else version[0]
                self.connection.memoryking_schema = cached
            if cached is None:
                raise LocalError("schema_unsupported")
            if not (cached.isdigit() and schema_compatible(int(cached))):
                raise schema_refusal(cached)
            self.db = self.facade = PostgresSQL(self.connection, lambda: self.active)
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
            # A delegated token is its issuer acting with a narrower scope, never the named
            # subject. A token minted when delegation acted as the subject has no `on_behalf_of`
            # and is refused outright instead of being reinterpreted.
            if "on_behalf_of" not in delegation:
                raise LocalError("delegation_superseded")
            if (
                identity.get("principal") != delegation["by"]
                or project != delegation["project"]
                or capability not in delegation["capabilities"]
                or at >= delegation["expires"]
            ):
                raise LocalError("denied")
            # The named recipient must still hold the capability, on the generation it held at
            # delegation time; the issuer's own grant and generation are checked below.
            recipient = self._token(
                "credential",
                tenant=self.tenant,
                principal=delegation["on_behalf_of"],
                generation=delegation["on_behalf_of_generation"],
            )
            try:
                StoreCore._auth(self, recipient, capability, project, at)
            except LocalError as exc:
                raise LocalError(exc.code) from None  # nothing about the recipient's grants
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
        try:
            identity = self._decode(token, "credential")
            if identity.get("tenant") != self.tenant:
                raise LocalError("denied")
            if identity.get("principal") == self.principal:
                return self._auth(self.credential, capability, project, at)
            return LocalStore._auth(self, token, capability, project, at)
        except LocalError as exc:
            # The subject may be another principal: never say which capability they lack.
            raise LocalError(exc.code) from None

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
                try:
                    LocalStore._auth(self, subject, capability, project, decision.evaluated_ms)
                except LocalError as exc:
                    raise LocalError(exc.code) from None  # nothing about the subject's grants
            delegation = {
                "id": uid("delegation"),
                "by": self.principal,
                "on_behalf_of": principal,
                "on_behalf_of_generation": self._generation(self.tenant, principal),
                "project": project,
                "capabilities": sorted(set(capabilities)),
                "expires": decision.evaluated_ms + ttl_ms,
            }
            token = self._token(
                "credential",
                tenant=self.tenant,
                principal=self.principal,
                generation=self._generation(self.tenant, self.principal),
                delegation=delegation,
                delegation_chain=[delegation["id"]],
            )
        self._expose()
        return token

    def _settings(self, statement_ms: int | None = None) -> None:
        """Transaction settings. `statement_ms` replaces the request budget for one statement,
        which only journal replay uses: its work is not part of any request's five seconds."""
        self.connection.execute(
            "SET TRANSACTION ISOLATION LEVEL READ COMMITTED; SET LOCAL search_path=memoryking,pg_catalog"
        )
        remaining_ms = (
            5000
            if self.request_deadline is None
            else min(5000, int((self.request_deadline - time.monotonic()) * 1000))
        )
        if remaining_ms <= 0:
            raise LocalError("service_busy")
        # Journal replay applies records other principals wrote; it carries no subject.
        self.connection.execute(
            "SELECT set_config('lock_timeout',%s,true),set_config('statement_timeout',%s,true),"
            "set_config('memoryking.tenant',%s,true),set_config('memoryking.principal',%s,true),"
            "set_config('memoryking.on_behalf_of',%s,true)",
            (
                str(remaining_ms),
                str(remaining_ms if statement_ms is None else statement_ms),
                self.tenant,
                self.principal,
                (self.on_behalf_of or "") if statement_ms is None else "",
            ),
        )

    def _control_guard(self) -> Any:
        return self.control.locked()

    def _validate_anchor(self) -> list[dict[str, Any]]:
        with self.control.locked():
            self.anchor = self.control.read_anchor()
            held = self.control.verified_count(self.anchor)
            held_operations = self.control.operations.verified_count(self.anchor)
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
        applied, applied_operations = int(meta["journal"]), int(meta.get("operations", 0))
        if applied_operations > held_operations:
            raise LocalError("journal_rollback")
        if applied > held:
            # The database applied more journal records than the control journal holds: the
            # journal tail was lost or another control tree is in use. Never skip silently.
            raise LocalError("journal_rollback")
        self.last_utc = max(self.last_utc, self.anchor["last_utc"])
        self.serial_seen = int(meta["commit_serial"])
        if applied == held and applied_operations == held_operations:
            return []
        with self.control.locked():
            return [
                *self.control.records_after(self.anchor, applied),
                *self.control.operations.records_after(self.anchor, applied_operations),
            ]

    def _replay_pending(self, records: list[dict[str, Any]]) -> None:
        """Apply unapplied journal records, each in its own transaction without a request budget.

        Journal first, database second, anchor third: a record is applied and committed before
        the anchor advances over it, and progress survives an interruption because every
        record that committed stays applied. A record that fails is noted for the operator
        (sequence, kind, tenant and error code only) and raised; nothing is skipped, so the
        records after it stay pending.
        """
        restore, self.release_only = self.release_only, False  # applying is a policy write
        try:
            self._apply_pending(records)
        finally:
            self.release_only = restore

    def _apply_pending(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            try:
                with self.connection.transaction():
                    self._settings(JOURNAL_APPLY_STATEMENT_MS)
                    self.active = True
                    row = take_gate(self.connection, self.tenant, shared=False)
                    if row is None:
                        raise LocalError("denied")
                    self._apply(record, self._now().evaluated_ms)
            except BaseException as exc:
                self.control.note_apply_failure(record, exc)
                raise
            finally:
                self.active = False
            with self.control.locked():
                anchor = self.control.read_anchor()
                if record.get("log") == "operations":
                    if anchor.get("operations_sequence", 0) < record["sequence"]:
                        self.control.operations.verified_count(anchor)
                        anchor["operations_sequence"] = record["sequence"]
                        anchor["operations_head"] = self.control.operations.head(record["sequence"])
                        self.control.anchor(anchor)
                elif anchor["journal_sequence"] < record["sequence"]:
                    anchor["journal_sequence"] = record["sequence"]
                    anchor["journal_head"] = hashlib.sha256(
                        self.control.sign(record).encode()
                    ).hexdigest()
                    self.control.anchor(anchor)
                self.anchor = anchor
            if record.get("log") != "operations":
                self.control.clear_apply_failure(record["sequence"])

    def _lease_writable(self) -> bool:
        # The grants decide: the runtime role writes the lease, the worker and backup roles
        # only read it. Asked only when a claim or renewal is due, inside the open transaction.
        row = self.connection.execute(
            "SELECT has_table_privilege('memoryking.instance_lease','INSERT') "
            "AND has_table_privilege('memoryking.instance_lease','UPDATE')"
        ).fetchone()
        return bool(row[0])

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
                replays = 0
                while True:
                    try:
                        with self.connection.transaction():
                            self._settings()
                            self.active = True
                            row = take_gate(
                                self.connection,
                                self.tenant,
                                shared=self.release_only or self.append,
                            )
                            if row is None:
                                raise LocalError("denied")
                            if self.append:
                                # Appends keep their order among themselves (positions, retries,
                                # one lexical index writer) exactly as under the exclusive gate.
                                self.connection.execute(
                                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                                    ("memoryking.append:" + self.tenant,),
                                )
                            locked = time.monotonic()
                            # Before any replay: a refused holder must not apply records.
                            self._hold_lease(
                                self.clock.utc_ms(), writer=not (self.release_only or self.append)
                            )
                            pending = self._validate_anchor()
                            if pending and self.release_only and not self.replay_pending:
                                raise LocalError("journal_replay_required")
                            if pending:
                                # Applied outside this request's transaction and budget, one record
                                # per transaction, then this request starts over on a settled journal.
                                raise _ReplayPending(pending)
                            decision = self._now()
                            self.facade.writes = 0
                            yield decision
                            self.fault("before_commit")
                            decision.check(self.clock)
                            clock_started = time.monotonic()
                            if self.facade.writes:
                                serial = self.connection.execute(
                                    "SELECT memoryking.next_commit_serial(%s,%s)",
                                    (self.tenant, MAX_COUNTER),
                                ).fetchone()
                                if serial is None or serial[0] is None:
                                    raise LocalError("counter_exhausted")
                            else:
                                # Nothing was written: there is nothing to order, and the one
                                # tenant-wide counter row is not taken (a plain read does not
                                # queue behind every committing request).
                                serial = (self.serial_seen,)
                            clock_locked = time.monotonic()
                            position = self.db.execute(
                                "SELECT position FROM tenants WHERE tenant=?", (self.tenant,)
                            ).fetchone()[0]
                            decision.check(self.clock)
                            committed = {
                                "commit_serial": int(serial[0]),
                                "position": position,
                                "last_utc": self.last_utc,
                                "wrote": bool(self.facade.writes),
                            }
                        self.committed = committed
                        committed_ok = True
                        break
                    except _ReplayPending as replay:
                        self.active = False
                        replays += 1
                        if replays > 3:
                            raise LocalError("journal_replay_required") from None
                        self._replay_pending(replay.records)
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
        if self.release_only or self.append or action["tenant"] != self.tenant:
            raise LocalError("exclusive_gate_required")
        with self.control.locked():
            self.anchor = self.control.read_anchor()
            return super()._journal(action)

    def _expose(self) -> None:
        """Make the anchor cover the history this request committed before it acknowledges.

        Every request that committed a row is anchored first (see `LocalStore._expose_locked`),
        and a request that committed nothing writes nothing unless it names a position the
        anchor does not yet cover. Under the control lock the anchor is read again, so a writer
        whose commit an earlier writer's anchor write already covers acknowledges without a
        second write. `anchor_interval_ms` above zero is the operator's opt-in to writing the
        serial and clock of a request that moved no position at most that late."""
        if not self.healthy or self.committed is None:
            raise LocalError("recovery_required")
        committed, held = self.committed, self.anchor
        covered = committed["position"] <= held["frontiers"].get(self.tenant, 0)
        now = time.monotonic()
        key = str(self.control_path)
        interval = self.anchor_interval_ms
        if interval:
            soft = (
                committed["commit_serial"] > held["commit_serial"]
                or committed["last_utc"] > held["last_utc"]
            )
            due = soft and (
                now - ANCHOR_WRITTEN.get(key, 0.0) >= interval / 1000
                or committed["last_utc"] - held["last_utc"] >= interval
            )
        else:
            due = committed["wrote"]
        if covered and not due:
            if self.metrics:
                self.metrics[-1]["anchor_ms"] = 0.0
            return
        started = time.monotonic()
        try:
            with self.control.locked():
                anchor = self.control.read_anchor()
                if anchor["state"] != "active" or anchor["incarnation"] != self.incarnation:
                    raise LocalError("history_changed")
                if (
                    committed["position"] <= anchor["frontiers"].get(self.tenant, 0)
                    and committed["commit_serial"] <= anchor["commit_serial"]
                    and committed["last_utc"] <= anchor["last_utc"]
                ):
                    self.anchor = anchor  # an earlier writer's anchor write covers this one
                    return
                self.fault("before_anchor")
                anchor["frontiers"][self.tenant] = max(
                    anchor["frontiers"].get(self.tenant, 0), committed["position"]
                )
                for field in ("commit_serial", "last_utc"):
                    anchor[field] = max(anchor[field], committed[field])
                self.control.anchor(anchor)
                self.anchor = anchor
                ANCHOR_WRITTEN[key] = now
                self.fault("after_anchor")
            if self.metrics:
                self.metrics[-1]["anchor_ms"] = (time.monotonic() - started) * 1000
        except BaseException:
            self.healthy = False
            raise

    def gc(self) -> int:
        # The exclusive gate prevents deleting a durable staging blob before adoption.
        if self.release_only or self.append:
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
                touched = {directory}
                for name, path in blobfiles.iterate(directory):
                    private_file(path)
                    if name not in live:
                        path.unlink()
                        touched.add(path.parent)
                        removed += 1
                blobfiles.sync_directories(touched)
        return removed

    def _delete_unreferenced(self, items: list[tuple[str, str]]) -> int:
        """Delete the files of purged observations that no other observation names, under the
        exclusive gate, so a capture that is staging the same content cannot lose its file."""
        if self.release_only or self.append:
            raise LocalError("exclusive_gate_required")
        removed = 0
        with self.transaction():
            for tenant, digest in dict.fromkeys(items):
                if (
                    self.db.execute(
                        "SELECT 1 FROM observations WHERE tenant=? AND blob=? LIMIT 1",
                        (tenant, digest),
                    ).fetchone()
                    is None
                ):
                    removed += blobfiles.remove(self.root, tenant, digest)
        return removed

    def backup(self, destination: Path) -> dict[str, Any]:
        raise LocalError("shared_backup_command_required")

    @classmethod
    def restore(cls, *args: Any, **kwargs: Any) -> Any:
        raise LocalError("shared_restore_command_required")

    @classmethod
    def initialize(cls, *args: Any, **kwargs: Any) -> Any:
        raise LocalError("shared_migration_command_required")
