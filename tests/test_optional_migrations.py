"""Real version upgrades and fresh-history retirement of optional derived state."""

import hashlib
import sqlite3
import unittest

import test_knowledge as knowledge
from migration_helpers import historical_inventory
from recovery_helpers import backup_fixture, restore_fixture

from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_store import LocalStore

TABLES = (
    "embedding_partitions",
    "embedding_jobs",
    "embedding_items",
    "processing_usage",
    "adapter_bases",
)


class OptionalRecoveryCases:
    def optional_rows(self):
        with self.store.transaction():
            self.store.db.execute(
                "INSERT INTO embedding_partitions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.tenant,
                    "p",
                    "a" * 64,
                    self.store.incarnation,
                    3,
                    2,
                    "{}",
                    "experimental",
                    1,
                    1,
                    1,
                ),
            )
            self.store.db.execute(
                "INSERT INTO embedding_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.tenant,
                    "p",
                    "a" * 64,
                    3,
                    self.obs["observation"],
                    self.store.incarnation,
                    1,
                    self.span["content_hash"],
                    "b" * 64,
                    "[1000000,0]",
                    0,
                    10,
                    3,
                    0,
                    1,
                ),
            )
            self.store.db.execute(
                "INSERT INTO adapter_bases VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.tenant,
                    "base_fixture",
                    "p",
                    "owner",
                    self.store.incarnation,
                    "{}",
                    b'{"private":"retained"}',
                    "c" * 64,
                    "[]",
                    1,
                    9999999999999,
                ),
            )

    def test_restore_retires_vectors_bases_and_disables_partitions(self):
        self.optional_rows()
        archive = backup_fixture(self)
        previous = self.store.incarnation
        restore_fixture(self, archive)
        self.assertNotEqual(previous, self.store.incarnation)
        with self.store.transaction():
            for table in ("embedding_items", "embedding_jobs", "adapter_bases"):
                self.assertEqual(
                    self.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0
                )
            partition = self.store.db.execute("SELECT * FROM embedding_partitions").fetchone()
            self.assertEqual(partition["state"], "disabled")
            self.assertEqual(partition["incarnation"], self.store.incarnation)
            self.assertEqual(partition["frontier"], 0)

    def test_purge_removes_vectors_and_retained_adapter_bases(self):
        self.optional_rows()
        self.store.purge(self.owner, self.obs["observation"])
        with self.store.transaction():
            self.assertEqual(
                self.store.db.execute("SELECT count(*) FROM embedding_items").fetchone()[0], 0
            )
            self.assertEqual(
                self.store.db.execute("SELECT count(*) FROM adapter_bases").fetchone()[0], 0
            )


class LocalOptionalMigrations(OptionalRecoveryCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown

    def test_version_six_upgrade_preserves_content(self):
        self.store.close()
        with sqlite3.connect(self.data / "store.sqlite") as db:
            historical_inventory(db, 6)
        self.store = LocalStore(self.data, self.control)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 13)
        self.assertEqual(
            self.store.db.execute(
                "SELECT blob FROM observations WHERE id=?", (self.obs["observation"],)
            ).fetchone()[0],
            self.span["content_hash"],
        )

    def test_legacy_schema_six_backup_upgrades_before_restore_policy_replay(self):
        archive = backup_fixture(self)
        with sqlite3.connect(archive / "store.sqlite") as db:
            historical_inventory(db, 6)
        manifest = self.store.control.verify((archive / "manifest").read_text())
        manifest.update(
            schema=6,
            database_sha256=hashlib.sha256((archive / "store.sqlite").read_bytes()).hexdigest(),
        )
        atomic(archive / "manifest", self.store.control.sign(manifest).encode())
        self.store.purge(self.owner, self.obs["observation"])
        restore_fixture(self, archive)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 13)
        self.assertEqual(
            self.store.db.execute(
                "SELECT deleted FROM observations WHERE id=?", (self.obs["observation"],)
            ).fetchone()[0],
            1,
        )
