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
from memoryking.application.records import worker_tables
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


# The pgvector release this build is written and tested against. A test may widen it.
PGVECTOR_VERSIONS: tuple[str, ...] = ("0.8.7",)
# The approximate index serves vectors of this many dimensions (the default model's); a
# partition of another size is still searched exactly.
NATIVE_DIMENSIONS = 384

NATIVE_DDL = (
    # The converted copy of each vector, derived from `embedding_vectors` by a trigger and
    # removed with its source row by the cascade, so purge, reset and restore need no extra
    # statement and nothing here is part of an archive.
    """CREATE TABLE IF NOT EXISTS memoryking.embedding_native (
 tenant TEXT NOT NULL, project TEXT NOT NULL, fingerprint TEXT NOT NULL, generation BIGINT NOT NULL,
 kind TEXT NOT NULL, subject TEXT NOT NULL, part BIGINT NOT NULL,
 vec memoryking_vector.vector({dims}) NOT NULL, position BIGINT NOT NULL,
 PRIMARY KEY(tenant,project,fingerprint,generation,kind,subject,part),
 FOREIGN KEY(tenant,project,fingerprint,generation,kind,subject,part)
  REFERENCES memoryking.embedding_vectors(tenant,project,fingerprint,generation,kind,subject,part)
  ON DELETE CASCADE)""",
    "ALTER TABLE memoryking.embedding_native ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE memoryking.embedding_native FORCE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS tenant_isolation ON memoryking.embedding_native",
    """CREATE POLICY tenant_isolation ON memoryking.embedding_native
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true))""",
    """CREATE OR REPLACE FUNCTION memoryking.vec_from_i16(b bytea) RETURNS memoryking_vector.vector
 LANGUAGE sql IMMUTABLE STRICT AS $$
 SELECT (array_agg(
   (CASE WHEN get_byte(b, 2*i) + get_byte(b, 2*i+1) * 256 >= 32768
         THEN get_byte(b, 2*i) + get_byte(b, 2*i+1) * 256 - 65536
         ELSE get_byte(b, 2*i) + get_byte(b, 2*i+1) * 256 END)::real / 32767.0 ORDER BY i
 ))::real[]::memoryking_vector.vector
 FROM generate_series(0, length(b) / 2 - 1) AS i $$""",
    """CREATE OR REPLACE FUNCTION memoryking.embedding_native_sync() RETURNS trigger
 LANGUAGE plpgsql AS $$
BEGIN
 IF length(NEW.vector) = {bytes} THEN
  INSERT INTO memoryking.embedding_native(tenant,project,fingerprint,generation,kind,subject,part,vec,position)
  VALUES (NEW.tenant,NEW.project,NEW.fingerprint,NEW.generation,NEW.kind,NEW.subject,NEW.part,
          memoryking.vec_from_i16(NEW.vector),NEW.position)
  ON CONFLICT (tenant,project,fingerprint,generation,kind,subject,part)
  DO UPDATE SET vec=EXCLUDED.vec,position=EXCLUDED.position;
 END IF;
 RETURN NEW;
END $$""",
    "DROP TRIGGER IF EXISTS embedding_native_sync ON memoryking.embedding_vectors",
    """CREATE TRIGGER embedding_native_sync AFTER INSERT OR UPDATE ON memoryking.embedding_vectors
 FOR EACH ROW EXECUTE FUNCTION memoryking.embedding_native_sync()""",
    # Vectors written before the operator enabled the extension (or restored without the
    # trigger) are converted here; running this again fills only what is missing.
    """INSERT INTO memoryking.embedding_native(tenant,project,fingerprint,generation,kind,subject,part,vec,position)
 SELECT v.tenant,v.project,v.fingerprint,v.generation,v.kind,v.subject,v.part,
        memoryking.vec_from_i16(v.vector),v.position
 FROM memoryking.embedding_vectors v WHERE length(v.vector) = {bytes}
 ON CONFLICT DO NOTHING""",
    """CREATE INDEX IF NOT EXISTS embedding_native_hnsw ON memoryking.embedding_native
 USING hnsw (vec memoryking_vector.vector_cosine_ops)""",
)


