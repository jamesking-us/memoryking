"""The scale regression tool: a small real run, its budget check and its committed budgets."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ScaleRegressionTests(unittest.TestCase):
    def test_small_run_measures_every_step_and_restores_durability(self):
        import os

        from memoryking.adapters import local_files
        from tools.scale_regression import DEADLINE_MS, check, flatten, measure, requests

        fsync, fcntl = os.fsync, local_files.fcntl
        report = measure(260, progress=False)
        self.assertIs(os.fsync, fsync)
        self.assertIs(local_files.fcntl, fcntl)
        self.assertEqual(report["observations"], 260)
        self.assertEqual(report["drain"]["jobs"], 200)
        self.assertEqual(report["lexical_state"], "current")
        figures = flatten(report)
        self.assertEqual(len(figures), 10)
        self.assertTrue(all(value >= 0 for value in figures.values()))
        self.assertTrue(all(value < DEADLINE_MS for value in requests(report).values()))
        generous = {"sizes": {"260": {name: 10**6 for name in figures}}}
        self.assertEqual(check(report, generous), [])
        tight = {"sizes": {"260": {name: 0 for name in figures}}}
        self.assertEqual(len(check(report, tight)), sum(1 for v in figures.values() if v > 0))
        self.assertEqual(check(report, {}), ["no budget recorded for 260 observations"])
        slow = json.loads(json.dumps(report))
        slow["brief_3_terms_ms"]["max"] = DEADLINE_MS
        self.assertIn("brief (3 terms) took", check(slow, generous)[0])

    def test_committed_budgets_cover_both_sizes_and_every_figure(self):
        budgets = json.loads((ROOT / "fixtures/evaluation/scale-budgets.json").read_text())
        names = {
            "capture_ms_p95",
            "drain_ms_per_job",
            "brief_3_terms_ms_p50",
            "brief_3_terms_ms_p95",
            "brief_20_terms_ms_p50",
            "brief_20_terms_ms_p95",
            "status_ms_p95",
            "reindex_total_seconds",
            "backup_seconds",
            "restore_seconds",
        }
        self.assertEqual(sorted(budgets["sizes"]), ["100000", "20000"])
        for limits in budgets["sizes"].values():
            self.assertEqual(set(limits), names)


if __name__ == "__main__":
    unittest.main()
