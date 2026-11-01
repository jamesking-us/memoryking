"""Explicit single-tenant native PostgreSQL backup/restore with independent fencing.

Backups stream native relational rows into a signed, checksummed portable archive.
Maintenance fences new requests before long copying; no five-second release gate
is held across backup I/O. Control identity/journal/anchor are never backed up here.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
from pathlib import Path
from typing import Any, Callable

from psycopg import sql

from memoryking.adapters.local_files import Ownership, atomic, private_dir, private_file, sync_dir
from memoryking.adapters.local_store import LocalStore, identifier, recovery_admin, uid
from memoryking.adapters.postgres_admin import Roles
from memoryking.adapters.postgres_sql import PostgresSQL, record_factory
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.shared_control import SharedControl
from memoryking.contracts.canonical import canonical_bytes, load_strict
from memoryking.domain.local import Clock, LocalError, SystemClock, temporal_valid

# Preserve the revision-1 archive inventory for supported historical restores.
TABLES_V1 = (
    "tenants",
    "grants",
    "events",
    "observations",
    "lexical",
    "retries",
    "jobs",
    "projections",
    "projection_items",
    "receipts",
    "retired_sources",
    "knowledge_state",
    "families",
    "revisions",
    "evidence_spans",
    "lifecycle",
    "replacements",
    "conflicts",
    "attestations",
    "rendered_artifacts",
    "checkpoint_streams",
    "checkpoints",
    "operation_intents",
    "handoffs",
    "handoff_views",
    "repositories",
    "repository_clones",
    "repository_snapshots",
    "code_policies",
    "code_validations",
    "repository_jobs",
    "tenant_gates",
    "tenant_meta",
)
TABLES_V2 = TABLES_V1 + (
    "embedding_partitions",
    "embedding_jobs",
    "embedding_items",
    "processing_usage",
    "adapter_bases",
)
TABLES_V3 = TABLES_V2 + ("experience_records", "ranking_state", "ranking_history")
# Schema 4 archives add the egress audit and session summaries; schema 3 archives
# written before those tables existed stay restorable.
TABLES = TABLES_V3 + ("egress_audit", "session_summaries")
ROW_LIMIT = 1048576
ARCHIVE_LIMIT = 16 * 1024 * 1024 * 1024


def encode(value: Any) -> Any:
    return (
        {"bytea": base64.b64encode(bytes(value)).decode()}
        if isinstance(value, (bytes, memoryview))
        else value
    )


def decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) != {"bytea"} or not isinstance(value["bytea"], str):
            raise LocalError("backup_invalid")
        return base64.b64decode(value["bytea"], validate=True)
    if isinstance(value, (list, float, bool)):
        raise LocalError("backup_invalid")
    return value


def digest_file(path: Path) -> str:
    private_file(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1048576):
            digest.update(block)
    return digest.hexdigest()


def columns(connection: Any, table: str) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='memoryking' "
            "AND table_name=%s ORDER BY ordinal_position",
            (table,),
        )
    ]


def settings(connection: Any, tenant: str) -> None:
    connection.execute("SET LOCAL search_path=memoryking,pg_catalog")
    connection.execute("SELECT set_config('memoryking.tenant',%s,true)", (tenant,))
    connection.execute("SET LOCAL lock_timeout='5s'")


def backup(
    store: PostgresStore, backup_connection: Any, destination: Path, *, roles: Roles | None = None
) -> dict[str, Any]:
    roles = roles or Roles()
    if destination.exists():
        raise LocalError("backup_requires_new_directory")
    if backup_connection.execute("SELECT current_user").fetchone()[0] != roles.backup:
        raise LocalError("backup_role_required")
    identifier(store.tenant)
    owner = Ownership(store.control_path)
    operation = secrets.token_hex(16)
    fenced = False
    try:
        with store.transaction() as decision:
            store._auth(store.credential, "admin", "*", decision.evaluated_ms)
            with store.control.locked():
                anchor = store.control.read_anchor()
                anchor.update(state="maintenance", maintenance_operation=operation)
                store.control.anchor(anchor)
                fenced = True
        private_dir(destination, create=True)
        manifest: dict[str, Any] = {
            "format": "memoryking-postgres-1",
            "schema": 4,
            "store": store.store_id,
            "tenant": store.tenant,
            "incarnation": store.incarnation,
            "tables": {},
            "blobs": [],
        }
        total = 0
        with backup_connection.transaction():
            backup_connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            settings(backup_connection, store.tenant)
            for table in TABLES:
                names = columns(backup_connection, table)
                if not names:
                    raise LocalError("schema_unsupported")
                path = destination / (table + ".ndjson")
                count = 0
                with path.open("xb") as out:
                    path.chmod(0o600)
                    with backup_connection.cursor(name="archive_" + table) as cursor:
                        cursor.execute(
                            sql.SQL("SELECT {} FROM {} WHERE tenant=%s").format(
                                sql.SQL(",").join(map(sql.Identifier, names)), sql.Identifier(table)
                            ),
                            (store.tenant,),
                        )
                        for row in cursor:
                            line = canonical_bytes([encode(value) for value in row]) + b"\n"
                            if len(line) > ROW_LIMIT:
                                raise LocalError("backup_row_limit")
                            total += len(line)
                            if total > ARCHIVE_LIMIT:
                                raise LocalError("backup_archive_limit")
                            out.write(line)
                            count += 1
                    out.flush()
                    import os

                    os.fsync(out.fileno())
                manifest["tables"][table] = {
                    "columns": names,
                    "rows": count,
                    "sha256": digest_file(path),
                }
            digests = backup_connection.execute(
                "SELECT DISTINCT blob FROM observations WHERE tenant=%s AND blob IS NOT NULL",
                (store.tenant,),
            ).fetchall()
            private_dir(destination / "blobs", create=True)
            for (digest,) in digests:
                if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    raise LocalError("blob_corrupt")
                source = store.root / "blobs" / store.tenant / digest
                if digest_file(source) != digest:
                    raise LocalError("blob_corrupt")
                raw = source.read_bytes()
                total += len(raw)
                if total > ARCHIVE_LIMIT:
                    raise LocalError("backup_archive_limit")
                atomic(destination / "blobs" / digest, raw)
                manifest["blobs"].append(digest)
        manifest["blobs"].sort()
        manifest["archive_bytes"] = total
        atomic(destination / "manifest", store.control.sign(manifest).encode())
        sync_dir(destination)
        return manifest
    finally:
        if fenced:
            with store.control.locked():
                anchor = store.control.read_anchor()
                if (
                    anchor.get("maintenance_operation") == operation
                    and anchor["state"] == "maintenance"
                ):
                    anchor["state"] = "active"
                    anchor.pop("maintenance_operation")
                    store.control.anchor(anchor)
        owner.close()


def restore(
    connection: Any,
    root: Path,
    control: Path,
    tenant: str,
    archive: Path,
    *,
    audience: str = "memoryking-shared",
    roles: Roles | None = None,
    clock: Clock | None = None,
    fault: Callable[[str], None] | None = None,
    lost_anchor_journal_head: str | None = None,
) -> Path:
    """Offline operator action, retaining current independent policy/operation journal.

    An interruption leaves the independent fence in place. Re-run explicit restore
    against the verified archive; old watermarks, credentials and retries retire.
    """
    roles = roles or Roles()
    identifier(tenant)
    identifier(audience)
    for directory in (root, control, control / tenant, archive):
        private_dir(directory)
    if connection.execute("SELECT current_user").fetchone()[0] != roles.migration:
        raise LocalError("migration_role_required")
    if connection.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
        raise LocalError("primary_required")
    for setting in ("fsync", "synchronous_commit", "full_page_writes"):
        if connection.execute(f"SHOW {setting}").fetchone()[0] != "on":
            raise LocalError("database_durability_required")
    owner = Ownership(control / tenant)
    controller = SharedControl(control / tenant)
    clock = clock or SystemClock()
    hook = fault or (lambda _: None)
    try:
        with controller.locked():
            if lost_anchor_journal_head is not None:
                if (control / tenant / "anchor").exists() or not re.fullmatch(
                    r"[a-f0-9]{64}", lost_anchor_journal_head
                ):
                    raise LocalError("recovery_checkpoint_invalid")
                anchor: dict[str, Any] = {
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
            records = controller.journal(anchor)
        private_file(archive / "manifest")
        manifest = controller.verify_document(archive / "manifest")
        archive_tables = {1: TABLES_V1, 2: TABLES_V2, 3: TABLES_V3, 4: TABLES}.get(
            manifest.get("schema", 0), ()
        )
        if (
            manifest.get("format") != "memoryking-postgres-1"
            or manifest.get("schema") not in {1, 2, 3, 4}
            or manifest.get("store") != controller.store
            or manifest.get("tenant") != tenant
            or set(manifest.get("tables", {})) != set(archive_tables)
        ):
            raise LocalError("backup_invalid")
        if clock.utc_ms() < anchor["last_utc"]:
            raise LocalError("clock_untrusted")
        for table in archive_tables:
            if digest_file(archive / (table + ".ndjson")) != manifest["tables"][table]["sha256"]:
                raise LocalError("backup_invalid")
            if columns(connection, table) != manifest["tables"][table]["columns"]:
                raise LocalError("schema_unsupported")
        for digest in manifest["blobs"]:
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise LocalError("backup_invalid")
            if digest_file(archive / "blobs" / digest) != digest:
                raise LocalError("backup_invalid")
        with connection.transaction():
            settings(connection, tenant)
            connection.execute(
                "SELECT tenant FROM tenant_gates WHERE tenant=%s FOR UPDATE", (tenant,)
            )
            with controller.locked():
                if lost_anchor_journal_head is None:
                    anchor = controller.read_anchor()
                elif (control / tenant / "anchor").exists():
                    raise LocalError("recovery_checkpoint_invalid")
                records = controller.journal(anchor)
                anchor.update(
                    state="restoring",
                    reconciliation_required=True,
                    history_loss=lost_anchor_journal_head is not None,
                )
                controller.anchor(anchor)
            hook("restore_fenced")
            directory = root / "blobs" / tenant
            private_dir(directory, create=True)
            for digest in manifest["blobs"]:
                atomic(directory / digest, (archive / "blobs" / digest).read_bytes())
            for table in reversed(TABLES):
                if table not in {"tenants", "tenant_gates"}:
                    connection.execute(
                        sql.SQL("DELETE FROM {} WHERE tenant=%s").format(sql.Identifier(table)),
                        (tenant,),
                    )
            for table in archive_tables:
                names = manifest["tables"][table]["columns"]
                count = 0
                with (archive / (table + ".ndjson")).open("rb") as stream:
                    while line := stream.readline(ROW_LIMIT + 1):
                        if len(line) > ROW_LIMIT or not line.endswith(b"\n"):
                            raise LocalError("backup_invalid")
                        row = load_strict(line)
                        if (
                            not isinstance(row, list)
                            or len(row) != len(names)
                            or row[names.index("tenant")] != tenant
                        ):
                            raise LocalError("backup_invalid")
                        values = [decode(value) for value in row]
                        if table == "tenant_gates":
                            count += 1
                            continue
                        if table == "tenants":
                            connection.execute(
                                "UPDATE tenants SET position=%s,policy_epoch=%s,purge_epoch=%s WHERE tenant=%s",
                                (*values[1:], tenant),
                            )
                        else:
                            connection.execute(
                                sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                                    sql.Identifier(table),
                                    sql.SQL(",").join(map(sql.Identifier, names)),
                                    sql.SQL(",").join(sql.Placeholder() for _ in names),
                                ),
                                values,
                            )
                        count += 1
                if count != manifest["tables"][table]["rows"] or (
                    table in {"tenants", "tenant_gates"} and count != 1
                ):
                    raise LocalError("backup_invalid")
            # Reuse reviewed journal reconciliation, under an explicit native DB gate.
            recovery = LocalStore.__new__(LocalStore)
            recovery.root, recovery.control_path = root, control / tenant
            recovery.control, recovery.anchor = controller, anchor
            recovery.clock, recovery.lock = clock, threading.RLock()
            recovery.healthy = True
            recovery.store_id, recovery.incarnation = controller.store, manifest["incarnation"]
            recovery.last_utc = max(anchor["last_utc"], clock.utc_ms())
            connection.row_factory = record_factory
            recovery.db = PostgresSQL(connection)
            for record in records:
                recovery._apply(record, clock.utc_ms())
            recovery.incarnation = uid("hist")
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
            recovery._reindex(tenant)
            live = {
                row[0]
                for row in recovery.db.execute(
                    "SELECT blob FROM observations WHERE blob IS NOT NULL"
                )
            }
            admin = recovery_admin(recovery.db, clock.utc_ms())
            if admin is None:
                raise LocalError("recovery_admin_missing")
            position = recovery.db.execute("SELECT position FROM tenants").fetchone()[0]
            serial = int(
                recovery.db.execute("SELECT value FROM meta WHERE key='commit_serial'").fetchone()[
                    0
                ]
            )
        hook("restore_before_activation")
        credential = controller.sign(
            {
                "kind": "credential",
                "store": controller.store,
                "incarnation": recovery.incarnation,
                "tenant": tenant,
                "principal": admin[1],
                "audience": audience,
            }
        )
        credential_path = control / tenant / "owner.credential"
        atomic(credential_path, credential.encode())
        with controller.locked():
            anchor.update(
                state="active",
                incarnation=recovery.incarnation,
                frontiers={tenant: position},
                commit_serial=serial,
                last_utc=max(anchor["last_utc"], recovery.last_utc, clock.utc_ms()),
            )
            if records:
                anchor.update(
                    journal_sequence=records[-1]["sequence"],
                    journal_head=hashlib.sha256(controller.sign(records[-1]).encode()).hexdigest(),
                )
            anchor.pop("maintenance_operation", None)
            controller.anchor(anchor)
        # Orphan blobs go only after the database and anchor are committed, so an
        # interrupted restore never deletes content a retry would still need.
        for path in directory.iterdir():
            private_file(path)
            if path.name not in live:
                path.unlink()
        sync_dir(directory)
        return credential_path
    finally:
        owner.close()


def end_maintenance(
    connection: Any,
    control: Path,
    tenant: str,
    operation: str,
    credential: str,
    *,
    audience: str = "memoryking-shared",
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Clear a maintenance fence left by a backup that died before its own cleanup.

    The caller must present a current admin credential for the tenant and the exact
    operation identifier recorded in the anchor; any other fence stays closed.
    """
    identifier(tenant)
    if not re.fullmatch(r"[a-f0-9]{32}", operation):
        raise LocalError("invalid_request")
    private_dir(control / tenant)
    controller = SharedControl(control / tenant)
    identity = controller.verify(credential)
    if (
        identity.get("kind") != "credential"
        or identity.get("store") != controller.store
        or identity.get("tenant") != tenant
        or identity.get("audience") != audience
    ):
        raise LocalError("denied")
    principal = identifier(identity["principal"])
    at = (clock or SystemClock()).utc_ms()
    with connection.transaction():
        settings(connection, tenant)
        row = connection.execute(
            "SELECT capabilities,starts,expires FROM grants WHERE tenant=%s AND principal=%s AND project='*'",
            (tenant, principal),
        ).fetchone()
    capabilities = row[0] if row is not None else []
    if isinstance(capabilities, str):
        capabilities = load_strict(capabilities.encode())
    if row is None or "admin" not in capabilities or not temporal_valid(at, row[1], row[2]):
        raise LocalError("denied")
    with controller.locked():
        anchor = controller.read_anchor()
        if identity.get("incarnation") != anchor["incarnation"]:
            raise LocalError("history_changed")
        if anchor["state"] != "maintenance" or anchor.get("maintenance_operation") != operation:
            raise LocalError("maintenance_operation_mismatch")
        anchor["state"] = "active"
        anchor.pop("maintenance_operation")
        controller.anchor(anchor)
    return {"status": "active", "tenant": tenant, "operation": operation}
