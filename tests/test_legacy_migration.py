"""Real SQLite import/retry/authorization tests with detached synthetic legacy data."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.legacy_snapshot import inspect
from memoryking.adapters.local_store import LocalStore
from memoryking.application.legacy_import import apply_batch, make_plan, reconcile
from memoryking.domain.local import LocalError
from memoryking.migration import freeze

SCHEMA = Path("fixtures/migration/kingforge-v1-schema.sql")


def fixture(path):
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA.read_text())
        db.execute(
            "INSERT INTO plans(id,title,objective,author,created_at,updated_at) VALUES (1,'Synthetic plan','Retain constraints','legacy-root',1720000000,1720000001)"
        )
        db.execute(
            "INSERT INTO steps(id,plan_id,seq,description,updated_at) VALUES(1,1,1,'Verify before activation',1720000001)"
        )
        db.execute(
            "INSERT INTO lessons(id,plan_id,title,situation,lesson,author,created_at,embedding) VALUES(1,1,'Synthetic lesson','Interruption','Keep exact retry identity','administrator',1720000002,?)",
            (b"opaque-vector",),
        )


class DirectOperator:
    def __init__(self, store, credential):
        self.store, self.credential = store, credential

    def context(self):
        return json.loads(self.store.context(self.credential, "p").body)

    def capture(self, key, observation):
        return self.store.capture(
            self.credential, self.store.namespace(self.credential, "p"), key, observation
        )

    def knowledge(self, command):
        return json.loads(Knowledge(self.store).execute(self.credential, command).body)

    def read_legacy(self, handle):
        return json.loads(self.store.read(self.credential, "p", observation=handle).body)


class MigrationCases:
    def legacy(self):
        self.legacy_source = self.root / "legacy.sqlite"
        fixture(self.legacy_source)
        self.before_hash = hashlib.sha256(self.legacy_source.read_bytes()).hexdigest()
        self.snapshot = freeze(self.legacy_source, self.root / "migration")
        self.operator = DirectOperator(self.store, self.owner)
        self.plan = make_plan(self.snapshot, "town-test", ["kingforge-v1"], self.operator)
        self.journal = self.root / "migration/journal"
        return self.plan

    def test_repeat_import_is_private_and_authority_free(self):
        self.legacy()
        first = apply_batch(self.snapshot, self.plan, self.operator, self.journal)
        second = apply_batch(self.snapshot, self.plan, self.operator, self.journal)
        self.assertEqual(
            [x["target"] for x in first["mapping"]], [x["target"] for x in second["mapping"]]
        )
        verified = reconcile(self.snapshot, self.plan, self.operator, self.journal)
        self.assertEqual(verified["verified_rows"], 3)
        self.assertEqual(verified["verified_references"], 2)
        self.assertEqual(
            hashlib.sha256(self.legacy_source.read_bytes()).hexdigest(), self.before_hash
        )
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM families").fetchone()[0], 1)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM experience_records").fetchone()[0], 0
        )
        other = self.store.grant(self.owner, "other", "p", ["read"])
        with self.actor(other) as actor:
            read = json.loads(actor.read(other, "p", query="Synthetic").body)
        self.assertEqual(read["items"], [])
        texts = [
            self.operator.read_legacy(r["handle"])["items"][0]["text"] for r in first["mapping"]
        ]
        self.assertIn("legacy-root", "".join(texts))
        self.assertIn("untrusted_historical_evidence", "".join(texts))
        self.assertIn("opaque_hash_not_an_index", "".join(texts))
        proposals = [r["proposal"] for r in first["mapping"] if r["proposal"]]
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["state"], "proposed")
        self.assertFalse(proposals[0]["executable"])

    def test_batch_restart_and_missing_reconciliation(self):
        self.legacy()
        first = apply_batch(self.snapshot, self.plan, self.operator, self.journal, limit=1)
        self.assertEqual(first["next_start"], 1)
        self.assertFalse(first["end_reached"])
        with self.assertRaisesRegex(LocalError, "legacy_incomplete"):
            reconcile(self.snapshot, self.plan, self.operator, self.journal)
        rest = apply_batch(self.snapshot, self.plan, self.operator, self.journal, start=1, limit=2)
        self.assertTrue(rest["end_reached"])
        self.assertEqual(
            reconcile(self.snapshot, self.plan, self.operator, self.journal)["verified_rows"], 3
        )

    def test_crash_after_capture_before_journal_recovers_without_duplicate(self):
        self.legacy()
        from memoryking.application import legacy_import

        original = legacy_import.atomic

        def fail_result(path, data):
            if path.name not in {"plan.json", "last-failure.json"}:
                raise OSError("synthetic crash before journal receipt")
            return original(path, data)

        with patch.object(legacy_import, "atomic", fail_result), self.assertRaises(OSError):
            apply_batch(self.snapshot, self.plan, self.operator, self.journal)
        result = apply_batch(self.snapshot, self.plan, self.operator, self.journal)
        self.assertEqual(len(result["mapping"]), 3)
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM observations WHERE source LIKE 'legacy_%'"
            ).fetchone()[0],
            3,
        )

    def test_purge_mapping_cannot_be_resurrected_by_repeat_import(self):
        self.legacy()
        first = apply_batch(self.snapshot, self.plan, self.operator, self.journal)
        self.store.purge(self.owner, first["mapping"][0]["target"])
        with self.assertRaisesRegex(LocalError, "unavailable"):
            reconcile(self.snapshot, self.plan, self.operator, self.journal)
        with self.assertRaisesRegex(LocalError, "source_retired"):
            apply_batch(self.snapshot, self.plan, self.operator, self.journal)
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM observations WHERE source LIKE 'legacy_%'"
            ).fetchone()[0],
            3,
        )

    def test_changed_snapshot_or_target_binding_rejects_old_plan(self):
        self.legacy()
        with sqlite3.connect(self.snapshot) as db:
            db.execute("UPDATE plans SET title='changed' WHERE id=1")
        with self.assertRaisesRegex(LocalError, "legacy_plan_changed"):
            apply_batch(self.snapshot, self.plan, self.operator, self.journal)
        revised = make_plan(self.snapshot, "town-test", ["kingforge-v1"], self.operator)
        revised["binding"]["incarnation"] = "old-history"
        with self.assertRaisesRegex(LocalError, "legacy_plan_changed"):
            apply_batch(self.snapshot, revised, self.operator, self.journal)

    def test_updated_source_record_requires_reconciliation_not_overwrite(self):
        self.legacy()
        apply_batch(self.snapshot, self.plan, self.operator, self.journal)
        with sqlite3.connect(self.snapshot) as db:
            db.execute("UPDATE lessons SET lesson='replacement' WHERE id=1")
        updated = make_plan(self.snapshot, "town-test", ["kingforge-v1"], self.operator)
        with self.assertRaisesRegex(LocalError, "legacy_journal_plan_mismatch"):
            apply_batch(self.snapshot, updated, self.operator, self.journal)
        with self.assertRaisesRegex(LocalError, "source_conflict"):
            apply_batch(self.snapshot, updated, self.operator, self.root / "new-journal")

    def test_operator_revocation_denies_replay(self):
        self.legacy()
        apply_batch(self.snapshot, self.plan, self.operator, self.journal)
        self.store._policy(self.owner, {"kind": "revoke", "principal": "owner", "project": "*"})
        with self.assertRaises(LocalError):
            apply_batch(self.snapshot, self.plan, self.operator, self.journal)

    def test_restore_rejects_old_import_plan_and_preserves_later_purge(self):
        from recovery_helpers import backup_fixture, restore_fixture

        self.legacy()
        first = apply_batch(self.snapshot, self.plan, self.operator, self.journal, limit=1)
        archive = backup_fixture(self)
        apply_batch(self.snapshot, self.plan, self.operator, self.journal, start=1)
        self.store.purge(self.owner, first["mapping"][0]["target"])
        restore_fixture(self, archive)
        self.operator = DirectOperator(self.store, self.owner)
        with self.assertRaisesRegex(LocalError, "legacy_plan_changed"):
            apply_batch(self.snapshot, self.plan, self.operator, self.journal)
        replacement = make_plan(self.snapshot, "town-test", ["kingforge-v1"], self.operator)
        with self.assertRaisesRegex(LocalError, "legacy_journal_plan_mismatch"):
            apply_batch(self.snapshot, replacement, self.operator, self.journal)
        with self.assertRaisesRegex(LocalError, "source_retired"):
            apply_batch(self.snapshot, replacement, self.operator, self.root / "after-restore")

    def test_universal_active_labels_never_activate_and_retired_bodies_are_omitted(self):
        source = self.root / "universal.sqlite"
        universal_fixture(source)
        with sqlite3.connect(source) as db:
            db.execute("UPDATE lw_scopes SET lifecycle='deleted'")
        snapshot = freeze(source, self.root / "universal-import")
        operator = DirectOperator(self.store, self.owner)
        plan = make_plan(snapshot, "universal-test", ["universal"], operator)
        journal = self.root / "universal-import/journal"
        result = apply_batch(snapshot, plan, operator, journal)
        self.assertEqual(reconcile(snapshot, plan, operator, journal)["verified_rows"], 8)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM experience_records").fetchone()[0], 0
        )
        for item in result["mapping"]:
            text = operator.read_legacy(item["handle"])["items"][0]["text"]
            self.assertNotIn("Untrusted imported interpretation", text)
            self.assertNotIn("Not currently activated", text)
            if item["table"] != "lw_schema_migrations":
                self.assertTrue(item["retired"])
                self.assertIn("legacy-tombstone", text)


class LocalMigrationTests(MigrationCases, unittest.TestCase):
    @contextmanager
    def actor(self, credential):
        yield self.store

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-legacy-")
        self.root = Path(self.temp.name)
        self.data, self.control = self.root / "data", self.root / "control"
        self.owner = LocalStore.initialize(self.data, self.control).read_text()
        self.store = LocalStore(self.data, self.control)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-snapshot-")
        self.root = Path(self.temp.name)
        self.source = self.root / "legacy.sqlite"
        fixture(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def test_unknown_table_and_orphan_fail_closed_without_source_mutation(self):
        with sqlite3.connect(self.source) as db:
            db.execute("CREATE TABLE credentials(id INTEGER PRIMARY KEY, secret TEXT)")
            db.execute("INSERT INTO credentials VALUES(1,'must not be imported')")
            db.execute("UPDATE steps SET plan_id=99")
        before = self.source.read_bytes()
        report = inspect(self.source, "town", expected_profiles=["kingforge-v1"])
        self.assertEqual(
            {x["code"] for x in report["unresolved"]}, {"unsupported_table", "unresolved_reference"}
        )
        self.assertNotIn("must not be imported", json.dumps(report))
        self.assertEqual(self.source.read_bytes(), before)

    def test_live_sidecar_and_symlink_are_rejected(self):
        wal = Path(str(self.source) + "-wal")
        wal.touch()
        with self.assertRaisesRegex(LocalError, "not_detached"):
            freeze(self.source, self.root / "copy")
        wal.unlink()
        link = self.root / "linked.sqlite"
        link.symlink_to(self.source)
        with self.assertRaises(LocalError):
            inspect(link, "town", expected_profiles=["kingforge-v1"])

    def test_profile_mismatch_is_explicit_and_retirement_propagates(self):
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE plans SET status='deleted'")
        result = inspect(self.source, "town", expected_profiles=["universal"])
        self.assertIn("profile_mismatch", [x["code"] for x in result["unresolved"]])
        self.assertTrue(all(row["retired"] for row in result["rows"]))

    def test_null_primary_key_or_missing_stable_key_blocks(self):
        with sqlite3.connect(self.source) as db:
            db.execute("CREATE TABLE lw_links(subject_type TEXT)")
        result = inspect(self.source, "town", expected_profiles=["kingforge-v1"])
        self.assertIn("missing_stable_key", [x["code"] for x in result["unresolved"]])


def universal_fixture(path, *, mixed=False):
    if mixed:
        fixture(path)
    with sqlite3.connect(path) as db:
        db.executescript(Path("fixtures/migration/universal-schema.sql").read_text())
        db.execute("INSERT INTO lw_schema_migrations VALUES(1,'synthetic','2026-09-19T00:00:00Z')")
        db.execute(
            "INSERT INTO lw_scopes(id,kind,external_key,name,created_at,updated_at) VALUES(1,'project','legacy-project','Synthetic','2026-09-19','2026-09-19')"
        )
        body = "Synthetic legacy evidence"
        db.execute(
            "INSERT INTO lw_evidence(id,scope_id,kind,title,content,content_hash,recorded_at) VALUES(1,1,'review','Legacy review',?,?,'2026-09-19')",
            (body, hashlib.sha256(body.encode()).hexdigest().upper()),
        )
        db.execute(
            "INSERT INTO lw_memories(id,scope_id,memory_key,revision,type,title,body,lifecycle,recorded_at,created_by) VALUES(1,1,'sample',1,'semantic','Synthetic','Untrusted imported interpretation','verified','2026-09-19','root')"
        )
        db.execute("INSERT INTO lw_memory_evidence VALUES(1,1,'2026-09-19','root')")
        db.execute(
            "INSERT INTO lw_procedures(id,scope_id,procedure_key,revision,kind,title,summary,steps_json,applicability_json,tests_json,lifecycle,created_at) VALUES(1,1,'restart',1,'recovery','Legacy active','Not currently activated','[]','{}','[]','active','2026-09-19')"
        )
        db.execute("INSERT INTO lw_procedure_evidence VALUES(1,1,'2026-09-19','root')")
        db.execute(
            "INSERT INTO lw_links(subject_type,subject_id,relation,object_type,object_id,created_at) VALUES('memory',1,'derived_from','evidence',1,'2026-09-19')"
        )


class UniversalSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-universal-")
        self.root = Path(self.temp.name)
        self.source = self.root / "legacy.sqlite"

    def tearDown(self):
        self.temp.cleanup()

    def test_universal_ddl_hashes_relationships_and_retired_closure(self):
        universal_fixture(self.source)
        report = inspect(self.source, "universal", expected_profiles=["universal"])
        self.assertEqual(report["unresolved"], [])
        self.assertEqual(sum(report["tables"].values()), 8)
        self.assertTrue(any(len(r["references"]) == 2 for r in report["rows"]))
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE lw_scopes SET lifecycle='deleted'")
        retired = inspect(self.source, "universal", expected_profiles=["universal"])
        self.assertTrue(
            all(row["retired"] for row in retired["rows"] if row["table"] != "lw_schema_migrations")
        )

    def test_unknown_typed_link_and_hash_mismatch_block(self):
        universal_fixture(self.source)
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE lw_links SET object_type='external-secret'")
            db.execute("UPDATE lw_evidence SET content='tampered'")
        report = inspect(self.source, "universal", expected_profiles=["universal"])
        self.assertEqual(
            {r["code"] for r in report["unresolved"]},
            {"content_hash_mismatch", "unresolved_typed_link"},
        )

    def test_coexisting_profiles_must_be_declared(self):
        universal_fixture(self.source, mixed=True)
        report = inspect(self.source, "mixed", expected_profiles=["kingforge-v1"])
        self.assertIn("profile_mismatch", [r["code"] for r in report["unresolved"]])
        report = inspect(self.source, "mixed", expected_profiles=["kingforge-v1", "universal"])
        self.assertEqual(report["unresolved"], [])

    def test_standalone_document_lesson_and_chunk_links(self):
        with sqlite3.connect(self.source) as db:
            db.executescript(Path("fixtures/migration/standalone-schema.sql").read_text())
            content = "Synthetic postmortem"
            db.execute(
                "INSERT INTO documents(id,kind,feature,title,content,content_hash,created_at,updated_at) VALUES(1,'postmortem','feature','Title',?,?,'2026-09-19','2026-09-19')",
                (content, hashlib.sha256(content.encode()).hexdigest()),
            )
            db.execute(
                "INSERT INTO chunks(id,document_id,seq,content) VALUES(1,1,0,'Synthetic chunk')"
            )
            db.execute(
                "INSERT INTO lessons(title,body,created_at,updated_at) VALUES('Lesson','Synthetic lesson','2026-09-19','2026-09-19')"
            )
        report = inspect(self.source, "standalone", expected_profiles=["standalone"])
        self.assertEqual(report["unresolved"], [])
        self.assertEqual(len(report["rows"]), 3)
        self.assertEqual(len(report["rows"][0]["references"]), 1)
