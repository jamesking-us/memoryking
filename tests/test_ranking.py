"""Experimental ranking: frozen evidence, real shadow retrieval, rollback and privacy."""

import json
import unittest

import test_knowledge as knowledge
from recovery_helpers import backup_fixture, restore_fixture
from test_experience_records import RecordCases

from memoryking.domain.learning import adjust, freeze
from memoryking.domain.local import LocalError


class LearningTests(unittest.TestCase):
    def vote(self, actor="a", run="run", usefulness="helpful"):
        return dict(actor=actor, task="task", run=run, reference="item", usefulness=usefulness)

    def test_unknown_exposure_bounds_and_actor_weighting(self):
        self.assertEqual(freeze([self.vote(usefulness="unknown")], 100000), {})
        once = [self.vote(), self.vote(actor="b", usefulness="harmful")]
        repeated = [self.vote(run=str(i)) for i in range(100)] + [once[1]]
        self.assertEqual(freeze(once, 100000), {"item": 0})
        self.assertEqual(freeze(repeated, 100000), {"item": 0})
        self.assertEqual(freeze([self.vote()], 25000), {"item": 25000})
        with self.assertRaisesRegex(LocalError, "ranking_duplicate_vote"):
            freeze([self.vote(), self.vote()], 100000)
        with self.assertRaisesRegex(LocalError, "ranking_limit"):
            freeze([self.vote()], 100001)

    def test_adjustment_preserves_zero_relevance_and_whole_groups(self):
        group = [{"reference": "a"}, {"reference": "b"}]
        ranked = [(100, "a", group), (0, "z", [{"reference": "z"}])]
        result = adjust(ranked, {"a": 100000, "b": -100000, "z": 100000})
        self.assertEqual(result, ranked)
        self.assertIs(result[0][2], group)


