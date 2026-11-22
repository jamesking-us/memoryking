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
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Self

from memoryking.adapters import blobs as blobfiles
from memoryking.adapters.local_files import (
    Control,
    Ownership,
    atomic,
    durable_sync,
    private_dir,
    private_file,
    read_blob_index,
    sync_dir,
    write_blob_index,
)
from memoryking.application.storage import SQLiteDialect
from memoryking.application.store_core import (  # noqa: F401
    ARTIFACT_BYTES_LIMIT,
    ARTIFACT_LIMIT,
    CAPABILITIES,
    CAPACITY_LIMITS,
    CONCURRENCY_LIMIT,
    INTENT_LIMIT,
    INTENT_SCOPE_LIMIT,
    MAX_ATTEMPTS,
    MAX_COUNTER,
    OPEN_INTENT_STATES,
    QUEUE_LIMIT,
    RETRY_MS,
    USAGE_LIMIT,
    StoreCore,
    identifier,
    lease_holder,
    uid,
)
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import (
    Clock,
    Decision,
    LocalError,
    SystemClock,
    temporal_valid,
)
from memoryking.observability_metrics import Metrics

# Per-tenant capacity bounds. Each has a release path: the daemon drains audit jobs,
# retained artifacts evict oldest-first, only open intents count, and expired usage rows
# are pruned. `status` reports utilisation for every entry of CAPACITY_LIMITS.


def recovery_admin(db: Any, at: int, tenant: str | None = None) -> tuple[str, str] | None:
    """The first tenant-wide grant that holds a currently valid admin capability.

    The local restore searches every tenant; the shared restore names its own.
    """
    scope, bound = (" AND tenant=?", (tenant,)) if tenant else ("", ())
    for row in db.execute(
        "SELECT tenant,principal,capabilities,starts,expires FROM grants WHERE project='*'"
        + scope
        + " ORDER BY tenant,principal",
        bound,
    ).fetchall():
        capabilities = row["capabilities"]
        if isinstance(capabilities, str):
            capabilities = json.loads(capabilities)
        if "admin" in capabilities and temporal_valid(at, row["starts"], row["expires"]):
            return str(row["tenant"]), str(row["principal"])
    return None


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


__all__ = [
    "ARTIFACT_BYTES_LIMIT",
    "ARTIFACT_LIMIT",
    "CAPABILITIES",
    "CAPACITY_LIMITS",
    "CONCURRENCY_LIMIT",
    "INTENT_LIMIT",
    "INTENT_SCOPE_LIMIT",
    "MAX_ATTEMPTS",
    "MAX_COUNTER",
    "OPEN_INTENT_STATES",
    "QUEUE_LIMIT",
    "RETRY_MS",
    "USAGE_LIMIT",
    "LocalStore",
    "StoreCore",
    "connect",
    "identifier",
    "paths",
    "recovery_admin",
    "uid",
]


SCHEMA_VERSION = 21  # the SQLite user_version the daemon migrates every store to


