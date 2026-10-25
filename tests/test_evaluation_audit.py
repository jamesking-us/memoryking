"""Exercise the offline audit against real input bytes and complete cost records.

These establish evaluation workflow safeguards, never task-quality conformance.
"""

import copy
import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.evaluation_audit import ARMS, METRICS, audit, read


def fixture(directory):
    evidence = b"Earlier diagnostic observation; no solution or later outcome."
    (directory / "evidence.txt").write_bytes(evidence)
    protocol = {
        "version": 1,
        "classification": "synthetic",
        "budget_approved": False,
        "cohort": "fixed_corpus",
        "arms": list(ARMS),
        "baseline": "compact_checkpoint",
        "allocation": "equal_per_arm_once",
        "frozen_at_ms": 100,
        "identities": {
            "dataset_sha256": hashlib.sha256(b"synthetic fixture").hexdigest(),
            "source_revision": "a" * 40,
            "model_version": "no-model-synthetic-records",
            "tool_version": "audit-v1",
            "judge_version": "synthetic-explicit-outcomes",
            "tokenizer_version": "synthetic-counts",
            "resource_profile": "temporary-files-only",
            "stopping_rule": "one fixed synthetic task",
            "statistical_method": "none-no-efficacy-claim",
        },
        "tasks": [
            {
                "id": "task",
                "family": "family",
                "split": "holdout",
                "cutoff_ms": 50,
                "duplicate_group": "task-group",
                "excluded_sha256": [],
            }
        ],
        "resources": [
            {
                "id": "earlier",
                "path": "evidence.txt",
                "sha256": hashlib.sha256(evidence).hexdigest(),
                "available_at_ms": 20,
                "kind": "evidence",
                "duplicate_group": "prior-unrelated",
                "origin_arm": None,
                "origin_task": None,
            }
        ],
        "shared_processing_ids": ["ingestion"],
    }
    run = {
        "protocol_sha256": "filled when frozen",
        "started_at_ms": 200,
        "split": "holdout",
        "initial_memory": [
            {
                "task": "task",
                "arm": a,
                "namespace": "namespace-" + a,
                "resources": [] if a == "no_memory" else ["earlier"],
            }
            for a in ARMS
        ],
        "attempt_inventory": [{"id": a + "-1", "task": "task", "arm": a} for a in ARMS],
        "attempts": [
            {
                "id": a + "-1",
                "task": "task",
                "arm": a,
                "status": "succeeded",
                "cost": {m: 10 for m in METRICS},
            }
            for a in ARMS
        ],
        "shared_costs": [{"id": "ingestion", "cost": {m: 8 for m in METRICS}}],
        "task_results": [{"task": "task", "arm": a, "outcome": "success"} for a in ARMS],
    }
    return protocol, run


class EvaluationAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.protocol, self.run = fixture(self.directory)

    def freeze(self):
        path = self.directory / "protocol.json"
        path.write_text(json.dumps(self.protocol, sort_keys=True) + "\n")
        self.sha = hashlib.sha256(path.read_bytes()).hexdigest()
        self.run["protocol_sha256"] = self.sha

    def result(self, freeze=True):
        if freeze:
            self.freeze()
        (self.directory / "run.json").write_text(json.dumps(self.run) + "\n")
        return audit(self.directory, self.sha)

    def test_all_costs_once_including_failed_attempt_and_unknown_usage(self):
        extra = {"id": "failed-0", "task": "task", "arm": "full_system"}
        self.run["attempt_inventory"].append(extra)
        self.run["attempts"].append({**extra, "status": "failed", "cost": {m: 4 for m in METRICS}})
        self.run["attempts"][-1]["cost"]["tokens"] = None
        result = self.result()
        self.assertTrue(result["audit_passed"])
        self.assertEqual(result["attempts"], 5)
        self.assertEqual(result["attempt_statuses"]["failed"], 1)
        self.assertEqual(result["known_cost_lower_bounds"]["full_system"]["human_minutes"], "16")
        self.assertEqual(result["known_cost_lower_bounds"]["no_memory"]["tokens"], "12")
        self.assertIsNone(result["tokens_per_success"]["full_system"])
        self.assertFalse(result["complete_cost_accounting"])
        self.assertFalse(result["exact_savings_claim_allowed"])
        self.assertEqual(result["quality_gate"], "not_evaluated")

    def test_known_synthetic_counts_and_zero_success_never_claim_efficacy(self):
        self.run["task_results"][-1]["outcome"] = "failure"
        result = self.result()
        self.assertTrue(result["audit_passed"])
        self.assertTrue(result["complete_cost_accounting"])
        self.assertIsNone(result["tokens_per_success"]["full_system"])
        self.assertFalse(result["exact_savings_claim_allowed"])
        self.assertEqual(result["tokens_per_success"]["no_memory"], "12")

    def test_seeded_solutions_postmortems_future_and_cross_arm_outcomes_block(self):
        original = copy.deepcopy(self.protocol)
        for changes, expected in [
            ({"kind": "solution"}, "solution_or_postmortem:earlier"),
            ({"kind": "postmortem"}, "solution_or_postmortem:earlier"),
            ({"available_at_ms": 51}, "future_input:earlier"),
            ({"origin_arm": "compact_checkpoint"}, "cross_arm_outcome:earlier"),
            ({"origin_task": "other"}, "cross_arm_outcome:earlier"),
            ({"duplicate_group": "task-group"}, "solved_task_duplicate:earlier"),
        ]:
            with self.subTest(changes=changes):
                self.protocol = copy.deepcopy(original)
                self.protocol["resources"][0].update(changes)
                result = self.result()
                self.assertFalse(result["audit_passed"])
                self.assertIn(expected, result["errors"])
        self.protocol = original
        self.protocol["tasks"][0]["excluded_sha256"] = [self.protocol["resources"][0]["sha256"]]
        self.assertIn("excluded_solution_hash:earlier", self.result()["errors"])

    def test_cross_arm_namespaces_and_split_family_overlap_block(self):
        self.run["initial_memory"][-1]["namespace"] = self.run["initial_memory"][0]["namespace"]
        self.protocol["tasks"].append(
            {**self.protocol["tasks"][0], "id": "tuning", "split": "development"}
        )
        result = self.result()
        self.assertIn("cross_arm_namespace", result["errors"])
        self.assertIn("split_overlap:family", result["errors"])
        self.assertIn("split_overlap:duplicate_group", result["errors"])

    def test_frozen_hash_changed_bytes_and_symlinks_block(self):
        self.freeze()
        (self.directory / "protocol.json").write_text(json.dumps(self.protocol) + " ")
        self.assertIn("protocol_hash_mismatch", self.result(False)["errors"])
        (self.directory / "evidence.txt").write_text("changed bytes")
        self.assertIn("resource_hash:earlier", self.result()["errors"])
        (self.directory / "link.txt").symlink_to(self.directory / "evidence.txt")
        self.protocol["resources"][0]["path"] = "link.txt"
        self.assertIn("resource_path:earlier", self.result()["errors"])
        self.protocol["resources"][0]["path"] = "../outside"
        self.assertIn("resource_path:earlier", self.result()["errors"])

    def test_missing_failed_attempt_shared_duplicate_and_incomplete_pairs_block(self):
        self.run["attempts"].pop()
        self.run["shared_costs"].append(copy.deepcopy(self.run["shared_costs"][0]))
        self.run["task_results"].pop()
        result = self.result()
        self.assertIn("missing_or_duplicate_attempt_cost", result["errors"])
        self.assertIn("missing_or_duplicate_shared_cost", result["errors"])
        self.assertIn("task_result_inventory", result["errors"])
        self.assertFalse(result["complete_cost_accounting"])

    def test_invalid_cost_and_unapproved_real_task_run_block(self):
        self.protocol["classification"] = "real_task"
        self.run["attempts"][0]["cost"]["tokens"] = -1
        result = self.result()
        self.assertIn("real_task_budget_unapproved", result["errors"])
        self.assertIn("invalid_cost:no_memory-1:tokens", result["errors"])

    def test_nonfinite_duplicate_json_and_empty_cohort_refused(self):
        self.freeze()
        for body in ['{"a":1,"a":2}', '{"a":NaN}']:
            (self.directory / "bad.json").write_text(body)
            with self.assertRaises(ValueError):
                read(self.directory / "bad.json")
        self.run["split"] = "development"
        self.assertIn("empty_cohort", self.result()["errors"])
