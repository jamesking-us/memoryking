"""The extractive summary is deterministic, bounded, and quotes only the checkpoint."""

import json
import unittest
from pathlib import Path

from memoryking.domain.summary import SUMMARY_LIMIT, compose

ROOT = Path(__file__).resolve().parents[1]


def checkpoint():
    return json.loads((ROOT / "fixtures/contracts/checkpoint.json").read_text())


class SummaryTests(unittest.TestCase):
    def test_three_sentences_from_a_full_checkpoint(self):
        body = checkpoint()
        observations = [
            {"id": "obs_1", "source": "pytest"},
            {"id": "obs_2", "source": "git"},
            {"id": "obs_3", "source": "pytest"},
        ]
        operations = [{"reconciliation_required": True}, {"reconciliation_required": False}]
        text = compose(body, observations, operations)
        self.assertEqual(text, compose(body, observations, operations))
        self.assertIn(body["objective"][:40], text)
        self.assertIn(body["current_state"][:40], text)
        self.assertIn("3 observations recorded since the checkpoint (from git, pytest)", text)
        self.assertIn("1 uncertain operation to reconcile", text)
        if body["next_actions"]:
            self.assertIn("Next: " + body["next_actions"][0][:40], text)
        if body["failed_attempts"]:
            self.assertIn("do not repeat", text)
        self.assertLessEqual(len(text), SUMMARY_LIMIT)
        # Observation text is never quoted; only counts and sources appear.
        self.assertNotIn("obs_1", text)

    def test_empty_checkpoint_still_reads_as_prose(self):
        text = compose({"objective": "Fix it"}, [], [])
        self.assertEqual(
            text,
            "Working on Fix it. Nothing has been tried or recorded yet. No next action was recorded.",
        )

    def test_long_fields_are_clipped_at_word_boundaries_within_the_limit(self):
        body = {
            "objective": "word " * 200,
            "current_state": "state " * 400,
            "next_actions": ["step " * 300],
            "findings": [{"text": "finding " * 100}] * 5,
            "blockers": [{"text": "blocked " * 100}],
        }
        text = compose(body, [{"id": f"o{i}", "source": f"s{i}"} for i in range(9)], [])
        self.assertLessEqual(len(text), SUMMARY_LIMIT)
        self.assertNotIn("  ", text)
        self.assertIn("from s0, s1, s2, s3, …", text)
        self.assertIn("5 findings", text)
        self.assertTrue(text.endswith(("…", ".")))


if __name__ == "__main__":
    unittest.main()
