"""M02 report schema upgrade atomicity and native storage boundaries."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import psycopg
import test_knowledge as knowledge
from migration_helpers import historical_inventory
from test_postgres_parity import BIN

from memoryking.adapters import postgres_admin
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.postgres_admin import initialize_tenant, migrate, provision_roles
from tools.postgres_dev_cluster import DevCluster


class LocalReportMigrations(unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown

    def test_schema_nine_upgrade_rolls_back_then_retries(self):
        self.store.close()
        with sqlite3.connect(self.data / "store.sqlite") as db:
            historical_inventory(db, 9)
        original = Path.read_text

        def invalid(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            return (
                text.replace("COMMIT;", "SELECT missing_m02_function(); COMMIT;")
                if path.name == "report_schema.sql"
                else text
            )

        with patch.object(Path, "read_text", invalid), self.assertRaises(sqlite3.OperationalError):
            LocalStore(self.data, self.control)
        with sqlite3.connect(self.data / "store.sqlite") as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertIsNone(
                db.execute("SELECT name FROM sqlite_master WHERE name='report_sources'").fetchone()
            )
        self.store = LocalStore(self.data, self.control)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 10)
        self.assertEqual(
            self.store.db.execute("SELECT blob FROM observations").fetchone()[0],
            self.span["content_hash"],
        )


@unittest.skipUnless((BIN / "pg_ctl").is_file(), "native PostgreSQL runtime not configured")
class NativeReportUpgrade(unittest.TestCase):
    def test_schema_four_upgrade_is_atomic_and_repeatable(self):
        cluster = DevCluster(BIN)
        try:
            with psycopg.connect(cluster.dsn(), autocommit=True) as admin:
                provision_roles(admin)
            with tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-report-upgrade-") as folder:
                with psycopg.connect(cluster.dsn("mk_migration"), autocommit=True) as db:
                    with db.transaction():
                        db.execute("SET LOCAL search_path=memoryking,pg_catalog")
                        for name in (
                            "postgres_schema.sql",
                            "postgres_optional_schema.sql",
                            "postgres_experience_schema.sql",
                            "postgres_viewer_schema.sql",
                        ):
                            db.execute(Path(postgres_admin.__file__).with_name(name).read_text())
                        db.execute("INSERT INTO shared_meta VALUES ('schema','4')")
                    initialize_tenant(db, Path(folder) / "data", Path(folder) / "control", "legacy")
                    original = Path.read_text

                    def invalid(path, *args, **kwargs):
                        text = original(path, *args, **kwargs)
                        return (
                            text + "\nSELECT 1/0;"
                            if path.name == "postgres_report_schema.sql"
                            else text
                        )

                    with (
                        patch.object(Path, "read_text", invalid),
                        self.assertRaises(psycopg.errors.DivisionByZero),
                    ):
                        migrate(db)
                    self.assertEqual(
                        db.execute(
                            "SELECT value FROM memoryking.shared_meta WHERE key='schema'"
                        ).fetchone()[0],
                        "4",
                    )
                    self.assertIsNone(
                        db.execute("SELECT to_regclass('memoryking.report_sources')").fetchone()[0]
                    )
                    migrate(db)
                    migrate(db)
                    self.assertEqual(
                        db.execute(
                            "SELECT value FROM memoryking.shared_meta WHERE key='schema'"
                        ).fetchone()[0],
                        "5",
                    )
        finally:
            cluster.close()
