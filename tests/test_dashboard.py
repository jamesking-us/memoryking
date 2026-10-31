import http.client
import json
import tempfile
import unittest
from pathlib import Path

from memoryking.adapters.dashboard import Dashboard, PlanFiles
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.retrieval import Retrieval
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError, SystemClock

ROOT = Path(__file__).resolve().parents[1]


class Clock(SystemClock):
    offset = 0

    def utc_ms(self):
        return super().utc_ms() + self.offset

    def monotonic_ms(self):
        return super().monotonic_ms() + self.offset


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-web-")
        self.root = Path(self.temp.name)
        self.owner = LocalStore.initialize(self.root / "db", self.root / "ctl").read_text()
        self.clock = Clock()
        self.store = LocalStore(self.root / "db", self.root / "ctl", clock=self.clock)
        self.web = Dashboard(self.store, ROOT)
        self.web.start()
        self.cookie = None

    def tearDown(self):
        self.web.stop()
        self.store.close()
        self.temp.cleanup()

    def call(self, method, path, body=None, headers=None):
        h = {}
        if self.cookie:
            h["Cookie"] = self.cookie
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=10)
        conn.request(method, path, None if body is None else json.dumps(body), h)
        response = conn.getresponse()
        raw = response.read()
        result = (response.status, dict(response.headers), raw)
        conn.close()
        return result

    def login(self):
        issued = self.web.issue(self.owner)
        result = self.call(
            "POST", "/api/bootstrap", {"code": issued["code"]}, {"Origin": self.web.origin}
        )
        self.assertEqual(result[0], 200, result[2])
        self.cookie = result[1]["Set-Cookie"].split(";")[0]
        return issued

    def test_browser_command_auth_receipt_origin_and_epoch(self):
        self.login()
        snapshot = json.loads(self.call("GET", "/api/snapshot")[2])
        body = {
            "csrf": snapshot["csrf"],
            "operation": "inspection",
            "command": {
                "version": 1,
                "incarnation": snapshot["incarnation"],
                "action": "history",
                "args": {"project": "demo"},
            },
        }
        for headers in [
            {},
            {"Origin": "null"},
            {"Origin": "https://evil.test"},
            {"Origin": self.web.origin, "Authorization": "Bearer invalid"},
        ]:
            self.assertEqual(403, self.call("POST", "/api/command", body, headers)[0])
        headers = {"Origin": self.web.origin}
        result = self.call("POST", "/api/command", body, headers)
        self.assertEqual(200, result[0], result[2])
        self.assertEqual("current", json.loads(result[2])["authorization"])
        self.assertIn("X-MemoryKing-Access-Receipt", result[1])
        self.assertEqual(
            403, self.call("POST", "/api/command", {**body, "csrf": "bad"}, headers)[0]
        )
        self.store.grant(self.owner, "other", "demo", ["read"])
        self.assertEqual(409, self.call("POST", "/api/command", body, headers)[0])

    def test_browser_command_delayed_release_and_session_expiry(self):
        self.login()
        snapshot = json.loads(self.call("GET", "/api/snapshot")[2])
        body = {
            "csrf": snapshot["csrf"],
            "operation": "inspection",
            "command": {
                "version": 1,
                "incarnation": snapshot["incarnation"],
                "action": "cost",
                "args": {"project": "demo"},
            },
        }
        self.store.fault = lambda point: (
            setattr(self.clock, "offset", 30000) if point == "after_validation" else None
        )
        result = self.call("POST", "/api/command", body, {"Origin": self.web.origin})
        self.assertEqual(403, result[0])
        self.assertNotIn(b'"attempts"', result[2])
        self.store.fault = lambda _: None
        self.clock.offset = 900001
        self.assertEqual(
            409, self.call("POST", "/api/command", body, {"Origin": self.web.origin})[0]
        )

    def test_packaged_modules_are_public_bootstrap_assets_but_never_arbitrary_files(self):
        from memoryking.adapters.dashboard import ASSETS

        for path in ASSETS:
            result = self.call("GET", path)
            self.assertEqual(200, result[0], path)
            self.assertTrue(result[2])
            self.assertIn("worker-src 'self'", result[1]["Content-Security-Policy"])
        self.assertEqual(403, self.call("GET", "/../../AGENTS.md")[0])

    def test_navigation_login_reads_and_logout(self):
        for path in ["/", "/app.js", "/app.css", "/crown.svg", "/elephant.png"]:
            result = self.call("GET", path)
            self.assertEqual(result[0], 200)
            self.assertEqual(result[1]["Cache-Control"], "no-store")
        self.assertEqual(self.call("GET", "/api/snapshot")[0], 403)

        self.login()
        status, headers, raw = self.call("GET", "/api/snapshot")
        self.assertEqual(status, 200, raw)
        state = json.loads(raw)
        self.assertEqual(state["phases"], [])
        self.assertEqual(state["activity"]["status"], "unknown")
        self.assertEqual(self.call("HEAD", "/api/snapshot")[2], b"")
        self.assertEqual(self.call("GET", "/api/artifact/roadmap")[0], 403)
        self.assertEqual(
            self.call("POST", "/api/logout", {"csrf": state["csrf"]}, {"Origin": self.web.origin})[
                0
            ],
            200,
        )
        self.assertEqual(self.call("GET", "/api/snapshot")[0], 403)

    def test_bearer_retrieval_exact_bytes_and_readonly_receipt_inspector(self):
        args = {
            "project": "demo",
            "query": "astronomy",
            "bindings": [{}],
            "max_output_tokens": 3000,
            "tokenizer": "unknown",
        }
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": "brief",
            "args": args,
        }
        headers = {"Authorization": "Bearer " + self.owner}
        status, response_headers, raw = self.call("POST", "/api/v1/retrieval", command, headers)
        self.assertEqual(status, 200, raw)
        result = json.loads(raw)
        self.assertEqual(raw, canonical_bytes(result))
        first_access = response_headers["X-MemoryKing-Access-Receipt"]
        command.update(action="replay", args={"project": "demo", "receipt": result["receipt"]})
        status, response_headers, replay = self.call("POST", "/api/v1/retrieval", command, headers)
        self.assertEqual(status, 200)
        self.assertEqual(replay, raw)
        self.assertNotEqual(first_access, response_headers["X-MemoryKing-Access-Receipt"])
        self.assertEqual(self.call("POST", "/api/v1/retrieval", command)[0], 403)
        self.assertEqual(
            self.call(
                "POST", "/api/v1/retrieval", command, {**headers, "Origin": "https://hostile.test"}
            )[0],
            403,
        )
        self.login()
        self.assertEqual(self.call("POST", "/api/v1/retrieval", command, headers)[0], 403)
        listing = self.call("GET", "/api/receipts/demo")
        self.assertEqual(listing[0], 200, listing[2])
        self.assertEqual(json.loads(listing[2])["items"][0]["receipt"], result["receipt"])
        detail = self.call("GET", "/api/receipt/demo/" + result["receipt"])
        self.assertEqual(detail[0], 200, detail[2])
        self.assertEqual(json.loads(detail[2])["diagnostics"]["status"], "no_relevant_memory")
        self.assertEqual(self.call("GET", "/api/receipt/other/" + result["receipt"])[0], 403)
        self.assertEqual(self.call("GET", "/api/receipts/demo?path=private")[0], 403)

    def test_receipt_inspector_rechecks_source_audience_and_session_epoch(self):
        reader = self.store.grant(self.owner, "reader", "demo", ["read"])
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": "brief",
            "args": {
                "project": "demo",
                "query": "absent",
                "bindings": [{}],
                "max_output_tokens": 3000,
                "tokenizer": "unknown",
            },
        }
        raw = Retrieval(self.store).execute(reader, command).body
        result = json.loads(raw)
        self.login()
        self.assertEqual(self.call("GET", "/api/receipt/demo/" + result["receipt"])[0], 403)
        self.store.revoke(self.owner, "reader", "demo")
        self.assertEqual(self.call("GET", "/api/receipts/demo")[0], 409)

    def test_host_origin_fetch_metadata_and_mutation_guards(self):
        issued = self.web.issue(self.owner)
        for headers in [
            {"Host": "evil.test"},
            {"Origin": "https://evil.test"},
            {"Origin": "null"},
        ]:
            self.assertEqual(self.call("GET", "/", headers=headers)[0], 403)
        for path in ("/", "/app.js", "/app.css", "/graph-worker.js"):
            self.assertEqual(
                self.call("GET", path, headers={"Sec-Fetch-Site": "cross-site"})[0], 200
            )
        self.assertEqual(
            self.call("GET", "/api/snapshot", headers={"Sec-Fetch-Site": "cross-site"})[0], 403
        )
        self.assertEqual(self.call("POST", "/api/bootstrap", {"code": issued["code"]})[0], 403)
        self.assertEqual(
            self.call(
                "POST",
                "/api/bootstrap",
                {"code": issued["code"]},
                {"Origin": self.web.origin, "Content-Type": "application/x-www-form-urlencoded"},
            )[0],
            403,
        )
        self.login()
        self.assertEqual(
            self.call("POST", "/api/logout", {"csrf": "wrong"}, {"Origin": self.web.origin})[0], 403
        )
        self.assertEqual(self.call("GET", "/api/logout")[0], 403)
        self.assertEqual(
            self.call("GET", "/api/snapshot", headers={"Origin": "https://evil.test"})[0], 403
        )
        self.assertEqual(
            self.call(
                "OPTIONS",
                "/api/snapshot",
                headers={"Origin": self.web.origin, "Access-Control-Request-Method": "DELETE"},
            )[0],
            403,
        )
        self.assertEqual(
            self.call(
                "OPTIONS",
                "/api/snapshot",
                headers={"Origin": self.web.origin, "Access-Control-Request-Method": "GET"},
            )[0],
            204,
        )

    def test_expired_single_use_codes_and_current_session_epoch(self):
        issued = self.login()
        self.assertEqual(
            self.call(
                "POST", "/api/bootstrap", {"code": issued["code"]}, {"Origin": self.web.origin}
            )[0],
            403,
        )
        self.store.grant(self.owner, "reader", "p", ["read"])
        self.assertEqual(self.call("GET", "/api/snapshot")[0], 409)
        old = self.web.issue(self.owner)
        self.clock.offset = 60001
        self.assertEqual(
            self.call("POST", "/api/bootstrap", {"code": old["code"]}, {"Origin": self.web.origin})[
                0
            ],
            403,
        )
        self.login()
        self.clock.offset += 900001
        self.assertEqual(self.call("GET", "/api/snapshot")[0], 403)

    def test_no_scoped_principal_bootstrap_or_path_traversal(self):
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        with self.assertRaisesRegex(LocalError, "denied"):
            self.web.issue(reader)
        self.login()
        for path in [
            "/api/artifact/../../.env",
            "/api/artifact/%2e%2e/.env",
            "/api/artifact/roadmap?file=.env",
            "/api/review",
        ]:
            result = self.call("GET", path)
            self.assertEqual(result[0], 403)

    def test_delayed_release_emits_no_source(self):
        root = self.root / "release-fixture"
        (root / "docs").mkdir(parents=True)
        (root / "docs/IMPLEMENTATION-ROADMAP.md").write_text("Synthetic protected document")
        self.web.files = PlanFiles(root)
        self.login()
        allowed = self.call("GET", "/api/artifact/roadmap")
        self.assertEqual(allowed[0], 200)
        self.assertIn(b"Synthetic protected document", allowed[2])

        def stall(point):
            if point == "after_validation":
                self.clock.offset += 6000

        self.store.fault = stall
        result = self.call("GET", "/api/artifact/roadmap")
        self.assertEqual(result[0], 403)
        self.assertNotIn(b"Synthetic protected document", result[2])
        self.store.fault = lambda _: None

    def test_snapshot_reports_capacity_for_every_bounded_resource(self):
        self.login()
        status, _, raw = self.call("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        capacity = json.loads(raw)["capacity"]
        self.assertEqual(
            set(capacity),
            {
                "jobs",
                "rendered_artifacts",
                "rendered_bytes",
                "operation_intents",
                "processing_usage",
            },
        )
        self.assertEqual(capacity["jobs"]["limit"], 1000)
        self.assertFalse(any(v["warning"] for v in capacity.values()))

    def test_workspace_snapshot_needs_no_development_files(self):
        self.assertEqual(PlanFiles(None).snapshot(5)["phases"], [])
        with self.assertRaises(LocalError):
            PlanFiles(None).read("docs/IMPLEMENTATION-ROADMAP.md")
        root = self.root / "product"
        root.mkdir()
        state = PlanFiles(root).snapshot(123)
        self.assertEqual(state["phases"], [])
        self.assertEqual(state["counts"]["total"], 0)
        self.assertEqual(state["fresh_until_ms"], 15123)
        (root / "docs").mkdir()
        (root / "docs/dashboard-plan.json").write_text("{}")
        with self.assertRaises(LocalError):
            PlanFiles(root).snapshot(124)

    def test_plan_adapter_empty_and_corrupt_sources(self):
        root = self.root / "fixture"
        (root / "docs/reports").mkdir(parents=True)
        (root / "docs/dashboard-plan.json").write_bytes(
            (ROOT / "fixtures/dashboard/plan.json").read_bytes()
        )
        phases = {str(i): {"status": "not_started"} for i in range(1, 13)}
        register = root / "docs/reports/phases.json"
        register.write_text(json.dumps({"schema_version": 1, "phases": phases}))
        files = PlanFiles(root)
        result = files.snapshot(1)
        self.assertEqual(result["counts"]["closed"], 0)
        self.assertTrue(
            all(p["evidence"]["verification"] == "not_evaluated" for p in result["phases"])
        )
        phases["1"]["status"] = "complete"
        register.write_text(json.dumps({"schema_version": 1, "phases": phases}))
        result = files.snapshot(2)
        self.assertEqual(result["counts"]["closed"], 0)
        self.assertEqual(result["phases"][0]["evidence"]["report"], "invalid_or_missing")
        (root / "docs/dashboard-plan.json").write_text('{"version":1,"phases":[]}')
        with self.assertRaises(LocalError):
            files.snapshot(3)
        (root / "docs/IMPLEMENTATION-ROADMAP.md").symlink_to(ROOT / "README.md")
        with self.assertRaises(LocalError):
            files.read("docs/IMPLEMENTATION-ROADMAP.md")

    def test_partial_and_blocked_reports_count_without_claiming_technical_completion(self):
        import hashlib

        root = self.root / "partial-reports"
        (root / "docs/reports").mkdir(parents=True)
        (root / "docs/dashboard-plan.json").write_bytes(
            (ROOT / "fixtures/dashboard/plan.json").read_bytes()
        )
        states = {str(i): {"status": "not_started"} for i in range(1, 13)}
        for index, status in enumerate(("complete", "partial", "blocked"), 1):
            states[str(index)]["status"] = status
            prefix = f"docs/reports/evidence/phase-{index:02d}/"
            (root / prefix / "local-job").mkdir(parents=True)
            job_path = prefix + "local-job/job.json"
            (root / job_path).write_text(
                json.dumps(
                    {
                        "passed": status != "blocked",
                        "revision": "a" * 40,
                        "platform": "synthetic adapter fixture",
                        "steps": [],
                    }
                )
            )
            paths = {
                "source": f"docs/reports/phase-{index:02d}-report.md",
                "pdf": f"docs/reports/phase-{index:02d}-report.pdf",
                "epub": f"docs/reports/phase-{index:02d}-report.epub",
                "readme": prefix + "README.snapshot.md",
                "evidence": prefix + "manifest.json",
            }
            for kind, path in paths.items():
                (root / path).write_text(
                    json.dumps(
                        {
                            "files": [
                                {
                                    "path": job_path,
                                    "sha256": hashlib.sha256(
                                        (root / job_path).read_bytes()
                                    ).hexdigest(),
                                }
                            ]
                        }
                    )
                    if kind == "evidence"
                    else "Synthetic plan-adapter bytes; never sent or represented as an actual report"
                )
            hashes = {
                kind: hashlib.sha256((root / path).read_bytes()).hexdigest()
                for kind, path in paths.items()
            }
            (root / prefix / "report-qa.json").write_text(
                json.dumps(
                    {
                        "hashes": hashes,
                        **{
                            key: True
                            for key in (
                                "pdf_reviewed",
                                "epub_reviewed",
                                "evidence_reviewed",
                                "readme_reviewed",
                            )
                        },
                    }
                )
            )
            (root / prefix / "kindle-submission.json").write_text(
                json.dumps(
                    {"hashes": hashes, "status": "smtp_accepted", "device_delivery": "unverified"}
                )
            )
        (root / "docs/reports/phases.json").write_text(
            json.dumps({"schema_version": 1, "phases": states})
        )
        snapshot = PlanFiles(root).snapshot(1)
        self.assertEqual(snapshot["counts"]["closed"], 1)
        self.assertEqual(snapshot["counts"]["report_closeouts"], 3)
        self.assertEqual(snapshot["counts"]["historical_jobs_passed"], 2)
        self.assertEqual(snapshot["phases"][1]["execution"], "partial")
        self.assertEqual(snapshot["phases"][2]["evidence"]["verification"], "failed")
        (root / "docs/reports/phase-02-report.pdf").write_text("tampered")
        snapshot = PlanFiles(root).snapshot(2)
        self.assertEqual(snapshot["counts"]["closed"], 1)
        self.assertEqual(snapshot["counts"]["report_closeouts"], 2)
