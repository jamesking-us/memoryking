"""Both real PostgreSQL schema lineages and their signed archive inventories."""

import unittest
from pathlib import Path

import psycopg
from test_postgres_parity import BIN

from memoryking.adapters import postgres_admin
from memoryking.adapters.mobile_migrations import EXTENSIONS
from memoryking.adapters.postgres_admin import migrate, provision_roles
from memoryking.adapters.postgres_recovery import (
    TABLES,
    TABLES_PRIVATE_V4,
    TABLES_PRIVATE_V5,
    TABLES_V4,
    TABLES_V5,
    TABLES_V6,
    archive_inventory,
)
from tools.postgres_dev_cluster import DevCluster

SQL = Path(postgres_admin.__file__).parent


class ArchiveInventories(unittest.TestCase):
    def test_colliding_numbers_require_exact_inventory(self):
        for number, tables in (
            (4, TABLES_V4),
            (4, TABLES_PRIVATE_V4),
            (5, TABLES_V5),
            (5, TABLES_PRIVATE_V5),
            (6, TABLES_V6),
            (7, TABLES),
        ):
            with self.subTest(number=number, tables=len(tables)):
                manifest = {"schema": number, "tables": dict.fromkeys(tables, {})}
                self.assertEqual(archive_inventory(manifest), tables)
                manifest["tables"]["unexpected"] = {}
                self.assertEqual(archive_inventory(manifest), ())
        for schema in (True, "7", [], {}, None, 8):
            self.assertEqual(
                archive_inventory({"schema": schema, "tables": dict.fromkeys(TABLES, {})}), ()
            )


@unittest.skipUnless((BIN / "pg_ctl").is_file(), "native PostgreSQL runtime not configured")
class PostgresMobileMigrations(unittest.TestCase):
    def test_each_native_and_private_predecessor_preserves_records_and_rls(self):
        cluster = DevCluster(BIN)
        try:
            with psycopg.connect(cluster.dsn(), autocommit=True) as admin:
                provision_roles(admin)
                for private, version in [(False, n) for n in range(3, 9)] + [(True, 4), (True, 5)]:
                    with self.subTest(private=private, version=version):
                        admin.execute("DROP SCHEMA memoryking CASCADE")
                        admin.execute("CREATE SCHEMA memoryking AUTHORIZATION mk_migration")
                        with psycopg.connect(cluster.dsn("mk_migration"), autocommit=True) as db:
                            with db.transaction():
                                db.execute("SET LOCAL search_path=memoryking,pg_catalog")
                                db.execute("SELECT set_config('memoryking.tenant','fixture',true)")
                                names = ["schema", "optional_schema", "experience_schema"]
                                if private:
                                    names += ["viewer_schema"]
                                    if version == 5:
                                        names += ["report_schema"]
                                else:
                                    for introduced, scripts in (
                                        (4, ["counter", "index_schema"]),
                                        (5, ["egress_schema"]),
                                        (6, ["summary_schema"]),
                                        (7, ["trust_schema"]),
                                        (8, ["anchor_schema"]),
                                    ):
                                        if version >= introduced:
                                            names += scripts
                                for name in names:
                                    db.execute((SQL / f"postgres_{name}.sql").read_text())
                                db.execute(
                                    "INSERT INTO shared_meta VALUES ('schema',%s)", (str(version),)
                                )
                                db.execute(
                                    "INSERT INTO tenants(tenant,position,policy_epoch,purge_epoch) VALUES ('fixture',17,23,5)"
                                )
                                db.execute(
                                    "INSERT INTO tenant_meta VALUES ('fixture','store','original-store')"
                                )
                                if private:
                                    db.execute(
                                        "INSERT INTO viewer_bindings VALUES ('fixture','phone','https://github.com','123',1,7,9)"
                                    )
                                if private and version == 5:
                                    db.execute(
                                        "INSERT INTO report_revisions VALUES ('fixture','p','r',3,'original-history','ready','writer',1,'body','qa','lease',50)"
                                    )
                            migrate(db)
                            migrate(db)
                            self.assertEqual(
                                db.execute(
                                    "SELECT value FROM memoryking.shared_meta WHERE key='schema'"
                                ).fetchone()[0],
                                "9",
                            )
                            self.assertEqual(
                                db.execute(
                                    "SELECT value FROM memoryking.shared_meta WHERE key='mobile_schema'"
                                ).fetchone()[0],
                                "1",
                            )
                            with db.transaction():
                                db.execute("SET LOCAL search_path=memoryking,pg_catalog")
                                db.execute("SELECT set_config('memoryking.tenant','fixture',true)")
                                self.assertEqual(
                                    db.execute(
                                        "SELECT position,policy_epoch,purge_epoch FROM tenants"
                                    ).fetchone(),
                                    (17, 23, 5),
                                )
                                self.assertEqual(
                                    db.execute(
                                        "SELECT value FROM tenant_meta WHERE key='store'"
                                    ).fetchone()[0],
                                    "original-store",
                                )
                                if private:
                                    self.assertEqual(
                                        db.execute(
                                            "SELECT user_id,generation,version FROM viewer_bindings"
                                        ).fetchone(),
                                        ("123", 7, 9),
                                    )
                                if private and version == 5:
                                    self.assertEqual(
                                        db.execute(
                                            "SELECT body,token,lease_until FROM report_revisions"
                                        ).fetchone(),
                                        ("body", "lease", 50),
                                    )
                            for tables in EXTENSIONS.values():
                                for table in tables:
                                    row = db.execute(
                                        "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=to_regclass(%s)",
                                        (f"memoryking.{table}",),
                                    ).fetchone()
                                    self.assertEqual(row, (True, True), table)
        finally:
            cluster.close()
