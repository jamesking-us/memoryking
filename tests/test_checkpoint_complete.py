"""Progressive checkpoint completion records every defaulted section as a gap."""

import json
import unittest
from pathlib import Path

from memoryking.domain.checkpoint import DEFAULT_SECTIONS, complete, dependencies
from memoryking.domain.local import LocalError

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/contracts/checkpoint.json"


class CompleteTests(unittest.TestCase):
    def test_minimal_body_becomes_a_full_checkpoint_with_gaps(self):
        body = complete({"objective": "o", "current_state": "s"}, 5)
        full = json.loads(FIXTURE.read_text())
        self.assertEqual(set(body), set(full))
        for section in DEFAULT_SECTIONS:
            self.assertEqual(body[section], [])
            self.assertIn("defaulted:" + section, body["capture"]["gaps"])
        self.assertEqual(body["repository"]["artifacts"], [])
        self.assertIsNone(body["repository"]["repository"])
        self.assertEqual(body["capture"]["observed_ms"], 5)
        self.assertEqual(dependencies(body), [])

    def test_supplied_sections_are_kept_and_not_reported_as_gaps(self):
        body = complete(
            {
                "objective": "o",
                "current_state": "s",
                "next_actions": ["a"],
                "capture": {"last_event": "e1", "observed_ms": 1, "gaps": ["custom"]},
            },
            9,
        )
        self.assertEqual(body["next_actions"], ["a"])
        self.assertNotIn("defaulted:next_actions", body["capture"]["gaps"])
        self.assertEqual(body["capture"]["gaps"][0], "custom")
        self.assertEqual(body["capture"]["observed_ms"], 1)

    def test_objective_and_state_are_mandatory(self):
        for bad in ({}, {"objective": "o"}, {"objective": "", "current_state": "s"}, "text"):
            with self.assertRaises(LocalError):
                complete(bad, 1)
