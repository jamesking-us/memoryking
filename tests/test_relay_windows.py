"""The Windows client through a real daemon in WSL2 or the container image.

These run only on Windows, and only when `MEMORYKING_TEST_RELAY` names the back end
(`wsl:DISTRO` or `docker:CONTAINER`) and `MEMORYKING_TEST_RELAY_PYTHON` the Python with
MemoryKing on that side. Each test class creates a disposable store there, starts its
daemon, and removes both afterwards; nothing on the Windows side is written.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import unittest
from typing import Any

from memoryking import __version__
from memoryking.domain.local import LocalError
from memoryking.relay_client import connect
from memoryking.sdk import LocalClient

SPEC = os.environ.get("MEMORYKING_TEST_RELAY", "")
PYTHON = os.environ.get("MEMORYKING_TEST_RELAY_PYTHON", "")
REASON = (
    "needs Windows with MEMORYKING_TEST_RELAY (wsl:DISTRO or docker:CONTAINER) and "
    "MEMORYKING_TEST_RELAY_PYTHON"
)
RECEIPT = re.compile(r"rcpt_[0-9a-f]+")
# Python's default on Windows: piped stdout in the ANSI code page, not UTF-8.
DEFAULT_STDIO = {
    k: v for k, v in os.environ.items() if k not in {"PYTHONIOENCODING", "PYTHONUTF8"}
} | {"PYTHONUTF8": "0"}
START = """
import json, os, subprocess, sys, tempfile, time
from pathlib import Path
root = Path(tempfile.mkdtemp(prefix="mk-wintest-"))
os.chmod(root, 0o700)
store, control = root / "data", root / "control"
subprocess.run([sys.executable, "-m", "memoryking", "init", "--store", str(store),
                "--control", str(control)], check=True, capture_output=True)
log = open(root / "serve.log", "wb")
daemon = subprocess.Popen([sys.executable, "-m", "memoryking", "serve", "--store", str(store),
                           "--control", str(control)], stdin=subprocess.DEVNULL, stdout=log,
                          stderr=log, start_new_session=True)
for _ in range(400):
    if (store / "service.sock").exists():
        break
    time.sleep(0.05)
import memoryking
print(json.dumps({"root": str(root), "pid": daemon.pid, "version": memoryking.__version__,
                  "socket": str(store / "service.sock"),
                  "credential": str(control / "owner.credential")}))
"""
STOP = """
import os, shutil, signal, sys, time
pid, root = int(sys.argv[1]), sys.argv[2]
os.kill(pid, signal.SIGTERM)
for _ in range(200):
    try:
        os.kill(pid, 0)
    except OSError:
        break
    time.sleep(0.05)
