"""Operator logging is redacted, request ids link replies to log lines, status codes differ."""

import http.client
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_journal import observation

from memoryking.adapters.local_ipc import Server, request
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.http_status import http_status
from memoryking.domain.local import LocalError
from memoryking.observability import LOGGER, configure, event, failure


class ObservabilityTests(unittest.TestCase):
    def test_failure_logs_type_and_frames_but_never_the_message(self):
        try:
            raise RuntimeError("SELECT secret FROM /private/path credential=abc")
        except RuntimeError as exc:
            with self.assertLogs(LOGGER, level="ERROR") as logs:
                request_id = failure("unit", exc, tenant="main")
        self.assertRegex(request_id, r"^[a-f0-9]{16}$")
        line = "\n".join(logs.output)
        self.assertIn("request_id=" + request_id, line)
        self.assertIn("error=RuntimeError", line)
        self.assertIn("test_observability.py", line)
        self.assertIn("tenant=main", line)
        for secret in ("SELECT", "/private/path", "credential=abc"):
            self.assertNotIn(secret, line)

    def test_configure_is_idempotent_and_validates_levels(self):
        configure("info")
        configure("info")
        self.assertEqual(1, sum(getattr(h, "_memoryking", False) for h in LOGGER.handlers))
        self.assertEqual(logging.INFO, LOGGER.level)
        with self.assertRaises(ValueError):
            configure("verbose")
        with self.assertLogs(LOGGER, level="INFO") as logs:
            event("serve", "ready", dashboard=False)
        self.assertIn("component=serve event=ready dashboard=False", logs.output[0])

    def test_status_codes_separate_conflict_capacity_and_unavailability(self):
        self.assertEqual(409, http_status("idempotency_conflict", 400))
        self.assertEqual(409, http_status("history_changed", 403))
        self.assertEqual(409, http_status("session_changed", 403))
        self.assertEqual(409, http_status("operation_identity_consumed", 400))
        self.assertEqual(429, http_status("queue_full", 400))
        self.assertEqual(429, http_status("retention_capacity", 400))
        self.assertEqual(429, http_status("operation_scope_capacity", 400))
        self.assertEqual(429, http_status("usage_retention_limit", 400))
        self.assertEqual(503, http_status("recovery_required", 400))
        self.assertEqual(503, http_status("service_busy", 400))
        self.assertEqual(403, http_status("denied", 403))
        self.assertEqual(400, http_status("invalid_request", 400))
        self.assertEqual(403, http_status("session_expired", 403))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.credential = LocalStore.initialize(base / "store", base / "control").read_text()
        self.store = LocalStore(base / "store", base / "control")
        namespace = self.store.namespace(self.credential, "demo")
        self.store.capture(self.credential, namespace, "k1", observation(1))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_internal_error_reply_carries_a_request_id_and_logs_nothing_sensitive(self):
        with Server(self.store) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with (
                    patch.object(
                        server.operations,
                        "execute",
                        side_effect=RuntimeError("sqlite3 says /secret/path"),
                    ),
                    self.assertLogs(LOGGER, level="ERROR") as logs,
                ):
                    with self.assertRaises(LocalError) as caught:
                        request(server.path, self.credential, "read", {"project": "demo"})
                self.assertEqual("internal_error", caught.exception.code)
                self.assertIn("component=local_ipc", logs.output[0])
                self.assertNotIn("/secret/path", "\n".join(logs.output))
                # The server keeps serving other clients afterwards.
                result = request(server.path, self.credential, "read", {"project": "demo"})
                self.assertIn("items", result)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_concurrent_clients_are_served_in_parallel_within_the_bound(self):
        with Server(self.store) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                results = []

                def call():
                    results.append(
                        request(server.path, self.credential, "read", {"project": "demo"})
                    )

                workers = [threading.Thread(target=call) for _ in range(12)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=30)
                self.assertEqual(12, len(results))
            finally:
                server.shutdown()
                thread.join(timeout=5)


