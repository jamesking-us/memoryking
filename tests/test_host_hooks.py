"""Host hooks against a real local service: load at start, save before loss, install."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from memoryking import host_hooks
from memoryking.adapters.local_ipc import Server, request
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError

CLAUDE_LINES = [
    {"type": "user", "message": {"role": "user", "content": "Fix the retry bug"}},
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "The retry deadline is scoped per request."}],
        },
    },
    {"type": "assistant", "message": {"role": "assistant", "content": "Tests pass now."}},
]


class HostHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-hooks-")
        self.root = Path(self.temp.name)
        self.credential_file = LocalStore.initialize(self.root / "db", self.root / "ctl")
        self.store = LocalStore(self.root / "db", self.root / "ctl")
        self.server = Server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.where = {
            "socket": self.server.path,
            "credential": self.credential_file,
            "project": "demo",
        }
        self.transcript = self.root / "transcript.jsonl"
        self.transcript.write_text("".join(json.dumps(line) + "\n" for line in CLAUDE_LINES))

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def test_event_shapes_from_three_hosts_normalize_the_same_way(self):
        claude = host_hooks.read_event(
            json.dumps(
                {
                    "session_id": "s1",
                    "transcript_path": "/t",
                    "cwd": "/w",
                    "hook_event_name": "PreCompact",
                    "trigger": "auto",
                    "custom_instructions": "keep the test plan",
                    "prompt_id": "p",
                }
            ),
            {},
        )
        self.assertEqual(
            (claude["host"], claude["kind"], claude["trigger"]), ("claude", "pre-compact", "auto")
        )
        self.assertEqual(claude["instructions"], "keep the test plan")
        codex = host_hooks.read_event(
            json.dumps(
                {
                    "session_id": "s2",
                    "hook_event_name": "Stop",
                    "model": "gpt",
                    "turn_id": "t",
                    "last_assistant_message": "Done.",
                }
            ),
            {},
        )
        self.assertEqual(
            (codex["host"], codex["kind"], codex["last_message"]), ("codex", "session-end", "Done.")
        )
        grok = host_hooks.read_event(
            json.dumps({"hookEventName": "SessionStart", "sessionId": "s3", "cwd": "/g"}),
            {"GROK_HOOK_EVENT": "SessionStart"},
        )
        self.assertEqual(
            (grok["host"], grok["kind"], grok["session"]), ("grok", "session-start", "s3")
        )
        empty = host_hooks.read_event("", {})
        self.assertIsNone(empty["kind"])
        with self.assertRaisesRegex(LocalError, "hook_event_invalid"):
            host_hooks.read_event("[1, 2]", {})
        with self.assertRaisesRegex(LocalError, "hook_event_invalid"):
            host_hooks.read_event("not json", {})

    def test_transcript_tail_prefers_assistant_text_and_is_bounded(self):
        tail = host_hooks.transcript_tail(self.transcript)
        self.assertIn("scoped per request", tail)
        self.assertIn("Tests pass now.", tail)
        self.assertNotIn("Fix the retry bug", tail)
        plain = self.root / "plain.txt"
        plain.write_text("x" * 50000 + "END")
        self.assertTrue(host_hooks.transcript_tail(plain, limit=100).endswith("END"))
        self.assertEqual(len(host_hooks.transcript_tail(plain, limit=100)), 100)
        self.assertEqual(host_hooks.transcript_tail(self.root / "missing"), "")
        self.assertEqual(host_hooks.transcript_tail(None), "")

    def test_pre_compact_captures_once_and_checkpoints_the_task(self):
        event = host_hooks.read_event(
            json.dumps(
                {
                    "session_id": "sess",
                    "transcript_path": str(self.transcript),
                    "hook_event_name": "PreCompact",
                    "trigger": "auto",
                    "prompt_id": "p",
                }
            ),
            {},
        )
        first = host_hooks.save(event, "pre-compact", task="retry-fix", **self.where)
        again = host_hooks.save(event, "pre-compact", task="retry-fix", **self.where)
        self.assertTrue(first["captured"])
        self.assertEqual(first["observation"], again["observation"])
        self.assertEqual(first["source"], "claude-compact")
        self.assertEqual(first["checkpoint"], again["checkpoint"])
        self.assertEqual(again["revision"], 1)
        # The briefing at the next session start cites what was saved.
        text = host_hooks.session_start(
            event, query="retry deadline", task="retry-fix", **self.where
        )
        self.assertIn("# Resume: Session sess on demo", text)
        self.assertIn("scoped per request", text)
        self.assertIn("# Briefing: retry deadline", text)
        self.assertIn("claude-compact", text)

    def test_session_end_uses_the_last_message_and_start_without_a_task_still_briefs(self):
        event = host_hooks.read_event(
            json.dumps(
                {
                    "session_id": "sess",
                    "hook_event_name": "Stop",
                    "model": "gpt",
                    "turn_id": "t",
                    "last_assistant_message": "Final state: the fix is merged.",
                }
            ),
            {},
        )
        result = host_hooks.save(event, "session-end", **self.where)
        self.assertEqual(result["source"], "codex-session")
        self.assertNotIn("checkpoint", result)
        text = host_hooks.session_start(event, query="fix merged", **self.where)
        self.assertIn("fix is merged", text)
        self.assertNotIn("# Resume", text)
        nothing = host_hooks.save(
            host_hooks.read_event(json.dumps({"hook_event_name": "SessionEnd"}), {}),
            "session-end",
            **self.where,
        )
        self.assertEqual(nothing, {"captured": False, "reason": "nothing_to_capture"})

    def test_cli_hook_never_fails_the_host_and_strict_does(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"PYTHONPATH", "PYTHONHOME"} and not k.startswith("MEMORYKING_")
        }
        base = [sys.executable, "-m", "memoryking", "hook"]
        run = subprocess.run(
            [*base, "session-start", "--project", "demo"],
            input="{}",
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("socket_required", run.stderr)
        self.assertEqual(run.stdout, "")
        run = subprocess.run(
            [*base, "session-start", "--project", "demo", "--strict"],
            input="{}",
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(run.returncode, 1)
        connected = [
            *base,
            "session-end",
            "--host",
            "claude",
            "--project",
            "demo",
            "--socket",
            str(self.server.path),
            "--credential",
            str(self.credential_file),
        ]
        run = subprocess.run(
            connected,
            input=json.dumps(
                {
                    "session_id": "cli",
                    "transcript_path": str(self.transcript),
                    "hook_event_name": "SessionEnd",
                }
            ),
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('"captured": true', run.stderr)
        run = subprocess.run(
            [*base, "session-start", *connected[len(base) + 1 :], "--query", "retry"],
            input="{}",
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("# Briefing: retry", run.stdout)

    def test_install_merges_idempotently_and_removes_cleanly(self):
        cwd, home = self.root / "repo", self.root / "home"
        cwd.mkdir()
        (cwd / ".claude").mkdir()
        (cwd / ".claude/settings.json").write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash(ls)"]},
                    "hooks": {
                        "PreCompact": [{"hooks": [{"type": "command", "command": "echo mine"}]}]
                    },
                }
            )
        )
        common = {
            "scope": "project",
            "project": "demo",
            "cwd": cwd,
            "home": home,
            "profile": "home",
            "task": "retry-fix",
        }
        first = host_hooks.install(host="claude", **common)
        second = host_hooks.install(host="claude", **common)
        config = json.loads((cwd / ".claude/settings.json").read_text())
        self.assertEqual(config["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(first["config"], second["config"])
        self.assertEqual(sorted(config["hooks"]), ["PreCompact", "SessionEnd", "SessionStart"])
        self.assertEqual(len(config["hooks"]["PreCompact"]), 2)  # theirs kept, ours once
        self.assertEqual(config["hooks"]["PreCompact"][0]["hooks"][0]["command"], "echo mine")
        start = config["hooks"]["SessionStart"][0]
        self.assertEqual(start["matcher"], "startup|resume|compact")
        self.assertIn(
            "hook session-start --host claude --project demo", start["hooks"][0]["command"]
        )
        self.assertIn("--profile home", start["hooks"][0]["command"])
        self.assertIn("--task retry-fix", start["hooks"][0]["command"])
        removed = host_hooks.install(host="claude", remove=True, **common)
        self.assertEqual(
            removed["config"]["hooks"],
            {"PreCompact": [{"hooks": [{"type": "command", "command": "echo mine"}]}]},
        )
        for host, relative in (
            ("codex", ".codex/hooks.json"),
            ("grok", ".grok/hooks/memoryking.json"),
        ):
            result = host_hooks.install(host=host, **{**common, "scope": "user"})
            self.assertEqual(result["path"], str(home / relative))
            self.assertTrue((home / relative).exists())
            written = json.loads((home / relative).read_text())
            self.assertEqual(sorted(written["hooks"]), ["PreCompact", "SessionEnd", "SessionStart"])
        self.assertIsNotNone(result["note"])
        preview = host_hooks.install(
            host="claude", write=False, **{**common, "cwd": self.root / "elsewhere"}
        )
        self.assertFalse((self.root / "elsewhere/.claude/settings.json").exists())
        self.assertIn("SessionStart", preview["config"]["hooks"])
        with self.assertRaisesRegex(LocalError, "host_unsupported"):
            host_hooks.install(host="cursor", **common)
        (cwd / ".codex").mkdir()
        (cwd / ".codex/hooks.json").write_text("[]")
        with self.assertRaisesRegex(LocalError, "hook_config_invalid"):
            host_hooks.install(host="codex", **common)

    def test_hook_checkpoint_is_readable_by_an_ordinary_resume(self):
        event = host_hooks.read_event(
            json.dumps(
                {
                    "session_id": "sess",
                    "transcript_path": str(self.transcript),
                    "hook_event_name": "PreCompact",
                    "prompt_id": "p",
                }
            ),
            {},
        )
        host_hooks.save(event, "pre-compact", task="retry-fix", **self.where)
        token = self.credential_file.read_text()
        context = request(self.server.path, token, "context", {"project": "demo"})
        packet = request(
            self.server.path,
            token,
            "working",
            {
                "command": {
                    "version": 1,
                    "incarnation": context["incarnation"],
                    "action": "resume",
                    "args": {
                        "project": "demo",
                        "task": "retry-fix",
                        "run": None,
                        "checkpoint": None,
                        "bindings": [{}],
                        "max_output_tokens": 4000,
                        "tokenizer": "utf8-byte-estimate-v1",
                    },
                }
            },
        )
        state = packet["checkpoint"]["state"]
        self.assertEqual(state["objective"], "Session sess on demo")
        self.assertIn("scoped per request", state["current_state"])
        self.assertEqual(state["references"][0]["kind"], "observation")
        self.assertIn("defaulted:next_actions", state["capture"]["gaps"])
