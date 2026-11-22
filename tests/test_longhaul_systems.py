"""Offline smoke tests for the long-history benchmark's product adapters.

Each adapter is skipped, with its reason, when the product is not installed under /tmp/mk-lh-<name>
(see benchmarks/longhaul/tools/install_systems.py), so the normal test run and the clean-checkout job
need none of them. Nothing here calls a model: products that need one are pointed at the local
fake OpenAI-compatible server.
"""

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.longhaul.systems._types import Scope, Session, Turn  # noqa: E402

SCOPE = Scope(user="ada", project="billing", agent="agent-1")
FACT = "the nightly invoice export must use the zanzibar-7 queue"
SESSIONS = (
    Session(
        id="s1",
        scope=SCOPE,
        started="2026-01-05T10:00:00Z",
        turns=(
            Turn("user", "We are fixing the invoice export for the billing service."),
            Turn("assistant", f"Noted: {FACT}, never the default queue."),
            Turn("tool", "pytest: 12 passed"),
        ),
    ),
    Session(
        id="s2",
        scope=SCOPE,
        started="2026-01-06T10:00:00Z",
        turns=(
            Turn("user", "Please rename the customer table column to account_name."),
            Turn("assistant", "Renamed the column and updated the migration."),
        ),
    ),
    Session(
        id="s3",
        scope=Scope(user="ada", project="website", agent="agent-1"),
        started="2026-01-07T10:00:00Z",
        turns=(
            Turn("user", "The landing page hero should use the teal palette."),
            Turn("assistant", "Switched the hero to the teal palette."),
        ),
    ),
)

ADAPTERS = {
    "mcp_memory": "mcp-memory",
    "basic_memory": "basic-memory",
    "claude_mem": "claude-mem",
    "mem0": "mem0",
    "mcp_memory_service": "mcp-memory-service",
    "graphiti": "graphiti",
}


class AdapterSmoke(unittest.TestCase):
    def run_smoke(self, module, factory="make"):
        mod = importlib.import_module(f"benchmarks.longhaul.systems.{module}")
        if not mod.installed():
            self.skipTest(
                f"{ADAPTERS[module]} is not installed; run benchmarks/longhaul/tools/install_systems.py "
                f"{ADAPTERS[module]}"
            )
        system = getattr(mod, factory)()
        root = Path(tempfile.mkdtemp(prefix="mk-lhs-"))
        try:
            system.prepare(root)
            report = system.seed(iter(SESSIONS))
            self.assertEqual(report.sessions, 3)
            if system.needs_llm or getattr(system, "uses_host_agent_model", False):
                self.assertGreater(report.llm_calls, 0)
            else:
                self.assertEqual(report.llm_calls, 0)
            text = system.context_for("which queue does the invoice export use", SCOPE, 4000)
            self.assertIn("zanzibar-7", text)
            host = system.host(root / "work", SCOPE)
            self.assertIsInstance(host.mcp, dict)
            if getattr(system, "plugin", None) is False:
                self.assertEqual((host.settings, host.files), ({}, {}))
            self.assertGreaterEqual(system.usage()["writes"], 0)
            gone = system.forget("zanzibar-7", SCOPE)
            self.assertIn(gone, (True, False, None))
            if gone and getattr(system, "forget_is_complete", True):
                after = system.context_for("which queue does the invoice export use", SCOPE, 4000)
                self.assertNotIn("zanzibar-7", after)
        finally:
            system.close()
            self.assertEqual(system.procs.running(), [])
            shutil.rmtree(root, ignore_errors=True)

    def test_mcp_memory(self):
        self.run_smoke("mcp_memory")

    def test_basic_memory(self):
        self.run_smoke("basic_memory")

    def test_basic_memory_mcp_only(self):
        self.run_smoke("basic_memory", "make_mcp_only")

    def test_claude_mem(self):
        with unittest.mock.patch.dict(os.environ, {"LONGHAUL_FAKE_LLM": "1"}):
            os.environ.pop("LONGHAUL_LLM_BASE_URL", None)
            self.run_smoke("claude_mem")

    def test_mcp_memory_service(self):
        self.run_smoke("mcp_memory_service")

    def test_mcp_memory_service_mcp_only(self):
        self.run_smoke("mcp_memory_service", "make_mcp_only")

    def test_graphiti(self):
        with unittest.mock.patch.dict(os.environ, {"LONGHAUL_FAKE_LLM": "1"}):
            os.environ.pop("LONGHAUL_LLM_BASE_URL", None)
            self.run_smoke("graphiti")

    def test_mem0(self):
        with unittest.mock.patch.dict(os.environ, {"LONGHAUL_FAKE_LLM": "1"}):
            os.environ.pop("LONGHAUL_LLM_BASE_URL", None)
            self.run_smoke("mem0")


