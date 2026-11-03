"""Native optional tables, historical archives, role separation and transactional upgrade."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import psycopg
from test_optional_migrations import OptionalRecoveryCases
from test_postgres_parity import BIN, NativeFixture

from memoryking.adapters.local_files import atomic
from memoryking.adapters.postgres_admin import initialize_tenant, migrate, provision_roles
from memoryking.adapters.postgres_recovery import TABLES_V1
from tools.postgres_dev_cluster import DevCluster


class NativeOptionalRecovery(OptionalRecoveryCases, NativeFixture):
    def test_populated_optional_state_is_isolated_by_forced_rls(self):
        self.optional_rows()
        with psycopg.connect(self.cluster.dsn("mk_runtime"), autocommit=True) as db:
            for name in ("embedding_partitions", "embedding_items", "adapter_bases"):
                self.assertEqual(
                    db.execute(f"SELECT count(*) FROM memoryking.{name}").fetchone()[0], 0
                )
                with db.transaction():
                    db.execute("SELECT set_config('memoryking.tenant',%s,true)", (self.tenant,))
                    self.assertEqual(
                        db.execute(f"SELECT count(*) FROM memoryking.{name}").fetchone()[0], 1
                    )
                with db.transaction():
                    db.execute("SELECT set_config('memoryking.tenant','unrelated',true)")
                    self.assertEqual(
                        db.execute(f"SELECT count(*) FROM memoryking.{name}").fetchone()[0], 0
                    )
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        with db.transaction():
                            db.execute(
                                "INSERT INTO memoryking.adapter_bases VALUES (%s,'wrong','p','owner','h','{}','x','hash','[]',1,2)",
                                (self.tenant,),
                            )

    def test_legacy_schema_one_archive_replays_policy_into_current_schema(self):
        from recovery_helpers import backup_fixture, restore_fixture

        archive = backup_fixture(self)
        manifest = self.store.control.verify_document(archive / "manifest")
        manifest["schema"] = 1
        manifest["tables"] = {
            name: meta for name, meta in manifest["tables"].items() if name in TABLES_V1
        }
        atomic(archive / "manifest", self.store.control.sign(manifest).encode())
        self.store.purge(self.owner, self.obs["observation"])
        restore_fixture(self, archive)
        with self.store.transaction():
            self.assertEqual(
                self.store.db.execute(
                    "SELECT deleted FROM observations WHERE id=?", (self.obs["observation"],)
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                self.store.db.execute("SELECT count(*) FROM embedding_items").fetchone()[0], 0
            )


@unittest.skipUnless((BIN / "pg_ctl").is_file(), "native PostgreSQL runtime not configured")
class NativeOptionalUpgrade(unittest.TestCase):
    def test_upgrade_is_transactional_repeatable_and_preserves_existing_tenant(self):
        from memoryking.adapters import postgres_admin

        cluster = DevCluster(BIN)
        try:
            with psycopg.connect(cluster.dsn(), autocommit=True) as admin:
                provision_roles(admin)
            with tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-upgrade-") as temp:
                with psycopg.connect(cluster.dsn("mk_migration"), autocommit=True) as db:
                    with db.transaction():
                        db.execute("SET LOCAL search_path=memoryking,pg_catalog")
                        db.execute(
                            Path(postgres_admin.__file__)
                            .with_name("postgres_schema.sql")
                            .read_text()
                        )
                        db.execute("INSERT INTO shared_meta VALUES ('schema','1')")
                    initialize_tenant(db, Path(temp) / "data", Path(temp) / "control", "legacy")
                    original_read = Path.read_text

                    def invalid_migration(path, *args, **kwargs):
                        text = original_read(path, *args, **kwargs)
                        return (
                            text + "\nSELECT 1/0;\n"
                            if path.name == "postgres_optional_schema.sql"
                            else text
                        )

                    with patch.object(Path, "read_text", invalid_migration):
                        with self.assertRaises(psycopg.errors.DivisionByZero):
                            migrate(db)
                    self.assertEqual(
                        db.execute(
                            "SELECT value FROM memoryking.shared_meta WHERE key='schema'"
                        ).fetchone()[0],
                        "1",
                    )
                    self.assertIsNone(
                        db.execute(
                            "SELECT to_regclass('memoryking.embedding_partitions')"
                        ).fetchone()[0]
                    )
                    migrate(db)
                    migrate(db)
                    self.assertEqual(
                        db.execute(
                            "SELECT value FROM memoryking.shared_meta WHERE key='schema'"
                        ).fetchone()[0],
                        "8",
                    )
                    with db.transaction():
                        db.execute("SELECT set_config('memoryking.tenant','legacy',true)")
                        self.assertEqual(
                            db.execute("SELECT count(*) FROM memoryking.grants").fetchone()[0], 1
                        )
            with psycopg.connect(cluster.dsn("mk_worker"), autocommit=True) as worker:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    worker.execute("UPDATE memoryking.embedding_partitions SET state='enabled'")
                worker.execute("UPDATE memoryking.embedding_partitions SET frontier=0")
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    worker.execute("DELETE FROM memoryking.adapter_bases")
            with psycopg.connect(cluster.dsn("mk_runtime"), autocommit=True) as runtime:
                for name in (
                    "embedding_partitions",
                    "embedding_jobs",
                    "embedding_items",
                    "processing_usage",
                    "adapter_bases",
                ):
                    self.assertEqual(
                        runtime.execute(f"SELECT count(*) FROM memoryking.{name}").fetchone()[0], 0
                    )
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        runtime.execute(f"TRUNCATE memoryking.{name} CASCADE")
        finally:
            cluster.close()
