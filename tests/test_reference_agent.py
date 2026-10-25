"""Actual command-process continuation through the public reference-agent module."""

import json
import subprocess
import sys
import unittest

import test_harness as harness_helpers
from test_working import checkpoint


class ReferenceAgentTests(unittest.TestCase):
    setUp = harness_helpers.HarnessTests.setUp
    tearDown = harness_helpers.HarnessTests.tearDown

    def command(self, *args, success=True, extra=None):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking.reference_agent",
                "--socket",
                str(self.server.path),
                "--credential",
                str(self.control / "owner.credential"),
                "--project",
                "p",
                "--journal",
                str(self.root / "cli-journal"),
                "--tool-root",
                str(self.tool.root),
                "--task",
                "cli-task",
                *(extra or []),
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0 if success else 2, result.stderr)
        return json.loads(result.stdout) if success else result.stderr.strip()

    def start(self, extra=None):
        state = self.root / "state.json"
        state.write_text(json.dumps(checkpoint()))
        return self.command("start", "--state", str(state), extra=extra)

    def test_real_commands_preserve_state_and_do_not_repeat_file_mutation(self):
        self.start()
        self.command("constraint", "--text", "Preserve the original file", "--approve")
        self.assertEqual(
            self.command(
                "create-file",
                "--operation",
                "op1",
                "--name",
                "one.txt",
                "--content",
                "actual content",
                success=False,
            ),
            "host_authorization_required",
        )
        self.assertFalse((self.tool.root / "one.txt").exists())
        result = self.command(
            "create-file",
            "--operation",
            "op1",
            "--name",
            "one.txt",
            "--content",
            "actual content",
            "--approve",
        )
        self.assertEqual(result["state"], "succeeded")
        before = (self.tool.root / "one.txt").stat()
        self.assertEqual(
            self.command(
                "create-file",
                "--operation",
                "op1",
                "--name",
                "one.txt",
                "--content",
                "actual content",
                "--approve",
                success=False,
            ),
            "operation_already_recorded",
        )
        self.assertEqual(before.st_mtime_ns, (self.tool.root / "one.txt").stat().st_mtime_ns)
        self.assertIn("Preserve the original file", json.dumps(self.command("resume")))
        self.assertEqual(self.command("close")["checkpointed"], True)

    def test_local_provider_and_command_inspection_preserve_handoff(self):
        recipient = self.store.grant(self.owner, "recipient", "p", ["read", "handoff"])
        self.start(extra=["--reader", "recipient"])
        result = self.command("model-turn", "--provider-root", str(self.root / "provider"))
        self.assertTrue((self.root / "provider").exists())
        self.assertIn("request_path", result)
        prepared = self.command(
            "prepare-handoff", "--recipient", "recipient", "--role", "review", "--stage", "verify"
        )
        revision = self.command("status")["revision"]
        self.command("resume")
        self.assertEqual(self.command("status")["revision"], revision)
        from memoryking.adapters.handoff import Handoff

        # The exact server view remains materializable after intervening CLI inspection.
        packet = Handoff(self.store).execute(
            recipient,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "materialize",
                "key": "cli-recipient-view",
                "args": {
                    "project": "p",
                    "handle": prepared["handle"],
                    "max_output_tokens": 16000,
                    "tokenizer": "cl100k_base",
                },
            },
        )
        self.assertIsNotNone(packet)

    def test_start_is_explicit_and_missing_state_fails_without_arbitrary_execution(self):
        self.assertEqual(self.command("status", success=False), "harness_state_required")
        self.start()
        self.assertEqual(self.start_again(), "harness_already_exists")
        self.assertEqual(
            self.command("status", extra=["--reader", "unexpected"], success=False),
            "readers_only_at_start",
        )

    def start_again(self):
        return self.command("start", "--state", str(self.root / "state.json"), success=False)
