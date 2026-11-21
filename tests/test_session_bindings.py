"""Session bindings: formats, validation against the selector vocabulary, precedence."""

import os
import tempfile
import unittest
from pathlib import Path

from memoryking import profiles, session_bindings
from memoryking.domain.local import LocalError
from memoryking.domain.selectors import BINDING_KEYS


class SessionBindingTests(unittest.TestCase):
    def test_pairs_and_environment_formats(self):
        self.assertEqual(
            session_bindings.parse_pairs(["environment=prod", "role = implement"]),
            {"environment": "prod", "role": "implement"},
        )
        self.assertEqual(
            session_bindings.parse_environment("environment=prod,role=implement"),
            {"environment": "prod", "role": "implement"},
        )
        self.assertEqual(
            session_bindings.parse_environment('{"path": "src/a,b.py", "repository_id": "r"}'),
            {"path": "src/a,b.py", "repository_id": "r"},
        )
        self.assertEqual(session_bindings.parse_environment("  "), {})

    def test_keys_are_the_selector_vocabulary_and_nothing_else(self):
        self.assertEqual(set(session_bindings.KEYS), BINDING_KEYS - {"dependencies"})
        for bad in ("colour=blue", "environment", "=prod", "environment=", "dependencies=lib"):
            with self.assertRaises(LocalError, msg=bad) as caught:
                session_bindings.parse_pairs([bad])
            self.assertEqual(caught.exception.code, "invalid_binding")
        with self.assertRaises(LocalError):
            session_bindings.parse_environment("{not json")
        with self.assertRaises(LocalError):
            session_bindings.parse_pairs(["environment=" + "x" * 300])

    def test_precedence_is_profile_then_environment_then_flags(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-b-") as temp:
            config = Path(temp) / "config.toml"
            profiles.save(
                "work",
                {
                    "store": temp + "/s",
                    "control": temp + "/c",
                    "bindings": {"environment": "dev", "role": "review", "task_stage": "plan"},
                },
                config,
            )
            self.assertEqual(profiles.load(config)["work"]["bindings"]["task_stage"], "plan")
            environ = {"MEMORYKING_BINDINGS": "environment=staging,role=implement"}
            self.assertEqual(
                session_bindings.resolve(
                    ["role=verify"], profile="work", config=config, environ=environ
                ),
                [{"environment": "staging", "role": "verify", "task_stage": "plan"}],
            )
            self.assertEqual(
                session_bindings.resolve(profile="work", config=config, environ={}),
                [{"environment": "dev", "role": "review", "task_stage": "plan"}],
            )
            # MEMORYKING_PROFILE names the profile when none is passed.
            self.assertEqual(
                session_bindings.resolve(environ={"MEMORYKING_PROFILE": "work"}, config=config),
                [{"environment": "dev", "role": "review", "task_stage": "plan"}],
            )
            with self.assertRaises(LocalError):
                profiles.save(
                    "bad",
                    {"store": temp + "/s", "control": temp + "/c", "bindings": {"x": "y"}},
                    config,
                )
        self.assertEqual(session_bindings.resolve(environ={}), [{}])

    def test_default_environment_is_read_from_the_process(self):
        old = os.environ.pop("MEMORYKING_PROFILE", None)
        try:
            os.environ["MEMORYKING_BINDINGS"] = "environment=prod"
            self.assertEqual(session_bindings.resolve(), [{"environment": "prod"}])
        finally:
            os.environ.pop("MEMORYKING_BINDINGS", None)
            if old is not None:
                os.environ["MEMORYKING_PROFILE"] = old


if __name__ == "__main__":
    unittest.main()