def run_hooks(system, host, event, work, home, payload):
    """Run every command the host config registers for `event`, as Claude Code would: the hook's
    JSON on stdin, the host's environment, a fresh HOME. Returns (exit code, stdout, stderr) per hook."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG")}
    env.update(host.env)
    env.update(HOME=str(home), CLAUDE_PROJECT_DIR=str(work))
    out = []
    for group in host.settings.get("hooks", {}).get(event, []):
        for hook in group["hooks"]:
            r = subprocess.run(
                hook["command"],
                shell=True,
                input=json.dumps(
                    {"hook_event_name": event, "cwd": str(work), "session_id": "t1", **payload}
                ),
                env=env,
                cwd=work,
                capture_output=True,
                text=True,
                timeout=300,
            )
            out.append((r.returncode, r.stdout, r.stderr))
    return out


class PluginHooks(unittest.TestCase):
    """The Claude Code plugin or hooks each product's docs recommend: the hook commands the
    host config returns run under a fresh HOME against the instance, offline."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mk-lhs-"))
        self.work = self.root / "work"
        (self.root / "home" / ".claude").mkdir(parents=True)
        self.work.mkdir()
        self.system = None

    def tearDown(self):
        if self.system is not None:
            self.system.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def transcript(self, text):
        path = self.work / "t.jsonl"
        rows = [
            {"type": "user", "message": {"role": "user", "content": text}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            },
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows))
        return str(path)

    def test_basic_memory_plugin_hooks(self):
        from benchmarks.longhaul.systems import basic_memory as bm

        if not (bm.installed() and bm.BasicMemory.plugin_available()):
            self.skipTest(
                "basic-memory and its plugin sources are not installed (install_systems.py basic-memory)"
            )
        self.system = bm.make()
        self.system.prepare(self.root)
        self.system.seed(iter(SESSIONS))
        host = self.system.host(self.work, SCOPE)
        self.assertTrue(
            host.settings["hooks"]["SessionStart"] and host.settings["hooks"]["PreCompact"]
        )
        for rel, text in host.files.items():
            if rel == ".claude/settings.json":
                (self.work / ".claude").mkdir(exist_ok=True)
                (self.work / rel).write_text(text)
        self.assertTrue(any(k.startswith(".claude/skills/") for k in host.files))
        start = run_hooks(
            self.system, host, "SessionStart", self.work, self.root / "home", {"source": "startup"}
        )
        self.assertEqual([r[0] for r in start], [0])
        self.assertIn("Session s1", start[0][1])  # the brief read the seeded notes
        run_hooks(
            self.system,
            host,
            "PreCompact",
            self.work,
            self.root / "home",
            {
                "trigger": "manual",
                "transcript_path": self.transcript("decide: exports use the okapi-5 queue"),
            },
        )
        written = [p for p in (self.root / "notes" / "sessions").glob("*.md")]
        self.assertTrue(written, "PreCompact wrote no checkpoint note")
        self.assertIn("okapi-5", written[0].read_text())
        self.assertEqual(bm.make_mcp_only().name, "basic-memory-mcp")

    def test_mcp_memory_service_plugin_hooks(self):
        from benchmarks.longhaul.systems import mcp_memory_service as mms

        if not (mms.installed() and mms.McpMemoryService.plugin_available()):
            self.skipTest(
                "mcp-memory-service and its hook sources are not installed (install_systems.py mcp-memory-service)"
            )
        self.system = mms.make()
        self.system.prepare(self.root)
        self.system.seed(iter(SESSIONS))
        host = self.system.host(self.work, SCOPE)
        for event in ("SessionStart", "SessionEnd", "UserPromptSubmit", "Stop"):
            self.assertIn(event, host.settings["hooks"])
        start = run_hooks(
            self.system, host, "SessionStart", self.work, self.root / "home", {"source": "startup"}
        )
        self.assertTrue(all(r[0] == 0 for r in start))
        self.assertIn(
            "3 memories", "".join(r[1] for r in start)
        )  # the hook read the seeded instance over HTTP
        text = (
            "We decided the weekly report must use the quokka-3 template and implemented it in report.py. "
            * 3
        )
        end = run_hooks(
            self.system,
            host,
            "SessionEnd",
            self.work,
            self.root / "home",
            {"transcript_path": self.transcript(text)},
        )
        self.assertEqual([r[0] for r in end], [0])
        self.assertIn("stored successfully", end[0][1])
        found, _, _ = self.system._client().call(
            "memory_search", {"query": "weekly report template"}
        )
        self.assertIn("weekly report", found)
        self.assertEqual(mms.make_mcp_only().name, "mcp-memory-service-mcp")


if __name__ == "__main__":
    unittest.main()