def enable_vector(admin: Any, roles: Roles | None = None) -> None:
    """Explicit operator action after installing pgvector; never automatic.

    Creates the extension in its own schema, then (when the shared schema is at the version
    that has the vector table) an approximate HNSW index over a converted copy of the
    vectors. Searching still rescoring the shortlist exactly, so the index only chooses
    which vectors to compare. Safe to run again: it fills in what is missing."""
    roles = roles or Roles()
    with admin.transaction():
        if not admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[
            0
        ]:
            raise LocalError("extension_administrator_required")
        existing = admin.execute(
            "SELECT e.extversion,n.nspname FROM pg_extension e JOIN pg_namespace n ON e.extnamespace=n.oid WHERE e.extname='vector'"
        ).fetchone()
        if existing is not None and (
            existing[0] not in PGVECTOR_VERSIONS or existing[1] != "memoryking_vector"
        ):
            raise LocalError("vector_extension_mismatch")
        if existing is None:
            # Refuse a pre-existing schema; its ownership/functions are unknown.
            admin.execute("CREATE SCHEMA memoryking_vector")
            admin.execute(
                sql.SQL("CREATE EXTENSION vector WITH SCHEMA memoryking_vector VERSION {}").format(
                    sql.Literal(PGVECTOR_VERSIONS[0])
                )
            )
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
        has_table = admin.execute(
            "SELECT to_regclass('memoryking.embedding_vectors') IS NOT NULL"
        ).fetchone()[0]
        if not has_table:
            return  # migrate first; running this again then adds the index
        for statement in NATIVE_DDL:
            admin.execute(
                sql.SQL(
                    statement.replace("{dims}", str(NATIVE_DIMENSIONS)).replace(
                        "{bytes}", str(NATIVE_DIMENSIONS * 2)
                    )
                )
            )
        for role in (roles.runtime, roles.worker):
            admin.execute(
                sql.SQL(
                    "GRANT SELECT,INSERT,UPDATE,DELETE ON memoryking.embedding_native TO {}"
                ).format(sql.Identifier(role))
            )
            admin.execute(
                sql.SQL("GRANT EXECUTE ON FUNCTION memoryking.vec_from_i16(bytea) TO {}").format(
                    sql.Identifier(role)
                )
            )
        admin.execute(
            sql.SQL("GRANT SELECT ON memoryking.embedding_native TO {}").format(
                sql.Identifier(roles.backup)
            )
        )


# The shared schema version this build serves. A service accepts a database at exactly this
# version, or one version ahead when that migration is listed as additive: it only adds tables,
# columns or indexes that this build ignores, and nothing this build writes is read differently
# by the newer one. Rolling upgrade order for an additive migration: migrate the database, then
# restart services one at a time. Any other migration needs stop, migrate, start.
SCHEMA_VERSION = 16
# Migration 13 -> 14 is not listed: the newer service finds receipts to delete on purge through
# a join table the older one does not fill, so a mixed fleet could leave a receipt behind.
ADDITIVE_MIGRATIONS: frozenset[int] = frozenset()


def schema_compatible(found: int) -> bool:
    return found == SCHEMA_VERSION or (found == SCHEMA_VERSION + 1 and found in ADDITIVE_MIGRATIONS)


