"""Explicit single-tenant native PostgreSQL backup/restore with independent fencing.

Backups stream native relational rows into a signed, checksummed portable archive.
The tenant is fenced only while the database snapshot is established (the exclusive gate
means no write is in flight, and the repeatable-read transaction sees everything committed
before it). Rows are then dumped and blob files copied from that snapshot while requests run:
blobs are immutable and named by their hash, so no fence is needed to copy them. With
`previous` (an earlier archive of this tenant), a blob that archive holds is hard-linked
after its copy hashes correctly. Control identity/journal/anchor are never backed up here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable

from psycopg import sql

from memoryking.adapters import blobs as blobfiles
from memoryking.adapters.local_files import (
    Ownership,
    atomic,
    durable_sync,
    private_dir,
    private_file,
    read_blob_index,
    sync_dir,
    write_blob_index,
)
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
TABLES_V4 = TABLES_V3 + ("egress_audit", "session_summaries")
# Schema 5 archives add source trust rules and observation holds.
TABLES_V5 = TABLES_V4 + ("source_trust", "observation_holds")
# Schema 6 archives add entity anchors.
TABLES_V6 = TABLES_V5 + ("anchors",)
# Schema 8 archives add credential generations, so a restore knows which credentials were replaced.
TABLES_V8 = TABLES_V6 + ("credential_generations",)
# Schema 9 archives add the lexical statistics and the state of the lexical index.
TABLES_V9 = TABLES_V8 + ("lexical_state", "lexical_projects", "lexical_terms")
# Schema 10 archives add supersession links, retirement requests and pins.
TABLES_V10 = TABLES_V9 + ("supersessions", "retirement_requests", "pins")
# Schema 12 archives add the consolidation runs and the unreviewed-tier policy (schema 11
# archives hold the same tables as schema 10; they added the operations log's own files).
TABLES = TABLES_V10 + ("consolidation_runs", "unreviewed_tier")
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


def pending_journal(pool: Any, control: Path, tenant: str) -> dict[str, Any]:
    """Journal records the tenant's database has not applied, without applying any.

    Names the sequence, kind and tenant of each and the error from the last attempt to apply
    it (recorded beside the journal), never record content. The journal is verified against
    the anchor exactly as a request would, and nothing is skipped, altered or replayed.
    """
    from memoryking.adapters.postgres_pool import acquire

    identifier(tenant)
    private_dir(control / tenant)
    controller = SharedControl(control / tenant)
    connection = acquire(pool, timeout=5)
    try:
        with connection.transaction():
            settings(connection, tenant)
            row = connection.execute("SELECT value FROM meta WHERE key='journal'").fetchone()
            operations = connection.execute(
                "SELECT value FROM meta WHERE key='operations'"
            ).fetchone()
        applied = int(row[0]) if row is not None else 0
        applied_operations = int(operations[0]) if operations is not None else 0
    finally:
        pool.putconn(connection)
    with controller.locked():
        anchor = controller.read_anchor()
        held = controller.verified_count(anchor)
        held_operations = controller.operations.verified_count(anchor)
        if applied > held or applied_operations > held_operations:
            raise LocalError("journal_rollback")
        records = controller.records_after(anchor, applied)
        pending = controller.operations.records_after(anchor, applied_operations)
    result = controller.describe_pending(records, applied, held)
    result["operations"] = controller.describe_pending(pending, applied_operations, held_operations)
    return result


def backup(
    store: PostgresStore,
    backup_connection: Any,
    destination: Path,
    *,
    roles: Roles | None = None,
    previous: Path | None = None,
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
    snapshot = ExitStack()
    try:
        with store.transaction() as decision:
            # The exclusive gate: no write is in flight, and none starts until it is released.
            store._auth(store.credential, "admin", "*", decision.evaluated_ms)
            with store.control.locked():
                anchor = store.control.read_anchor()
                anchor.update(state="maintenance", maintenance_operation=operation)
                store.control.anchor(anchor)
                fenced = True
            private_dir(destination, create=True)
            snapshot.enter_context(backup_connection.transaction())
            backup_connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            settings(backup_connection, store.tenant)  # the first query takes the snapshot
            # Back to active before the gate is released: the fence lasted as long as it took
            # to establish the snapshot, not as long as the copy.
            _unfence(store, operation)
            fenced = False
        manifest: dict[str, Any] = {
            "format": "memoryking-postgres-1",
            "schema": 12,
            "store": store.store_id,
            "tenant": store.tenant,
            "incarnation": store.incarnation,
            "tables": {},
        }
        total = 0
        with snapshot:
            # How far the operations log was applied in this snapshot; compaction never drops
            # records above the oldest retained backup's position.
            position = backup_connection.execute(
                "SELECT value FROM tenant_meta WHERE tenant=%s AND key='operations'",
                (store.tenant,),
            ).fetchone()
            manifest["operations_sequence"] = int(position[0]) if position is not None else 0
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
                    durable_sync(out.fileno())
                manifest["tables"][table] = {
                    "columns": names,
                    "rows": count,
                    "sha256": digest_file(path),
                }
            digests = [
                row[0]
                for row in backup_connection.execute(
                    "SELECT DISTINCT blob FROM observations WHERE tenant=%s AND blob IS NOT NULL",
                    (store.tenant,),
                ).fetchall()
            ]
        # The snapshot is closed. Blob files are copied now, with requests running.
        blobs, linked, copied, size = _copy_blobs(
            store, backup_connection, destination, digests, previous
        )
        total += size
        if total > ARCHIVE_LIMIT:
            raise LocalError("backup_archive_limit")
        manifest["blob_index"] = write_blob_index(destination, blobs)
        manifest["archive_bytes"] = total
        manifest["blobs_linked"] = linked
        manifest["blobs_copied"] = copied
        atomic(destination / "manifest", store.control.sign(manifest).encode())
        sync_dir(destination)
        atomic(store.control_path / "last-backup", str(store.clock.utc_ms()).encode())
        return manifest
    finally:
        snapshot.close()
        if fenced:
            _unfence(store, operation)
        owner.close()


def _unfence(store: PostgresStore, operation: str) -> None:
    with store.control.locked():
        anchor = store.control.read_anchor()
        if anchor.get("maintenance_operation") == operation and anchor["state"] == "maintenance":
            anchor["state"] = "active"
            anchor.pop("maintenance_operation")
            store.control.anchor(anchor)


def _previous_blobs(store: PostgresStore, previous: Path | None) -> tuple[Path, set[str]] | None:
    """The digests a verified earlier archive of this tenant holds, or None to copy everything."""
    if previous is None:
        return None
    try:
        manifest = store.control.verify_document(previous / "manifest")
        if (
            manifest.get("format") != "memoryking-postgres-1"
            or manifest.get("store") != store.store_id
            or manifest.get("tenant") != store.tenant
            or "blob_index" not in manifest
        ):
            return None
        return previous, set(read_blob_index(previous, manifest["blob_index"]))
    except (OSError, LocalError):
        return None


def archived_blob(archive: Path, digest: str) -> Path:
    """An archive's copy of a blob: sharded by hash prefix, or flat as earlier archives wrote it."""
    sharded = archive / "blobs" / digest[: blobfiles.SHARD] / digest
    return sharded if sharded.is_file() else archive / "blobs" / digest


def _copy_blobs(
    store: PostgresStore,
    backup_connection: Any,
    destination: Path,
    digests: list[str],
    previous: Path | None,
) -> tuple[list[str], int, int, int]:
    earlier = _previous_blobs(store, previous)
    directory = destination / "blobs"
    private_dir(directory, create=True)
    touched: set[Path] = {directory}
    kept: list[str] = []
    linked = copied = total = 0
    for digest in digests:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise LocalError("blob_corrupt")
        shard = directory / digest[: blobfiles.SHARD]
        if shard not in touched:
            private_dir(shard, create=True)
            touched.add(shard)
        source = blobfiles.blob_path(store.root, store.tenant, digest)
        try:
            if earlier is not None and digest in earlier[1]:
                how, size = blobfiles.link_or_copy(
                    archived_blob(earlier[0], digest), source, shard / digest, digest
                )
            else:
                how, size = "copied", blobfiles.copy_verified(source, shard / digest, digest)
        except FileNotFoundError:
            # Purged after the snapshot: the restore replays the purge from the control journal.
            # A file missing while an observation still names it is corruption.
            with backup_connection.transaction():
                settings(backup_connection, store.tenant)
                named = backup_connection.execute(
                    "SELECT 1 FROM observations WHERE tenant=%s AND blob=%s LIMIT 1",
                    (store.tenant, digest),
                ).fetchone()
            if named is not None:
                raise LocalError("blob_corrupt") from None
            continue
        linked += how == "linked"
        copied += how == "copied"
        total += size
        kept.append(digest)
    blobfiles.sync_directories(touched)
    return kept, linked, copied, total


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
    # Restore writes one tenant's rows; a role that bypasses row-level security would not
    # be confined to them, so refuse it rather than trusting the role name alone.
    attributes = connection.execute(
        "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
    ).fetchone()
    if attributes is None or any(attributes):
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
                # The operations log has no separate checkpoint: its own verified chain stands.
                state = controller.operations.sync(anchor, trust_anchor=True)
                anchor.update(operations_sequence=state.count, operations_head=state.head)
            else:
                anchor = controller.read_anchor()
            records = controller.journal(anchor)
            controller.operations.sync(anchor)
        private_file(archive / "manifest")
        manifest = controller.verify_document(archive / "manifest")
        archive_tables = {
            1: TABLES_V1,
            2: TABLES_V2,
            3: TABLES_V3,
            4: TABLES_V4,
            5: TABLES_V5,
            6: TABLES_V6,
            7: TABLES_V6,
            8: TABLES_V8,
            9: TABLES_V9,
            10: TABLES_V10,
            11: TABLES_V10,
            12: TABLES,
        }.get(manifest.get("schema", 0), ())
        if (
            manifest.get("format") != "memoryking-postgres-1"
            or manifest.get("schema") not in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}
            or manifest.get("store") != controller.store
            or manifest.get("tenant") != tenant
            or set(manifest.get("tables", {})) != set(archive_tables)
        ):
            raise LocalError("backup_invalid")
        if clock.utc_ms() < anchor["last_utc"]:
            raise LocalError("clock_untrusted")
        # An archive older than what compaction kept cannot find its operation intents.
        operations_applied = manifest.get("operations_sequence", 0)
        if operations_applied < controller.operations.compacted_through():
            raise LocalError("backup_invalid")
        for table in archive_tables:
            if digest_file(archive / (table + ".ndjson")) != manifest["tables"][table]["sha256"]:
                raise LocalError("backup_invalid")
            current, archived = columns(connection, table), manifest["tables"][table]["columns"]
            # Archives before schema 11 predate the nullable on_behalf_of column.
            if archived != current and not (
                manifest["schema"] < 11
                and table in {"events", "receipts"}
                and current[-1] == "on_behalf_of"
                and archived == current[:-1]
            ):
                raise LocalError("schema_unsupported")
        # Archives before schema 7 embed the list; later ones commit to a separate index.
        blobs = (
            read_blob_index(archive, manifest["blob_index"])
            if "blob_index" in manifest
            else manifest["blobs"]
        )
        for digest in blobs:
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise LocalError("backup_invalid")
            if digest_file(archived_blob(archive, digest)) != digest:
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
            touched: set[Path] = {directory}
            for digest in blobs:
                shard = blobfiles.shard_directory(directory, digest)
                with archived_blob(archive, digest).open("rb") as stream:
                    blobfiles.write_durable(shard / digest, stream, digest)
                (directory / digest).unlink(missing_ok=True)  # a flat copy is now redundant
                touched.add(shard)
            blobfiles.sync_directories(touched)
            # The lease is not archived: a restore starts it again.
            connection.execute("DELETE FROM instance_lease WHERE tenant=%s", (tenant,))
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
            # Archives before schema 11 carry no operations position.
            connection.execute(
                "INSERT INTO tenant_meta(tenant,key,value) VALUES (%s,'operations','0') ON CONFLICT DO NOTHING",
                (tenant,),
            )
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
            operation_records = controller.operations.records_after(anchor, operations_applied)
            for record in operation_records:
                recovery._apply(record, clock.utc_ms())
            recovery.incarnation = uid("hist")
            recovery.db.execute(
                "UPDATE meta SET value=? WHERE key='incarnation'", (recovery.incarnation,)
            )
            recovery.db.execute("UPDATE retries SET result=NULL WHERE tenant=?", (tenant,))
            recovery.db.execute("DELETE FROM receipts WHERE tenant=?", (tenant,))
            recovery.db.execute(
                "UPDATE rendered_artifacts SET state='retired',body=NULL,request_json=NULL,diagnostics=NULL,dependencies='[]' WHERE tenant=?",
                (tenant,),
            )
            recovery.db.execute(
                "UPDATE checkpoints SET state='retired',body=NULL,dependencies='[]' WHERE tenant=?",
                (tenant,),
            )
            recovery.db.execute("UPDATE handoffs SET state='retired' WHERE tenant=?", (tenant,))
            recovery._retire_optional(tenant)
            recovery._reindex(tenant)
            live = {
                row[0]
                for row in recovery.db.execute(
                    "SELECT blob FROM observations WHERE blob IS NOT NULL AND tenant=?", (tenant,)
                )
            }
            admin = recovery_admin(recovery.db, clock.utc_ms(), tenant)
            if admin is None:
                raise LocalError("recovery_admin_missing")
            # The generation as the restored database (archive plus replayed journal) records
            # it, read in this transaction: the credential handed back must be the admin's
            # current one, not a generation-0 credential that authorization would refuse.
            generation = recovery._generation(tenant, admin[1])
            position = recovery.db.execute(
                "SELECT position FROM tenants WHERE tenant=?", (tenant,)
            ).fetchone()[0]
            serial = int(
                recovery.db.execute("SELECT value FROM meta WHERE key='commit_serial'").fetchone()[
                    0
                ]
            )
        hook("restore_before_activation")
        # Generation 0 is omitted from the token, so it stays byte-identical to one minted
        # before credential generations existed.
        credential = recovery._token(
            "credential",
            tenant=tenant,
            principal=admin[1],
            audience=audience,
            generation=generation,
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
            if operation_records:
                anchor.update(
                    operations_sequence=operation_records[-1]["sequence"],
                    operations_head=controller.operations.head(operation_records[-1]["sequence"]),
                )
            anchor.pop("maintenance_operation", None)
            controller.anchor(anchor)
        # Orphan blobs go only after the database and anchor are committed, so an
        # interrupted restore never deletes content a retry would still need.
        swept = {directory}
        for name, path in blobfiles.iterate(directory):
            private_file(path)
            if name not in live:
                path.unlink()
                swept.add(path.parent)
        blobfiles.sync_directories(swept)
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
        generation_row = connection.execute(
            "SELECT generation FROM credential_generations WHERE tenant=%s AND principal=%s",
            (tenant, principal),
        ).fetchone()
    capabilities = row[0] if row is not None else []
    if isinstance(capabilities, str):
        capabilities = load_strict(capabilities.encode())
    if row is None or "admin" not in capabilities or not temporal_valid(at, row[1], row[2]):
        raise LocalError("denied")
    # After the grant check, as in the stores' _auth: a principal without a current admin
    # grant is refused exactly as before, and only a credential that would otherwise be
    # accepted learns it was replaced. Compared as integers, never as token text.
    presented = identity.get("generation", 0)
    if (
        not isinstance(presented, int)
        or isinstance(presented, bool)
        or presented != (int(generation_row[0]) if generation_row is not None else 0)
    ):
        raise LocalError("credential_replaced")
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


def rotate_owner(
    pool: Any,
    root: Path,
    control: Path,
    tenant: str,
    *,
    audience: str = "memoryking-shared",
    clock: Clock | None = None,
    fault: Callable[[str], None] | None = None,
    instance_id: str | None = None,
) -> Path:
    """Replace the tenant's operator credential file with one of the next generation.

    The operator holds `control/<tenant>/owner.credential` (written by `init-tenant` and
    `restore`). The rotation is the same journaled grant restatement that makes any
    rotation, restating the admin's current capabilities (which the `grant` operation
    cannot, since it never grants `admin`), so a restore replays it. The new credential is
    made durable as `owner.credential.next` before the generation commits and is then
    moved over `owner.credential`; a failure in between leaves the working credential in
    the `.next` file and a repeat run completes the rename instead of rotating a second
    time. The previous credential stops working at the commit. A running service needs no
    stop: the rotation is one ordinary tenant transaction.
    """
    identifier(tenant)
    directory = control / tenant
    private_dir(directory)
    target, pending = directory / "owner.credential", directory / "owner.credential.next"
    private_file(target)
    # Only decoded to open the store; authorization below needs a current admin grant.
    held = target.read_text().strip()
    with PostgresStore(
        pool,
        root,
        control,
        tenant,
        held,
        audience=audience,
        clock=clock,
        fault=fault,
        instance_id=instance_id,
    ) as store:
        principal = store.principal
        with store.transaction() as decision:
            generation = store._generation(tenant, principal)
            resumed = False
            if pending.exists() or pending.is_symlink():
                private_file(pending)
                try:
                    body = store.control.verify(pending.read_text().strip())
                except LocalError:
                    body = {}
                resumed = (
                    body.get("kind") == "credential"
                    and body.get("incarnation") == store.incarnation
                    and body.get("tenant") == tenant
                    and body.get("principal") == principal
                    and body.get("audience") == audience
                    and body.get("generation", 0) == generation > 0
                )
                if not resumed:
                    pending.unlink()
            if not resumed:
                row = store.db.execute(
                    "SELECT capabilities,starts,expires FROM grants "
                    "WHERE tenant=? AND principal=? AND project='*'",
                    (tenant, principal),
                ).fetchone()
                capabilities = row["capabilities"] if row is not None else []
                if isinstance(capabilities, str):
                    capabilities = json.loads(capabilities)
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
        sync_dir(directory)
    return target
