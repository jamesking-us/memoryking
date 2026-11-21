"""User memory through a real daemon: the CLI commands and the lean stdio bridge."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import test_knowledge as knowledge_helpers
from test_sdk import initialize, rpc

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.contracts.canonical import canonical_bytes

USER = "_user_owner"


class UserScopeDaemonTests(unittest.TestCase):
    run_k = knowledge_helpers.KnowledgeTests.run_k
    body = knowledge_helpers.KnowledgeTests.body

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-us-", dir="/tmp")
        self.base = Path(self.temp.name)
        self.owner_file = LocalStore.initialize(self.base / "store", self.base / "control")
        self.owner = self.owner_file.read_text()
        self.store = LocalStore(self.base / "store", self.base / "control")
        self.n = 0
        self.k = Knowledge(self.store)
        self.server = Server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYKING_")}
        self.agent = self.issue("agent-a")
        self.other = self.issue("agent-b", "--for-user", "victor")

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def cli(self, *args, credential=None, expect=0, stdin=None):
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                *args,
                "--socket",
                str(self.server.path),
                "--credential",
                str(credential or self.owner_file),
            ],
            capture_output=True,
            text=True,
            input=stdin,
            env=self.env,
        )
        self.assertEqual(run.returncode, expect, run.stdout + run.stderr)
        return run.stdout

    def issue(self, name, *extra):
        path = self.base / f"{name}.credential"
        self.cli_issue(name, path, *extra)
        return path

    def cli_issue(self, name, path, *extra):
        # `credential issue` takes its connection after the sub-command, like every command.
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                "credential",
                "issue",
                name,
                "--project",
                "p",
                "--capabilities",
                "capture,read",
                "--output",
                str(path),
                "--socket",
                str(self.server.path),
                "--credential",
                str(self.owner_file),
                *extra,
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def bridge(self, credential, calls):
        frames = [initialize(), {"jsonrpc": "2.0", "method": "notifications/initialized"}]
        frames += [
            rpc(10 + n, "tools/call", {"name": name, "arguments": arguments})
            for n, (name, arguments) in enumerate(calls)
        ]
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking.adapters.stdio",
                "--socket",
                str(self.server.path),
                "--credential",
                str(credential),
                "--project",
                "p",
                "--profile",
                "lean",
            ],
            input=b"".join(canonical_bytes(frame) + b"\n" for frame in frames),
            capture_output=True,
            timeout=60,
            env=self.env,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        replies = {
            r["id"]: r["result"] for r in map(json.loads, run.stdout.splitlines()) if "id" in r
        }
        return [replies[10 + n] for n in range(len(calls))]

    def text(self, reply):
        return "\n".join(part["text"] for part in reply["content"])

    def observation(self, text):
        # What the CLI printed for `capture`: the new observation id.
        return json.loads(text)["observation"]

    def test_agents_write_the_users_memory_and_every_project_reads_it(self):
        saved = json.loads(
            self.cli(
                "capture",
                "--scope",
                "user",
                "--project",
                "p",
                "--source",
                "chat",
                "--text",
                "Always use pnpm, never npm.",
                credential=self.agent,
            )
        )
        self.assertTrue(saved["observation"].startswith("obs_"))
        # The user sees what is kept and which agents act for them.
        shown = json.loads(self.cli("user", "show", "--json"))
        self.assertEqual((shown["user"], shown["observations"]), ("owner", 1))
        self.assertIn("agent-a", [a["principal"] for a in shown["agents"]])
        self.assertNotIn("agent-b", [a["principal"] for a in shown["agents"]])
        # The user reviews it as a claim; the agent's next briefing, in any project, carries it.
        raw = "Always use pnpm, never npm."
        span = {
            "observation": saved["observation"],
            "start_byte": 0,
            "end_byte": len(raw),
            "content_hash": hashlib.sha256(raw.encode()).hexdigest(),
        }
        self.span = span
        proposed = self.run_k(
            "propose",
            {
                "project": USER,
                "logical_key": "pm",
                "expected_version": 0,
                "body": self.body(text="Use pnpm.", evidence=[span]),
            },
        )
        self.run_k(
            "review",
            {
                "project": USER,
                "revision": proposed["revision"],
                "expected_version": proposed["family_version"],
                "decision": "accept",
                "reason": "reviewed_evidence",
                "breadth_reviewed": True,
            },
        )
        (brief,) = self.bridge(self.agent, [("recall", {"query": "pnpm npm packages"})])
        text = self.text(brief)
        self.assertIn("Use pnpm.", text)
        self.assertIn("from your own user memory", text)
        # The agent can also write through the lean tool, with the scope argument.
        (remembered,) = self.bridge(
            self.agent,
            [
                (
                    "remember",
                    {"text": "The user prefers small commits.", "scope": "user", "source": "chat"},
                )
            ],
        )
        self.assertFalse(remembered.get("isError"), remembered)
        self.assertEqual(json.loads(self.cli("user", "show", "--json"))["observations"], 2)
        # Another user's agent sees none of it.
        (theirs,) = self.bridge(self.other, [("recall", {"query": "pnpm npm packages commits"})])
        self.assertNotIn("pnpm", self.text(theirs).replace("pnpm npm packages commits", ""))
        self.assertNotIn("own user memory", self.text(theirs))
        denied = self.cli("user", "show", "--json", credential=self.other, expect=0)
        self.assertEqual(json.loads(denied)["user"], "victor")

    def test_export_vault_and_purge(self):
        self.cli(
            "capture",
            "--scope",
            "user",
            "--project",
            "p",
            "--text",
            "Reply in British English.",
            credential=self.agent,
        )
        target = self.base / "out"
        self.cli("user", "export", "--dir", str(target))
        files = list((target / "user").rglob("*.md"))
        self.assertTrue(any("British English" in f.read_text() for f in files), files)
        # `--yes` is required: erasing is not a default.
        self.cli("user", "purge", expect=2)
        self.assertEqual(json.loads(self.cli("user", "show", "--json"))["observations"], 1)
        done = json.loads(self.cli("user", "purge", "--yes", "--json"))
        self.assertEqual((done["remaining"], done["purged"]), (0, 1))
        self.assertEqual(json.loads(self.cli("user", "show", "--json"))["observations"], 0)

    def test_the_memory_tool_shows_the_user_scope_as_its_own_directory(self):
        from memoryking.memory_tool import MemoryTool, MemoryToolError
        from memoryking.sdk import LocalClient

        tool = MemoryTool.for_session(LocalClient(self.server.path, self.agent, "p"))
        tool.execute(
            {
                "command": "create",
                "path": "/memories/user/chat/style.md",
                "file_text": "Reply in British English.",
            }
        )
        self.assertEqual(json.loads(self.cli("user", "show", "--json"))["observations"], 1)
        self.assertIn("/memories/user", tool.execute({"command": "view", "path": "/memories"}))
        listing = tool.execute({"command": "view", "path": "/memories/user"})
        self.assertIn("/memories/user/chat/style.md", listing)
        self.assertNotIn("_user_", listing)
        shown = tool.execute({"command": "view", "path": "/memories/user/chat/style.md"})
        self.assertIn("Reply in British English.", shown)
        with self.assertRaises(MemoryToolError):
            tool.execute(
                {
                    "command": "rename",
                    "old_path": "/memories/user/chat/style.md",
                    "new_path": "/memories/p/chat/style.md",
                }
            )
        # Without a user link the project's tool never lists it.
        loose = self.base / "loose.credential"
        self.cli_issue("loose", loose, "--no-user")
        plain = MemoryTool.for_session(LocalClient(self.server.path, loose, "p"))
        self.assertNotIn("/memories/user", plain.execute({"command": "view", "path": "/memories"}))

    def test_the_user_switches_the_unreviewed_tier_on_for_their_own_scope(self):
        out = json.loads(self.cli("unreviewed", "on", "--project", USER, "--json"))
        self.assertTrue(out["enabled"])
        self.cli("unreviewed", "on", "--project", USER, credential=self.agent, expect=2)

    def test_a_credential_with_no_user_link_writes_no_user_memory(self):
        loose = self.base / "loose.credential"
        self.cli_issue("loose", loose, "--no-user")
        self.cli(
            "capture",
            "--scope",
            "user",
            "--project",
            "p",
            "--text",
            "x",
            credential=loose,
            expect=2,
        )


if __name__ == "__main__":
    unittest.main()