class MetricsTests(unittest.TestCase):
    def test_counters_histogram_and_rendering(self):
        from memoryking.observability_metrics import Metrics, render

        metrics = Metrics()
        metrics.observe("read", 4.0, None)
        metrics.observe("read", 40.0, None)
        metrics.observe("capture", 2.0, "denied")
        snap = metrics.snapshot()
        self.assertEqual(snap["requests"], {"read": 2, "capture": 1})
        self.assertEqual(snap["errors"], {"denied": 1})
        self.assertEqual(snap["latency_buckets"]["read"][0], 1)  # le 5
        self.assertEqual(snap["latency_buckets"]["read"][-1], 2)  # +Inf
        text = render(
            metrics,
            {
                "position": 7,
                "projection": {"frontier": 7},
                "jobs": {"complete": 7},
                "capacity": {"jobs": {"used": 0, "limit": 1000, "warning": False}},
            },
        )
        self.assertIn('memoryking_requests_total{operation="read"} 2', text)
        self.assertIn('memoryking_request_duration_ms_count{operation="read"} 2', text)
        self.assertIn('memoryking_jobs{state="complete"} 7', text)
        self.assertIn("memoryking_frontier 7", text)

    def test_trace_lines_carry_timings_and_outcomes_but_never_content(self):
        import tempfile
        from pathlib import Path

        from memoryking.adapters.local_ipc import StoreOperations
        from memoryking.adapters.local_store import LocalStore
        from memoryking.observability import LOGGER, configure, tracing

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            credential = LocalStore.initialize(base / "s", base / "c").read_text()
            with LocalStore(base / "s", base / "c") as store:
                ops = StoreOperations(store)
                namespace = store.namespace(credential, "demo")
                capture = {
                    "version": 1,
                    "credential": credential,
                    "operation": "capture",
                    "args": {
                        "namespace": namespace,
                        "key": "k1",
                        "request": {
                            "version": 1,
                            "project": "demo",
                            "source": "test",
                            "source_event": "e1",
                            "text": "SECRET pineapple must stay out of the log",
                            "readers": None,
                            "classification": "internal",
                            "observed_at_ms": 1,
                        },
                    },
                }
                brief = {
                    "version": 1,
                    "credential": credential,
                    "operation": "retrieval",
                    "args": {
                        "command": {
                            "version": 1,
                            "incarnation": store.incarnation,
                            "action": "brief",
                            "args": {
                                "project": "demo",
                                "query": "pineapple",
                                "bindings": [{}],
                                "max_output_tokens": 3000,
                                "tokenizer": "utf8-byte-estimate-v1",
                            },
                        }
                    },
                }
                try:
                    configure("warning", trace=True)
                    self.assertTrue(tracing())
                    self.assertEqual(LOGGER.level, logging.INFO)
                    with self.assertLogs("memoryking", level="INFO") as logs:
                        ops.execute(capture)
                        ops.execute(brief)
                        with self.assertRaises(LocalError):
                            ops.execute({**brief, "credential": "bad"})
                    lines = [line for line in logs.output if "event=trace" in line]
                    self.assertEqual(len(lines), 3)
                    self.assertIn("component=operations", lines[0])
                    self.assertIn(" operation=capture ", lines[0])
                    self.assertIn(" action=- ", lines[0])
                    self.assertIn(" outcome=ok ", lines[0])
                    self.assertRegex(lines[0], r" transactions=1(\s|$)")
                    self.assertIn(" operation=retrieval ", lines[1])
                    self.assertIn(" action=brief ", lines[1])
                    self.assertRegex(lines[1], r" elapsed_ms=\d+\.\d+")
                    self.assertRegex(lines[1], r" lock_wait_ms=\d+\.\d+")
                    self.assertRegex(lines[1], r" transaction_ms=\d+\.\d+")
                    self.assertRegex(lines[1], r" response_bytes=[1-9]\d*")
                    self.assertIn(" outcome=invalid_token ", lines[2])
                    joined = "\n".join(lines)
                    for secret in ("pineapple", "SECRET", "demo", "owner", credential[:12]):
                        self.assertNotIn(secret, joined)
                    self.assertIsNone(getattr(store.timings, "items", None))
                    configure("warning")
                    self.assertFalse(tracing())
                    self.assertEqual(LOGGER.level, logging.WARNING)
                    with self.assertNoLogs("memoryking", level="INFO"):
                        ops.execute(brief)
                    configure("debug", trace=True)
                    self.assertEqual(LOGGER.level, logging.DEBUG)
                finally:
                    configure("warning")

    def test_dashboard_trace_lines_name_the_route_family_and_status(self):
        import tempfile
        from pathlib import Path

        from memoryking.adapters.dashboard import Dashboard
        from memoryking.adapters.local_store import LocalStore
        from memoryking.observability import configure

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            LocalStore.initialize(base / "s", base / "c")
            with LocalStore(base / "s", base / "c") as store:
                web = Dashboard(store, Path(__file__).resolve().parents[1])
                web.start()
                try:
                    configure("warning", trace=True)
                    with self.assertLogs("memoryking", level="INFO") as logs:
                        conn = http.client.HTTPConnection("127.0.0.1", web.server_port, timeout=10)
                        conn.request("GET", "/app.css")
                        conn.getresponse().read()
                        conn.close()
                        conn = http.client.HTTPConnection("127.0.0.1", web.server_port, timeout=10)
                        conn.request("GET", "/api/receipts/demo?x=1")
                        conn.getresponse().read()
                        conn.close()
                    lines = [line for line in logs.output if "event=trace" in line]
                    self.assertEqual(len(lines), 2)
                    self.assertIn("component=dashboard event=trace", lines[0])
                    self.assertIn(" method=GET ", lines[0])
                    self.assertIn(" route=/app.css ", lines[0])
                    self.assertIn(" status=200 ", lines[0])
                    self.assertIn(" route=/api/receipts ", lines[1])
                    self.assertNotIn("demo", lines[1])
                    self.assertNotIn("x=1", lines[1])
                    self.assertIn(" status=403 ", lines[1])
                finally:
                    configure("warning")
                    web.stop()

    def test_ipc_dispatch_records_every_request(self):
        import tempfile
        from pathlib import Path

        from memoryking.adapters.local_ipc import StoreOperations
        from memoryking.adapters.local_store import LocalStore

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            credential = LocalStore.initialize(base / "s", base / "c").read_text()
            with LocalStore(base / "s", base / "c") as store:
                ops = StoreOperations(store)
                ops.execute(
                    {
                        "version": 1,
                        "credential": credential,
                        "operation": "context",
                        "args": {"project": "demo"},
                    }
                )
                with self.assertRaises(LocalError):
                    ops.execute(
                        {
                            "version": 1,
                            "credential": "bad",
                            "operation": "context",
                            "args": {"project": "demo"},
                        }
                    )
                snap = store.counters.snapshot()
                self.assertEqual(snap["requests"]["context"], 2)
                self.assertEqual(snap["errors"], {"invalid_token": 1})
