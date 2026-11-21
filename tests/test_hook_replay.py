"""Hooks end to end: what session 1's hooks capture is what session 2's start briefing shows.

Synthetic transcripts in the shape Claude Code writes (a user prompt as a plain string, assistant
text and tool calls, tool results as user messages, system-reminder lines marked `isMeta`) are
replayed through the session-end hook into a real store; the session-start hook for a new
session then has to put the carried knowledge in front of the model, with no query and no
model choosing to call a tool. One case per knowledge kind of the task-outcome benchmark, using
that task's own session-1 prompt. No real transcript is committed."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from memoryking import host_hooks
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore

TASKS = Path(__file__).resolve().parents[1] / "benchmarks" / "outcomes" / "tasks"


def task(name):
    meta = json.loads((TASKS / name / "meta.json").read_text())
    return (TASKS / name / "session1.md").read_text().strip(), meta


def transcript(prompt, closing, *, noise=0, error=None):
    """Lines as Claude Code writes them: the prompt, some tool work, a closing message."""
    meta_line = {
        "type": "user",
        "isMeta": True,
        "message": {
            "role": "user",
            "content": "<system-reminder>As you answer, use tools.</system-reminder>",
        },
    }
    lines = [
        meta_line,
        {"type": "user", "message": {"role": "user", "content": prompt}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll start by looking at the repository."},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
        },
    ]
    for n in range(noise):
        lines += [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": f"n{n}", "content": "ok " * 400}
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"Step {n}: continuing with the work. " * 6},
                        {
                            "type": "tool_use",
                            "id": f"n{n}",
                            "name": "Bash",
                            "input": {"command": "x"},
                        },
                    ],
                },
            },
        ]
    if error:
        lines.append(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "e",
                            "content": error,
                            "is_error": True,
                        }
                    ],
                },
            }
        )
    lines.append(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": closing}]},
        }
    )
    return "".join(json.dumps(line) + "\n" for line in lines)


class HookReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-replay-")
        self.root = Path(self.temp.name)
        credential = LocalStore.initialize(self.root / "db", self.root / "ctl")
        self.store = LocalStore(self.root / "db", self.root / "ctl")
        self.server = Server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.where = {"socket": self.server.path, "credential": credential, "project": "work"}

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def replay(self, text, **start):
        """Session 1 ends (its hook runs on the transcript); session 2 starts. Returns the
        text the host would add to the model's context."""
        path = self.root / "s1.jsonl"
        path.write_text(text)
        end = host_hooks.read_event(
            json.dumps(
                {
                    "session_id": "s1",
                    "transcript_path": str(path),
                    "hook_event_name": "SessionEnd",
                    "reason": "other",
                    "scratchpad_dir": "/x",
                }
            ),
            {},
        )
        self.assertTrue(host_hooks.save(end, "session-end", **self.where)["captured"])
        begin = host_hooks.read_event(
            json.dumps(
                {
                    "session_id": "s2",
                    "hook_event_name": "SessionStart",
                    "source": "startup",
                    "prompt_id": "p",
                }
            ),
            {},
        )
        return host_hooks.session_start(begin, **self.where, **start)

    def assertCarries(self, shown, markers):
        for marker in markers:
            self.assertIn(marker.lower(), shown.lower(), marker)

    def test_a_decision_the_user_stated_reaches_the_next_session(self):
        prompt, meta = task("alert-sink")
        shown = self.replay(
            transcript(
                prompt,
                "I added alerts/rules.py with evaluate() and tests; all tests pass.",
                noise=3,
            )
        )
        self.assertCarries(shown, meta["stub"]["must_recall"] + ["webhook sink only", "e-mail"])

    def test_an_environment_fact_reaches_the_next_session(self):
        prompt, meta = task("staging-collector")
        shown = self.replay(
            transcript(prompt, "Added metricsd/batch.py with chunks() and tests.", noise=3)
        )
        self.assertCarries(
            shown, ["9411", "/v4/ingest", "ENABLE_COLLECTOR_V2", "X-Collector-Key", "COLLECTOR_KEY"]
        )
        prompt, meta = task("partner-feed")
        shown = self.replay(transcript(prompt, "Added ingest/codes.py and tests.", noise=3))
        self.assertCarries(
            shown, ["ddmmyyyy", "CRC32", "minor units", "8 lowercase hex", "header count"]
        )

    def test_an_instruction_reaches_the_next_session(self):
        prompt, meta = task("legacy-rounding")
        shown = self.replay(transcript(prompt, "Done; the tests pass.", noise=2))
        self.assertCarries(shown, ["legacy/", "signature stays exactly"])
        prompt, meta = task("config-aliases")
        shown = self.replay(transcript(prompt, "Done.", noise=2))
        self.assertCarries(shown, ["not ours to edit", "old name has to keep working"])

    def test_what_the_agent_found_out_reaches_the_next_session(self):
        # The finding came from a long tool run the transcript hook never reads; what survives
        # is the agent's own closing report (and any tool error), the last thing in the session.
        prompt, meta = task("cache-lock")
        closing = (
            "Findings: a plain threading.Lock deadlocks the service because the eviction "
            "callback runs inside put() and calls cache.stats(), which takes the lock again; "
            "use an RLock. The hits, misses and evictions counters race as well, so every "
            "method that touches them needs the lock."
        )
        shown = self.replay(
            transcript(prompt, closing, noise=6, error="Traceback: deadlock detected")
        )
        self.assertCarries(shown, ["plain threading.Lock deadlocks", "RLock", "counters race"])

    def test_the_start_briefing_names_the_memory_tools_and_when_to_save(self):
        prompt, _ = task("alert-sink")
        shown = self.replay(transcript(prompt, "Done."), tools="mcp")
        for word in (
            "mcp__memoryking__remember",
            "mcp__memoryking__recall",
            "decision",
            "instruction",
        ):
            self.assertIn(word, shown)
        self.assertLess(shown.index("mcp__memoryking__remember"), shown.index("webhook sink only"))

    def test_an_empty_store_says_so_and_still_names_the_tools(self):
        begin = host_hooks.read_event(
            json.dumps({"session_id": "s", "hook_event_name": "SessionStart", "prompt_id": "p"}), {}
        )
        shown = host_hooks.session_start(begin, tools="mcp", **self.where)
        self.assertIn("mcp__memoryking__remember", shown)
        self.assertIn("nothing", shown.lower())

    def test_other_hosts_use_plain_tool_names(self):
        begin = host_hooks.read_event(
            json.dumps({"hookEventName": "SessionStart", "sessionId": "g"}),
            {"GROK_HOOK_EVENT": "SessionStart"},
        )
        shown = host_hooks.session_start(begin, tools="mcp", **self.where)
        self.assertIn("`remember`", shown)
        self.assertNotIn("mcp__", shown)

    def test_the_installed_command_names_the_tools_through_the_cli(self):
        installed = host_hooks.install(
            host="claude",
            scope="user",
            project="work",
            cwd=self.root,
            home=self.root,
            socket=self.where["socket"],
            credential=self.where["credential"],
            tools="mcp",
            write=False,
        )
        commands = {
            event: groups[0]["hooks"][0]["command"]
            for event, groups in installed["config"]["hooks"].items()
        }
        self.assertIn("--tools mcp", commands["SessionStart"])
        self.assertNotIn("--tools", commands["PreCompact"] + commands["SessionEnd"])
        # Run the start command the way a host does: the event on stdin, nothing else.
        import subprocess
        import sys

        proc = subprocess.run(
            commands["SessionStart"],
            shell=True,
            input='{"session_id": "x", "hook_event_name": "SessionStart"}',
            capture_output=True,
            text=True,
            timeout=60,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("mcp__memoryking__remember", proc.stdout)
        self.assertIn(sys.executable, commands["SessionStart"])

    def test_a_secret_in_a_user_turn_is_not_stored_or_shown(self):
        prompt = "Use the staging key api_key=sk-abcdefghijklmnop1234567890 when you send metrics."
        shown = self.replay(transcript(prompt, "Done."))
        self.assertNotIn("abcdefghijklmnop", shown)
        self.assertIn("staging key", shown)


if __name__ == "__main__":
    unittest.main()
