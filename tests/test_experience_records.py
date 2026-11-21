"""Outcome uncertainty and receipt-derived feedback against actual storage."""

import unittest

import test_knowledge as knowledge
from recovery_helpers import backup_fixture, restore_fixture
from test_experience import ProcedureCases

from memoryking.domain.local import LocalError


class RecordCases:
    run_e = ProcedureCases.run_e
    run_r = ProcedureCases.run_r
    brief_args = ProcedureCases.brief_args

    def outcome(self, version=0, key="run", **changes):
        body = {
            "task": "task",
            "run": "run",
            "result": "unknown",
            "acceptance_evidence": [],
            "failures": [],
            "rework": [],
            "elapsed_ms": None,
            "input_tokens": None,
            "cached_input_tokens": None,
            "output_tokens": None,
            "limitations": ["Integration did not observe success or usage"],
            "readers": None,
            **changes,
        }
        return self.run_e(
            "outcome",
            {"project": "p", "logical_key": key, "expected_revision": version, "body": body},
        )

    def feedback(self, brief=None, version=0, **changes):
        brief = brief or self.run_r()
        return self.run_e(
            "feedback",
            {
                "project": "p",
                "receipt": brief["receipt"],
                "reference": self.obs["observation"],
                "task": "task",
                "run": "run",
                "expected_revision": version,
                "use": "unknown",
                "usefulness": "unknown",
                "comment": None,
                "readers": None,
                **changes,
            },
        )

    def tenant_id(self):
        return self.store.db.execute("SELECT tenant FROM tenants").fetchone()[0]

    def fabricate_records(self, keys, revisions, prefix="fab"):
        """Insert `keys` feedback records of `revisions` revisions each (direct rows, not saves)."""
        tenant = self.tenant_id()
        position = self.store.db.execute(
            "SELECT min(position) FROM events WHERE tenant=?", (tenant,)
        ).fetchone()[0]
        with self.store.transaction():
            for number in range(keys):
                for revision in range(1, revisions + 1):
                    self.store.db.execute(
                        "INSERT INTO experience_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            tenant,
                            f"exp_{prefix}{number}_{revision}",
                            "p",
                            "owner",
                            self.store.incarnation,
                            "feedback",
                            f"{prefix}{number}",
                            revision,
                            "{}",
                            "h" * 64,
                            "[]",
                            "null",
                            position,
                        ),
                    )

    def experience_capacity(self):
        return self.store.status(self.owner, "p")["capacity"]["experience_records"]

    def records(self, kind, credential=None):
        return self.run_e("records", {"project": "p", "kind": kind}, credential)

    def test_unknown_outcome_stays_unknown_and_success_requires_evidence(self):
        self.outcome()
        body = self.records("outcome")["items"][0]["body"]
        self.assertEqual(body["result"], "unknown")
        self.assertIsNone(body["input_tokens"])
        self.assertEqual(body["assurance"], "actor_reported")
        with self.assertRaisesRegex(LocalError, "acceptance_evidence_required"):
            self.outcome(1, result="success")
        result = self.outcome(
            1,
            result="success",
            acceptance_evidence=[self.span],
            failures=["First attempt failed"],
            rework=["Retried after reconciliation"],
            elapsed_ms=23,
            input_tokens=90,
            cached_input_tokens=50,
        )
        self.assertEqual(result["revision"], 2)
        latest = self.records("outcome")["items"]
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["body"]["failures"], ["First attempt failed"])
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM experience_records WHERE kind='outcome'"
            ).fetchone()[0],
            2,
        )
        with self.assertRaisesRegex(LocalError, "invalid_usage"):
            self.outcome(2, input_tokens=1, cached_input_tokens=2)

    def test_feedback_proves_retrieval_only_and_deduplicates_repeated_exposure(self):
        first = self.feedback()
        latest = self.records("feedback")["items"][0]
        self.assertEqual(latest["body"]["retrieved"], "receipt_verified")
        self.assertEqual(latest["body"]["usefulness"], "unknown")
        self.assertEqual(latest["body"]["use_assurance"], "actor_reported")
        self.assertFalse(latest["body"]["causal_claim"])
        with self.assertRaisesRegex(LocalError, "experience_revision_conflict"):
            self.feedback()
        second = self.feedback(version=first["revision"], use="used", usefulness="helpful")
        self.assertEqual(second["revision"], 2)
        self.assertEqual(len(self.records("feedback")["items"]), 1)
        with self.assertRaisesRegex(LocalError, "feedback_use_required"):
            self.feedback(version=2, usefulness="helpful")
        with self.assertRaisesRegex(LocalError, "feedback_reference_not_retrieved"):
            self.feedback(reference="unretrieved")

    def test_feedback_cannot_borrow_another_principals_receipt(self):
        brief = self.run_r()
        writer = self.store.grant(self.owner, "writer", "p", ["read", "capture"])
        with self.assertRaisesRegex(LocalError, "replay_missing"):
            self.run_e(
                "feedback",
                {
                    "project": "p",
                    "receipt": brief["receipt"],
                    "reference": self.obs["observation"],
                    "task": "task",
                    "run": "run",
                    "expected_revision": 0,
                    "use": "used",
                    "usefulness": "helpful",
                    "comment": None,
                    "readers": None,
                },
                writer,
            )

    def test_current_audience_dependencies_and_purge_gate_records(self):
        self.outcome(result="success", acceptance_evidence=[self.span], readers=["owner"])
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        self.assertEqual(self.records("outcome", reader)["items"], [])
        self.feedback()
        self.assertEqual(len(self.records("feedback", reader)["items"]), 1)
        self.store.purge(self.owner, self.obs["observation"])
        self.assertEqual(self.records("outcome")["items"], [])
        self.assertEqual(self.records("feedback")["items"], [])
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM experience_records WHERE body IS NOT NULL"
            ).fetchone()[0],
            0,
        )

    def test_restored_feedback_cannot_seed_the_replacement_history(self):
        self.feedback(use="used", usefulness="helpful")
        archive = backup_fixture(self)
        prior = self.store.incarnation
        restore_fixture(self, archive)
        self.assertNotEqual(prior, self.store.incarnation)
        self.assertEqual(self.records("feedback")["items"], [])
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM experience_records WHERE kind='feedback'"
            ).fetchone()[0],
            1,
        )

    def test_superseded_revisions_do_not_count_and_old_ones_are_released(self):
        for version in range(12):
            self.outcome(version)
        self.assertEqual(self.experience_capacity()["used"], 1)
        count = "SELECT count(*),min(revision),max(revision) FROM experience_records"
        self.assertEqual(tuple(self.store.db.execute(count).fetchone()), (12, 1, 12))
        self.store.expire_results()
        self.assertEqual(tuple(self.store.db.execute(count).fetchone()), (8, 5, 12))
        # The consumed revision numbers stay consumed.
        with self.assertRaisesRegex(LocalError, "experience_revision_conflict"):
            self.outcome(0)
        self.assertEqual(self.outcome(12)["revision"], 13)

    def test_only_live_records_count_toward_the_bound(self):
        self.fabricate_records(6000, 2)  # 12,000 rows, past the old 10,000 refusal point
        self.assertEqual(self.experience_capacity()["used"], 6000)
        self.assertEqual(self.outcome()["revision"], 1)
        self.fabricate_records(3999, 1, prefix="more")
        full = self.experience_capacity()
        self.assertEqual((full["used"], full["limit"], full["warning"]), (10000, 10000, True))
        with self.assertRaisesRegex(LocalError, "experience_capacity"):
            self.outcome(key="another")
        # A new revision of a record that still has its body adds nothing, so it fits.
        self.assertEqual(self.outcome(1)["revision"], 2)
        self.assertEqual(self.experience_capacity()["used"], 10000)

    def test_a_purged_record_frees_its_slot_but_keeps_its_revision_number(self):
        self.outcome(result="success", acceptance_evidence=[self.span], readers=["owner"])
        self.assertEqual(self.experience_capacity()["used"], 1)
        self.store.purge(self.owner, self.obs["observation"])
        self.assertEqual(self.experience_capacity()["used"], 0)
        self.store.expire_results()
        # The tombstone of the latest revision stays, so revision 1 cannot be handed out again.
        with self.assertRaisesRegex(LocalError, "experience_revision_conflict"):
            self.outcome(0)


class LocalRecordTests(RecordCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown
