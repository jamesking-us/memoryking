"""The judge-free evidence-recall harness on the committed sample, and its gate."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class EvidenceRecallTests(unittest.TestCase):
    def test_sample_scores_perfectly_and_the_gate_passes(self):
        from tools.evidence_recall import gate, load, run, session_ms

        self.assertEqual(session_ms("2026/03/01 (Sun) 09:00"), 1772355600000)
        self.assertEqual(session_ms("2026-03-01"), 1772323200000)
        questions = load(ROOT / "fixtures/evaluation/longmemeval-sample.json", None, None)
        report = run(questions, top_k=8, pool=True, as_of=True, budget=6000)
        self.assertEqual(report["questions"], 3)
        self.assertEqual(report["observations"], 12)
        self.assertEqual(report["session_recall_at_k"], 1.0)
        self.assertEqual(report["answer_turn_recall_at_k"], 1.0)
        self.assertEqual(report["session_mrr"], 1.0)
        self.assertEqual(
            sorted(report["by_question_type"]), ["knowledge-update", "single-session-user"]
        )
        self.assertEqual(
            gate(
                report,
                json.loads((ROOT / "fixtures/evaluation/evidence-recall-gate.json").read_text()),
            ),
            [],
        )
        self.assertEqual(
            gate({"session_recall_at_k": 0.5}, {"minimum": {"session_recall_at_k": 1.0}}),
            ["session_recall_at_k 0.5 below 1.0"],
        )
        unpooled = run(
            load(ROOT / "fixtures/evaluation/longmemeval-sample.json", 1, {"knowledge-update"}),
            top_k=8,
            pool=False,
            as_of=False,
            budget=6000,
        )
        self.assertEqual(unpooled["questions"], 1)
        self.assertEqual(unpooled["session_recall_at_k"], 1.0)

    def test_cli_writes_a_report_with_the_data_hash(self):
        with tempfile.TemporaryDirectory(prefix="mk-evidence-", dir="/tmp") as directory:
            output = Path(directory) / "report.json"
            run = subprocess.run(
                [
                    sys.executable,
                    "tools/evidence_recall.py",
                    "--data",
                    "fixtures/evaluation/longmemeval-sample.json",
                    "--output",
                    str(output),
                    "--gate",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report["failures"], [])
            self.assertEqual(len(report["data"]["sha256"]), 64)
            self.assertIn("judge_free", report["protocol"])