def schema_refusal(found: str) -> LocalError:
    """The refusal names both versions: what the database has and what this service needs."""
    return LocalError(
        "schema_unsupported",
        f"The database is at schema {found[:8]} and this service needs {SCHEMA_VERSION}. "
        "Stop the service, run migrate with this release, then start it.",
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
            connection.execute("INSERT INTO shared_meta(key,value) VALUES ('schema','1')")
        version = connection.execute("SELECT value FROM shared_meta WHERE key='schema'").fetchone()
        if version is None or version[0] not in {
            "1",
            "2",
            "3",
            "4",
            "5",
            "6",
            "7",
            "8",
            "9",
            "10",
            "11",
            "12",
            "13",
            "14",
            "15",
            "16",
        }:
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
        if version[0] in {"1", "2", "3", "4", "5"}:
            connection.execute(Path(__file__).with_name("postgres_summary_schema.sql").read_text())
            connection.execute("UPDATE shared_meta SET value='6' WHERE key='schema'")
        if version[0] in {"1", "2", "3", "4", "5", "6"}:
            connection.execute(Path(__file__).with_name("postgres_trust_schema.sql").read_text())
            connection.execute("UPDATE shared_meta SET value='7' WHERE key='schema'")
        if version[0] in {"1", "2", "3", "4", "5", "6", "7"}:
            connection.execute(Path(__file__).with_name("postgres_anchor_schema.sql").read_text())
            connection.execute("UPDATE shared_meta SET value='8' WHERE key='schema'")
        if version[0] in {"1", "2", "3", "4", "5", "6", "7", "8"}:
            connection.execute(Path(__file__).with_name("postgres_position_schema.sql").read_text())
            connection.execute("UPDATE shared_meta SET value='9' WHERE key='schema'")
        if version[0] in {"1", "2", "3", "4", "5", "6", "7", "8", "9"}:
            connection.execute(
                Path(__file__).with_name("postgres_credential_schema.sql").read_text()
            )
            connection.execute("UPDATE shared_meta SET value='10' WHERE key='schema'")
        if version[0] in {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10"}:
            connection.execute(Path(__file__).with_name("postgres_lexical_schema.sql").read_text())
            connection.execute("UPDATE shared_meta SET value='11' WHERE key='schema'")
        if version[0] in {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"}:
            connection.execute(
                Path(__file__).with_name("postgres_corrections_schema.sql").read_text()
            )
            connection.execute("UPDATE shared_meta SET value='12' WHERE key='schema'")
        if version[0] in {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"}:
            connection.execute(
                Path(__file__).with_name("postgres_operations_schema.sql").read_text()
            )
            connection.execute("UPDATE shared_meta SET value='13' WHERE key='schema'")
        if version[0] in {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13"}:
            connection.execute(
                Path(__file__).with_name("postgres_retention_schema.sql").read_text()
            )
            connection.execute("UPDATE shared_meta SET value='14' WHERE key='schema'")
        if version[0] in {
            "1",
            "2",
            "3",
            "4",
            "5",
            "6",
            "7",
            "8",
            "9",
            "10",
            "11",
            "12",
            "13",
            "14",
        }:
            connection.execute(
                Path(__file__).with_name("postgres_consolidation_schema.sql").read_text()
            )
            connection.execute("UPDATE shared_meta SET value='15' WHERE key='schema'")
        if int(version[0]) < 16:
            connection.execute(Path(__file__).with_name("postgres_vectors_schema.sql").read_text())
            connection.execute("UPDATE shared_meta SET value='16' WHERE key='schema'")
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
        for table in worker_tables():
            connection.execute(
                sql.SQL("GRANT INSERT,UPDATE,DELETE ON {} TO {}").format(
                    sql.Identifier(table), sql.Identifier(roles.worker)
                )
            )
        for table in ("receipts", "receipt_observations", "tenant_meta"):
            connection.execute(
                sql.SQL("REVOKE INSERT,UPDATE,DELETE ON {} FROM {}").format(
                    sql.Identifier(table), sql.Identifier(roles.worker)
                )
            )
        connection.execute(
            sql.SQL("GRANT INSERT ON receipts TO {}").format(sql.Identifier(roles.worker))
        )
        connection.execute(
            sql.SQL("GRANT INSERT ON receipt_observations TO {}").format(
                sql.Identifier(roles.worker)
            )
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
        "operations_sequence": 0,
        "operations_head": "0" * 64,
        "last_utc": 0,
        "commit_serial": 0,
    }
    controller.anchor(anchor)
    with connection.transaction():
        connection.execute("SET LOCAL search_path=memoryking,pg_catalog")
        connection.execute("SELECT set_config('memoryking.tenant',%s,true)", (tenant,))
        connection.execute("INSERT INTO tenants(tenant) VALUES (%s)", (tenant,))
        connection.execute("INSERT INTO tenant_gates(tenant) VALUES (%s)", (tenant,))
        for key, value in (
            ("store", store),
            ("incarnation", incarnation),
            ("journal", "0"),
            ("operations", "0"),
            ("commit_serial", "0"),
        ):
            connection.execute(
                "INSERT INTO tenant_meta(tenant,key,value) VALUES (%s,%s,%s)", (tenant, key, value)
            )
        connection.execute(
            "INSERT INTO grants(tenant,principal,project,capabilities,starts,expires) VALUES (%s,%s,'*',%s,NULL,NULL)",
            (tenant, principal, json.dumps(sorted(CAPABILITIES))),
        )
        connection.execute(
            "INSERT INTO projections(tenant,incarnation,channel,configuration,generation,frontier) VALUES (%s,%s,'audit_probe','v1',1,0)",
            (tenant, incarnation),
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
