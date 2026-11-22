"""Offline smoke tests for the long-history benchmark's product adapters.

Each adapter is skipped, with its reason, when the product is not installed under /tmp/mk-lh-<name>
(see benchmarks/longhaul/tools/install_systems.py), so the normal test run and the clean-checkout job
need none of them. Nothing here calls a model: products that need one are pointed at the local
fake OpenAI-compatible server.
"""

import importlib
import os
import shutil
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
    def run_smoke(self, module):
        mod = importlib.import_module(f"benchmarks.longhaul.systems.{module}")
        if not mod.installed():
            self.skipTest(
                f"{ADAPTERS[module]} is not installed; run benchmarks/longhaul/tools/install_systems.py "
                f"{ADAPTERS[module]}"
            )
        system = mod.make()
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

    def test_claude_mem(self):
        with unittest.mock.patch.dict(os.environ, {"LONGHAUL_FAKE_LLM": "1"}):
            os.environ.pop("LONGHAUL_LLM_BASE_URL", None)
            self.run_smoke("claude_mem")

    def test_mcp_memory_service(self):
        self.run_smoke("mcp_memory_service")

    def test_graphiti(self):
        with unittest.mock.patch.dict(os.environ, {"LONGHAUL_FAKE_LLM": "1"}):
            os.environ.pop("LONGHAUL_LLM_BASE_URL", None)
            self.run_smoke("graphiti")

    def test_mem0(self):
        with unittest.mock.patch.dict(os.environ, {"LONGHAUL_FAKE_LLM": "1"}):
            os.environ.pop("LONGHAUL_LLM_BASE_URL", None)
            self.run_smoke("mem0")


if __name__ == "__main__":
    unittest.main()
