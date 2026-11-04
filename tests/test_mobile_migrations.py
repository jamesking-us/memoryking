"""Real SQLite fixtures for the two incompatible schema-number lineages."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memoryking.adapters import mobile_migrations as migration
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.viewer_identity import ViewerIdentity
from memoryking.domain.local import LocalError

SQL = Path(migration.__file__).parent
BASE = (
    "local",
    "knowledge",
    "retrieval",
    "working",
    "handoff",
    "repository",
    "optional",
    "experience",
)


def fixture(*extensions):
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("PRAGMA foreign_keys=ON")
    for name in (*BASE, *extensions):
        db.executescript((SQL / f"{name}_schema.sql").read_text())
    db.execute("INSERT INTO tenants(tenant) VALUES ('owner-tenant')")
    db.executemany(
        "INSERT INTO meta VALUES (?, ?)",
        [
            ("store", "original-store"),
            ("incarnation", "original-history"),
            ("journal", "987"),
            ("commit_serial", "1234"),
        ],
    )
    return db


class MobileMigrations(unittest.TestCase):
    def upgrade(self, db):
        original_meta = dict(db.execute("SELECT * FROM meta"))
        original_tenant = db.execute("SELECT * FROM tenants").fetchall()
        migration.ensure_mobile_schema(db)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 14)
        self.assertEqual(
            original_meta,
            {k: v for k, v in db.execute("SELECT * FROM meta") if k != "mobile_schema"},
        )
        self.assertEqual(original_tenant, db.execute("SELECT * FROM tenants").fetchall())
        for group in migration.EXTENSIONS.values():
            for table in group:
                self.assertTrue(db.execute(f"PRAGMA table_info({table})").fetchall(), table)
        self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        # A normal reopen must neither migrate again nor change stored history.
        before = list(db.iterdump())
        migration.ensure_mobile_schema(db)
        self.assertEqual(before, list(db.iterdump()))

    def test_each_released_schema_eight_through_thirteen(self):
        names = ("index", "egress", "summary", "trust", "anchor")
        for version in range(8, 14):
            with self.subTest(version=version):
                db = fixture(*names[: version - 8])
                try:
                    self.upgrade(db)
                finally:
                    db.close()

    def test_private_nine_preserves_viewer_identity(self):
        db = fixture("viewer")
        self.addCleanup(db.close)
        db.execute(
            "INSERT INTO viewer_bindings VALUES ('owner-tenant','phone','https://github.com','123',1,7,9)"
        )
        db.execute("INSERT INTO viewer_grants VALUES ('owner-tenant','phone','project',10,20)")
        before = db.execute("SELECT * FROM viewer_bindings").fetchall()
        grants = db.execute("SELECT * FROM viewer_grants").fetchall()
        self.upgrade(db)
        self.assertEqual(before, db.execute("SELECT * FROM viewer_bindings").fetchall())
        self.assertEqual(grants, db.execute("SELECT * FROM viewer_grants").fetchall())

    def test_private_ten_preserves_report_bodies_files_and_pending_delivery(self):
        db = fixture("viewer", "report")
        self.addCleanup(db.close)
        db.execute(
            "INSERT INTO report_revisions VALUES ('owner-tenant','p','r',3,'original-history','ready','writer',1,'body','qa','lease',50)"
        )
        db.execute(
            "INSERT INTO report_files VALUES ('owner-tenant','p','r',3,'report.pdf','retained-blob',100,'pdf')"
        )
        db.execute(
            "INSERT INTO report_deliveries VALUES ('owner-tenant','delivery','p','r',3,'queued','original delivery')"
        )
        preserved = {
            table: db.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("report_revisions", "report_files", "report_deliveries")
        }
        self.upgrade(db)
        for table, rows in preserved.items():
            self.assertEqual(rows, db.execute(f"SELECT * FROM {table}").fetchall())

    def test_failed_join_rolls_back_ddl_and_version(self):
        db = fixture("viewer", "report")
        self.addCleanup(db.close)
        before = list(db.iterdump())
        execute = migration._execute_migration

        def fail(connection, name):
            execute(connection, name)
            if name == "egress":
                raise RuntimeError("simulated interruption after DDL")

        with patch.object(migration, "_execute_migration", fail), self.assertRaises(RuntimeError):
            migration.ensure_mobile_schema(db)
        self.assertFalse(db.in_transaction)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 10)
        self.assertEqual(before, list(db.iterdump()))
        self.upgrade(db)

    def test_partial_private_schema_is_refused_without_mutation(self):
        db = fixture("viewer", "report")
        self.addCleanup(db.close)
        db.execute("DROP TABLE viewer_admin_retries")
        self.assert_refused_unchanged(db)

    def test_wrong_existing_table_shape_is_refused_without_mutation(self):
        db = fixture("index", "egress")
        self.addCleanup(db.close)
        db.execute("ALTER TABLE egress_audit ADD COLUMN unknown TEXT")
        self.assert_refused_unchanged(db)

    def test_joined_version_requires_complete_inventory_and_marker(self):
        db = fixture("viewer", "report")
        self.addCleanup(db.close)
        migration.ensure_mobile_schema(db)
        db.execute("DELETE FROM meta WHERE key='mobile_schema'")
        self.assert_refused_unchanged(db)

    def test_future_schema_is_refused(self):
        db = fixture("viewer", "report")
        self.addCleanup(db.close)
        db.execute("PRAGMA user_version=15")
        self.assert_refused_unchanged(db)

    def assert_refused_unchanged(self, db):
        before, version = list(db.iterdump()), db.execute("PRAGMA user_version").fetchone()
        with self.assertRaises(LocalError) as error:
            migration.ensure_mobile_schema(db)
        self.assertEqual(error.exception.code, "schema_unsupported")
        self.assertEqual(before, list(db.iterdump()))
        self.assertEqual(version, db.execute("PRAGMA user_version").fetchone())


class MobileCoreIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-mobile-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root, self.control = self.base / "data", self.base / "control"
        self.owner = LocalStore.initialize(self.root, self.control).read_text()
        self.store = LocalStore(self.root, self.control)
        self.addCleanup(lambda: self.store.close() if self.store is not None else None)
        self.n = 0

    def policy(self, action, args):
        self.n += 1
        return ViewerIdentity(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "key": f"v{self.n}",
                "action": action,
                "args": args,
            },
        )

    def viewer(self):
        self.policy(
            "bind", {"principal": "phone", "issuer": "https://github.com", "user_id": "123"}
        )
        self.policy(
            "grant",
            {
                "principal": "phone",
                "project": "p",
                "expected_version": 1,
                "starts": None,
                "expires": None,
            },
        )
        return ViewerIdentity(self.store).resolve_authenticated_subject("https://github.com", "123")

    def test_viewer_reads_preserve_nested_identity_and_current_policy_ceiling(self):
        namespace = self.store.namespace(self.owner, "p")
        self.store.capture(
            self.owner,
            namespace,
            "capture",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "one",
                "text": "shared phone content",
                "readers": ["owner", "phone"],
                "classification": "internal",
                "observed_at_ms": 0,
            },
        )
        viewer = self.viewer()
        received = []
        self.store.read(viewer.credential, "p").emit(received.append)
        self.assertEqual(json.loads(received[0])["items"][0]["text"], "shared phone content")
        forged = self.store._token("credential", tenant="main", principal="phone")
        with self.assertRaises(LocalError):
            self.store.read(forged, "p")
        self.policy("revoke", {"principal": "phone", "project": "p", "expected_version": 2})
        with self.assertRaises(LocalError):
            self.store.read(viewer.credential, "p")

    def test_generic_policy_cannot_replace_bound_viewer_grants(self):
        self.viewer()
        sequence = self.store.anchor["journal_sequence"]
        for action in (
            lambda: self.store.grant(self.owner, "phone", "p", ["read"]),
            lambda: self.store.revoke(self.owner, "phone", "p"),
        ):
            with self.assertRaises(LocalError) as error:
                action()
            self.assertEqual(error.exception.code, "viewer_policy_operation_required")
            self.assertEqual(self.store.anchor["journal_sequence"], sequence)

    def test_report_blob_survives_gc_backup_and_fresh_history_restore(self):
        raw = b"retained report fixture"
        digest = self.store._blob("main", raw)
        with self.store.transaction():
            self.store.db.execute(
                "INSERT INTO report_revisions VALUES ('main','p','r',1,?,'ready','owner',1,'{}',NULL,NULL,NULL)",
                (self.store.incarnation,),
            )
            self.store.db.execute(
                "INSERT INTO report_files VALUES ('main','p','r',1,'report.pdf',?,?,'pdf')",
                (digest, len(raw)),
            )
        self.assertEqual(self.store.gc(), 0)
        backup = self.base / "backup"
        manifest = self.store.backup(backup)
        self.assertEqual(manifest["schema"], 14)
        self.assertIn(f"blobs/main/{digest}", manifest["blobs"])
        old_history = self.store.incarnation
        self.store.close()
        self.store = None
        LocalStore.restore(self.root, self.control, backup)
        self.store = LocalStore(self.root, self.control)
        self.assertNotEqual(old_history, self.store.incarnation)
        self.assertEqual((self.root / "blobs/main" / digest).read_bytes(), raw)
        self.assertEqual(
            self.store.db.execute("SELECT blob FROM report_files").fetchone()[0], digest
        )
