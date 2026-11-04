"""Actual M01 schema upgrades, corruption failures, RLS and worker boundaries."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import psycopg
import test_knowledge as knowledge
from migration_helpers import historical_inventory
from test_postgres_parity import BIN, NativeFixture

from memoryking.adapters import postgres_admin
from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.postgres_admin import initialize_tenant, migrate, provision_roles
from memoryking.adapters.viewer_identity import ViewerIdentity
from memoryking.domain.local import LocalError
from tools.postgres_dev_cluster import DevCluster


class LocalViewerMigrations(unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown

    def test_schema_eight_upgrade_rolls_back_then_retries(self):
        self.store.close()
        with sqlite3.connect(self.data / "store.sqlite") as db:
            historical_inventory(db, 8)
        original = Path.read_text

        def invalid(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            return (
                text.replace("COMMIT;", "SELECT missing_m01_function();\nCOMMIT;")
                if path.name == "viewer_schema.sql"
                else text
            )

        with patch.object(Path, "read_text", invalid), self.assertRaises(sqlite3.OperationalError):
            LocalStore(self.data, self.control)
        with sqlite3.connect(self.data / "store.sqlite") as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 8)
            self.assertIsNone(
                db.execute("SELECT name FROM sqlite_master WHERE name='viewer_bindings'").fetchone()
            )
        self.store = LocalStore(self.data, self.control)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 14)
        self.assertEqual(
            self.store.db.execute("SELECT blob FROM observations").fetchone()[0],
            self.span["content_hash"],
        )

    def test_viewer_control_corruption_blocks_startup(self):
        ViewerIdentity(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "key": "bind",
                "action": "bind",
                "args": {"principal": "phone", "issuer": "https://github.com", "user_id": "12345"},
            },
        )
        self.store.close()
        journal = self.control / "journal"
        atomic(journal, journal.read_bytes() + b"{}\n")
        with self.assertRaises(LocalError):
            LocalStore(self.data, self.control)


class NativeViewerStorage(NativeFixture):
    def test_viewer_tables_are_tenant_isolated_and_worker_read_only(self):
        ViewerIdentity(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "key": "bind",
                "action": "bind",
                "args": {"principal": "phone", "issuer": "https://github.com", "user_id": "12345"},
            },
        )
        tables = ("viewer_bindings", "viewer_admin_retries")
        with psycopg.connect(self.cluster.dsn("mk_runtime"), autocommit=True) as db:
            for table in tables:
                self.assertEqual(
                    db.execute(f"SELECT count(*) FROM memoryking.{table}").fetchone()[0], 0
                )
                with db.transaction():
                    db.execute("SELECT set_config('memoryking.tenant',%s,true)", (self.tenant,))
                    self.assertGreater(
                        db.execute(f"SELECT count(*) FROM memoryking.{table}").fetchone()[0], 0
                    )
                with db.transaction():
                    db.execute("SELECT set_config('memoryking.tenant','unrelated',true)")
                    self.assertEqual(
                        db.execute(f"SELECT count(*) FROM memoryking.{table}").fetchone()[0], 0
                    )
        with psycopg.connect(self.cluster.dsn("mk_worker"), autocommit=True) as db:
            for table in (*tables, "viewer_grants"):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    db.execute(f"DELETE FROM memoryking.{table}")


@unittest.skipUnless((BIN / "pg_ctl").is_file(), "native PostgreSQL runtime not configured")
class NativeViewerUpgrade(unittest.TestCase):
    def test_schema_three_upgrade_is_atomic_and_repeatable(self):
        cluster = DevCluster(BIN)
        try:
            with psycopg.connect(cluster.dsn(), autocommit=True) as admin:
                provision_roles(admin)
            with tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-viewer-upgrade-") as folder:
                with psycopg.connect(cluster.dsn("mk_migration"), autocommit=True) as db:
                    with db.transaction():
                        db.execute("SET LOCAL search_path=memoryking,pg_catalog")
                        for name in (
                            "postgres_schema.sql",
                            "postgres_optional_schema.sql",
                            "postgres_experience_schema.sql",
                        ):
                            db.execute(Path(postgres_admin.__file__).with_name(name).read_text())
                        db.execute("INSERT INTO shared_meta VALUES ('schema','3')")
                    initialize_tenant(db, Path(folder) / "data", Path(folder) / "control", "legacy")
                    original = Path.read_text

                    def invalid(path, *args, **kwargs):
                        text = original(path, *args, **kwargs)
                        return (
                            text + "\nSELECT 1/0;"
                            if path.name == "postgres_viewer_schema.sql"
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
                        "3",
                    )
                    self.assertIsNone(
                        db.execute("SELECT to_regclass('memoryking.viewer_bindings')").fetchone()[0]
                    )
                    migrate(db)
                    migrate(db)
                    self.assertEqual(
                        db.execute(
                            "SELECT value FROM memoryking.shared_meta WHERE key='schema'"
                        ).fetchone()[0],
                        "9",
                    )
        finally:
            cluster.close()
