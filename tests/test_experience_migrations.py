"""Actual predecessor upgrades, interrupted migration, tenant isolation and old archives."""

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import psycopg
import test_knowledge as knowledge
from migration_helpers import historical_inventory
from recovery_helpers import backup_fixture, restore_fixture
from test_postgres_parity import BIN, NativeFixture
from test_ranking import RankingCases

from memoryking.adapters import postgres_admin
from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.postgres_admin import initialize_tenant, migrate, provision_roles
from memoryking.adapters.postgres_recovery import TABLES_V2, TABLES_V3
from tools.postgres_dev_cluster import DevCluster


class LocalExperienceMigrations(unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown

    def test_predecessor_upgrade_rolls_back_then_retries(self):
        self.store.close()
        with sqlite3.connect(self.data / "store.sqlite") as db:
            historical_inventory(db, 7)
        original = Path.read_text

        def invalid(path, *args, **kwargs):
            source = original(path, *args, **kwargs)
            return (
                source.replace("COMMIT;", "SELECT missing_migration_function(); COMMIT;")
                if path.name == "experience_schema.sql"
                else source
            )

        with patch.object(Path, "read_text", invalid), self.assertRaises(sqlite3.OperationalError):
            LocalStore(self.data, self.control)
        with sqlite3.connect(self.data / "store.sqlite") as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertIsNone(
                db.execute("SELECT 1 FROM sqlite_master WHERE name='experience_records'").fetchone()
            )
        self.store = LocalStore(self.data, self.control)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 14)
        self.assertEqual(
            self.store.db.execute("SELECT blob FROM observations").fetchone()[0],
            self.span["content_hash"],
        )

    def test_predecessor_archive_upgrades_and_replays_current_purge(self):
        archive = backup_fixture(self)
        with sqlite3.connect(archive / "store.sqlite") as db:
            historical_inventory(db, 7)
        manifest = self.store.control.verify((archive / "manifest").read_text())
        manifest.update(
            schema=7,
            database_sha256=hashlib.sha256((archive / "store.sqlite").read_bytes()).hexdigest(),
        )
        atomic(archive / "manifest", self.store.control.sign(manifest).encode())
        self.store.purge(self.owner, self.obs["observation"])
        restore_fixture(self, archive)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 14)
        self.assertEqual(self.store.db.execute("SELECT deleted FROM observations").fetchone()[0], 1)


class NativeExperienceStorage(NativeFixture):
    run_e = RankingCases.run_e
    run_r = RankingCases.run_r
    brief_args = RankingCases.brief_args
    feedback = RankingCases.feedback
    seed = RankingCases.seed
    propose_rank = RankingCases.propose_rank
    evaluate = RankingCases.evaluate
    activate = RankingCases.activate

    def test_populated_records_and_decisions_are_rls_isolated_and_worker_read_only(self):
        self.seed()
        self.activate(self.evaluate())
        tables = ("experience_records", "ranking_state", "ranking_history")
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
            for table in tables:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    db.execute(f"DELETE FROM memoryking.{table}")

    def test_predecessor_archive_restores_with_current_purge(self):
        archive = backup_fixture(self)
        manifest = self.store.control.verify_document(archive / "manifest")
        manifest["schema"] = 2
        manifest["tables"] = {
            name: meta for name, meta in manifest["tables"].items() if name in TABLES_V2
        }
        atomic(archive / "manifest", self.store.control.sign(manifest).encode())
        self.store.purge(self.owner, self.obs["observation"])
        restore_fixture(self, archive)
        self.assertEqual(self.store.db.execute("SELECT deleted FROM observations").fetchone()[0], 1)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM experience_records").fetchone()[0], 0
        )

    def test_schema_three_archive_without_audit_or_summaries_restores(self):
        # Archives written before egress_audit and session_summaries existed.
        archive = backup_fixture(self)
        manifest = self.store.control.verify_document(archive / "manifest")
        self.assertEqual(manifest["schema"], 7)
        manifest["schema"] = 3
        manifest["tables"] = {
            name: meta for name, meta in manifest["tables"].items() if name in TABLES_V3
        }
        atomic(archive / "manifest", self.store.control.sign(manifest).encode())
        restore_fixture(self, archive)
        self.assertEqual(self.store.db.execute("SELECT deleted FROM observations").fetchone()[0], 0)


@unittest.skipUnless((BIN / "pg_ctl").is_file(), "native PostgreSQL runtime not configured")
class NativeExperienceUpgrade(unittest.TestCase):
    def test_version_two_upgrade_is_atomic_and_repeatable(self):
        cluster = DevCluster(BIN)
        try:
            with psycopg.connect(cluster.dsn(), autocommit=True) as admin:
                provision_roles(admin)
            with tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-exp-upgrade-") as temp:
                with psycopg.connect(cluster.dsn("mk_migration"), autocommit=True) as db:
                    with db.transaction():
                        db.execute("SET LOCAL search_path=memoryking,pg_catalog")
                        for name in ("postgres_schema.sql", "postgres_optional_schema.sql"):
                            db.execute(Path(postgres_admin.__file__).with_name(name).read_text())
                        db.execute("INSERT INTO shared_meta VALUES ('schema','2')")
                    initialize_tenant(db, Path(temp) / "data", Path(temp) / "control", "legacy")
                    original = Path.read_text

                    def invalid(path, *args, **kwargs):
                        source = original(path, *args, **kwargs)
                        return (
                            source + "\nSELECT 1/0;"
                            if path.name == "postgres_experience_schema.sql"
                            else source
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
                        "2",
                    )
                    self.assertIsNone(
                        db.execute(
                            "SELECT to_regclass('memoryking.experience_records')"
                        ).fetchone()[0]
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