shutil.rmtree(root)
"""


def on_store_side(code: str, *args: str) -> str:
    kind, _, name = SPEC.partition(":")
    prefix = ["wsl.exe", "-d", name, "--exec"] if kind == "wsl" else ["docker", "exec", name]
    return subprocess.run(
        [*prefix, PYTHON, "-c", textwrap.dedent(code), *args],
        check=True,
        capture_output=True,
        timeout=120,
    ).stdout.decode()


@unittest.skipUnless(sys.platform == "win32" and SPEC and PYTHON, REASON)
class WindowsRelayTests(unittest.TestCase):
    store: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.store = json.loads(on_store_side(START))

    @classmethod
    def tearDownClass(cls) -> None:
        on_store_side(STOP, str(cls.store["pid"]), cls.store["root"])

    def cli(self, *argv: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                sys.executable, "-m", "memoryking", "--relay", SPEC, "--relay-python", PYTHON,
                *argv, "--socket", self.store["socket"], "--credential", self.store["credential"],
                "--project", "wintest",
            ],
            input=stdin,
            capture_output=True,
            timeout=120,
            env=DEFAULT_STDIO,
        )  # fmt: skip

    def test_the_store_side_runs_this_version(self) -> None:
        self.assertEqual(self.store["version"], __version__)

    def test_cli_capture_and_recall(self) -> None:
        saved = self.cli("capture", "--text", "Windows CLI capture reaches the daemon.")
        self.assertEqual(saved.returncode, 0, saved.stderr)
        self.assertIn("observation", json.loads(saved.stdout))
        brief = self.cli("recall", "--query", "Windows CLI capture daemon")
        self.assertEqual(brief.returncode, 0, brief.stderr)
        self.assertIn("Windows CLI capture reaches the daemon.", brief.stdout.decode("utf-8"))

    def test_output_beyond_the_ansi_code_page_survives_a_pipe(self) -> None:
        text = "Arrows → and checks ✓ survive the pipe."
        self.assertEqual(self.cli("capture", "--text", text).returncode, 0)
        brief = self.cli("recall", "--query", "arrows checks survive pipe")
        self.assertEqual(brief.returncode, 0, brief.stderr)
        self.assertIn(text, brief.stdout.decode("utf-8"))

    def test_piped_text_loses_powershell_marks_and_crlf_but_keeps_its_characters(self) -> None:
        piped = "﻿﻿Café naïve 3× rule\r\nsecond line\r\n".encode()
        saved = self.cli("capture", "--file", "-", "--source", "pipe", stdin=piped)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        found = json.loads(self.cli("recall", "--query", "Café naïve rule", "--json").stdout)
        texts = [item["text"] for item in found["guidance"]]
        self.assertIn("Café naïve 3× rule\nsecond line\n", texts)

    def hook(self, module: str, kind: str, event: dict[str, Any]) -> tuple[int, str]:
        result = subprocess.run(
            [
                sys.executable, "-m", module, "--relay", SPEC, "--relay-python", PYTHON,
                "hook", kind, "--host", "claude", "--socket", self.store["socket"],
                "--credential", self.store["credential"], "--project", "wintest", "--strict",
            ],
            input=json.dumps(event).encode(),
            capture_output=True,
            timeout=120,
            env=DEFAULT_STDIO,
        )  # fmt: skip
        return result.returncode, result.stdout.decode("utf-8")

    def test_the_light_hook_entry_answers_as_the_cli_does(self) -> None:
        self.cli("capture", "--text", "Hook recall on Windows finds the deploy freeze rule.")
        start = {"hook_event_name": "SessionStart", "session_id": "w1", "source": "startup"}
        prompt = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "w1",
            "prompt": "what is the deploy freeze rule for hook recall on Windows",
        }
        end = {"hook_event_name": "SessionEnd", "session_id": "w1", "reason": "exit"}
        for kind, event in (("session-start", start), ("prompt", prompt), ("session-end", end)):
            with self.subTest(kind=kind):
                full = self.hook("memoryking", kind, event)
                light = self.hook("memoryking.hook_cli", kind, event)
                self.assertEqual(full[0], 0)
                self.assertEqual(light[0], 0)
                if kind != "session-start":  # a session start lists recent records, which move
                    # Each delivery has its own receipt; everything else is the same.
                    self.assertEqual(RECEIPT.sub("rcpt_", light[1]), RECEIPT.sub("rcpt_", full[1]))
        self.assertIn("deploy freeze rule", self.hook("memoryking.hook_cli", "prompt", prompt)[1])

    def test_sdk_through_one_relay(self) -> None:
        endpoint, credential = connect(SPEC, self.store["socket"], self.store["credential"], PYTHON)
        with endpoint:
            client = LocalClient(endpoint, credential, "wintest")
            saved = client.remember("The SDK on Windows uses one relay.", source="sdk")
            self.assertFalse(saved.held)
            self.assertIn("one relay", client.recall("SDK Windows relay").text)

    def test_a_store_command_is_refused_here(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "memoryking", "init", "--store", "s", "--control", "c"],
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(json.loads(result.stderr)["error"], "local_store_unsupported")

    def test_mcp_bridge_through_the_relay(self) -> None:
        bridge = subprocess.Popen(
            [
                sys.executable, "-m", "memoryking", "mcp", "--relay", SPEC, "--relay-python",
                PYTHON, "--socket", self.store["socket"], "--credential",
                self.store["credential"], "--project", "wintest",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )  # fmt: skip
        assert bridge.stdin is not None and bridge.stdout is not None

        def call(message: dict[str, Any]) -> dict[str, Any] | None:
            assert bridge.stdin is not None and bridge.stdout is not None
            bridge.stdin.write(json.dumps(message).encode() + b"\n")
            bridge.stdin.flush()
            return json.loads(bridge.stdout.readline()) if "id" in message else None

        try:
            hello = call(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                }
            )
            self.assertIn("result", hello or {})
            call({"jsonrpc": "2.0", "method": "notifications/initialized"})
            remembered = call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "remember",
                        "arguments": {"text": "The bridge on Windows saved this.", "source": "mcp"},
                    },
                }
            )
            self.assertIn("Saved memory", json.dumps(remembered))
            recalled = call(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "recall", "arguments": {"query": "bridge Windows saved"}},
                }
            )
            self.assertIn("The bridge on Windows saved this.", json.dumps(recalled))
        finally:
            bridge.stdin.close()
            bridge.wait(timeout=30)
            bridge.stdout.close()
            assert bridge.stderr is not None
            bridge.stderr.close()

    def test_a_wrong_socket_is_refused_on_the_store_side(self) -> None:
        with self.assertRaises(LocalError) as caught:
            connect(SPEC, self.store["root"], None, PYTHON)
        self.assertEqual(caught.exception.code, "unsafe_socket")


if __name__ == "__main__":
    unittest.main()
