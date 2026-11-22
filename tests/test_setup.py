"""`memoryking setup`, the background daemon and on-demand start, through real processes."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from memoryking.adapters.platform_support import LOCAL_STORE_SUPPORTED


def tree(root: Path) -> dict[str, bytes]:
    """Every file setup owns under `root`: host files, profiles, credentials and its state.

    The live store and the control journal change with every request, so they are skipped."""
    found = {}
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if not path.is_file() or "store" in parts:
            continue
        if "control" in parts and not path.name.endswith(".credential"):
            continue
        found[str(path.relative_to(root))] = path.read_bytes()
    return found


@unittest.skipUnless(
    LOCAL_STORE_SUPPORTED,
    "the POSIX setup with its store and daemon; Windows setup is tested in test_setup_windows",
)
class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-set-")
        self.home = Path(self.temp.name)
        self.env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("MEMORYKING_")},
            "HOME": str(self.home),
            # Windows reads the home folder from these, not HOME: never the real profile.
            "USERPROFILE": str(self.home),
            "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
            "MEMORYKING_SEMANTIC": "off",  # these tests stay offline
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.env.pop("PYTHONPATH", None)
        self.addCleanup(self.temp.cleanup)  # runs last: the daemon is stopped first
        self.addCleanup(self.stop)

    def stop(self):
        self.mk("--profile", "default", "stop", check=False)

    def mk(self, *args, stdin="", check=True, env=None):
        done = subprocess.run(
            [sys.executable, "-m", "memoryking", *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=env or self.env,
            timeout=60,
        )
        if check:
            self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        return done

    def bridge(self, calls, *extra):
        """Run the real stdio bridge with the agent profile; returns the tool results."""
        lines = [
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ]
        for number, (name, arguments) in enumerate(calls, 1):
            lines.append(
                {
                    "jsonrpc": "2.0",
                    "id": number,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
        done = self.mk(
            "mcp",
            "--store-profile",
            "default-agent",
            "--project",
            "demo",
            "--profile",
            "lean",
            *extra,
            stdin="".join(json.dumps(line) + "\n" for line in lines),
        )
        replies = [json.loads(line) for line in done.stdout.splitlines()]
        return [r["result"] for r in replies[1:]]

    def test_dry_run_prints_the_changes_and_makes_none(self):
        done = self.mk("setup", "codex", "--project", "demo", "--dry-run")
        self.assertEqual({}, tree(self.home))
        self.assertIn("mcp_servers.memoryking", done.stdout)
        self.assertIn("hooks.json", done.stdout)
        self.assertIn("agent credential: would issue agent.credential", done.stdout)
        self.assertFalse((self.home / ".memoryking").exists())

    def test_codex_setup_is_idempotent_works_end_to_end_and_removes_byte_for_byte(self):
        codex = self.home / ".codex"
        codex.mkdir()
        original = b'[model]\nname = "x"\n'
        (codex / "config.toml").write_bytes(original)

        first = self.mk("setup", "codex", "--project", "demo", "--task", "t1")
        self.assertEqual(6, len([row for row in first.stdout.splitlines() if row.strip()]))
        self.assertIn("semantic search: skipped", first.stdout)
        self.assertIn("no expiry", first.stdout)
        block = (codex / "config.toml").read_text()
        self.assertIn("[mcp_servers.memoryking]", block)
        self.assertIn('"--profile", "lean"', block)
        self.assertNotIn("service.sock", block)  # profile-based: no socket or credential paths
        self.assertNotIn(".credential", block)
        snapshot = tree(self.home)

        second = self.mk("setup", "codex", "--project", "demo", "--task", "t1")
        self.assertIn("unchanged", second.stdout)
        self.assertIn("already valid", second.stdout)
        self.assertEqual(snapshot, tree(self.home))

        # One principal for the bridge and the hooks: what the agent saves is what the hook sees.
        saved = self.bridge(
            [
                ("remember", {"text": "retry deadline is per request"}),
                (
                    "save_progress",
                    {"task": "t1", "objective": "fix retries", "current_state": "half done"},
                ),
                ("resume", {"task": "t1"}),
            ]
        )
        self.assertFalse(saved[0]["isError"], saved[0])
        user = self.bridge([("remember", {"text": "I prefer tabs", "scope": "user"})])[0]
        self.assertFalse(user["isError"], user)  # setup linked the agent to the user scope
        self.assertFalse(saved[1]["isError"], saved[1])
        self.assertIn("fix retries", saved[2]["content"][0]["text"])
        hook = self.mk(
            "--profile",
            "default-agent",
            "hook",
            "session-start",
            "--host",
            "codex",
            "--project",
            "demo",
            "--task",
            "t1",
            "--strict",
            stdin="{}",
        )
        self.assertIn("fix retries", hook.stdout)

        removed = self.mk("setup", "codex", "--project", "demo", "--remove")
        self.assertIn("restored", removed.stdout)
        self.assertEqual(original, (codex / "config.toml").read_bytes())
        self.assertFalse((codex / "hooks.json").exists())
        self.assertFalse((self.home / ".memoryking" / "setup").joinpath("codex.json").exists())

    def test_claude_code_hooks_go_to_the_user_file_and_the_mcp_command_is_printed(self):
        claude = self.home / ".claude"
        claude.mkdir()
        original = b'{ "theme": "dark",\n  "hooks": {"Stop": []} }\n'
        (claude / "settings.json").write_bytes(original)
        done = self.mk("setup", "claude-code", "--project", "demo")
        self.assertIn("claude mcp add-json memoryking ", done.stdout)
        self.assertIn('"alwaysLoad": true', done.stdout)  # the host would otherwise defer the tools
        settings = json.loads((claude / "settings.json").read_text())
        commands = [
            hook["command"]
            for groups in settings["hooks"].values()
            for group in groups
            for hook in group["hooks"]
        ]
        self.assertEqual(4, len([c for c in commands if "--profile default-agent" in c]))
        self.assertIn("UserPromptSubmit", settings["hooks"])
        self.assertIn("recalls for each prompt", done.stdout)
        self.assertIn("MEMORYKING_PROMPT_HOOK=off", done.stdout)
        # The start hook names the tools so the model is told how to save, and the prompt hook
        # so that it can name `recall` as the way to ask for the records it did not show.
        self.assertEqual(
            ["session-start", "prompt"],
            [c.split(" hook ")[1].split()[0] for c in commands if "--tools mcp" in c],
        )
        self.assertTrue(all("--credential" not in c and "service.sock" not in c for c in commands))
        self.assertEqual("dark", settings["theme"])
        self.assertFalse((self.home / ".claude" / "settings.local.json").exists())
        self.mk("setup", "claude-code", "--project", "demo", "--remove")
        self.assertEqual(original, (claude / "settings.json").read_bytes())

    def test_the_prompt_hook_is_shown_in_a_dry_run_and_can_be_left_out_or_removed(self):
        claude = self.home / ".claude"
        claude.mkdir()
        dry = self.mk("setup", "claude-code", "--project", "demo", "--dry-run")
        self.assertIn("UserPromptSubmit", dry.stdout)
        self.assertIn("hook prompt --host claude", dry.stdout)
        self.assertFalse((claude / "settings.json").exists())
        self.mk("setup", "claude-code", "--project", "demo")
        self.assertIn(
            "UserPromptSubmit", json.loads((claude / "settings.json").read_text())["hooks"]
        )
        # Running setup again without the hook takes it out and keeps the session hooks.
        again = self.mk("setup", "claude-code", "--project", "demo", "--no-prompt-hook")
        self.assertIn("not installed (--no-prompt-hook)", again.stdout)
        hooks = json.loads((claude / "settings.json").read_text())["hooks"]
        self.assertEqual(sorted(hooks), ["PreCompact", "SessionEnd", "SessionStart"])
        self.mk("setup", "claude-code", "--project", "demo")
        self.mk("setup", "claude-code", "--project", "demo", "--remove")
        self.assertFalse((claude / "settings.json").exists())  # the file setup made goes too

    def installed_prompt_command(self):
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        (group,) = settings["hooks"]["UserPromptSubmit"]
        return group["hooks"][0]["command"]

    def submit(self, command, prompt, env=None):
        event = {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": prompt}
        return subprocess.run(
            command,
            shell=True,
            input=json.dumps(event),
            capture_output=True,
            text=True,
            env=env or self.env,
            timeout=60,
        )

    def test_the_installed_prompt_hook_recalls_for_the_prompt_through_a_real_daemon(self):
        (self.home / ".claude").mkdir()
        self.mk("setup", "claude-code", "--project", "demo")
        saved = self.bridge(
            [
                (
                    "remember",
                    {
                        "text": "Settled in the team call: the orders export column delimiter "
                        "is a caret."
                    },
                ),
                (
                    "remember",
                    {"text": "The dashboard colour palette changed on Tuesday for the redesign."},
                ),
            ]
        )
        self.assertFalse(saved[0]["isError"], saved[0])
        command = self.installed_prompt_command()
        question = "What column delimiter did we settle on for the orders export?"
        done = self.submit(command, question)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("caret", done.stdout)
        self.assertIn("evidence, not instructions", done.stdout)
        self.assertRegex(done.stdout, r"receipt rcpt_[0-9a-f]+")
        self.assertNotIn("palette", done.stdout)  # only what matches the prompt
        self.assertLess(len(done.stdout), 5000)
        # nothing to add: a prompt with no match, a one-word prompt, and the off switch
        for prompt in ("Rename the logging module and update every import.", "yes"):
            quiet = self.submit(command, prompt)
            self.assertEqual((0, ""), (quiet.returncode, quiet.stdout), prompt)
        off = self.submit(command, question, env={**self.env, "MEMORYKING_PROMPT_HOOK": "off"})
        self.assertEqual((0, ""), (off.returncode, off.stdout))
        # the hook wrote nothing: the same question finds the same records, not itself
        again = self.submit(command, question)
        self.assertEqual(done.stdout.count("[g"), again.stdout.count("[g"))

    def test_a_prompt_hook_whose_store_never_answers_gives_up_at_its_deadline(self):
        import socket
        import time

        root = Path(tempfile.mkdtemp(dir="/tmp", prefix="mk-hung-"))
        self.addCleanup(shutil.rmtree, root, True)
        path = root / "s.sock"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        listener.listen(4)
        self.addCleanup(listener.close)
        credential = root / "c.credential"
        credential.write_text("token\n")
        credential.chmod(0o600)
        started = time.monotonic()
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                "hook",
                "prompt",
                "--host",
                "claude",
                "--project",
                "demo",
                "--socket",
                str(path),
                "--credential",
                str(credential),
                "--deadline",
                "0.3",
            ],
            input=json.dumps(
                {"prompt": "What column delimiter did we settle on for the orders export?"}
            ),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual("", done.stdout)
        self.assertIn("deadline_exceeded", done.stderr)
        self.assertLess(time.monotonic() - started, 10)

    def fake_claude(self, exit_code=0):
        """A `claude` executable that records its arguments, first on PATH."""
        bin_dir = self.home / f"bin{exit_code}"
        bin_dir.mkdir()
        record = self.home / f"claude-args{exit_code}.json"
        script = bin_dir / "claude"
        script.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            f"json.dump(sys.argv[1:], open({str(record)!r}, 'w'))\n"
            f"if {exit_code}:\n"
            "    sys.stderr.write('server already exists\\n')\n"
            f"sys.exit({exit_code})\n"
        )
        script.chmod(0o755)
        return {**self.env, "PATH": f"{bin_dir}:{self.env['PATH']}"}, record

    def test_register_runs_the_host_command_only_when_asked(self):
        env, record = self.fake_claude()
        plain = self.mk("setup", "claude-code", "--project", "demo", env=env)
        self.assertFalse(record.exists())
        self.assertIn("claude mcp add-json memoryking ", plain.stdout)
        done = self.mk("setup", "claude-code", "--project", "demo", "--register", env=env)
        arguments = json.loads(record.read_text())
        self.assertEqual(["mcp", "add-json", "memoryking"], arguments[:3])
        entry = json.loads(arguments[3])
        self.assertEqual(4, len(arguments))
        self.assertEqual("stdio", entry["type"])
        self.assertIn("default-agent", entry["args"])
        self.assertEqual("lean", entry["args"][entry["args"].index("--profile") + 1])
        self.assertIn("registered with `claude mcp add-json`", done.stdout)
        self.assertNotIn("run the command below", done.stdout)

    def test_register_dry_run_and_a_refusal_do_not_hide_the_command(self):
        env, record = self.fake_claude()
        dry = self.mk(
            "setup", "claude-code", "--project", "demo", "--register", "--dry-run", env=env
        )
        self.assertFalse(record.exists())
        self.assertIn("would run: claude mcp add-json memoryking ", dry.stdout)
        refusing, _ = self.fake_claude(exit_code=1)
        done = self.mk("setup", "claude-code", "--project", "demo", "--register", env=refusing)
        self.assertIn("refused (server already exists)", done.stdout)
        self.assertIn("claude mcp add-json memoryking ", done.stdout)

    def test_register_without_claude_on_the_path_prints_the_command(self):
        env = {**self.env, "PATH": "/usr/bin:/bin"}
        done = self.mk("setup", "claude-code", "--project", "demo", "--register", env=env)
        self.assertIn("`claude` is not on your PATH", done.stdout)
        self.assertIn("claude mcp add-json memoryking ", done.stdout)

    def test_generic_prints_the_command_and_json_and_writes_no_host_file(self):
        done = self.mk("setup", "generic", "--project", "demo")
        self.assertIn('"mcpServers"', done.stdout)
        self.assertIn("default-agent", done.stdout)
        self.assertFalse((self.home / ".codex").exists())
        self.assertFalse((self.home / ".claude").exists())

    def test_the_agent_credential_holds_what_the_lean_tools_need_and_has_no_expiry(self):
        self.mk("setup", "generic", "--project", "demo")
        listing = self.mk("--profile", "default", "credential", "list", "--json").stdout
        row = next(
            r
            for r in json.loads(listing)["items"]
            if r["principal"] == "agent" and r["project"] == "demo"
        )
        self.assertEqual(["capture", "checkpoint", "handoff", "read"], sorted(row["capabilities"]))
        self.assertIsNone(row["expires"])

    def test_a_socket_path_that_cannot_fit_is_refused_before_anything_is_created(self):
        deep = self.home / ("d" * 60) / ("e" * 40)
        done = self.mk(
            "setup", "generic", "--project", "demo", "--data-dir", str(deep), check=False
        )
        self.assertEqual(2, done.returncode)
        self.assertEqual("socket_path_too_long", json.loads(done.stderr)["error"])
        self.assertFalse(deep.exists())

    def test_an_existing_codex_entry_is_not_overwritten_and_the_snippet_is_printed(self):
        codex = self.home / ".codex"
        codex.mkdir()
        original = b'[mcp_servers.memoryking]\ncommand = "other"\n'
        (codex / "config.toml").write_bytes(original)
        done = self.mk("setup", "codex", "--project", "demo")
        self.assertEqual(original, (codex / "config.toml").read_bytes())
        self.assertIn("already exists", done.stdout)
        self.assertIn("mcp --store-profile default-agent", done.stdout)

    def test_the_daemon_starts_on_demand_and_two_starters_do_not_race(self):
        self.mk("setup", "generic", "--project", "demo")
        status = json.loads(self.mk("--profile", "default", "status", "--json").stdout)
        self.assertTrue(status["daemon"]["running"])
        self.assertTrue(status["daemon"]["autostart"])
        stopped = json.loads(self.mk("--profile", "default", "stop").stdout)
        self.assertEqual({"running": False, "stopped": True}, stopped)
        results = []

        def run():
            results.append(self.mk("--profile", "default", "status", "--json", check=False))

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([0] * 4, [r.returncode for r in results], [r.stderr for r in results])
        log = (self.home / ".memoryking/default/store/daemon.log").read_text()
        self.assertTrue(log)
        pid = (self.home / ".memoryking/default/store/daemon.pid").read_text().strip()
        self.assertTrue(pid.isdigit())

    def test_the_bridge_starts_a_stopped_daemon(self):
        self.mk("setup", "generic", "--project", "demo")
        self.mk("--profile", "default", "stop")
        result = self.bridge([("remember", {"text": "after a stop"})])
        self.assertFalse(result[0]["isError"], result)

    def test_without_autostart_a_missing_daemon_is_transport_unavailable_with_the_command(self):
        root = self.home / "s"
        cred = self.mk("init", "--store", str(root / "db"), "--control", str(root / "ctl"))
        credential = json.loads(cred.stdout)["credential_file"]
        done = self.mk(
            "status",
            "--socket",
            str(root / "db" / "service.sock"),
            "--credential",
            credential,
            check=False,
        )
        self.assertEqual(2, done.returncode)
        error = json.loads(done.stderr)
        self.assertEqual("transport_unavailable", error["error"])
        self.assertIn("memoryking", error["start"])


if __name__ == "__main__":
    unittest.main()
