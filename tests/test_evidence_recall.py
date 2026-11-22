"""The judge-free evidence-recall harness, its gate, its sample and its comparison mode."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TINY = ROOT / "fixtures/evaluation/longmemeval-sample.json"
REGRESSION = ROOT / "fixtures/evaluation/longmemeval-regression-sample.json"
GATE = ROOT / "fixtures/evaluation/evidence-recall-gate.json"


class EvidenceRecallTests(unittest.TestCase):
    def test_tiny_sample_scores_perfectly(self):
        from tools.evidence_recall import gate, load, run, session_ms

        self.assertEqual(session_ms("2026/03/01 (Sun) 09:00"), 1772355600000)
        self.assertEqual(session_ms("2026-03-01"), 1772323200000)
        questions = load(TINY, None, None)
        report = run(questions, top_k=8, pool=True, as_of=True, budget=6000)
        self.assertEqual(report["questions"], 3)
        self.assertEqual(report["observations"], 12)
        for metric in (
            "session_recall_at_k",
            "answer_turn_recall_at_k",
            "session_mrr",
            "session_recall_any_at_5",
            "session_recall_all_at_10",
            "turn_recall_any_at_5",
            "turn_recall_all_at_10",
            "session_mrr_distinct",
            "turn_mrr",
        ):
            self.assertEqual(report[metric], 1.0, metric)
        self.assertEqual(
            sorted(report["by_question_type"]), ["knowledge-update", "single-session-user"]
        )
        self.assertEqual(report["by_question_type"]["single-session-user"]["questions"], 2)
        self.assertEqual(gate(report, {"minimum": {"turn_mrr": 1.0}}), [])
        self.assertEqual(
            gate({"session_recall_at_k": 0.5}, {"minimum": {"session_recall_at_k": 1.0}}),
            ["session_recall_at_k 0.5 below 1.0"],
        )
        self.assertEqual(
            gate(
                {"by_question_type": {"a": {"m": 0.4}}},
                {"by_question_type_minimum": {"m": {"a": 0.5, "b": 0.5}}},
            ),
            ["a m 0.4 below 0.5", "b m None below 0.5"],
        )
        unpooled = run(
            load(TINY, 1, {"knowledge-update"}),
            top_k=8,
            pool=False,
            as_of=False,
            budget=6000,
        )
        self.assertEqual(unpooled["questions"], 1)
        self.assertEqual(unpooled["session_recall_at_k"], 1.0)
        self.assertEqual(unpooled["pooled"], False)

    def test_metrics_count_distinct_sessions_and_own_answer_turns(self):
        from tools.evidence_recall import distinct, scores

        self.assertEqual(distinct(["a", "b", "a", "c"]), ["a", "b", "c"])
        got = scores(["x", "g1", "y", "z", "w", "v", "g2"], {"g1", "g2"})
        self.assertEqual(got["any_at_5"], 1.0)
        self.assertEqual(got["all_at_5"], 0.0)
        self.assertEqual(got["all_at_10"], 1.0)
        self.assertEqual(got["mrr"], 0.5)
        self.assertEqual(scores(["x"], {"g"})["mrr"], 0.0)
        self.assertEqual(scores(["x"], set())["all_at_5"], 0.0)

    def test_regression_sample_is_large_stratified_and_gated(self):
        data = json.loads(REGRESSION.read_text(encoding="utf-8"))
        kinds: dict[str, int] = {}
        for item in data:
            kinds[item["question_type"]] = kinds.get(item["question_type"], 0) + 1
            self.assertLessEqual(set(item["answer_session_ids"]), set(item["haystack_session_ids"]))
            self.assertFalse(item["question_id"].endswith("_abs"))
        self.assertGreaterEqual(len(data), 60)
        self.assertEqual(len(kinds), 6)
        self.assertGreaterEqual(min(kinds.values()), 10)
        gate = json.loads(GATE.read_text(encoding="utf-8"))
        self.assertEqual(gate["sample"], REGRESSION.name)
        self.assertEqual(
            set(gate["by_question_type_minimum"]["session_recall_any_at_10"]), set(kinds)
        )

    def test_sample_builder_is_deterministic_and_stratified(self):
        from tools.build_recall_sample import select

        data = json.loads(REGRESSION.read_text(encoding="utf-8"))
        again = select(data, 12)
        self.assertEqual(
            [d["question_id"] for d in again], [d["question_id"] for d in select(again, 12)]
        )
        self.assertEqual(len(select(data, 2)), 12)

    def test_compare_flags_drops_beyond_tolerance_only(self):
        from tools.evidence_recall import HEADLINE, comparable, compare

        kind = {
            "questions": 10,
            "session_recall_at_k": 0.8,
            "session_recall_any_at_5": 0.8,
            "session_recall_any_at_10": 0.9,
            "turn_recall_any_at_5": 0.7,
            "turn_recall_any_at_10": 0.8,
            "session_mrr_distinct": 0.6,
            "turn_mrr": 0.5,
        }
        base = {m: 0.8 for m in HEADLINE}
        base.update(
            by_question_type={"a": kind, "b": dict(kind)},
            per_question={"q1": {"type": "a", "session_rank": 2, "turn_rank": 3}},
            pooled=True,
            as_of_question_date=True,
            top_k=8,
            depth=32,
            budget=65536,
            questions=2,
            reranker=None,
            data={"sha256": "x"},
        )
        same = copy.deepcopy(base)
        lines, regressions = compare(base, same, tolerance=0.02, type_tolerance=0.1)
        self.assertEqual(regressions, [])
        self.assertTrue(any("no change" in line or "+0.0000" in line for line in lines))
        worse = copy.deepcopy(base)
        worse["session_recall_any_at_5"] = 0.77
        worse["session_mrr_distinct"] = 0.79  # within tolerance
        worse["by_question_type"]["b"]["turn_mrr"] = 0.35
        worse["per_question"]["q1"]["session_rank"] = 7
        lines, regressions = compare(base, worse, tolerance=0.02, type_tolerance=0.1)
        self.assertEqual(len(regressions), 2, regressions)
        self.assertIn("session_recall_any_at_5", regressions[0])
        self.assertTrue(regressions[1].startswith("b: "))
        self.assertTrue(any("left the top 5 sessions: 1" in line for line in lines))
        better = copy.deepcopy(base)
        better["session_recall_any_at_5"] = 0.95
        self.assertEqual(compare(base, better, tolerance=0.0, type_tolerance=0.0)[1], [])
        self.assertEqual(comparable(base, same), [])
        other = copy.deepcopy(base)
        other["pooled"] = False
        other["data"] = {"sha256": "y"}
        self.assertEqual(len(comparable(base, other)), 2)

    def test_cli_gate_and_compare_exit_codes(self):
        with tempfile.TemporaryDirectory(prefix="mk-evidence-", dir="/tmp") as directory:
            output = Path(directory) / "report.json"
            gate = Path(directory) / "gate.json"
            gate.write_text(json.dumps({"minimum": {"session_recall_any_at_5": 1.0}}))
            command = [sys.executable, "tools/evidence_recall.py"]
            first = subprocess.run(
                [*command, "--data", str(TINY), "--output", str(output), "--gate"]
                + ["--gate-file", str(gate)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["failures"], [])
            self.assertEqual(len(report["data"]["sha256"]), 64)
            self.assertNotIn("/", report["data"]["path"])
            self.assertIn("judge_free", report["protocol"])
            gate.write_text(json.dumps({"minimum": {"session_recall_any_at_5": 1.1}}))
            failing = subprocess.run(
                [*command, "--data", str(TINY), "--output", str(output), "--gate"]
                + ["--gate-file", str(gate)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(failing.returncode, 1)
            # Compare the saved report with itself, with a worse copy, and with a foreign one.
            base = Path(directory) / "base.json"
            base.write_text(output.read_text(encoding="utf-8"))
            same = subprocess.run(
                [*command, "--current", str(output), "--compare", str(base)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(same.returncode, 0, same.stdout + same.stderr)
            self.assertIn("no regression", same.stdout)
            worse = json.loads(output.read_text(encoding="utf-8"))
            worse["session_recall_any_at_5"] = 0.5
            current = Path(directory) / "current.json"
            current.write_text(json.dumps(worse))
            regressed = subprocess.run(
                [*command, "--current", str(current), "--compare", str(base)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(regressed.returncode, 1)
            self.assertIn("REGRESSION", regressed.stdout)
            worse["data"]["sha256"] = "0" * 64
            current.write_text(json.dumps(worse))
            foreign = subprocess.run(
                [*command, "--current", str(current), "--compare", str(base)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(foreign.returncode, 2)
            self.assertIn("not comparable", foreign.stderr)


if __name__ == "__main__":
    unittest.main()
