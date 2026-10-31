"""Operator logging is redacted, request ids link replies to log lines, status codes differ."""

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
