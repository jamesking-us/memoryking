"""The retrieval benchmark is deterministic, labelled correctly, and passes its gate."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import retrieval_benchmark as benchmark  # noqa: E402


class BenchmarkTests(unittest.TestCase):
    def test_corpus_is_deterministic_and_labelled(self):
        docs, labelled = benchmark.corpus(3, 60, 8)
        again, labelled_again = benchmark.corpus(3, 60, 8)
        self.assertEqual(docs, again)
        self.assertEqual(labelled, labelled_again)
        self.assertEqual(len(docs), 60)
        for q, item in enumerate(labelled):
            self.assertEqual(len(item["relevant"]), 3)
            terms = item["query"].split()
            for index in item["relevant"]:
                self.assertEqual(docs[index]["query"], q)
                self.assertGreaterEqual(sum(t in docs[index]["text"].split() for t in terms), 2)
        self.assertEqual(sum(d["role"] == "noise" for d in docs), 60 - 8 * 5)

    def test_reduced_run_passes_the_release_gate(self):
        report = benchmark.run(17, 90, 15, 8)
        thresholds = json.loads(benchmark.GATE.read_text())
        self.assertEqual(benchmark.gate(report, thresholds), [], report)
        self.assertEqual(report["recall_mean"], 1.0)
        self.assertEqual(report["mrr"], 1.0)
        self.assertEqual(
            benchmark.gate({**report, "recall_mean": 0.5}, thresholds),
            ["recall_mean 0.5 below 0.95"],
        )
