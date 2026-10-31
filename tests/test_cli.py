"""Operator CLI: version and the human status rendering."""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from memoryking import __version__
from memoryking.__main__ import doctor, duration_ms, render_grants, render_status
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError


class CliTests(unittest.TestCase):
    def test_version_flag_prints_the_installed_version(self):
        run = subprocess.run(
            [sys.executable, "-m", "memoryking", "--version"], capture_output=True, text=True
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "memoryking " + __version__)

    def test_status_rendering_shows_frontier_jobs_and_capacity_bars(self):
        result = {
            "store": "store_a",
            "incarnation": "hist_b",
            "position": 12,
            "projection": {"channel": "audit_probe", "generation": 1, "frontier": 12},
            "satisfied": True,
            "jobs": {"complete": 12},
            "capacity": {
                "jobs": {"used": 0, "limit": 1000, "warning": False},
                "rendered_bytes": {"used": 1048576, "limit": 134217728, "warning": False},
                "operation_intents": {"used": 4000, "limit": 4096, "warning": True},
            },
        }
        text = render_status(result, "0.0.0")
        self.assertIn("MemoryKing 0.0.0", text)
        self.assertIn("Frontier   12 / 12", text)
        self.assertIn("complete 12", text)
        self.assertIn("1.0 / 128 MiB", text)
        self.assertIn("[############]  warning", text)
        self.assertIn("Watermark  satisfied", text)


class OperatorCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-cli-", dir="/tmp")
        base = Path(self.temp.name)
        self.store_dir, self.control_dir = base / "store", base / "control"
        self.credential = LocalStore.initialize(self.store_dir, self.control_dir)
        self.store = LocalStore(self.store_dir, self.control_dir)
        self.server = Server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def cli(self, *args, expect=0):
        run = subprocess.run(
            [sys.executable, "-m", "memoryking", *args], capture_output=True, text=True
        )
        self.assertEqual(run.returncode, expect, run.stdout + run.stderr)
        return run.stdout

    def test_credential_issue_list_and_revoke_round_trip(self):
        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        issued = Path(self.temp.name) / "agent.credential"
        out = json.loads(
            self.cli(
                "credential",
                "issue",
                "agent-1",
                "--project",
                "demo",
                "--capabilities",
                "read,capture",
                "--expires",
                "12h",
                "--output",
                str(issued),
                *common,
            )
        )
        self.assertEqual(out["credential_file"], str(issued))
        self.assertEqual(out["capabilities"], ["capture", "read"])
        self.assertTrue(issued.read_text().startswith("ey"))
        self.assertEqual(issued.stat().st_mode & 0o777, 0o600)
        listed = self.cli("credential", "list", *common)
        self.assertIn("agent-1", listed)
        self.assertIn("capture,read", listed)
        as_json = json.loads(self.cli("credential", "list", "--json", *common))["items"]
        agent = next(i for i in as_json if i["principal"] == "agent-1")
        self.assertTrue(agent["valid_now"])
        self.assertIsNotNone(agent["expires"])
        self.cli("credential", "revoke", "agent-1", "--project", "demo", *common)
        self.assertNotIn("agent-1", self.cli("credential", "list", *common))
        # Issuing refuses to overwrite an existing private file.
        self.cli(
            "credential",
            "issue",
            "agent-2",
            "--project",
            "demo",
            "--capabilities",
            "read",
            "--output",
            str(issued),
            *common,
            expect=2,
        )

    def test_doctor_reports_a_healthy_store_and_a_missing_daemon_query(self):
        findings = doctor(self.store_dir, self.control_dir, self.server.path, self.credential)
        levels = {name: level for level, name, _ in findings}
        for name in (
            "python",
            "store directory",
            "control directory",
            "directory layout",
            "schema",
            "journal",
            "socket",
            "daemon",
        ):
            self.assertIn(name, levels)
            self.assertNotEqual(levels[name], "fail", findings)
        out = self.cli("doctor", "--store", str(self.store_dir), "--control", str(self.control_dir))
        self.assertIn("checks, 0 failed", out)
        missing = Path(self.temp.name) / "missing"
        self.cli("doctor", "--store", str(missing), "--control", str(self.control_dir), expect=1)

    def test_status_and_dashboard_link(self):
        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        out = self.cli("status", *common)
        self.assertIn("Frontier", out)
        self.assertIn("Capacity", out)
        # The dashboard is not enabled on this server, so bootstrap is refused clearly.
        run = subprocess.run(
            [sys.executable, "-m", "memoryking", "dashboard", "open", "--print-only", *common],
            capture_output=True,
            text=True,
        )
        self.assertEqual(run.returncode, 2)
        self.assertIn("dashboard_disabled", run.stdout + run.stderr)


class HelperTests(unittest.TestCase):
    def test_duration_and_grant_rendering(self):
        self.assertEqual(duration_ms("45m"), 45 * 60_000)
        self.assertEqual(duration_ms("30d"), 30 * 86_400_000)
        for bad in ("", "12", "12w", "h12", "-1h"):
            with self.assertRaises(LocalError):
                duration_ms(bad)
        text = render_grants(
            [
                {
                    "principal": "owner",
                    "project": "*",
                    "capabilities": ["admin", "read"],
                    "starts": None,
                    "expires": None,
                    "valid_now": True,
                },
                {
                    "principal": "bot",
                    "project": "p",
                    "capabilities": ["read"],
                    "starts": None,
                    "expires": 86_400_000,
                    "valid_now": False,
                },
            ]
        )
        self.assertIn("PRINCIPAL  PROJECT  CAPABILITIES  VALID  EXPIRES", text)
        self.assertIn("owner      *        admin,read    yes    never", text)
        self.assertIn("1970-01-02 00:00Z", text)
        self.assertEqual(render_grants([]), "No grants.")
