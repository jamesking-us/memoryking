"""The stale-versus-current benchmark: its scoring, its gate and a small real run."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def item(text, authority="observed_episode"):
    return {"text": text, "authority": authority}


class StaleFactsTests(unittest.TestCase):
    def test_scoring_reads_values_and_ranks(self):
        from tools.stale_facts_benchmark import score

        case = {"mechanism": "capture", "old": "kaka", "new": "toto"}
        newest_first = score(case, [item("is toto."), item("is kaka.")])
        self.assertTrue(newest_first["current_first"] and newest_first["current_top1"])
        self.assertTrue(newest_first["stale_returned"])
        self.assertFalse(newest_first["stale_first"])
        stale_first = score(case, [item("is kaka."), item("is toto.")])
        self.assertFalse(stale_first["current_first"])
        self.assertTrue(stale_first["stale_first"])
        self.assertEqual((stale_first["current_rank"], stale_first["stale_rank"]), (2, 1))
        only_current = score(case, [item("is toto.")])
        self.assertTrue(only_current["current_first"] and not only_current["stale_returned"])
        missing = score(case, [item("unrelated")])
        self.assertFalse(missing["current_returned"] or missing["stale_first"])
        # A claim case looks at accepted claims only; the evidence observation is not scored.
        claim = {"mechanism": "claim", "old": "kaka", "new": "toto"}
        got = score(claim, [item("is kaka."), item("is toto.", "accepted_knowledge")])
        self.assertTrue(got["current_first"] and not got["stale_returned"])

    def test_gate_reads_scopes_and_both_bounds(self):
        from tools.stale_facts_benchmark import gate

        report = {
            "overall": {"current_first_rate": 0.5, "stale_first_rate": 0.4},
            "by_mechanism": {"claim": {"current_returned_rate": 1.0}},
            "by_variant": {"capture/plain": {"current_first_rate": 0.2}},
        }
        thresholds = {
            "minimum": {
                "overall": {"current_first_rate": 0.5},
                "claim": {"current_returned_rate": 1.0},
                "capture/plain": {"current_first_rate": 0.9},
            },
            "maximum": {"overall": {"stale_first_rate": 0.3}},
        }
        self.assertEqual(
            gate(report, thresholds),
            [
                "capture/plain current_first_rate 0.2 below 0.9",
                "overall stale_first_rate 0.4 above 0.3",
            ],
        )

    def test_small_run_uses_the_three_correction_routes(self):
        from tools.stale_facts_benchmark import VARIANTS, run

        report = run(1)
        committed = json.loads((ROOT / "fixtures/evaluation/stale-facts-gate.json").read_text())
        known = {"overall", *report["by_mechanism"], *report["by_variant"]}
        for bounds in (committed["minimum"], committed["maximum"]):
            self.assertLessEqual(set(bounds), known)
        self.assertEqual(report["overall"]["cases"], sum(len(v) for v in VARIANTS.values()))
        self.assertEqual(sorted(report["by_mechanism"]), ["capture", "claim", "memory_file"])
        # Every current statement is found; a same-sentence correction and a file edit lead.
        self.assertEqual(report["overall"]["current_returned_rate"], 1.0)
        self.assertEqual(report["by_variant"]["capture/plain"]["current_first_rate"], 1.0)
        self.assertEqual(report["by_mechanism"]["memory_file"]["current_first_rate"], 1.0)
        self.assertEqual(
            len(report["failing_cases"]),
            report["overall"]["cases"]
            - round(report["overall"]["current_first_rate"] * report["overall"]["cases"]),
        )


if __name__ == "__main__":
    unittest.main()
