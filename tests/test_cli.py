"""Operator CLI: version and the human status rendering."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from memoryking import __version__
from memoryking.__main__ import doctor, duration, duration_ms, render_grants, render_status
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

    def test_influence_prints_where_an_observation_went(self):
        from test_working import checkpoint

        from memoryking.adapters.working import Working

        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        namespace = self.store.namespace(self.credential.read_text(), "demo")
        captured = self.store.capture(
            self.credential.read_text(),
            namespace,
            "cli-influence",
            {
                "version": 1,
                "project": "demo",
                "source": "cli-test",
                "source_event": "cli-influence",
                "text": "Timeout observed in the demo service.",
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        body = checkpoint()
        body["acceptance"][0].update(
            state="passed", evidence=[{"kind": "observation", "id": captured["observation"]}]
        )
        output = []
        Working(self.store).execute(
            self.credential.read_text(),
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "save",
                "key": "cli-influence-ckpt",
                "args": {
                    "project": "demo",
                    "task": "task",
                    "run": None,
                    "expected_revision": 0,
                    "readers": ["owner"],
                    "checkpoint": body,
                },
            },
        ).emit(output.append)
        text = self.cli("influence", captured["observation"], "--project", "demo", *common)
        self.assertIn(f"Observation {captured['observation']} from cli-test by owner", text)
        self.assertIn("Reached 1 principal(s): owner", text)
        self.assertIn("Checkpoints built on it: 1", text)
        self.assertIn("task task rev 1 by owner for owner", text)
        self.assertIn("Deliveries that included it: 0", text)
        raw = self.cli("influence", captured["observation"], "--project", "demo", "--json", *common)
        report = json.loads(raw)
        self.assertEqual(report["counts"]["checkpoints"], 1)
        self.assertEqual(report["principals"], ["owner"])
        self.cli("influence", "obs_missing", "--project", "demo", *common, expect=2)

    def test_health_prints_signals_and_counts_for_a_project(self):
        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        self.cli(
            "capture",
            "--project",
            "demo",
            "--source",
            "cli-test",
            "--text",
            "The retry budget in payments/retry.py is three per request.",
            *common,
        )
        text = self.cli("health", "--project", "demo", "--days", "7", *common)
        self.assertIn("Memory health for demo over the last 7 day(s)", text)
        self.assertIn("[ok] backlog: No proposal is waiting for review.", text)
        self.assertIn("Observations: 1 live, 1 captured in the window, 0 held, 0 purged", text)
        self.assertIn("Review backlog: 0 waiting", text)
        report = json.loads(self.cli("health", "--project", "demo", "--json", *common))
        self.assertEqual(report["window"]["days"], 30)
        self.assertEqual(report["observations"]["never_delivered"], 1)
        self.assertEqual(
            {s["name"] for s in report["signals"]},
            {"feedback", "briefings", "backlog", "evidence", "reach"},
        )
        self.cli("health", "--project", "demo", "--days", "0", *common, expect=2)

    def test_recall_as_of_accepts_dates_and_excludes_later_captures(self):
        from memoryking.hooks import as_of_ms

        self.assertEqual(as_of_ms("1700000000000"), 1700000000000)
        self.assertEqual(as_of_ms("2026-10-20"), 1792540799999)
        self.assertEqual(as_of_ms("2026-10-20T12:00:00+00:00"), 1792497600000)
        self.assertEqual(as_of_ms("2026-10-20T12:00:00"), 1792497600000)
        with self.assertRaises(LocalError):
            as_of_ms("yesterday")
        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        namespace = self.store.namespace(self.credential.read_text(), "demo")
        self.store.capture(
            self.credential.read_text(),
            namespace,
            "as-of",
            {
                "version": 1,
                "project": "demo",
                "source": "cli-test",
                "source_event": "as-of",
                "text": "Timeout finding for the as-of test.",
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        now = self.cli("recall", "--project", "demo", "--query", "timeout", *common)
        self.assertIn("Timeout finding", now)
        then = self.cli(
            "recall", "--project", "demo", "--query", "timeout", "--as-of", "2000-01-01", *common
        )
        self.assertNotIn("Timeout finding", then)
        self.assertIn("as of 2000-01-01", then)

    def test_reindex_starts_the_rebuild_and_waits_with_progress_through_the_daemon(self):
        from memoryking.application import lexical_index, store_core

        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        namespace = self.store.namespace(self.credential.read_text(), "demo")
        for number in range(6):
            self.store.capture(
                self.credential.read_text(),
                namespace,
                f"r{number}",
                {
                    "version": 1,
                    "project": "demo",
                    "source": "cli-test",
                    "source_event": f"r{number}",
                    "text": f"Retry note {number} for the nightly export.",
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            )
        original = (store_core.REINDEX_CALL_MS, lexical_index.BATCH_DOCUMENTS)
        store_core.REINDEX_CALL_MS, lexical_index.BATCH_DOCUMENTS = 0, 1
        try:
            started = self.cli("reindex", "--project", "demo", *common)
            self.assertIn("Reindex started: rebuilding (lexical-v1)", started)
            self.assertIn("rebuilding in batches", started)
            waited = self.cli("reindex", "--project", "demo", "--wait", *common)
        finally:
            store_core.REINDEX_CALL_MS, lexical_index.BATCH_DOCUMENTS = original
        self.assertIn("Reindex started:", waited)
        self.assertIn("Lexical    current (lexical-v2)", waited)
        self.assertTrue(waited.rstrip().endswith("Reindex complete."), waited)
        status = self.cli("status", "--project", "demo", *common)
        self.assertIn("Lexical    current (lexical-v2)", status)
        report = json.loads(self.cli("reindex", "--project", "demo", "--json", *common))
        self.assertIn(report["lexical"]["state"], {"rebuilding", "current"})
        # An agent's credential is not an admin one.
        agent = self.store.grant(
            self.credential.read_text(), principal="agent", project="demo", capabilities=["read"]
        )
        token = Path(self.temp.name) / "agent.token"
        token.write_text(agent)
        token.chmod(0o600)
        refused = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                "reindex",
                "--socket",
                str(self.server.path),
                "--credential",
                str(token),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(refused.returncode, 0)

    def test_status_and_doctor_show_percent_complete_and_an_estimate_for_a_rebuild(self):
        from memoryking.__main__ import lexical_line

        line = lexical_line(
            {
                "state": "rebuilding",
                "configuration": "lexical-v1",
                "target_configuration": "lexical-v2",
                "total_observations": 1000,
                "remaining_observations": 750,
                "percent_complete": 25,
                "estimated_seconds": 2,
                "action": "rebuilding in batches",
            }
        )
        self.assertIn("25% rebuilt into lexical-v2 (250 of 1,000 observations, about", line)
        self.assertIn("left)", line)
        self.assertEqual(
            "current (lexical-v2)",
            lexical_line({"state": "current", "configuration": "lexical-v2"}),
        )
        # The doctor reads the same from the files while no daemon is needed.
        namespace = self.store.namespace(self.credential.read_text(), "demo")
        for number in range(4):
            self.store.capture(
                self.credential.read_text(),
                namespace,
                f"d{number}",
                {
                    "version": 1,
                    "project": "demo",
                    "source": "cli-test",
                    "source_event": f"d{number}",
                    "text": f"Doctor note {number}.",
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            )
        tenant = self.store.db.execute("SELECT tenant FROM tenants").fetchone()[0]
        with self.store.transaction():
            self.store.db.execute("DELETE FROM lexical_state WHERE tenant=?", (tenant,))
        report = self.cli(
            "doctor", "--store", str(self.store_dir), "--control", str(self.control_dir)
        )
        self.assertIn("0% rebuilt", report)
        self.assertIn("run `memoryking reindex`", report)

    def test_recall_from_and_to_restrict_the_search_to_a_time_range(self):
        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        token = self.credential.read_text()
        namespace = self.store.namespace(token, "demo")
        for key, text, observed in (
            ("early", "Timeout finding from the early incident.", 1_700_000_000_000),  # 2023-11-14
            ("late", "Timeout finding from the late incident.", 1_800_000_000_000),  # 2027-01-15
        ):
            self.store.capture(
                token,
                namespace,
                key,
                {
                    "version": 1,
                    "project": "demo",
                    "source": "cli-test",
                    "source_event": key,
                    "text": text,
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": observed,
                },
            )
        recall = ["recall", "--project", "demo", "--query", "timeout finding", *common]
        both = self.cli(*recall)
        self.assertIn("early incident", both)
        self.assertIn("late incident", both)
        early = self.cli(*recall, "--from", "2023-11-14", "--to", "2023-11-14")
        self.assertIn("early incident", early)
        self.assertNotIn("late incident", early)
        late = self.cli(*recall, "--from", "2024-01-01")
        self.assertIn("late incident", late)
        self.assertNotIn("early incident", late)
        self.cli(*recall, "--from", "not-a-date", expect=2)

    def test_the_smallest_advertised_budget_gives_a_usable_briefing(self):
        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        empty = self.cli(
            "recall", "--project", "demo", "--query", "timeout", "--budget", "256", *common
        )
        self.assertIn("No relevant memory", empty)
        self.assertNotIn("budget_insufficient", empty)
        namespace = self.store.namespace(self.credential.read_text(), "demo")
        self.store.capture(
            self.credential.read_text(),
            namespace,
            "small",
            {
                "version": 1,
                "project": "demo",
                "source": "cli-test",
                "source_event": "small",
                "text": "Timeout finding for the smallest budget.",
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        small = self.cli(
            "recall", "--project", "demo", "--query", "timeout", "--budget", "256", *common
        )
        self.assertIn("Timeout finding for the smallest budget", small)
        self.assertNotIn("budget_insufficient", small)

    def test_summarize_prints_the_summary_and_its_receipt(self):
        from test_working import checkpoint

        from memoryking.adapters.working import Working

        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        output = []
        Working(self.store).execute(
            self.credential.read_text(),
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "save",
                "key": "cli-ckpt",
                "args": {
                    "project": "demo",
                    "task": "task",
                    "run": None,
                    "expected_revision": 0,
                    "readers": ["owner"],
                    "checkpoint": checkpoint(),
                },
            },
        ).emit(output.append)
        text = self.cli("summarize", "--project", "demo", "--task", "task", *common)
        self.assertIn("Working on", text)
        self.assertIn("extractive-v1", text)
        self.assertIn("receipt rcpt_", text)
        packet = json.loads(
            self.cli("summarize", "--project", "demo", "--task", "task", "--json", *common)
        )
        self.assertEqual(packet["method"], "extractive-v1")
        self.assertEqual(packet["status"], "derived")
        self.cli(
            "summarize",
            "--project",
            "demo",
            "--task",
            "task",
            "--provider",
            "openai-chat",
            *common,
            expect=2,
        )

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

    def test_doctor_says_how_far_ahead_a_recorded_clock_jump_is(self):
        from memoryking.adapters.local_files import Control

        controller = Control(self.control_dir)
        anchor = controller.read_anchor()
        original = anchor["last_utc"]
        self.assertEqual("2 d 3 h 4 min 5 s", duration(((2 * 24 + 3) * 60 + 4) * 60_000 + 5_000))
        try:
            anchor["last_utc"] = int(time.time() * 1000) + 3 * 3_600_000 + 5 * 60_000 + 30_000
            controller.anchor(anchor)
            ((level, _, detail),) = [
                item
                for item in doctor(self.store_dir, self.control_dir, None, None)
                if item[1] == "clock"
            ]
            self.assertEqual("fail", level)
            self.assertIn("3 h 5 min", detail)
            self.assertIn("clock_untrusted", detail)
        finally:
            anchor["last_utc"] = original
            controller.anchor(anchor)
        levels = {n: lv for lv, n, _ in doctor(self.store_dir, self.control_dir, None, None)}
        self.assertEqual("ok", levels["clock"])

    def test_credential_rotation_flags_and_rotate_owner_needs_a_stopped_daemon(self):
        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        issue = ["credential", "issue", "agent-r", "--project", "demo", "--capabilities", "read"]
        files = [Path(self.temp.name) / name for name in ("a", "b", "c")]
        self.cli(*issue, "--output", str(files[0]), *common)
        self.cli(*issue, "--output", str(files[1]), *common)
        self.assertEqual(files[0].read_text(), files[1].read_text())
        self.cli(*issue, "--rotate", "--output", str(files[2]), *common)
        self.assertNotEqual(files[0].read_text(), files[2].read_text())
        status = ["status", "--socket", str(self.server.path), "--credential"]
        self.cli(*status, str(files[0]), expect=2)  # refused: credential_replaced
        self.cli(*status, str(files[2]), expect=2)  # status needs admin; the grant is read only
        listed = json.loads(self.cli("credential", "list", "--json", *common))["items"]
        agent = next(i for i in listed if i["principal"] == "agent-r")
        self.assertEqual(["read"], agent["capabilities"])
        # The daemon holds the store, so the offline owner rotation refuses and changes nothing.
        before = self.credential.read_text()
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                "credential",
                "rotate-owner",
                "--store",
                str(self.store_dir),
                "--control",
                str(self.control_dir),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, run.returncode, run.stdout + run.stderr)
        self.assertIn("owner_busy_or_unsafe", run.stdout + run.stderr)
        self.assertEqual(before, self.credential.read_text())

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


class ProfileTests(OperatorCommandTests):
    def setUp(self):
        super().setUp()
        self.config = Path(self.temp.name) / "config.toml"
        self.env = {**os.environ, "MEMORYKING_CONFIG": str(self.config)}

    def cli(self, *args, expect=0):
        run = subprocess.run(
            [sys.executable, "-m", "memoryking", *args],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(run.returncode, expect, run.stdout + run.stderr)
        return run.stdout

    def test_profile_keeps_session_bindings_and_refuses_unknown_keys(self):
        base = ["profile", "add", "scoped", "--store", str(self.store_dir)]
        base += ["--control", str(self.control_dir)]
        self.cli(*base, "--binding", "environment=prod", "--binding", "role=implement")
        self.assertIn(
            "bindings = {'environment': 'prod', 'role': 'implement'}", self.cli("profile", "list")
        )
        self.cli(*base, "--binding", "colour=blue", expect=2)

    def test_profile_fills_paths_and_explicit_flags_still_win(self):
        self.cli(
            "profile",
            "add",
            "work",
            "--store",
            str(self.store_dir),
            "--control",
            str(self.control_dir),
            "--dashboard-port",
            "0",
        )
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        listed = self.cli("profile", "list")
        self.assertIn("work", listed)
        self.assertIn(f"store = {self.store_dir.resolve()}", listed)
        self.assertIn("dashboard_port = 0", listed)
        out = self.cli("--profile", "work", "status")
        self.assertIn("Frontier", out)
        out = self.cli("--profile", "work", "doctor")
        self.assertIn("checks, 0 failed", out)
        out = self.cli("--profile", "work", "credential", "list")
        self.assertIn("owner", out)
        # An explicit flag overrides the profile; a missing path without a profile is named.
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                "--profile",
                "work",
                "status",
                "--socket",
                "/nonexistent.sock",
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertNotEqual(run.returncode, 0)
        run = subprocess.run(
            [sys.executable, "-m", "memoryking", "status", "--credential", str(self.credential)],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertIn("socket_required", run.stdout + run.stderr)
        run = subprocess.run(
            [sys.executable, "-m", "memoryking", "--profile", "nope", "status"],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertIn("profile_unknown", run.stdout + run.stderr)
        self.cli("profile", "remove", "work")
        self.assertNotIn("work", self.cli("profile", "list"))

    def test_profile_validation_rejects_relative_paths_and_unknown_fields(self):
        from memoryking import profiles

        with self.assertRaises(LocalError):
            profiles.validate("x", {"store": "relative", "control": "/abs"})
        with self.assertRaises(LocalError):
            profiles.validate("x", {"store": "/a", "control": "/b", "token": "secret"})
        with self.assertRaises(LocalError):
            profiles.validate("bad name", {"store": "/a", "control": "/b"})
        self.config.write_text("profiles = 3\n")
        self.config.chmod(0o600)
        with self.assertRaises(LocalError):
            profiles.load(self.config)


class CaptureCommandTests(OperatorCommandTests):
    def test_draft_proposals_files_drafts_through_the_daemon(self):
        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        for source, text in (
            ("a", "Scoped the deadline per request in payments/retry.py yesterday."),
            ("b", "Review note: payments/retry.py now retries once per request."),
        ):
            self.cli("capture", "--project", "demo", "--source", source, "--text", text, *common)
        report = json.loads(
            self.cli("draft-proposals", "--project", "demo", "--key", "k1", "--json", *common)
        )
        self.assertEqual(len(report["drafted"]), 1)
        self.assertEqual(report["drafted"][0]["topic"], "payments/retry.py")
        text = self.cli("draft-proposals", "--project", "demo", "--key", "k1", *common)
        self.assertIn("1 draft(s) filed for review", text)  # same key: replayed, not refiled
        again = json.loads(
            self.cli("draft-proposals", "--project", "demo", "--key", "k2", "--json", *common)
        )
        self.assertEqual(again["drafted"], [])

    def test_trust_and_egress_audit_reach_the_daemon(self):
        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        self.cli(
            "trust",
            "set",
            "--project",
            "demo",
            "--source",
            "scraper-*",
            "--level",
            "untrusted",
            "--key",
            "rule-1",
            *common,
        )
        listed = self.cli("trust", "list", "--project", "demo", *common)
        self.assertIn("scraper-*", listed)
        self.assertIn("untrusted", listed)
        audit = json.loads(self.cli("egress-audit", "--project", "demo", "--json", *common))
        self.assertIsInstance(audit, dict)

    def test_trust_default_keys_are_valid_for_globs_and_stable_for_plain_sources(self):
        from memoryking.__main__ import default_trust_key

        self.assertEqual(
            default_trust_key("demo", "scraper", "untrusted"), "trust-demo-scraper-untrusted"
        )
        glob = default_trust_key("demo", "scraper-*", "untrusted")
        self.assertRegex(glob, r"^[A-Za-z0-9_-]{1,96}$")
        self.assertEqual(glob, default_trust_key("demo", "scraper-*", "untrusted"))
        self.assertNotEqual(glob, default_trust_key("demo", "scraper-?", "untrusted"))
        self.assertNotEqual(glob, default_trust_key("demo", "scraper-*", "trusted"))
        self.assertRegex(default_trust_key("*", "x", "trusted"), r"^[A-Za-z0-9_-]{1,96}$")
        self.assertRegex(default_trust_key("demo", "s" * 200, "trusted"), r"^[A-Za-z0-9_-]{1,96}$")

    def test_trust_set_held_and_release_reach_the_daemon_without_explicit_keys(self):
        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        rule = ["trust", "set", "--project", "demo", "--source", "scraper-*"]
        rule += ["--level", "untrusted", "--json", *common]
        first = json.loads(self.cli(*rule))
        self.assertEqual(first["status"], "recorded")
        self.assertTrue(json.loads(self.cli(*rule))["replayed"])
        self.assertIn("scraper-*", self.cli("trust", "list", "--project", "demo", *common))
        token = self.credential.read_text()
        held = self.store.capture(
            token,
            self.store.namespace(token, "demo"),
            "scrape-1",
            {
                "version": 1,
                "project": "demo",
                "source": "scraper-web",
                "source_event": "scrape-1",
                "text": "Zebra retry hint from a web page",
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        self.assertTrue(held["held"])
        queue = json.loads(self.cli("trust", "held", "--project", "demo", "--json", *common))
        self.assertEqual([i["observation"] for i in queue["held"]], [held["observation"]])
        self.assertIn(held["observation"], self.cli("trust", "held", "--project", "demo", *common))
        released = json.loads(
            self.cli(
                "trust", "release", held["observation"], "--project", "demo", "--json", *common
            )
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(
            json.loads(self.cli("trust", "held", "--project", "demo", "--json", *common))["held"],
            [],
        )
        self.assertIn("No held captures", self.cli("trust", "held", "--project", "demo", *common))

    def test_capture_and_recall_through_the_cli_and_hooks(self):
        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        out = json.loads(
            self.cli(
                "capture",
                "--project",
                "demo",
                "--source",
                "git",
                "--event",
                "commit-abc",
                "--text",
                "Commit abc fixed the retry deadline",
                *common,
            )
        )
        self.assertIn("observation", out)
        again = json.loads(
            self.cli(
                "capture",
                "--project",
                "demo",
                "--source",
                "git",
                "--event",
                "commit-abc",
                "--text",
                "Commit abc fixed the retry deadline",
                *common,
            )
        )
        self.assertEqual(out, again)
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                "capture",
                "--project",
                "demo",
                "--source",
                "stdin",
                *common,
            ],
            input="A note piped from stdin about the deadline",
            capture_output=True,
            text=True,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        text = self.cli("recall", "--project", "demo", "--query", "deadline", *common)
        self.assertTrue(text.startswith("# Briefing: deadline"))
        self.assertIn("fixed the retry deadline", text)
        self.assertIn("piped from stdin", text)
        packet = json.loads(
            self.cli("recall", "--project", "demo", "--query", "deadline", "--json", *common)
        )
        self.assertIn("guidance", packet)
        # Environment-driven connection, as hooks use it.
        env = {
            **os.environ,
            "MEMORYKING_SOCKET": str(self.server.path),
            "MEMORYKING_CREDENTIAL": str(self.credential),
            "MEMORYKING_PROJECT": "demo",
        }
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                "capture",
                "--project",
                "demo",
                "--text",
                "from env",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_project_defaults_from_the_environment_for_capture_recall_and_summarize(self):
        base = {k: v for k, v in os.environ.items() if k != "MEMORYKING_PROJECT"}
        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]

        def run(*args, project="demo"):
            env = {**base, "MEMORYKING_PROJECT": project} if project else base
            return subprocess.run(
                [sys.executable, "-m", "memoryking", *args, *common],
                capture_output=True,
                text=True,
                env=env,
            )

        saved = run("capture", "--text", "The deadline moved to Friday")
        self.assertEqual(saved.returncode, 0, saved.stderr)
        recalled = run("recall", "--query", "deadline")
        self.assertEqual(recalled.returncode, 0, recalled.stderr)
        self.assertIn("deadline moved to Friday", recalled.stdout)
        # summarize reaches the store (this task has no checkpoint) instead of asking for a project.
        unsaved = run("summarize", "--task", "none")
        self.assertNotEqual(unsaved.returncode, 0)
        self.assertNotIn("project_required", unsaved.stderr)
        self.assertIn("project_required", run("summarize", "--task", "none", project="").stderr)
        for command in (("capture", "--text", "x"), ("recall", "--query", "x")):
            missing = run(*command, project="")
            self.assertEqual(missing.returncode, 2, missing.stdout)
            self.assertIn("project_required", missing.stderr)
        # An explicit flag wins over the environment.
        other = run("recall", "--query", "deadline", "--project", "elsewhere")
        self.assertNotIn("deadline moved to Friday", other.stdout)

    def test_save_progress_and_resume_commands_match_the_lean_tools(self):
        env = {
            **os.environ,
            "MEMORYKING_SOCKET": str(self.server.path),
            "MEMORYKING_CREDENTIAL": str(self.credential),
            "MEMORYKING_PROJECT": "demo",
        }

        def run(*args, expect=0):
            proc = subprocess.run(
                [sys.executable, "-m", "memoryking", *args],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
            return proc

        saved = json.loads(
            run(
                "save-progress",
                "--task",
                "cli-task",
                "--objective",
                "Fix the retry deadline",
                "--state",
                "Investigating",
                "--next",
                "Run the regression",
                "--next",
                "Update the changelog",
                "--finding",
                "Backoff exceeds the deadline",
                "--blocker",
                "Need a full regression result",
                "--failed",
                "Raise every timeout => Still failed",
            ).stdout
        )
        self.assertEqual(saved["revision"], 1)
        text = run("resume", "--task", "cli-task").stdout
        self.assertIn("# Resume: Fix the retry deadline", text)
        self.assertIn("Update the changelog", text)
        self.assertIn("Raise every timeout", text)
        self.assertIn("Still failed", text)
        self.assertIn("Need a full regression result", text)
        # A section left out carries over; the revision moves on.
        second = json.loads(
            run(
                "save-progress",
                "--task",
                "cli-task",
                "--objective",
                "Fix the retry deadline",
                "--state",
                "Regression passes",
            ).stdout
        )
        self.assertEqual(second["revision"], 2)
        again = run("resume", "--task", "cli-task").stdout
        self.assertIn("Regression passes", again)
        self.assertIn("Update the changelog", again)
        packet = json.loads(run("resume", "--task", "cli-task", "--json").stdout)
        self.assertEqual(packet["format"], "text")
        # Malformed --failed is refused by the parser.
        bad = run(
            "save-progress",
            "--task",
            "t",
            "--objective",
            "o",
            "--state",
            "s",
            "--failed",
            "no arrow",
            expect=2,
        )
        self.assertIn("action => outcome", bad.stderr)

    def test_pytest_plugin_records_failures_only_when_configured(self):
        from memoryking import pytest_plugin

        class Report:
            nodeid = "tests/test_x.py::test_y"
            when = "call"
            failed = True
            longreprtext = "assert 1 == 2\nAssertionError"

        with patch.dict(os.environ, {"MEMORYKING_PROJECT": ""}, clear=False):
            self.assertIsNone(pytest_plugin.record_failure(Report()))
        env = {
            "MEMORYKING_SOCKET": str(self.server.path),
            "MEMORYKING_CREDENTIAL": str(self.credential),
            "MEMORYKING_PROJECT": "demo",
        }
        with patch.dict(os.environ, env, clear=False):
            first = pytest_plugin.record_failure(Report())
            self.assertIn("observation", first)
            self.assertEqual(first, pytest_plugin.record_failure(Report()))
            passed = Report()
            passed.failed = False
            self.assertIsNone(pytest_plugin.record_failure(passed))
        text = self.cli(
            "recall",
            "--project",
            "demo",
            "--query",
            "AssertionError",
            "--socket",
            str(self.server.path),
            "--credential",
            str(self.credential),
        )
        self.assertIn("pytest call failed: tests/test_x.py::test_y", text)


class EmbeddingsCommandTests(OperatorCommandTests):
    def test_enable_and_status_manage_the_partition_without_a_model(self):
        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        enabled = json.loads(self.cli("embeddings", "enable", "--project", "demo", *common))
        self.assertEqual(enabled["dimensions"], 384)
        again = json.loads(self.cli("embeddings", "enable", "--project", "demo", *common))
        self.assertTrue(again.get("already_enabled"))
        status = json.loads(self.cli("embeddings", "status", "--project", "demo", *common))
        self.assertEqual(status["state"], "experimental")
        self.assertEqual(status["generation"], 1)
        findings = doctor(
            self.store_dir, self.control_dir, None, None, Path(self.temp.name) / "nomodel"
        )
        levels = {name: level for level, name, _ in findings}
        self.assertEqual(levels["embedding model"], "fail")
