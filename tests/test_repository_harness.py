"""Actual harness boundaries for trusted snapshots and recoverable dirty bytes."""

import subprocess
import unittest

import test_harness as harness_helpers

from memoryking.domain.local import LocalError


class RepositoryHarnessTests(unittest.TestCase):
    setUp = harness_helpers.HarnessTests.setUp
    tearDown = harness_helpers.HarnessTests.tearDown
    start = harness_helpers.HarnessTests.start

    def register(self):
        root = self.tool.root
        for args in [
            ("init", "-q"),
            ("config", "user.name", "Fixture"),
            ("config", "user.email", "fixture@example.invalid"),
        ]:
            subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
        for name in ("one.py", "two.py"):
            (root / name).write_text("value = 1\n")
            (root / name).chmod(0o600)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-qm", "Data fixture"],
            check=True,
            capture_output=True,
        )
        result = self.client.repository(
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "register",
                "key": "register",
                "args": {
                    "project": "p",
                    "remote": "local:harness",
                    "clone": "first",
                    "path": str(root),
                    "readers": None,
                },
            }
        )
        return result["repository"]

    def test_dirty_artifacts_must_cover_exact_snapshot_and_declared_scope(self):
        rid = self.register()
        harness = self.start()
        for name in ("one.py", "two.py"):
            (self.tool.root / name).write_text("value = 2\n")
        harness.refresh_repository(rid, "first", "test", ["one.py", "two.py"], approved=True)
        self.assertEqual(harness.resume()["status"], "incomplete")
        harness.store_artifact("one.py", approved=True)
        self.assertEqual(harness.resume()["status"], "incomplete")
        (self.tool.root / "two.py").write_text("value = 3\n")
        harness.store_artifact("two.py", approved=True)
        packet = harness.resume()
        self.assertEqual(packet["status"], "incomplete")
        self.assertIn("dirty_snapshot_artifact_unavailable", packet["recovery_gaps"])
        harness.refresh_repository(rid, "first", "test", ["one.py", "two.py"], approved=True)
        self.assertEqual(harness.resume()["status"], "ready")
        args = {
            "project": "p",
            "task": "task",
            "run": None,
            "checkpoint": None,
            "bindings": harness.data["bindings"][:1],
            "max_output_tokens": 16000,
            "tokenizer": "cl100k_base",
        }
        packet = self.client.working(
            {"version": 1, "incarnation": self.store.incarnation, "action": "resume", "args": args}
        )
        self.assertEqual(packet["status"], "blocked")
        self.assertEqual(packet["reason"], "binding_coverage_incomplete")

    def test_deleted_paths_are_durable_tombstones_and_refresh_needs_approval(self):
        rid = self.register()
        harness = self.start()
        with self.assertRaisesRegex(LocalError, "host_authorization_required"):
            harness.refresh_repository(rid, "first", "test", ["one.py"], approved=False)
        (self.tool.root / "one.py").unlink()
        harness.refresh_repository(rid, "first", "test", ["one.py"], approved=True)
        packet = harness.resume()
        self.assertEqual(packet["status"], "ready")
        self.assertEqual(packet["checkpoint"]["state"]["repository"]["deleted_paths"], ["one.py"])

    def test_dispatch_recaptures_and_refuses_undeclared_file_effect(self):
        from memoryking.harness_context import LocalRecordingProvider

        rid = self.register()
        harness = self.start()
        harness.refresh_repository(rid, "first", "test", ["one.py", "new.txt"], approved=True)
        with self.assertRaisesRegex(LocalError, "tool_outside_declared_scope"):
            harness.create_file("unplanned", "unplanned.txt", "effect", approved=True)
        self.assertFalse((self.tool.root / "unplanned.txt").exists())
        (self.tool.root / "one.py").write_text("value = 9\n")
        provider = LocalRecordingProvider(self.root / "provider")
        with self.assertRaisesRegex(LocalError, "repository_snapshot_changed"):
            harness.model_turn(provider)
        self.assertFalse(list((self.root / "provider").glob("request-*")))


if __name__ == "__main__":
    unittest.main()
