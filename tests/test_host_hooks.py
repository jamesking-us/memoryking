"""Host hooks against a real local service: load at start, save before loss, install."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_knowledge as knowledge_helpers

from memoryking import host_hooks
from memoryking.adapters.local_ipc import Server, request
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.checkpoint import complete
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


class StartNoteTests(unittest.TestCase):
    def test_the_note_for_a_command_line_agent_saves_from_a_file_or_stdin_not_a_quoted_text(self):
        # A host refused `capture --text "..."` commands whose text had parentheses, and a
        # heredoc whose text had braces and quotes; a file written with the file tool passes.
        note = host_hooks.start_note("claude", "cli")
        self.assertIn("`memoryking capture --source agent --file FILE`", note)
        self.assertIn("pipe", note)
        self.assertNotIn('--text "..."`', note)
        self.assertLess(len(note), 1100)
        self.assertIn("mcp__memoryking__remember", host_hooks.start_note("claude", "mcp"))


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

    def test_transcript_tail_keeps_turns_and_is_bounded(self):
        tail = host_hooks.transcript_tail(self.transcript)
        self.assertIn("scoped per request", tail)
        self.assertIn("Tests pass now.", tail)
        self.assertIn("User: Fix the retry bug", tail)
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
        self.assertIn("# Resume: Session sess on demo (hook capture)", text)
        self.assertIn("scoped per request", text)
        self.assertIn("# Briefing: retry deadline", text)
        self.assertIn("claude-compact", text)

    def _event(self, name, session="sess", **extra):
        return host_hooks.read_event(
            json.dumps(
                {
                    "session_id": session,
                    "transcript_path": str(self.transcript),
                    "hook_event_name": name,
                    "prompt_id": "p",
                    **extra,
                }
            ),
            {},
        )

    def _agent_checkpoint(self, task):
        context = request(self.server.path, self.token(), "context", {"project": "demo"})
        return request(
            self.server.path,
            self.token(),
            "working",
            {
                "command": {
                    "version": 1,
                    "incarnation": context["incarnation"],
                    "action": "save",
                    "key": "agent-own-checkpoint",
                    "args": {
                        "project": "demo",
                        "task": task,
                        "run": None,
                        "readers": None,
                        "expected_revision": None,
                        "checkpoint": complete(
                            {
                                "objective": "Agent objective",
                                "current_state": "AGENT-AUTHORED-STATE",
                            },
                            0,
                        ),
                    },
                }
            },
        )

    def token(self):
        return self.credential_file.read_text().strip()

    def test_transcript_tail_never_reads_tool_inputs_and_scrubs_secrets(self):
        lines = [
            {"type": "user", "message": {"role": "user", "content": "Deploy with care"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "Writing the config. Authorization: Bearer abcdefghijklmnopqrstuvwx",
                        },
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {
                                "file_path": "/w/.env",
                                "content": "API_KEY=sk-live-PLANTEDSECRET1234567890\n",
                                "text": "FILEBODYMARKER",
                            },
                        },
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "content": "SUCCESSFUL-RESULT-BODY"},
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "content": [
                                {"type": "text", "text": "ENOENT while password=hunter2hunter2"}
                            ],
                        },
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": "key AKIAIOSFODNN7EXAMPLE ghp_"
                    + "a" * 36
                    + " sk-ant-api03-"
                    + "b" * 30,
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": "-----BEGIN RSA PRIVATE KEY-----\nMIIBOg\n-----END RSA PRIVATE KEY-----",
                },
            },
        ]
        path = self.root / "secrets.jsonl"
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        tail = host_hooks.transcript_tail(path)
        self.assertIn("User: Deploy with care", tail)
        self.assertIn("Tool error: ENOENT", tail)
        for leaked in (
            "PLANTEDSECRET",
            "FILEBODYMARKER",
            "SUCCESSFUL-RESULT-BODY",
            "hunter2",
            "abcdefghijklmnopqrstuvwx",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_",
            "sk-ant",
            "MIIBOg",
        ):
            self.assertNotIn(leaked, tail)
        self.assertIn(host_hooks.REDACTED, tail)
        event = host_hooks.read_event(
            json.dumps(
                {
                    "session_id": "s",
                    "transcript_path": str(path),
                    "hook_event_name": "SessionEnd",
                    "prompt_id": "p",
                }
            ),
            {},
        )
        result = host_hooks.save(event, "session-end", task="t", **self.where)
        self.assertGreaterEqual(result["redacted"], 5)
        text = host_hooks.session_start(event, query="Deploy with care", task="t", **self.where)
        self.assertIn("Deploy with care", text)
        self.assertIn("secret-shaped value(s) redacted", text)
        for leaked in ("PLANTEDSECRET", "hunter2", "AKIAIOSFODNN7EXAMPLE"):
            self.assertNotIn(leaked, text)
        plain = host_hooks.scrub('token_count=5 and password: s3cretpass api_key="abcd1234"')
        self.assertEqual(plain[1], 2)
        self.assertIn("token_count=5", plain[0])

    def test_user_instruction_is_recalled_at_session_start(self):
        lines = [
            {
                "type": "user",
                "message": {"role": "user", "content": "Always use pgbouncer transaction pooling"},
            },
            {"type": "assistant", "message": {"role": "assistant", "content": "Noted."}},
        ]
        self.transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))
        host_hooks.save(self._event("SessionEnd"), "session-end", **self.where)
        text = host_hooks.session_start(
            self._event("SessionStart"), query="pgbouncer pooling", **self.where
        )
        self.assertIn("pgbouncer transaction pooling", text)

    def test_agent_checkpoint_survives_hooks_and_both_hooks_succeed(self):
        self._agent_checkpoint("retry-fix")
        compact = self._event("PreCompact", trigger="auto")
        first = host_hooks.save(compact, "pre-compact", task="retry-fix", **self.where)
        ended = host_hooks.save(
            self._event("SessionEnd"), "session-end", task="retry-fix", **self.where
        )
        again = host_hooks.save(compact, "pre-compact", task="retry-fix", **self.where)
        self.assertEqual((first["revision"], ended["revision"]), (1, 2))
        self.assertEqual(again["checkpoint"], first["checkpoint"])
        text = host_hooks.session_start(
            self._event("SessionStart"), query="retry", task="retry-fix", **self.where
        )
        # The agent's own checkpoint (revision 1 of its stream) is still the first resume.
        self.assertIn("# Resume: Agent objective", text)
        self.assertIn("AGENT-AUTHORED-STATE", text)
        self.assertIn("(hook capture)", text)
        self.assertLess(text.index("AGENT-AUTHORED-STATE"), text.index("(hook capture)"))
        self.assertEqual(host_hooks.hook_task("t"), "t--mkhook")
        self.assertLessEqual(len(host_hooks.hook_task("x" * 96)), 96)

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
        self.assertEqual(
            sorted(config["hooks"]),
            ["PreCompact", "SessionEnd", "SessionStart", "UserPromptSubmit"],
        )
        prompt = config["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(prompt), 1)
        self.assertIn("hook prompt --host claude --project demo", prompt[0]["hooks"][0]["command"])
        self.assertEqual(prompt[0]["hooks"][0]["timeout"], 5)
        self.assertNotIn("--task", prompt[0]["hooks"][0]["command"])
        self.assertEqual(len(config["hooks"]["PreCompact"]), 2)  # theirs kept, ours once
        self.assertEqual(config["hooks"]["PreCompact"][0]["hooks"][0]["command"], "echo mine")
        start = config["hooks"]["SessionStart"][0]
        self.assertEqual(start["matcher"], "startup|resume|compact")
        self.assertIn(
            "hook session-start --host claude --project demo", start["hooks"][0]["command"]
        )
        self.assertIn("--profile home", start["hooks"][0]["command"])
        self.assertIn("--task retry-fix", start["hooks"][0]["command"])
        without = host_hooks.install(host="claude", prompts=False, **common)
        self.assertEqual(
            sorted(without["config"]["hooks"]), ["PreCompact", "SessionEnd", "SessionStart"]
        )
        self.assertNotIn("UserPromptSubmit", without["events"])
        self.assertEqual(len(without["config"]["hooks"]["PreCompact"]), 2)
        host_hooks.install(host="claude", **common)
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
            events = ["PreCompact", "SessionEnd", "SessionStart"]
            # Grok ignores hook output, so a recall-at-prompt hook there would only cost time.
            self.assertEqual(
                sorted(written["hooks"]), events + (["UserPromptSubmit"] if host == "codex" else [])
            )
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

    def test_install_writes_bindings_into_each_command_and_stays_idempotent(self):
        cwd, home = self.root / "repo", self.root / "home"
        cwd.mkdir()
        common = {
            "scope": "project",
            "project": "demo",
            "cwd": cwd,
            "home": home,
            "profile": "home",
            "task": "retry-fix",
        }
        path = cwd / ".claude/settings.json"
        plain = host_hooks.install(host="claude", **common)
        pairs = ["role=implement", "environment=prod"]
        bound = host_hooks.install(host="claude", binding=pairs, **common)
        self.assertNotEqual(plain["config"], bound["config"])
        written = path.read_text()
        for groups in json.loads(written)["hooks"].values():
            self.assertEqual(len(groups), 1)  # replaced, never duplicated
            command = groups[0]["hooks"][0]["command"]
            self.assertTrue(
                command.endswith('--binding "environment=prod" --binding "role=implement"'),
                command,
            )
        # The same arguments, in any order, change nothing.
        host_hooks.install(host="claude", binding=pairs[::-1], **common)
        host_hooks.install(host="claude", binding=pairs, **common)
        self.assertEqual(path.read_text(), written)
        with self.assertRaisesRegex(LocalError, "invalid_binding"):
            host_hooks.install(host="claude", binding=["colour=blue"], **common)
        removed = host_hooks.install(host="claude", binding=pairs, remove=True, **common)
        self.assertNotIn("hooks", removed["config"])

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
                        "task": "retry-fix--mkhook",
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
        self.assertEqual(state["objective"], "Session sess on demo (hook capture)")
        self.assertIn("scoped per request", state["current_state"])
        self.assertEqual(state["references"][0]["kind"], "observation")
        self.assertIn("defaulted:next_actions", state["capture"]["gaps"])


class HookBindingTests(unittest.TestCase):
    """Hooks and the CLI carry session bindings, so a scoped constraint does not lock them out."""

    run_k = knowledge_helpers.KnowledgeTests.run_k
    body = knowledge_helpers.KnowledgeTests.body
    propose = knowledge_helpers.KnowledgeTests.propose
    review = knowledge_helpers.KnowledgeTests.review

    def setUp(self):
        knowledge_helpers.KnowledgeTests.setUp(self)
        self.credential = self.control / "owner.credential"
        self.server = Server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.where = {"socket": self.server.path, "credential": self.credential, "project": "p"}
        self.review(
            self.propose(
                self.body(
                    "Never touch production data",
                    kind="constraint",
                    authority="project",
                    rule_key="prod",
                    effect="prohibit",
                    rule_value="write",
                    selector=knowledge_helpers.sel(knowledge_helpers.eq("environment", "prod")),
                ),
                key="prod-rule",
            )
        )
        token = self.credential.read_text().strip()
        context = request(self.server.path, token, "context", {"project": "p"})
        request(
            self.server.path,
            token,
            "working",
            {
                "command": {
                    "version": 1,
                    "incarnation": context["incarnation"],
                    "action": "save",
                    "key": "own-checkpoint",
                    "args": {
                        "project": "p",
                        "task": "retry-fix",
                        "run": None,
                        "readers": None,
                        "expected_revision": None,
                        "checkpoint": complete(
                            {"objective": "Own objective", "current_state": "OWN-STATE"}, 0
                        ),
                    },
                }
            },
        )
        self.env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"PYTHONPATH", "PYTHONHOME"} and not k.startswith("MEMORYKING_")
        }

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        knowledge_helpers.KnowledgeTests.tearDown(self)

    def test_session_start_names_the_missing_field_and_still_resumes_own_state(self):
        with patch.dict(os.environ, self.env, clear=True):
            text = host_hooks.session_start({}, query="retry", task="retry-fix", **self.where)
        self.assertIn("# Resume: Own objective", text)
        self.assertIn("OWN-STATE", text)
        self.assertIn("Missing binding fields: `environment`", text)
        self.assertIn("# Briefing: retry", text)
        self.assertNotIn("Never touch production", text)

    def test_memoryking_bindings_environment_resolves_the_constraint(self):
        with patch.dict(os.environ, {**self.env, "MEMORYKING_BINDINGS": "environment=prod"}):
            text = host_hooks.session_start({}, query="retry", task="retry-fix", **self.where)
        self.assertNotIn("Blocked", text)
        self.assertIn("Never touch production data", text)
        self.assertIn("OWN-STATE", text)
        # An explicit binding wins over the environment, key by key.
        with patch.dict(os.environ, {**self.env, "MEMORYKING_BINDINGS": "environment=prod"}):
            text = host_hooks.session_start(
                {}, query="retry", binding=["environment=dev"], **self.where
            )
        self.assertNotIn("Never touch production data", text)
        self.assertNotIn("Blocked", text)

    def test_cli_hook_and_recall_accept_the_environment_variable_and_flag(self):
        connected = [
            "--project",
            "p",
            "--socket",
            str(self.server.path),
            "--credential",
            str(self.credential),
        ]

        def run(*args, env=None):
            done = subprocess.run(
                [sys.executable, "-m", "memoryking", *args],
                input="{}",
                capture_output=True,
                text=True,
                env={**self.env, **(env or {})},
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout

        blocked = run("hook", "session-start", *connected, "--query", "retry")
        self.assertIn("Missing binding fields: `environment`", blocked)
        by_env = run(
            "hook",
            "session-start",
            *connected,
            "--query",
            "retry",
            env={"MEMORYKING_BINDINGS": "environment=prod"},
        )
        self.assertIn("Never touch production data", by_env)
        self.assertNotIn("Blocked", by_env)
        by_flag = run("recall", *connected, "--query", "retry", "--binding", "environment=prod")
        self.assertIn("Never touch production data", by_flag)
        self.assertIn("Missing binding fields", run("recall", *connected, "--query", "retry"))

    def test_installed_command_resolves_the_constraint_like_the_bridge(self):
        cwd = self.root / "repo"
        cwd.mkdir()
        install = [
            sys.executable,
            "-m",
            "memoryking",
            "hooks",
            "install",
            "--host",
            "claude",
            "--project",
            "p",
            "--socket",
            str(self.server.path),
            "--credential",
            str(self.credential),
            "--task",
            "retry-fix",
            "--query",
            "retry",
            "--cwd",
            str(cwd),
        ]

        def installed(*extra):
            done = subprocess.run([*install, *extra], capture_output=True, text=True, env=self.env)
            self.assertEqual(done.returncode, 0, done.stderr)
            config = json.loads((cwd / ".claude/settings.json").read_text())
            return config["hooks"]["SessionStart"][0]["hooks"][0]["command"]

        def start(command):
            done = subprocess.run(
                command, shell=True, input="{}", capture_output=True, text=True, env=self.env
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout

        self.assertIn("Missing binding fields: `environment`", start(installed()))
        command = installed("--binding", "environment=prod")
        self.assertIn('--binding "environment=prod"', command)
        text = start(command)
        self.assertNotIn("Blocked", text)
        self.assertIn("Never touch production data", text)
        self.assertIn("OWN-STATE", text)
        self.assertEqual(installed("--binding", "environment=prod"), command)