class LocalStore(StoreCore):
    def __init__(
        self,
        root: Path,
        control: Path,
        *,
        clock: Clock | None = None,
        fault: Callable[[str], None] | None = None,
        instance_id: str | None = None,
    ):
        paths(root, control)
        private_dir(root)
        private_dir(control)
        self.root, self.control_path = root, control
        self.lease = lease_holder(control, instance_id)
        self.owner = Ownership(root)
        try:
            self.control_owner = Ownership(control)
        except Exception:
            self.owner.close()
            raise
        self.clock = clock or SystemClock()
        self.fault = fault or (lambda _: None)
        self.lock = threading.RLock()
        self._anchor_at = self.clock.monotonic_ms()  # when the anchor was last written
        self.dialect = SQLiteDialect()
        # Per-thread transaction timings, collected only while a traced request runs.
        self.timings = threading.local()
        self.healthy = True
        self.retrieval_cache: dict[str, Any] = {}
        self._last_housekeeping = 0
        self._layout_done = False
        self.counters = Metrics()
        # A loaded local embedding model lets briefings embed their own queries.
        self.embedder: Any | None = None
        self.rerank_window: int | None = None  # candidates a cross-encoder reorders; None is 16
        self.fusion: dict[str, Any] | None = None  # None is the default combination
        self.reranker: Any | None = None
        # A scheduled backup policy the daemon checks between requests.
        self.backup_policy: Any | None = None
        # Idle threshold for owner-derived session summaries; 0 disables.
        self.summary_idle_ms = 0
        self._last_summary_scan = 0
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
            # A purge that committed but did not finish deleting its file (a crash) is the
            # only reason to sweep the blob directory at start; a normal start never does.
            if self._gc_pending():
                self.gc()
        except Exception:
            if hasattr(self, "db"):
                self.db.close()
            self.control_owner.close()
            self.owner.close()
            raise

    def _load(self) -> None:
        # Migrations run in order up to SCHEMA_VERSION.
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
        if version in {1, 2, 3, 4, 5, 6, 7, 8}:
            self.db.executescript(Path(__file__).with_name("index_schema.sql").read_text())
        if version in {1, 2, 3, 4, 5, 6, 7, 8, 9}:
            self.db.executescript(Path(__file__).with_name("egress_schema.sql").read_text())
        if version in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}:
            self.db.executescript(Path(__file__).with_name("summary_schema.sql").read_text())
        if version in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}:
            self.db.executescript(Path(__file__).with_name("trust_schema.sql").read_text())
        if version in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}:
            self.db.executescript(Path(__file__).with_name("anchor_schema.sql").read_text())
        if version in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13}:
            self.db.executescript(Path(__file__).with_name("position_schema.sql").read_text())
        if version in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14}:
            self.db.executescript(Path(__file__).with_name("credential_schema.sql").read_text())
        if version in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}:
            self.db.executescript(Path(__file__).with_name("lexical_schema.sql").read_text())
        if version in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16}:
            self.db.executescript(Path(__file__).with_name("corrections_schema.sql").read_text())
        if version in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17}:
            if "on_behalf_of" not in {
                row[1] for row in self.db.execute("PRAGMA table_info(events)").fetchall()
            }:
                self.db.execute("ALTER TABLE events ADD COLUMN on_behalf_of TEXT")
            self.db.executescript(Path(__file__).with_name("operations_schema.sql").read_text())
        if version in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18}:
            self.db.executescript(Path(__file__).with_name("retention_schema.sql").read_text())
        if version in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19}:
            self.db.executescript(Path(__file__).with_name("consolidation_schema.sql").read_text())
        if version in set(range(1, 21)):
            self.db.executescript(Path(__file__).with_name("vectors_schema.sql").read_text())
        elif version != SCHEMA_VERSION:
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
                "INSERT INTO meta(key,value) VALUES (?,?)",
                [
                    ("store", store),
                    ("incarnation", incarnation),
                    ("journal", "0"),
                    ("commit_serial", "0"),
                ],
            )
            db.execute("INSERT INTO tenants(tenant) VALUES (?)", (tenant,))
            db.execute(
                "INSERT INTO grants(tenant,principal,project,capabilities,starts,expires) VALUES (?,?,?,?,?,?)",
                (tenant, principal, "*", json.dumps(sorted(CAPABILITIES)), None, None),
            )
            db.execute(
                "INSERT INTO projections(tenant,incarnation,channel,configuration,generation,frontier) VALUES (?,?,?,?,?,?)",
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
            db.executescript(Path(__file__).with_name("index_schema.sql").read_text())
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
                "operations_sequence": 0,
                "operations_head": "0" * 64,
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

    @classmethod
    def pending_journal(cls, root: Path, control: Path) -> dict[str, Any]:
        """Offline: journal records the database has not applied, without applying any.

        Takes the ownership locks, so it refuses while a daemon holds the store, and opens the
        database read-only. Each entry names the sequence, kind and tenant and the error from
        the last attempt to apply it; record content is never printed, and no record is
        skipped or altered. The journal and anchor are verified exactly as at open.
        """
        paths(root, control)
        private_dir(root)
        private_dir(control)
        owner = Ownership(root)
        try:
            control_owner = Ownership(control)
            try:
                controller = Control(control)
                anchor = controller.read_anchor()
                held = controller.verified_count(anchor)
                with sqlite3.connect(f"file:{root / 'store.sqlite'}?mode=ro", uri=True) as db:
                    applied = int(
                        db.execute("SELECT value FROM meta WHERE key='journal'").fetchone()[0]
                    )
                    row = db.execute("SELECT value FROM meta WHERE key='operations'").fetchone()
                    applied_operations = int(row[0]) if row is not None else 0
                if applied > held:
                    raise LocalError("journal_rollback")
                result = controller.describe_pending(
                    controller.records_after(anchor, applied), applied, held
                )
                held_operations = controller.operations.verified_count(anchor)
                if applied_operations > held_operations:
                    raise LocalError("journal_rollback")
                result["operations"] = controller.describe_pending(
                    controller.operations.records_after(anchor, applied_operations),
                    applied_operations,
                    held_operations,
                )
                return result
            finally:
                control_owner.close()
        finally:
            owner.close()

    @classmethod
    def rotate_owner(
        cls,
        root: Path,
        control: Path,
        *,
        clock: Clock | None = None,
        fault: Callable[[str], None] | None = None,
        instance_id: str | None = None,
    ) -> Path:
        """Offline: replace the owner credential with one of the next generation.

        The operator owns the store directories, so the daemon must be stopped (the
        ownership locks refuse otherwise). The rotation is the same journaled grant restatement
        that `credential issue --rotate` makes, so a restore replays it. The new credential is
        made durable as `owner.credential.next` before the generation commits, and is moved
        over `owner.credential` (an atomic rename) afterwards: a failure in between leaves the
        working credential in the `.next` file, and running the command again completes the
        rename instead of rotating a second time. The previous credential stops working at
        the commit; the new one is on disk from before it.
        """
        target, pending = control / "owner.credential", control / "owner.credential.next"
        with cls(root, control, clock=clock, fault=fault, instance_id=instance_id) as store:
            at = store.clock.utc_ms()
            tenant = principal = None
            try:
                private_file(target)
                identity = store.control.verify(target.read_text().strip())
                tenant, principal = identity["tenant"], identity["principal"]
            except (OSError, KeyError, LocalError):
                admin = recovery_admin(store.db, at)
                if admin is not None:
                    tenant, principal = admin
            if tenant is None or principal is None:
                raise LocalError("recovery_admin_missing")
            if pending.exists() or pending.is_symlink():
                private_file(pending)
                try:
                    body = store.control.verify(pending.read_text().strip())
                except LocalError:
                    body = {}
                if (
                    body.get("kind") == "credential"
                    and body.get("incarnation") == store.incarnation
                    and body.get("tenant") == tenant
                    and body.get("principal") == principal
                    and body.get("generation", 0) == store._generation(tenant, principal) > 0
                ):
                    # An earlier rotation committed but did not finish moving its file.
                    os.replace(pending, target)
                    sync_dir(control)
                    return target
                pending.unlink()
            with store.transaction() as decision:
                row = store.db.execute(
                    "SELECT capabilities,starts,expires FROM grants WHERE tenant=? AND principal=? AND project='*'",
                    (tenant, principal),
                ).fetchone()
                capabilities = json.loads(row["capabilities"]) if row is not None else []
                if (
                    row is None
                    or "admin" not in capabilities
                    or not temporal_valid(decision.evaluated_ms, row["starts"], row["expires"])
                ):
                    raise LocalError("administrative_operation_required")
                record = store._journal(
                    {
                        "kind": "grant",
                        "actor": "operator",
                        "tenant": tenant,
                        "principal": principal,
                        "project": "*",
                        "capabilities": capabilities,
                        "starts": row["starts"],
                        "expires": row["expires"],
                        "rotate": True,
                    }
                )
                store._apply(record, decision.evaluated_ms)
                token = store._token("credential", tenant=tenant, principal=principal)
                atomic(pending, token.encode())
            store._expose()
            os.replace(pending, target)
            sync_dir(control)
        return target

    def close(self) -> None:
        try:
            self.flush_anchor(force=True)
        except Exception:
            pass  # the store reopens against the last anchor, which never leads the database
        self.db.close()
        self.control_owner.close()
        self.owner.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[Decision]:
        spans = getattr(self.timings, "items", None)
        waited = time.monotonic() if spans is not None else 0.0
        with self.lock:
            acquired = time.monotonic() if spans is not None else 0.0
            try:
                yield from self._transaction()
            finally:
                if spans is not None:
                    spans.append(
                        {
                            "lock_wait_ms": (acquired - waited) * 1000,
                            "transaction_ms": (time.monotonic() - acquired) * 1000,
                        }
                    )

    def _transaction(self) -> Iterator[Decision]:
        self.owner.check()
        if not self.healthy:
            raise LocalError("recovery_required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            decision = self._now()
            before = self.db.total_changes
            self._hold_lease(decision.evaluated_ms)
            yield decision
            self.fault("before_commit")
            decision.check(self.clock)
            # A transaction that changed no row has nothing to order: the serial counts commits
            # that changed the database, so a status read or a refused request costs no fsync.
            if self.db.total_changes != before:
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
        except BaseException as exc:
            try:
                self.db.rollback()
                applied = int(
                    self.db.execute("SELECT value FROM meta WHERE key='journal'").fetchone()[0]
                )
                operations = self._meta_int("operations")
                if (
                    self.anchor["journal_sequence"] > applied
                    or self.anchor.get("operations_sequence", 0) > operations
                ):
                    self.healthy = False
                    if (
                        isinstance(exc, LocalError)
                        and exc.code == "release_expired"
                        and self.anchor.get("operations_sequence", 0) <= operations
                        and all(
                            record["action"]["kind"] != "operation"
                            for record in self.control.records_after(self.anchor, applied)
                        )
                    ):
                        # A policy record was journalled and only ran out of its request
                        # budget (a long purge): apply it now, without one, instead of staying
                        # fenced until a restart would do exactly that. The caller still gets
                        # the expiry. Operation records keep the fence: a tool effect whose
                        # acknowledgment was lost must be reconciled after a restart, and any
                        # other failure also leaves the fence in place.
                        self._replay_journal()
                        self.healthy = True
            except Exception:
                # Never let cleanup mask the original failure; fail closed instead.
                self.healthy = False
            raise

    def _expose(self) -> None:
        """Fsync outside-DB evidence before any acknowledgment/handle leaves the process."""
        with self.lock:
            self._expose_locked()

    def _expose_locked(self) -> None:
        """Make the anchor cover everything this process has committed before it acknowledges.

        The anchor refuses a database or journal rolled back behind an acknowledged write, so
        every transaction that committed a row (a capture, a policy change, and a receipt too)
        is anchored before its bytes are released, exactly as before. A request that committed
        nothing needs no anchor and costs no fsync, and writers whose commits precede one
        anchor write share it: that write is made under this lock, so it has finished before
        the next writer looks, and each acknowledges only after the anchor covering its commit
        is durable.

        `anchor_interval_ms` above zero is the operator's opt-in to less: the commit serial and
        the clock's high-water mark of a transaction that moved no position are then written at
        most that late (on `close` and from the daemon's maintenance pass too). The cost is
        stated in docs/deployment.md."""
        if not self.healthy:
            raise LocalError("recovery_required")
        try:
            frontiers, serial = self._anchor_state()
            now = self.clock.monotonic_ms()
            covered = frontiers == self.anchor["frontiers"]
            committed = serial != self.anchor["commit_serial"]
            interval = self.anchor_interval_ms
            if interval:
                dirty = committed or self.last_utc != self.anchor["last_utc"]
                due = dirty and (
                    now - self._anchor_at >= interval
                    or self.last_utc - self.anchor["last_utc"] >= interval
                )
            else:
                due = committed
            if covered and not due:
                return
            self._write_anchor(frontiers, serial, now)
        except BaseException:
            self.healthy = False
            raise

    def _anchor_state(self) -> tuple[dict[str, int], int]:
        frontiers = dict(self.db.execute("SELECT tenant,position FROM tenants"))
        serial = int(
            self.db.execute("SELECT value FROM meta WHERE key='commit_serial'").fetchone()[0]
        )
        return frontiers, serial

    def _write_anchor(self, frontiers: dict[str, int], serial: int, now: int) -> None:
        self.fault("before_anchor")
        anchor = {
            **self.anchor,
            "frontiers": frontiers,
            "last_utc": self.last_utc,
            "commit_serial": serial,
        }
        self.control.anchor(anchor)
        self.anchor, self._anchor_at = anchor, now
        self.fault("after_anchor")

    def flush_anchor(self, *, force: bool = False) -> None:
        """Write what `_expose` left for later, when it is due (or at once with `force`)."""
        with self.lock:
            if not self.healthy:
                return
            if not force:
                self._expose_locked()
                return
            try:
                # Only ever the history this store opened: a control tree that was restored or
                # replaced underneath it is left exactly as the operation made it.
                disk = self.control.read_anchor()
                if disk["state"] != "active" or disk["incarnation"] != self.incarnation:
                    return
                frontiers, serial = self._anchor_state()
                if (
                    frontiers != self.anchor["frontiers"]
                    or serial != self.anchor["commit_serial"]
                    or self.last_utc != self.anchor["last_utc"]
                ):
                    self._write_anchor(frontiers, serial, self.clock.monotonic_ms())
            except BaseException:
                self.healthy = False
                raise

    def _replay_journal(self) -> None:
        applied = int(self.db.execute("SELECT value FROM meta WHERE key='journal'").fetchone()[0])
        held = self.control.verified_count(self.anchor)
        # Journal first, database second, anchor third: the database can never have applied
        # more records than the verified journal holds. A larger position means the journal
        # tail was lost or this database was paired with another control tree; replay would
        # skip those records silently, so the store stays closed.
        if applied > held:
            raise LocalError("journal_rollback")
        # Each record commits on its own, with no request budget, so a record that takes
        # long (a purge) can finish, and progress survives an interruption or a later failure.
        # A record that fails is noted for the operator and raised; none is ever skipped.
        for record in self.control.records_after(self.anchor, applied):
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._apply(record, self.clock.utc_ms())
                self.db.commit()
            except BaseException as exc:
                self.db.rollback()
                self.control.note_apply_failure(record, exc)
                raise
            if self.anchor["journal_sequence"] < record["sequence"]:
                self.anchor["journal_sequence"] = record["sequence"]
                self.anchor["journal_head"] = hashlib.sha256(
                    self.control.sign(record).encode()
                ).hexdigest()
                self.control.anchor(self.anchor)
            self.control.clear_apply_failure(record["sequence"])
        self._replay_operations()
        if self.anchor["journal_sequence"] < held:
            # An append that crashed before its anchor write: the records verified, so the
            # anchor is moved over them now, as a replay always did.
            self.anchor["journal_sequence"] = held
            self.anchor["journal_head"] = self.control.head(held)
            self.control.anchor(self.anchor)

    def readiness(self) -> dict[str, bool]:
        """The checks behind `/readyz`: the database answers, the control tree is readable and
        the active anchor belongs to this store, and the schema version is the one served."""
        checks = {"database": False, "control": False, "schema": False}
        if self.lock.acquire(timeout=1):
            try:
                self.db.execute("SELECT 1").fetchone()
                checks["database"] = self.healthy
                checks["schema"] = (
                    self.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
                )
            except Exception:
                pass
            finally:
                self.lock.release()
        try:
            anchor = self.control.read_anchor()
            checks["control"] = anchor["state"] == "active" and anchor["store"] == self.store_id
        except Exception:
            pass
        return checks

    def extra_gauges(self) -> list[Any]:
        """Gauges the metrics endpoint adds to the status figures: journal size, backup age,
        readiness and the daemon's concurrency limits."""
        from memoryking.health import CHECKS

        try:
            journal = (self.control_path / "journal").stat().st_size
        except OSError:
            journal = 0
        try:
            age = max(
                0.0, time.time() - int((self.control_path / "last-backup").read_text()) / 1000
            )
        except (OSError, ValueError):
            age = -1.0
        ready = self.readiness()
        gauges: list[Any] = [
            ("memoryking_journal_bytes", "Size of the control journal.", [({}, journal)]),
            (
                "memoryking_backup_age_seconds",
                "Seconds since the last completed backup; -1 when there is none.",
                [({}, age)],
            ),
            (
                "memoryking_ready",
                "1 when a readiness check passes.",
                [({"check": name}, float(ready.get(name, False))) for name in CHECKS],
            ),
        ]
        if self.concurrency_gauge is not None:
            gauges.append(
                (
                    "memoryking_concurrency",
                    "Configured limit and current use of each bounded concurrency resource.",
                    [
                        ({"resource": name, "kind": kind}, value)
                        for name, (limit, used) in sorted(self.concurrency_gauge().items())
                        for kind, value in (("limit", limit), ("in_use", used))
                    ],
                )
            )
        return gauges

    def _gc_pending(self) -> bool:
        row = self.db.execute("SELECT value FROM meta WHERE key='gc_pending'").fetchone()
        return row is not None and row[0] == "1"

    def _mark_gc_pending(self) -> None:
        if not isinstance(self.db, sqlite3.Connection):
            return  # the shared restore replays through this class on PostgreSQL
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES ('gc_pending','1')"
            " ON CONFLICT(key) DO UPDATE SET value='1'"
        )

    def _gc_settled(self) -> None:
        if not isinstance(self.db, sqlite3.Connection):
            return
        with self.lock:
            if self.healthy and not self.db.in_transaction:
                self.db.execute("UPDATE meta SET value='0' WHERE key='gc_pending'")

    def _replay_operations(self) -> None:
        """Apply operations-log records the database has not applied, as the journal's are."""
        log = self.control.operations
        applied = self._meta_int("operations")
        held = log.verified_count(self.anchor)
        if applied > held:
            raise LocalError("journal_rollback")
        for record in log.records_after(self.anchor, applied):
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._apply(record, self.clock.utc_ms())
                self.db.commit()
            except BaseException as exc:
                self.db.rollback()
                self.control.note_apply_failure(record, exc)
                raise
            if self.anchor.get("operations_sequence", 0) < record["sequence"]:
                self.anchor["operations_sequence"] = record["sequence"]
                self.anchor["operations_head"] = log.head(record["sequence"])
                self.control.anchor(self.anchor)
        if self.anchor.get("operations_sequence", 0) < held:
            # An append that crashed before its anchor write: the records verified.
            self.anchor["operations_sequence"] = held
            self.anchor["operations_head"] = log.head(held)
            self.control.anchor(self.anchor)

    def gc(self) -> int:
        """The explicit full sweep: delete every blob file no observation names (in either
        layout). Purge deletes its own file directly; this finds what a crash left behind."""
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
                touched = {directory}
                for name, path in blobfiles.iterate(directory):
                    private_file(path)
                    if (directory.name, name) not in live:
                        path.unlink()
                        touched.add(path.parent)
                        removed += 1
                blobfiles.sync_directories(touched)
            if not self.db.in_transaction:
                self.db.execute("UPDATE meta SET value='0' WHERE key='gc_pending'")
            return removed

    def _previous_backup(self, previous: Path | None) -> tuple[Path, set[str]] | None:
        """The blobs a verified earlier backup holds, or None when it cannot be trusted.

        The earlier backup must carry a manifest signed by this store and a blob index that
        matches the hash the manifest commits to; anything else means a full copy."""
        if previous is None:
            return None
        try:
            manifest = self.control.verify_document(previous / "manifest")
            if manifest.get("store") != self.store_id or "blob_index" not in manifest:
                return None
            entries = read_blob_index(previous, manifest["blob_index"])
            return previous, {e for e in entries if blobfiles.valid_relative(e)}
        except (OSError, LocalError):
            return None

    def backup(self, destination: Path, previous: Path | None = None) -> dict[str, Any]:
        """Offline administrative method; intentionally absent from the agent socket.

        The store lock is held only to snapshot the database (SQLite's online backup) and to
        list the live blobs. Blob files are immutable and named by their hash, so they are
        copied afterwards without the lock, each hashed while streamed and fsynced once.
        With `previous` (an earlier backup of this store), a blob that backup holds is
        hard-linked after its copy hashes correctly, and only new blobs are copied; every
        backup stays complete and restorable on its own."""
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
            live = [
                (row[0], row[1])
                for row in self.db.execute(
                    "SELECT DISTINCT tenant,blob FROM observations WHERE blob IS NOT NULL"
                )
            ]
            frontiers = dict(self.db.execute("SELECT tenant,position FROM tenants"))
            store_id, incarnation = self.store_id, self.incarnation
            operations_sequence = self._meta_int("operations")
        # The lock is released: requests run while the files are copied.
        with (destination / "store.sqlite").open("rb") as stream:
            durable_sync(stream.fileno())
        database_sha256 = blobfiles.sha256_file(destination / "store.sqlite")
        earlier = self._previous_backup(previous)
        private_dir(destination / "blobs", create=True)
        entries: list[str] = []
        linked = copied = total = 0
        touched: set[Path] = {destination / "blobs"}
        for tenant, digest in live:
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise LocalError("blob_corrupt")
            relative = blobfiles.relative(tenant, digest)
            output = destination / relative
            if output.parent not in touched:
                private_dir(output.parent.parent, create=True)
                private_dir(output.parent, create=True)
                touched.update({output.parent, output.parent.parent})
            source = blobfiles.blob_path(self.root, tenant, digest)
            try:
                how, size = self._place_blob(earlier, tenant, digest, source, output)
            except FileNotFoundError:
                # A purge deleted the file after the snapshot. The snapshot still lists it, and
                # a restore replays the purge from the control journal, so it is left out;
                # a file that is missing while an observation still names it is corruption.
                with self.lock:
                    named = self.db.execute(
                        "SELECT 1 FROM observations WHERE tenant=? AND blob=? LIMIT 1",
                        (tenant, digest),
                    ).fetchone()
                if named is not None:
                    raise LocalError("blob_corrupt") from None
                continue
            linked += how == "linked"
            copied += how == "copied"
            total += size
            entries.append(relative)
        blobfiles.sync_directories(touched)
        manifest = {
            "schema": 14,
            "store": store_id,
            "incarnation": incarnation,
            # How far the operations log was applied when this backup was taken; compaction
            # never drops records above the oldest retained backup's position.
            "operations_sequence": operations_sequence,
            "frontiers": frontiers,
            "database_sha256": database_sha256,
            "blob_index": write_blob_index(destination, entries),
            "blobs_linked": linked,
            "blobs_copied": copied,
            "archive_bytes": total,
        }
        atomic(destination / "manifest", self.control.sign(manifest).encode())
        sync_dir(destination)
        sync_dir(destination.parent)
        atomic(self.control_path / "last-backup", str(self.clock.utc_ms()).encode())
        return manifest

    def _place_blob(
        self,
        earlier: tuple[Path, set[str]] | None,
        tenant: str,
        digest: str,
        source: Path,
        output: Path,
    ) -> tuple[str, int]:
        if earlier is not None:
            root, held = earlier
            for relative in (
                blobfiles.relative(tenant, digest),
                f"blobs/{tenant}/{digest}",
            ):
                if relative in held:
                    return blobfiles.link_or_copy(root / relative, source, output, digest)
        return "copied", blobfiles.copy_verified(source, output, digest)

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
                # The operations log has no separate checkpoint: its own verified chain stands.
                state = controller.operations.sync(anchor, trust_anchor=True)
                anchor.update(operations_sequence=state.count, operations_head=state.head)
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
            controller.operations.sync(anchor)
            manifest = controller.verify_document(backup / "manifest")
            if (
                manifest.get("schema") not in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14}
                or manifest.get("store") != controller.store
            ):
                raise LocalError("backup_invalid")
            # A backup older than what compaction kept cannot find its operation intents.
            if manifest.get("operations_sequence", 0) < controller.operations.compacted_through():
                raise LocalError("backup_invalid")
            if blobfiles.sha256_file(backup / "store.sqlite") != manifest["database_sha256"]:
                raise LocalError("backup_invalid")
            # Backups before schema 9 embed the list; later ones commit to a separate index.
            blobs = (
                read_blob_index(backup, manifest["blob_index"])
                if "blob_index" in manifest
                else manifest["blobs"]
            )
            # Either layout is accepted: backups before sharding recorded `blobs/<tenant>/<digest>`.
            for relative in blobs:
                if not blobfiles.valid_relative(relative):
                    raise LocalError("backup_invalid")
                if blobfiles.sha256_file(backup / relative) != blobfiles.digest_of(relative):
                    raise LocalError("backup_invalid")
            anchor["state"] = "restoring"
            anchor["reconciliation_required"] = True
            anchor["history_loss"] = lost_anchor_journal_head is not None
            controller.anchor(anchor)
            hook("restore_fenced")
            for suffix in ("-wal", "-shm"):
                (root / ("store.sqlite" + suffix)).unlink(missing_ok=True)
            with (backup / "store.sqlite").open("rb") as stream:
                blobfiles.write_durable(root / "store.sqlite", stream, manifest["database_sha256"])
            sync_dir(root)
            touched: set[Path] = set()
            for relative in blobs:
                tenant_name, digest = relative.split("/")[1], blobfiles.digest_of(relative)
                tenant_directory = root / "blobs" / tenant_name
                private_dir(tenant_directory, create=True)
                shard = blobfiles.shard_directory(tenant_directory, digest)
                with (backup / relative).open("rb") as stream:
                    blobfiles.write_durable(shard / digest, stream, digest)
                # A flat copy of the same blob (the layout before sharding) is now redundant.
                (tenant_directory / digest).unlink(missing_ok=True)
                touched.update({shard, tenant_directory})
            blobfiles.sync_directories(touched)
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
                recovery.db.execute("DELETE FROM instance_lease")
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
                # Only the independent OS owner can recover administrative credentials,
                # and only a grant that currently carries admin rights may receive one.
                admin = recovery_admin(recovery.db, recovery.clock.utc_ms())
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

    def _retire_optional(self, tenant: str | None = None) -> None:
        """A restored history cannot reuse projected vectors, leases or adapter bases.

        A local restore replaces the whole store's history, so it passes no tenant and the
        statements cover every tenant; the shared restore names its tenant in each one.
        """
        scope, bound = (" WHERE tenant=?", (tenant,)) if tenant else ("", ())
        self.db.execute(
            "UPDATE ranking_state SET configuration=NULL,version=version+1" + scope, bound
        )
        self.db.execute("DELETE FROM adapter_bases" + scope, bound)
        self.db.execute("DELETE FROM embedding_items" + scope, bound)
        self.db.execute("DELETE FROM embedding_vectors" + scope, bound)
        self.db.execute("DELETE FROM embedding_skips" + scope, bound)
        self.db.execute("DELETE FROM embedding_jobs" + scope, bound)
        self.db.execute(
            "UPDATE embedding_partitions SET incarnation=?,state='disabled',generation=1,scheduled=0,frontier=0"
            + scope,
            (self.incarnation, *bound),
        )
