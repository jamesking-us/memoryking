"""Actual shared browser session and SSE authorization, replay/gap/identity behavior."""

import http.cookiejar
import json
import unittest
import urllib.error
import urllib.request

import test_shared_http as fixture

from memoryking.adapters.postgres_pool import create_pool
from memoryking.domain.local import SystemClock


class SharedDashboardTests(unittest.TestCase):
    dashboard = True
    clock_factory = SystemClock
    setUpClass = classmethod(fixture.SharedHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.SharedHTTPTests.tearDownClass.__func__)
    store = fixture.SharedHTTPTests.store
    command = fixture.SharedHTTPTests.command
    tearDown = fixture.SharedHTTPTests.tearDown

    def setUp(self):
        fixture.SharedHTTPTests.setUp(self)
        self.jar = http.cookiejar.CookieJar()
        self.browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def fetch(self, path, data=None, headers=None, method=None):
        request = urllib.request.Request(
            self.url + path,
            json.dumps(data).encode() if data is not None else None,
            headers={
                **(
                    {"Content-Type": "application/json", "Origin": self.url}
                    if data is not None
                    else {}
                ),
                **(headers or {}),
            },
            method=method,
        )
        try:
            response = self.browser.open(request, timeout=8)
        except urllib.error.HTTPError as exc:
            response = exc
        return response

    def login(self):
        code = self.command("dashboard_bootstrap", {})["code"]
        with self.fetch("/api/bootstrap", {"code": code}) as response:
            self.assertEqual(200, response.status)
        with self.fetch("/api/snapshot") as response:
            self.assertEqual(200, response.status)
            return json.loads(response.read()), code

    def event(self, response):
        result = {}
        while True:
            line = response.readline().decode().strip()
            if not line:
                return result
            key, value = line.split(":", 1)
            result[key] = value.strip()

    def test_pool_pressure_is_503_on_http_mcp_and_browser_without_retiring_session(self):
        self.login()
        pool = create_pool(self.cluster.dsn("mk_runtime"), size=1, timeout=1)
        original, self.service.pool = self.service.pool, pool
        held = pool.getconn()
        try:
            status, body = fixture.SharedHTTPTests.raw(
                self, {"version": 1, "operation": "context", "args": {"project": "demo"}}
            )
            self.assertEqual(503, status)
            self.assertEqual("service_busy", body["error"])
            self.assertIn("hint", body)
            request = urllib.request.Request(
                self.url + f"/v1/tenants/{self.tenant}/projects/demo/mcp",
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                ).encode(),
                headers={
                    "Authorization": "Bearer " + self.credential,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            with caught.exception as response:
                self.assertEqual(503, response.code)
                self.assertEqual("service_busy", json.loads(response.read())["error"])
            with self.fetch("/api/snapshot") as response:
                self.assertEqual(503, response.status)
                self.assertEqual("service_busy", json.loads(response.read())["error"])
            pool.putconn(held)
            held = None
            with self.fetch("/api/snapshot") as response:
                self.assertEqual(200, response.status)
                self.assertEqual("owner", json.loads(response.read())["principal"])
        finally:
            if held is not None:
                pool.putconn(held)
            self.service.pool = original
            pool.close()

    def test_browser_commands_use_current_cookie_authority_and_csrf(self):
        snapshot, _ = self.login()
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
        with self.fetch("/api/command", body) as response:
            self.assertEqual(200, response.status)
            self.assertEqual("current", json.loads(response.read())["authorization"])
            self.assertTrue(response.headers.get("X-MemoryKing-Access-Receipt"))
        for headers in [
            {"Origin": "null"},
            {"Origin": "https://evil.test"},
            {"Authorization": "Bearer invalid"},
        ]:
            with self.fetch("/api/command", body, headers) as response:
                self.assertEqual(403, response.status)
        with self.fetch("/api/command", {**body, "csrf": "bad"}) as response:
            self.assertEqual(403, response.status)
        self.command("grant", {"principal": "new", "project": "demo", "capabilities": ["read"]})
        with self.fetch("/api/command", body) as response:
            self.assertEqual(409, response.status)

    def test_all_packaged_worker_brand_and_view_assets_are_served(self):
        from memoryking.adapters.dashboard import ASSETS

        for path in ASSETS:
            with self.fetch(path) as response:
                self.assertEqual(200, response.status, path)
                self.assertTrue(response.read())
                self.assertIn("worker-src 'self'", response.headers["Content-Security-Policy"])

    def test_navigation_assets_bootstrap_get_head_csrf_and_single_use(self):
        for path in ("/", "/app.js", "/app.css", "/crown.svg"):
            with self.fetch(path) as response:
                self.assertEqual(200, response.status)
                self.assertTrue(response.read())
        for path in ("/", "/app.js", "/app.css", "/graph-worker.js"):
            with self.fetch(path, headers={"Sec-Fetch-Site": "cross-site"}) as response:
                self.assertEqual(200, response.status)
            with self.fetch(path, headers={"Origin": "https://evil.test"}) as response:
                self.assertEqual(403, response.status)
        with self.fetch("/api/snapshot") as response:
            self.assertEqual(403, response.status)
        code = self.command("dashboard_bootstrap", {})["code"]
        with self.fetch("/api/bootstrap", {"code": code}, {"Origin": "null"}) as response:
            self.assertEqual(403, response.status)
        with self.fetch("/api/bootstrap", {"code": code}) as response:
            self.assertEqual(200, response.status)
        with self.fetch("/api/bootstrap", {"code": code}) as response:
            self.assertEqual(403, response.status)
        with self.fetch("/api/snapshot") as response:
            current = json.loads(response.read())
            self.assertTrue(current["streaming"])
        with self.fetch("/api/snapshot", method="HEAD") as response:
            self.assertEqual(200, response.status)
            self.assertEqual(b"", response.read())
        with self.fetch("/api/snapshot", headers={"Sec-Fetch-Site": "cross-site"}) as response:
            self.assertEqual(403, response.status)
        with self.fetch("/api/logout", {"csrf": "wrong"}) as response:
            self.assertEqual(403, response.status)
        with self.fetch("/api/logout", {"csrf": current["csrf"]}) as response:
            self.assertEqual(200, response.status)
        with self.fetch("/api/snapshot") as response:
            self.assertEqual(403, response.status)

    def test_stream_reauthorizes_after_policy_change_and_does_not_replay(self):
        self.login()
        with self.fetch("/api/events") as response:
            first = self.event(response)
            self.assertEqual("snapshot", first["event"])
            self.command(
                "grant", {"principal": "new-principal", "project": "demo", "capabilities": ["read"]}
            )
            reset = self.event(response)
            self.assertEqual("context-reset", reset["event"])
            self.assertEqual({"reason": "reauthorization_required"}, json.loads(reset["data"]))
        with self.fetch("/api/snapshot") as response:
            self.assertEqual(403, response.status)

    def test_cursor_context_and_restart_gap_require_full_resync(self):
        snapshot, _ = self.login()
        old = snapshot["cursor"]
        dashboard = self.service.dashboard
        dashboard.history.generation = "replacement_generation"
        with self.fetch("/api/events", headers={"Last-Event-ID": old}) as response:
            frame = self.event(response)
            self.assertEqual("resync", frame["event"])
            self.assertEqual([], json.loads(frame["data"])["phases"])
        self.login()  # Replaces the current browser session; its old cursor is unusable.
        with self.fetch("/api/events", headers={"Last-Event-ID": old}) as response:
            self.assertEqual(409, response.status)
            self.assertEqual("cursor_context_changed", json.loads(response.read())["error"])

    def test_snapshot_duplicate_suppression_and_bounded_history(self):
        from memoryking.adapters.shared_dashboard import CursorHistory

        history = CursorHistory()
        sequence, gap = history.current("one", "a", None)
        self.assertFalse(gap)
        self.assertEqual(sequence, history.current("one", "a", None)[0])
        previous = {"generation": history.generation, "sequence": sequence}
        for number in range(70):
            history.current("one", str(number), None)
        self.assertTrue(history.current("one", "last", previous)[1])
        self.assertEqual(64, len(history.entries["one"]))
        for number in range(70):
            history.current(str(number), "a", None)
        self.assertEqual(64, len(history.entries))

    def test_evicted_cursor_position_is_not_reused(self):
        from memoryking.adapters.shared_dashboard import CursorHistory

        history = CursorHistory()
        sequence, _ = history.current("first", "original", None)
        previous = {"generation": history.generation, "sequence": sequence}
        for number in range(64):
            history.current(str(number), "other", None)
        current, gap = history.current("first", "replacement", previous)
        self.assertGreater(current, sequence)
        self.assertTrue(gap)
