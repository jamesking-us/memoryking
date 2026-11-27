"""Codex specifics of setup and the hooks, through real processes: where the files go
(`CODEX_HOME`), the session-end time limit and the hand-off that keeps it from losing a
session, a session with no transcript, and an MCP entry that already exists."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from memoryking import hook_spool, host_hooks
from memoryking.adapters.platform_support import LOCAL_STORE_SUPPORTED

WORD = "zebrafishquartz"


def codex_rollout(path, word=WORD):
    """A Codex session file in the shape the hook reads: session meta, then response items."""

    def item(payload):
        return json.dumps(
            {"timestamp": "2026-11-27T10:00:00.000Z", "type": "response_item", "payload": payload}
        )

    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": "s1", "cwd": "/tmp/x"}}),
        item(
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"Remember that the {word} deploy needs the blue key.",
                    }
                ],
            }
        ),
        item({"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c1"}),
        item({"type": "function_call_output", "call_id": "c1", "output": "listing"}),
        item(
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": f"Noted: the {word} deploy needs the blue key."}
                ],
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-cxh-")
        self.home = Path(self.temp.name)
        self.cwd = self.home / "proj"
        self.cwd.mkdir()
        self.env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("MEMORYKING_")},
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "MEMORYKING_SEMANTIC": "off",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.env.pop("PYTHONPATH", None)
        self.env.pop("CODEX_HOME", None)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stop)

    def stop(self):
        self.mk("--profile", "default", "stop", check=False)

    def mk(self, *args, check=True, env=None, stdin=""):
        done = subprocess.run(
            [sys.executable, "-m", "memoryking", *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=env or self.env,
            cwd=self.cwd,
            timeout=90,
        )
        if check:
            self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        return done


@unittest.skipUnless(LOCAL_STORE_SUPPORTED, "the POSIX setup with its store and daemon")
class CodexHomeTests(Base):
    def codex_env(self, where):
        return {**self.env, "CODEX_HOME": str(where)}

    def test_setup_follows_codex_home_and_remove_restores_it_byte_for_byte(self):
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        original = b'[model]\nname = "x"\n'
        (elsewhere / "config.toml").write_bytes(original)
        env = self.codex_env(elsewhere)
        done = self.mk("setup", "codex", "--project", "demo", env=env)
        self.assertIn(str(elsewhere / "config.toml"), done.stdout)
        self.assertIn(str(elsewhere / "hooks.json"), done.stdout)
        self.assertIn("[mcp_servers.memoryking]", (elsewhere / "config.toml").read_text())
        self.assertTrue((elsewhere / "hooks.json").exists())
        self.assertFalse((self.home / ".codex").exists())
        again = self.mk("setup", "codex", "--project", "demo", env=env)
        self.assertIn("unchanged", again.stdout)
        gone = self.mk("setup", "codex", "--project", "demo", "--remove", env=env)
        self.assertIn(str(elsewhere / "hooks.json"), gone.stdout)
        self.assertEqual(original, (elsewhere / "config.toml").read_bytes())
        self.assertFalse((elsewhere / "hooks.json").exists())
        self.assertFalse((self.home / ".codex").exists())

    def test_a_dry_run_names_the_codex_home_paths_and_writes_nothing(self):
        elsewhere = self.home / "elsewhere"
        done = self.mk(
            "setup", "codex", "--project", "demo", "--dry-run", env=self.codex_env(elsewhere)
        )
        self.assertIn(str(elsewhere / "config.toml"), done.stdout)
        self.assertIn(str(elsewhere / "hooks.json"), done.stdout)
        self.assertFalse(elsewhere.exists())
        self.assertFalse((self.home / ".codex").exists())

    def test_an_empty_codex_home_means_the_default_directory(self):
        self.mk("setup", "codex", "--project", "demo", env=self.codex_env(""))
        self.assertTrue((self.home / ".codex" / "hooks.json").exists())
        self.assertTrue((self.home / ".codex" / "config.toml").exists())

    def test_remove_still_finds_files_written_under_home_before_codex_home_was_followed(self):
        self.mk("setup", "codex", "--project", "demo")  # no CODEX_HOME: files under ~/.codex
        old = self.home / ".codex"
        self.assertTrue((old / "hooks.json").exists())
        elsewhere = self.home / "elsewhere"
        gone = self.mk(
            "setup", "codex", "--project", "demo", "--remove", env=self.codex_env(elsewhere)
        )
        self.assertIn(str(old / "hooks.json"), gone.stdout)
        self.assertIn(str(old / "config.toml"), gone.stdout)
        self.assertFalse((old / "hooks.json").exists())
        self.assertNotIn(
            "memoryking",
            (old / "config.toml").read_text() if (old / "config.toml").exists() else "",
        )
        self.assertFalse(elsewhere.exists())

    def test_the_hooks_installer_follows_codex_home_for_the_user_scope_only(self):
        elsewhere = self.home / "elsewhere"
        env = self.codex_env(elsewhere)
        args = [
            "hooks",
            "install",
            "--host",
            "codex",
            "--project",
            "demo",
            "--socket",
            "s",
            "--credential",
            "c",
        ]
        user = json.loads(self.mk(*args, "--scope", "user", env=env).stdout)
        self.assertEqual(str(elsewhere / "hooks.json"), user["path"])
        self.assertTrue((elsewhere / "hooks.json").exists())
        project = json.loads(self.mk(*args, "--cwd", str(self.cwd), env=env).stdout)
        self.assertEqual(str(self.cwd / ".codex" / "hooks.json"), project["path"])
        self.assertFalse((self.home / ".codex").exists())
        removed = json.loads(self.mk(*args, "--scope", "user", "--remove", env=env).stdout)
        self.assertEqual(str(elsewhere / "hooks.json"), removed["path"])

    def test_the_path_helper(self):
        home = Path("/h")
        with patch.dict(os.environ, {"CODEX_HOME": "/c"}):
            self.assertEqual(
                Path("/c/hooks.json"), host_hooks.config_path("codex", "user", Path("/p"), home)
            )
            self.assertEqual(
                Path("/p/.codex/hooks.json"),
                host_hooks.config_path("codex", "project", Path("/p"), home),
            )
            self.assertEqual(
                Path("/h/.claude/settings.json"),
                host_hooks.config_path("claude", "user", Path("/p"), home),
            )
            self.assertEqual(Path("/h/.codex"), host_hooks.codex_dir(home, legacy=True))
        with patch.dict(os.environ, {"CODEX_HOME": "  "}):
            self.assertEqual(
                Path("/h/.codex/hooks.json"),
                host_hooks.config_path("codex", "user", Path("/p"), home),
            )


class TimeoutTests(unittest.TestCase):
    def test_only_codex_asks_for_three_seconds_at_session_end(self):
        def end(host):
            entries = host_hooks._entries(host, dict.fromkeys(host_hooks.KINDS, "x"))
            return entries["SessionEnd"][0]["hooks"][0]["timeout"]

        self.assertEqual(3, end("codex"))
        self.assertEqual(10, end("claude"))
        self.assertEqual(20, end("grok"))
        codex = host_hooks._entries("codex", dict.fromkeys(host_hooks.KINDS, "x"))
        self.assertEqual(30, codex["SessionStart"][0]["hooks"][0]["timeout"])


@unittest.skipUnless(LOCAL_STORE_SUPPORTED, "the POSIX setup with its store and daemon")
class CodexSessionTests(Base):
    def setUp(self):
        super().setUp()
        self.mk("setup", "codex", "--project", "demo")
        hooks = json.loads((self.home / ".codex" / "hooks.json").read_text())["hooks"]
        self.command = {
            {
                "SessionStart": "session-start",
                "SessionEnd": "session-end",
                "UserPromptSubmit": "prompt",
            }.get(k, k): v[0]["hooks"][0]["command"]
            for k, v in hooks.items()
        }
        self.spool = self.home / ".memoryking" / "default" / "control" / hook_spool.DIRECTORY

    def hook(self, name, event):
        return subprocess.run(
            self.command[name],
            shell=True,
            input=json.dumps(event),
            text=True,
            capture_output=True,
            env=self.env,
            cwd=self.cwd,
            timeout=60,
        )

    def waiting(self):
        return sorted(self.spool.glob("*.json")) if self.spool.exists() else []

    def settled(self):
        deadline = time.monotonic() + 30
        while self.waiting() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([], self.waiting())

    def found(self, word=WORD):
        done = self.mk(
            "--profile", "default-agent", "recall", "--query", word, "--project", "demo", "--json"
        )
        return json.loads(done.stdout)

    def kept(self, word=WORD):
        return [
            g["reference"] for g in self.found(word)["guidance"] if g["source"] == "codex-session"
        ]

    def count(self, word=WORD):
        return len(self.kept(word))

    def end_event(self, name="rollout.jsonl", **more):
        path = self.home / name
        codex_rollout(path)
        return {
            "session_id": "sess-1",
            "transcript_path": str(path),
            "cwd": str(self.cwd),
            "hook_event_name": "SessionEnd",
            "reason": "other",
            **more,
        }

    def parts(self):
        args = host_hooks.hooks.connection_files(
            profile="default-agent",
            config=self.home / ".memoryking" / "config.toml",
            project="demo",
        )
        return {"socket": args[0], "credential": args[1], "project": "demo"}

    def test_the_session_end_hook_hands_the_capture_to_a_worker_that_stores_it(self):
        done = self.hook("session-end", self.end_event())
        self.assertEqual(0, done.returncode, done.stderr)
        self.settled()
        self.assertGreaterEqual(self.count(), 1)
        self.assertEqual([], list(self.spool.glob("*")) if self.spool.exists() else [])

    def test_a_hook_killed_before_its_worker_ran_loses_nothing_the_next_session_start_stores_it(
        self,
    ):
        event = self.end_event()
        with patch.object(hook_spool, "_worker"):  # the hook is stopped right after the record
            args = type("A", (), {"credential": self.parts()["credential"], "stdin_text": None})()
            with patch("sys.stdin", __import__("io").StringIO(json.dumps(event))):
                self.assertTrue(hook_spool.hand_off(args, self._argv()))
        self.assertEqual(1, len(self.waiting()))
        self.assertEqual(0, self.count())
        old = time.time() - 60
        os.utime(self.waiting()[0], (old, old))
        start = self.hook(
            "session-start",
            {
                "session_id": "s2",
                "cwd": str(self.cwd),
                "hook_event_name": "SessionStart",
                "source": "startup",
                "model": "m",
                "transcript_path": None,
                "permission_mode": "default",
            },
        )
        self.assertEqual(0, start.returncode, start.stderr)
        self.settled()
        self.assertGreaterEqual(self.count(), 1)
        # the turns a hook that ran to the end would store: the user's and the assistant's
        stored = [g["text"] for g in self.found()["guidance"] if g["source"] == "codex-session"]
        self.assertIn("blue key", stored[0])
        self.assertIn("User: ", stored[0])
        self.assertIn("Assistant: ", stored[0])

    def _argv(self):
        return [
            "--profile",
            "default-agent",
            "hook",
            "session-end",
            "--host",
            "codex",
            "--project",
            "demo",
        ]

    def test_a_worker_killed_midway_leaves_its_record_and_a_young_record_is_left_alone(self):
        event = self.end_event()
        args = type("A", (), {"credential": self.parts()["credential"]})()
        with (
            patch.object(hook_spool, "_worker"),
            patch("sys.stdin", __import__("io").StringIO(json.dumps(event))),
        ):
            hook_spool.hand_off(args, self._argv())
        with patch.object(hook_spool, "_worker") as worker:
            hook_spool.replay(args)
            worker.assert_not_called()  # younger than the settle time
            old = time.time() - 60
            os.utime(self.waiting()[0], (old, old))
            hook_spool.replay(args)
            worker.assert_called_once()

    def test_the_same_event_twice_is_one_record_and_a_cold_daemon_is_started_by_the_worker(self):
        self.mk("--profile", "default", "stop")
        event = self.end_event()
        self.assertEqual(0, self.hook("session-end", event).returncode)
        self.assertEqual(0, self.hook("session-end", event).returncode)
        self.settled()
        self.assertEqual(1, self.count())

    def test_a_record_is_given_up_after_its_tries_and_an_unreadable_one_is_dropped(self):
        event = self.end_event()
        args = type("A", (), {"credential": self.parts()["credential"]})()
        with (
            patch.object(hook_spool, "_worker"),
            patch("sys.stdin", __import__("io").StringIO(json.dumps(event))),
        ):
            hook_spool.hand_off(args, self._argv())
        record = self.waiting()[0]
        worker = type(
            "W",
            (),
            {
                "socket": self.home / "nowhere.sock",
                "credential": self.parts()["credential"],
                "project": "demo",
                "profile": None,
                "config": None,
                "host": "codex",
                "task": None,
                "no_user_preferences": False,
            },
        )()
        for _number in range(hook_spool.TRIES):
            self.assertTrue(record.exists())
            self.assertEqual(1, hook_spool.finish(worker, record))
        self.assertFalse(record.exists())
        record.write_text("not json")
        with patch.object(hook_spool, "_worker") as started:
            os.utime(record, (1, 1))
            hook_spool.replay(args)
            started.assert_not_called()
        self.assertFalse(record.exists())

    def test_a_session_with_no_transcript_exits_quietly_and_stores_nothing(self):
        base = {
            "session_id": "e1",
            "cwd": str(self.cwd),
            "model": "m",
            "permission_mode": "default",
            "transcript_path": None,
        }
        start = self.hook(
            "session-start", {**base, "hook_event_name": "SessionStart", "source": "startup"}
        )
        prompt = self.hook(
            "prompt",
            {
                **base,
                "hook_event_name": "UserPromptSubmit",
                "turn_id": "t1",
                "prompt": f"what about {WORD}",
            },
        )
        end = self.hook(
            "session-end",
            {
                k: v
                for k, v in {**base, "hook_event_name": "SessionEnd", "reason": "other"}.items()
                if k != "model"
            },
        )
        for done in (start, prompt, end):
            self.assertEqual(0, done.returncode, done.stderr)
        self.settled()
        self.assertEqual(0, self.count())
        self.assertNotIn("Traceback", start.stderr + prompt.stderr + end.stderr)
        result = host_hooks.save(
            host_hooks.read_event(
                json.dumps({**base, "hook_event_name": "SessionEnd", "reason": "other"}), {}
            ),
            "session-end",
            host="codex",
            **self.parts(),
        )
        self.assertEqual({"captured": False, "reason": "nothing_to_capture"}, result)


@unittest.skipUnless(LOCAL_STORE_SUPPORTED, "the POSIX setup with its store and daemon")
class ExistingEntryTests(Base):
    def test_an_existing_entry_is_kept_and_the_message_says_what_that_means(self):
        codex = self.home / ".codex"
        codex.mkdir()
        original = b'[mcp_servers.memoryking]\ncommand = "other"\n'
        (codex / "config.toml").write_bytes(original)
        done = self.mk("setup", "codex", "--project", "demo")
        self.assertEqual(original, (codex / "config.toml").read_bytes())
        out = " ".join(done.stdout.split())
        self.assertIn("existing entry kept", out)
        self.assertIn("The hooks installed now belong to project demo and this store", out)
        self.assertIn("may point at another project or store", out)
        self.assertIn("or delete that [mcp_servers.memoryking] table and run this setup again", out)
        self.assertIn("mcp --store-profile default-agent", out)
        self.assertTrue((codex / "hooks.json").exists())


if __name__ == "__main__":
    unittest.main()