class RankingCases:
    run_e = RecordCases.run_e
    run_r = RecordCases.run_r
    brief_args = RecordCases.brief_args
    feedback = RecordCases.feedback
    records = RecordCases.records

    def seed(self, readers=None):
        self.store.capture(
            self.owner,
            self.ns,
            "extra",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "extra",
                "text": self.raw,
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 2,
            },
        )
        packet = self.run_r(args=self.brief_args(top_k=2))
        self.target = packet["guidance"][1]["reference"]
        self.original = packet["guidance"][0]["reference"]
        self.vote = self.feedback(
            packet, reference=self.target, use="used", usefulness="helpful", readers=readers
        )
        self.config = self.propose_rank([self.vote["record"]])

    def propose_rank(self, votes, key="configuration", **changes):
        return self.run_e(
            "ranking_propose",
            {
                "project": "p",
                "logical_key": key,
                "feedback": votes,
                "max_adjustment_ppm": 100000,
                "policy": "bounded-feedback-v1",
                **changes,
            },
        )

    def evaluate(self, *, relevant=None, forbidden=None, key="evaluation", **changes):
        return self.run_e(
            "ranking_evaluate",
            {
                "project": "p",
                "configuration": self.config["record"],
                "logical_key": key,
                "cases": [
                    {
                        "brief": self.brief_args(top_k=1),
                        "relevant": relevant if relevant is not None else [self.target],
                        "forbidden": forbidden or [],
                    }
                ],
                "dataset_kind": "development",
                "judgment_provenance": "Synthetic declared relevant target; no held-out task study",
                **changes,
            },
        )

    def activate(self, shadow, version=0, credential=None):
        return self.run_e(
            "ranking_activate",
            {
                "project": "p",
                "configuration": self.config["record"],
                "shadow": shadow["record"],
                "expected_version": version,
                "reason": "Review_synthetic_incremental_value",
            },
            credential,
        )

    def inspect(self, credential=None):
        return self.run_e("ranking_inspect", {"project": "p"}, credential)

    def get_record(self, record):
        return self.run_e(
            "record", {"project": "p", "kind": record["kind"], "record": record["record"]}
        )

    def test_shadow_activation_rollback_and_exact_retained_replay(self):
        self.seed()
        self.assertEqual(self.inspect()["active"]["state"], "disabled")
        shadow = self.evaluate()
        body = self.get_record(shadow)["body"]
        self.assertEqual(body["status"], "passes_development_gates")
        self.assertTrue(body["measured_incremental_value"])
        self.assertFalse(body["task_quality_qualified"])
        self.assertEqual(body["cases"][0]["arms"]["baseline"]["metrics"]["recall_ppm"], 0)
        self.assertEqual(body["cases"][0]["arms"]["candidate"]["metrics"]["recall_ppm"], 1000000)
        self.activate(shadow)
        packet = self.run_r(args=self.brief_args(top_k=1))
        self.assertEqual(packet["guidance"][0]["reference"], self.target)
        diag = self.run_r("receipt", {"project": "p", "receipt": packet["receipt"]})["diagnostics"]
        self.assertEqual(diag["ranking_experiment"]["configuration"], self.config["record"])
        self.run_e(
            "ranking_rollback",
            {
                "project": "p",
                "configuration": None,
                "expected_version": 1,
                "reason": "Return_to_baseline",
            },
        )
        self.assertEqual(
            self.run_r(args=self.brief_args(top_k=1))["guidance"][0]["reference"], self.original
        )
        replay = self.run_r("replay", {"project": "p", "receipt": packet["receipt"]})
        self.assertEqual(replay, packet)
        self.run_e(
            "ranking_rollback",
            {
                "project": "p",
                "configuration": self.config["record"],
                "expected_version": 2,
                "reason": "Explicitly_restore_previously_active_configuration",
            },
        )
        self.assertEqual(self.inspect()["version"], 3)
        self.assertFalse(self.inspect()["automatic_online_learning"])

    def test_no_incremental_value_or_regression_cannot_activate(self):
        self.seed()
        for index, relevant in enumerate([[self.original], [self.original, self.target]]):
            shadow = self.evaluate(relevant=relevant, key=f"failed{index}")
            self.assertEqual(self.get_record(shadow)["body"]["status"], "fails_development_gates")
            with self.assertRaisesRegex(LocalError, "ranking_evaluation_required"):
                self.activate(shadow)
        self.assertEqual(self.inspect()["active"]["state"], "disabled")

    def test_unknown_feedback_has_no_positive_adjustment_and_cannot_pass(self):
        self.seed()
        revised = self.feedback(reference=self.target, version=1)
        self.config = self.propose_rank([revised["record"]], key="unknown")
        self.assertEqual(self.get_record(self.config)["body"]["adjustments"], {})
        shadow = self.evaluate()
        self.assertFalse(self.get_record(shadow)["body"]["measured_incremental_value"])
        with self.assertRaisesRegex(LocalError, "ranking_evaluation_required"):
            self.activate(shadow)
        with self.assertRaisesRegex(LocalError, "ranking_feedback_superseded"):
            self.propose_rank([self.vote["record"]], key="old")
        with self.assertRaisesRegex(LocalError, "invalid_request"):
            self.propose_rank([revised["record"], revised["record"]], key="duplicate")

    def test_stale_shadow_and_concurrent_version_fail(self):
        self.seed()
        shadow = self.evaluate()
        self.feedback(reference=self.target, version=1, use="used", usefulness="helpful")
        with self.assertRaisesRegex(LocalError, "ranking_shadow_stale"):
            self.activate(shadow)
        shadow = self.evaluate(key="fresh")
        self.activate(shadow)
        with self.assertRaisesRegex(LocalError, "ranking_version_conflict"):
            self.run_e(
                "ranking_rollback",
                {
                    "project": "p",
                    "configuration": None,
                    "expected_version": 0,
                    "reason": "Stale_concurrent_decision",
                },
            )

    def test_authority_and_private_feedback_do_not_change_reader_ranking(self):
        reader = self.store.grant(self.owner, "reader", "p", ["read", "review"])
        self.seed(readers=["owner"])
        shadow = self.evaluate()
        with self.assertRaisesRegex(LocalError, "denied"):
            self.activate(shadow, credential=reader)
        self.activate(shadow)
        packet = self.run_r(args=self.brief_args(top_k=1), credential=reader)
        self.assertEqual(packet["guidance"][0]["reference"], self.original)
        diag = self.run_r("receipt", {"project": "p", "receipt": packet["receipt"]}, reader)
        self.assertEqual(diag["diagnostics"]["ranking_experiment"]["state"], "unavailable")
        self.assertNotIn(self.config["record"], json.dumps(diag))
        self.assertEqual(self.records("ranking", reader)["items"], [])
        with self.assertRaisesRegex(LocalError, "denied"):
            self.inspect(reader)

    def test_restore_disables_experiment_and_old_config_cannot_be_reactivated(self):
        self.seed()
        self.activate(self.evaluate())
        archive = backup_fixture(self)
        restore_fixture(self, archive)
        self.assertIn(self.inspect()["active"]["state"], {"disabled", "reconciliation_required"})
        self.assertEqual(
            self.run_r(args=self.brief_args(top_k=1))["guidance"][0]["reference"], self.original
        )
        with self.assertRaisesRegex(LocalError, "experience_record_unavailable"):
            self.run_e(
                "ranking_rollback",
                {
                    "project": "p",
                    "configuration": self.config["record"],
                    "expected_version": self.inspect()["version"],
                    "reason": "Old_history_prohibited",
                },
            )

    def test_restore_before_configuration_retains_missing_decision_reconciliation(self):
        archive = backup_fixture(self)
        self.seed()
        self.activate(self.evaluate())
        restore_fixture(self, archive)
        self.assertEqual(self.inspect()["active"]["state"], "reconciliation_required")
        self.assertEqual(self.records("ranking")["items"], [])

    def test_purge_invalidates_config_and_retained_packet(self):
        self.seed()
        self.activate(self.evaluate())
        packet = self.run_r(args=self.brief_args(top_k=1))
        self.store.purge(self.owner, self.target)
        self.assertEqual(self.inspect()["active"]["state"], "disabled")
        with self.assertRaises(LocalError):
            self.run_r("replay", {"project": "p", "receipt": packet["receipt"]})
        self.assertEqual(self.records("ranking")["items"], [])

    def test_historical_override_does_not_silently_apply_current_experiment(self):
        self.seed()
        self.activate(self.evaluate())
        packet = self.run_r(args=self.brief_args(top_k=1, valid_ms=self.store.clock.utc_ms()))
        self.assertEqual(packet["guidance"][0]["reference"], self.original)
        diag = self.run_r("receipt", {"project": "p", "receipt": packet["receipt"]})
        self.assertEqual(
            diag["diagnostics"]["ranking_experiment"]["state"], "historical_override_disabled"
        )


class LocalRankingTests(RankingCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown
