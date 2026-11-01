"""Explicit PostgreSQL provisioning boundary; never imported by domain code.

Role creation is a privileged deployment operation. Runtime pools cannot migrate,
create tenants, or read credentials from this module.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg import sql

from memoryking.adapters.local_files import Control, atomic, private_dir, sync_dir
from memoryking.adapters.local_store import CAPABILITIES, identifier, paths, uid
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError


@dataclass(frozen=True)
class Roles:
    migration: str = "mk_migration"
    runtime: str = "mk_runtime"
    worker: str = "mk_worker"
    backup: str = "mk_backup"

    def __post_init__(self) -> None:
        values = (self.migration, self.runtime, self.worker, self.backup)
        if len(set(values)) != 4:
            raise ValueError("Database roles must be distinct")
        for value in values:
            identifier(value)


def provision_roles(admin: Any, roles: Roles | None = None) -> None:
    """Create fresh least-privileged roles, with no password or membership inheritance.

    Deployment must separately configure SCRAM secrets or certificate authentication.
    Private Unix-socket test fixtures authenticate these names only inside their fixture.
    Existing roles are rejected rather than silently trusting their privileges.
    """
    roles = roles or Roles()
    with admin.transaction():
        for role in (roles.migration, roles.runtime, roles.worker, roles.backup):
            admin.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(role))
            )
        admin.execute(
            sql.SQL("CREATE SCHEMA memoryking AUTHORIZATION {}").format(
                sql.Identifier(roles.migration)
            )
        )
        admin.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        admin.execute("REVOKE ALL ON SCHEMA memoryking FROM PUBLIC")


def enable_vector(admin: Any, roles: Roles | None = None) -> None:
    """Explicit operator action after installing pgvector 0.8.7; never automatic."""
    roles = roles or Roles()
    with admin.transaction():
        if not admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[
            0
        ]:
            raise LocalError("extension_administrator_required")
        existing = admin.execute(
            "SELECT e.extversion,n.nspname FROM pg_extension e JOIN pg_namespace n ON e.extnamespace=n.oid WHERE e.extname='vector'"
        ).fetchone()
        if existing is not None and tuple(existing) != ("0.8.7", "memoryking_vector"):
            raise LocalError("vector_extension_mismatch")
        if existing is None:
            # Refuse a pre-existing schema; its ownership/functions are unknown.
            admin.execute("CREATE SCHEMA memoryking_vector")
            admin.execute("CREATE EXTENSION vector WITH SCHEMA memoryking_vector VERSION '0.8.7'")
        admin.execute("REVOKE ALL ON SCHEMA memoryking_vector FROM PUBLIC")
        admin.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA memoryking_vector FROM PUBLIC")
        for role in (roles.runtime, roles.worker):
            admin.execute(
                sql.SQL("GRANT USAGE ON SCHEMA memoryking_vector TO {}").format(
                    sql.Identifier(role)
                )
            )
            admin.execute(
                sql.SQL("GRANT USAGE ON TYPE memoryking_vector.vector TO {}").format(
                    sql.Identifier(role)
                )
            )
            admin.execute(
                sql.SQL("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA memoryking_vector TO {}").format(
                    sql.Identifier(role)
                )
            )


def migrate(connection: Any, roles: Roles | None = None) -> None:
    """Apply supported migrations transactionally as the separate owner, offline."""
    roles = roles or Roles()
    with connection.transaction():
        role = connection.execute("SELECT current_user").fetchone()[0]
        if role != roles.migration:
            raise LocalError("migration_role_required")
        connection.execute("SET LOCAL search_path=memoryking,pg_catalog")
        connection.execute("SELECT pg_advisory_xact_lock(71102308)")
        if connection.execute("SELECT to_regclass('memoryking.shared_meta')").fetchone()[0] is None:
            connection.execute(Path(__file__).with_name("postgres_schema.sql").read_text())
            connection.execute("INSERT INTO shared_meta VALUES ('schema','1')")
        version = connection.execute("SELECT value FROM shared_meta WHERE key='schema'").fetchone()
        if version is None or version[0] not in {"1", "2", "3", "4", "5"}:
            raise LocalError("schema_unsupported")
        if version[0] == "1":
            connection.execute(Path(__file__).with_name("postgres_optional_schema.sql").read_text())
            connection.execute("UPDATE shared_meta SET value='2' WHERE key='schema'")
        if version[0] in {"1", "2"}:
            connection.execute(
                Path(__file__).with_name("postgres_experience_schema.sql").read_text()
            )
            connection.execute("UPDATE shared_meta SET value='3' WHERE key='schema'")
        if version[0] in {"1", "2", "3"}:
            connection.execute(Path(__file__).with_name("postgres_counter.sql").read_text())
            connection.execute(Path(__file__).with_name("postgres_index_schema.sql").read_text())
            connection.execute("UPDATE shared_meta SET value='4' WHERE key='schema'")
        if version[0] in {"1", "2", "3", "4"}:
            connection.execute(Path(__file__).with_name("postgres_egress_schema.sql").read_text())
            connection.execute("UPDATE shared_meta SET value='5' WHERE key='schema'")
        connection.execute("REVOKE ALL ON ALL TABLES IN SCHEMA memoryking FROM PUBLIC")
        connection.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA memoryking FROM PUBLIC")
        tables = [
            r[0]
            for r in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='memoryking' "
                "AND tablename!='shared_meta' ORDER BY tablename"
            )
        ]
        for role in (roles.runtime, roles.worker, roles.backup):
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA memoryking TO {}").format(sql.Identifier(role))
            )
            connection.execute(
                sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA memoryking TO {}").format(
                    sql.Identifier(role)
                )
            )
            connection.execute(
                sql.SQL("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA memoryking TO {}").format(
                    sql.Identifier(role)
                )
            )
        for table in [*tables, "meta"]:
            connection.execute(
                sql.SQL("GRANT INSERT,UPDATE,DELETE ON {} TO {}").format(
                    sql.Identifier(table), sql.Identifier(roles.runtime)
                )
            )
        # Workers can materialize projections and record their bounded release receipts;
        # they cannot create policy, content, checkpoint or handoff records, rewrite or
        # delete receipts, or touch tenant counters except through the owner-run function.
        for table in (
            "jobs",
            "projections",
            "projection_items",
            "repository_jobs",
            "embedding_jobs",
            "embedding_items",
            "processing_usage",
            "code_validations",
            "meta",
        ):
            connection.execute(
                sql.SQL("GRANT INSERT,UPDATE,DELETE ON {} TO {}").format(
                    sql.Identifier(table), sql.Identifier(roles.worker)
                )
            )
        for table in ("receipts", "tenant_meta"):
            connection.execute(
                sql.SQL("REVOKE INSERT,UPDATE,DELETE ON {} FROM {}").format(
                    sql.Identifier(table), sql.Identifier(roles.worker)
                )
            )
        connection.execute(
            sql.SQL("GRANT INSERT ON receipts TO {}").format(sql.Identifier(roles.worker))
        )
        connection.execute(
            sql.SQL("REVOKE EXECUTE ON FUNCTION next_commit_serial(text,bigint) FROM {}").format(
                sql.Identifier(roles.backup)
            )
        )
        # SELECT FOR UPDATE/SHARE requires UPDATE permission even on the lock-only row.
        connection.execute(
            sql.SQL("GRANT UPDATE ON tenant_gates TO {}").format(sql.Identifier(roles.worker))
        )
        connection.execute(
            sql.SQL("GRANT UPDATE(scheduled,frontier) ON embedding_partitions TO {}").format(
                sql.Identifier(roles.worker)
            )
        )
        for table in tables:
            if connection.execute(
                "SELECT 1 FROM pg_policies WHERE schemaname='memoryking' AND tablename=%s AND policyname='backup_read'",
                (table,),
            ).fetchone():
                continue
            connection.execute(
                sql.SQL("CREATE POLICY backup_read ON {} FOR SELECT TO {} USING (true)").format(
                    sql.Identifier(table), sql.Identifier(roles.backup)
                )
            )


def initialize_tenant(
    connection: Any,
    root: Path,
    control: Path,
    tenant: str,
    principal: str = "owner",
    audience: str = "memoryking-shared",
) -> Path:
    """Create a fresh tenant plus independent private identity/control volume.

    Run offline as migration owner. Incomplete provisioning is fail closed and must
    be reconciled explicitly; existing directories and tenant identifiers are refused.
    """
    identifier(tenant)
    identifier(principal)
    identifier(audience)
    paths(root, control)
    private_dir(root, create=True)
    private_dir(root / "blobs", create=True)
    private_dir(control, create=True)
    tenant_control = control / tenant
    if tenant_control.exists():
        raise LocalError("initialization_requires_new_directories")
    private_dir(tenant_control, create=True)
    store, incarnation = uid("store"), uid("hist")
    atomic(
        tenant_control / "identity.json",
        canonical_bytes({"store": store, "key": secrets.token_hex(32)}),
    )
    atomic(tenant_control / "journal", b"")
    controller = Control(tenant_control)
    anchor = {
        "store": store,
        "incarnation": incarnation,
        "state": "provisioning",
        "frontiers": {},
        "journal_sequence": 0,
        "journal_head": "0" * 64,
        "last_utc": 0,
        "commit_serial": 0,
    }
    controller.anchor(anchor)
    with connection.transaction():
        connection.execute("SET LOCAL search_path=memoryking,pg_catalog")
        connection.execute("SELECT set_config('memoryking.tenant',%s,true)", (tenant,))
        connection.execute("INSERT INTO tenants(tenant) VALUES (%s)", (tenant,))
        connection.execute("INSERT INTO tenant_gates VALUES (%s)", (tenant,))
        for key, value in (
            ("store", store),
            ("incarnation", incarnation),
            ("journal", "0"),
            ("commit_serial", "0"),
        ):
            connection.execute("INSERT INTO tenant_meta VALUES (%s,%s,%s)", (tenant, key, value))
        connection.execute(
            "INSERT INTO grants VALUES (%s,%s,'*',%s,NULL,NULL)",
            (tenant, principal, json.dumps(sorted(CAPABILITIES))),
        )
        connection.execute(
            "INSERT INTO projections VALUES (%s,%s,'audit_probe','v1',1,0)", (tenant, incarnation)
        )
        connection.execute("INSERT INTO knowledge_state(tenant) VALUES (%s)", (tenant,))
    anchor["state"] = "active"
    controller.anchor(anchor)
    token = controller.sign(
        {
            "kind": "credential",
            "store": store,
            "incarnation": incarnation,
            "tenant": tenant,
            "principal": principal,
            "audience": audience,
        }
    )
    credential = tenant_control / "owner.credential"
    atomic(credential, token.encode())
    sync_dir(control)
    return credential
