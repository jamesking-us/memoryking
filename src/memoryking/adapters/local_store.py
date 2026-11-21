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


SCHEMA_VERSION = 16  # the SQLite user_version the daemon migrates every store to


class LocalStore(StoreCore):
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
        self.dialect = SQLiteDialect()
        # Per-thread transaction timings, collected only while a traced request runs.
        self.timings = threading.local()
        self.healthy = True
        self.retrieval_cache: dict[str, Any] = {}
        self._last_housekeeping = 0
        self.counters = Metrics()
        # A loaded local embedding model lets briefings embed their own queries.
        self.embedder: Any | None = None
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
                if applied > held:
                    raise LocalError("journal_rollback")
                return controller.describe_pending(
                    controller.records_after(anchor, applied), applied, held
                )
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
        with cls(root, control, clock=clock, fault=fault) as store:
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
            yield decision
            self.fault("before_commit")
            decision.check(self.clock)
            if (
                int(
                    self.db.execute("SELECT value FROM meta WHERE key='commit_serial'").fetchone()[
                        0
                    ]
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
                if self.anchor["journal_sequence"] > applied:
                    self.healthy = False
                    if (
                        isinstance(exc, LocalError)
                        and exc.code == "release_expired"
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
        if self.anchor["journal_sequence"] < held:
            # An append that crashed before its anchor write: the records verified, so the
            # anchor is moved over them now, as a replay always did.
            self.anchor["journal_sequence"] = held
            self.anchor["journal_head"] = self.control.head(held)
            self.control.anchor(self.anchor)

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
                durable_sync(stream.fileno())
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
                "schema": 11,
                "store": self.store_id,
                "incarnation": self.incarnation,
                "frontiers": dict(self.db.execute("SELECT tenant,position FROM tenants")),
                "database_sha256": hashlib.sha256(
                    (destination / "store.sqlite").read_bytes()
                ).hexdigest(),
                "blob_index": write_blob_index(destination, blobs),
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
                manifest.get("schema") not in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}
                or manifest.get("store") != controller.store
            ):
                raise LocalError("backup_invalid")
            data = (backup / "store.sqlite").read_bytes()
            if hashlib.sha256(data).hexdigest() != manifest["database_sha256"]:
                raise LocalError("backup_invalid")
            # Backups before schema 9 embed the list; later ones commit to a separate index.
            blobs = (
                read_blob_index(backup, manifest["blob_index"])
                if "blob_index" in manifest
                else manifest["blobs"]
            )
            for relative in blobs:
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
            for relative in blobs:
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
        self.db.execute("DELETE FROM embedding_jobs" + scope, bound)
        self.db.execute(
            "UPDATE embedding_partitions SET incarnation=?,state='disabled',generation=1,scheduled=0,frontier=0"
            + scope,
            (self.incarnation, *bound),
        )
