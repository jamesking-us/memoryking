"""Metrics, health, readiness, concurrency limits and schema compatibility, on both profiles."""

import json
import re
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import psycopg
import test_shared_http as shared_http

from memoryking import health
from memoryking.adapters import postgres_admin
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import SCHEMA_VERSION, LocalStore
from memoryking.adapters.postgres_admin import schema_compatible
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.shared_http import Boundary
from memoryking.application.store_core import CAPACITY_LIMITS
from memoryking.domain.local import LocalError


def fetch(url, headers=None, method="GET"):
    request = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return (
                response.status,
                response.read(),
                {k.lower(): v for k, v in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


class SchemaCompatibilityTests(unittest.TestCase):
    def test_a_service_accepts_its_own_version_and_the_next_only_when_that_migration_is_additive(
        self,
    ):
        current = postgres_admin.SCHEMA_VERSION
        self.assertTrue(schema_compatible(current))
        self.assertFalse(schema_compatible(current + 1))
        self.assertFalse(schema_compatible(current - 1))
        with mock.patch.object(postgres_admin, "ADDITIVE_MIGRATIONS", frozenset({current + 1})):
            self.assertTrue(schema_compatible(current + 1))
            self.assertFalse(schema_compatible(current + 2))
            self.assertFalse(schema_compatible(current - 1))

    def test_the_refusal_names_both_versions_within_the_hint_limit(self):
        refusal = postgres_admin.schema_refusal("13")
        self.assertEqual("schema_unsupported", refusal.code)
        self.assertIn("13", refusal.hint)
        self.assertIn(str(postgres_admin.SCHEMA_VERSION), refusal.hint)
        self.assertLessEqual(len(refusal.hint), 200)


class SharedOperabilityTests(unittest.TestCase):
    setUpClass = classmethod(shared_http.SharedHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(shared_http.SharedHTTPTests.tearDownClass.__func__)
    store = shared_http.SharedHTTPTests.store
    raw = shared_http.SharedHTTPTests.raw
    setUp = shared_http.SharedHTTPTests.setUp
    tearDown = shared_http.SharedHTTPTests.tearDown
    clock_factory = shared_http.SharedHTTPTests.clock_factory

    def get(self, path, headers=None):
        return fetch(self.url + path, headers)

    def bearer(self, credential=None):
        return {"Authorization": "Bearer " + (credential or self.credential)}

    def metrics(self):
        status, body, _ = self.get(f"/v1/tenants/{self.tenant}/metrics", self.bearer())
        self.assertEqual(200, status, body)
        return body.decode()

    def test_liveness_and_readiness_reveal_nothing_and_need_no_credential_or_host(self):
        # Probes come by address, with no credential and not the configured Host header.
        for path, expected in (("/healthz", b'{"status":"ok"}'), ("/readyz", b'{"ready":true}')):
            status, body, headers = self.get(path, {"Host": "10.9.8.7:1"})
            self.assertEqual((200, expected), (status, body), path)
            self.assertEqual("no-store", headers["cache-control"])
        with mock.patch.object(
            self.service,
            "readiness",
            return_value={"database": True, "control": True, "schema": False},
        ):
            # Readiness is cached for a moment; a fresh app would see the failure at once.
            checks = health.CachedChecks(self.service.readiness, ttl=0)
            status, body = health.readiness_response(checks())
        self.assertEqual((503, b'{"ready":false}'), (status, body))
        self.assertNotIn(b"schema", body)

    def test_readiness_checks_the_database_the_control_tree_and_the_schema_version(self):
        self.assertEqual(
            {"database": True, "control": True, "schema": True}, self.service.readiness()
        )
        with mock.patch.object(Path, "is_dir", return_value=False):
            self.assertFalse(self.service.readiness()["control"])
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as db:
            db.execute("UPDATE memoryking.shared_meta SET value='99' WHERE key='schema'")
            try:
                checks = self.service.readiness()
            finally:
                db.execute(
                    "UPDATE memoryking.shared_meta SET value=%s WHERE key='schema'",
                    (str(postgres_admin.SCHEMA_VERSION),),
                )
        self.assertEqual({"database": True, "control": True, "schema": False}, checks)
        with mock.patch(
            "memoryking.adapters.postgres_pool.acquire", side_effect=LocalError("service_busy")
        ):
            self.assertFalse(self.service.readiness()["database"])

    def test_metrics_need_the_tenants_admin_credential_and_carry_no_content_or_identity(self):
        self.assertEqual(403, self.get(f"/v1/tenants/{self.tenant}/metrics")[0])
        self.assertEqual(
            400,
            self.get(
                f"/v1/tenants/{self.tenant}/metrics", {**self.bearer(), "Cookie": "mk_session=x"}
            )[0],
        )
        reader = self.reader_credential()
        status, body, _ = self.get(f"/v1/tenants/{self.tenant}/metrics", self.bearer(reader))
        self.assertEqual(403, status)
        self.client.capture(
            "m1",
            {
                "version": 1,
                "project": "demo",
                "source": "t",
                "source_event": "m1",
                "text": "a private sentence",
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        self.raw({"version": 1, "operation": "nonsense", "args": {}})  # an error, counted by code
        text = self.metrics()
        for family in (
            "memoryking_requests_total",
            "memoryking_errors_total",
            "memoryking_request_duration_ms_bucket",
            "memoryking_capacity_used",
            "memoryking_journal_bytes",
            "memoryking_backup_age_seconds",
            "memoryking_housekeeping_age_seconds",
            "memoryking_ready",
            "memoryking_pool",
            "memoryking_concurrency",
            "memoryking_http_responses_total",
        ):
            self.assertIn(family, text)
        self.assertIn('memoryking_requests_total{operation="capture"}', text)
        self.assertIn('memoryking_request_duration_ms_count{operation="gate_wait"}', text)
        self.assertRegex(text, r'memoryking_errors_total\{code="invalid_request"\} [1-9]')
        # Every capacity bound is reported.
        for name in CAPACITY_LIMITS:
            self.assertIn(f'memoryking_capacity_limit{{resource="{name}"}}', text)
        self.assertIn('memoryking_concurrency{kind="limit",resource="http_requests"} 32', text)
        self.assertIn('memoryking_concurrency{kind="limit",resource="database_pool"}', text)
        self.assertIn("memoryking_backup_age_seconds -1", text)
        # No content, tenant name, credential, principal or project appears in it.
        for secret in (self.tenant, self.credential, "a private sentence", "demo", "owner"):
            self.assertNotIn(secret, text)
        self.assertRegex(text, r'memoryking_ready\{check="schema"\} 1')

    def test_backup_age_and_journal_size_follow_the_tenant(self):
        (self.control / self.tenant / "last-backup").write_text(
            str(int((__import__("time").time() - 120) * 1000))
        )
        text = self.metrics()
        age = float(re.search(r"memoryking_backup_age_seconds ([0-9.e+]+)", text).group(1))
        self.assertTrue(110 <= age <= 400, age)
        journal = int(float(re.search(r"memoryking_journal_bytes ([0-9.e+]+)", text).group(1)))
        self.assertEqual((self.control / self.tenant / "journal").stat().st_size, journal)

    def reader_credential(self):
        with self.store() as store:
            return store.grant(self.credential, "reader", "demo", ["read"])

    def test_http_concurrency_is_configurable_with_the_documented_default(self):
        from memoryking.adapters.shared_http import create_app

        self.assertEqual(32, create_app(self.service, hosts=["h:1"], origins=[]).capacity)
        self.assertEqual(
            5, create_app(self.service, hosts=["h:1"], origins=[], capacity=5).capacity
        )
        for bad in (0, -1, 2000):
            with self.assertRaises(LocalError):
                Boundary(None, hosts=["h:1"], origins=[], capacity=bad)

    def test_a_service_one_schema_behind_runs_only_when_the_migration_is_additive(self):
        newer = str(postgres_admin.SCHEMA_VERSION + 1)

        def with_database_at(version, check):
            with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as db:
                db.execute(
                    "UPDATE memoryking.shared_meta SET value=%s WHERE key='schema'", (version,)
                )
                try:
                    return check()
                finally:
                    db.execute(
                        "UPDATE memoryking.shared_meta SET value=%s WHERE key='schema'",
                        (str(postgres_admin.SCHEMA_VERSION),),
                    )

        def open_store():
            with PostgresStore(self.pool, self.root, self.control, self.tenant, self.credential):
                return None

        with self.assertRaises(LocalError) as refused:
            with_database_at(newer, open_store)
        self.assertEqual("schema_unsupported", refused.exception.code)
        self.assertIn(newer, refused.exception.hint)
        self.assertIn(str(postgres_admin.SCHEMA_VERSION), refused.exception.hint)
        # Over HTTP the reply carries the same hint.
        status, body = with_database_at(
            newer,
            lambda: self.raw({"version": 1, "operation": "status", "args": {"project": "demo"}}),
        )
        self.assertEqual(503, status)
        self.assertIn(newer, json.dumps(body))
        with mock.patch.object(postgres_admin, "ADDITIVE_MIGRATIONS", frozenset({int(newer)})):
            self.assertIsNone(with_database_at(newer, open_store))
        with self.assertRaises(LocalError):
            with_database_at(str(postgres_admin.SCHEMA_VERSION + 2), open_store)
        with self.assertRaises(LocalError):
            with_database_at(str(postgres_admin.SCHEMA_VERSION - 1), open_store)


class LocalOperabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-op-", dir="/tmp")
        base = Path(self.temp.name)
        self.owner = LocalStore.initialize(base / "s", base / "c").read_text()
        self.store = LocalStore(base / "s", base / "c")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_concurrency_defaults_are_unchanged_and_the_limits_are_configurable(self):
        with Server(self.store) as server:
            self.assertEqual((8, 16), (server.max_concurrent, server.max_waiting))
            self.assertEqual(
                {"local_workers": (8, 0), "local_waiters": (16, 0)}, server.saturation()
            )
        with Server(self.store, workers=3, waiters=0) as server:
            self.assertEqual((3, 0), (server.max_concurrent, server.max_waiting))
            self.assertEqual(
                {"local_workers": (3, 0), "local_waiters": (0, 0)}, server.saturation()
            )
        for workers, waiters in ((0, None), (999, None), (None, -1), (None, 5000)):
            with self.assertRaises(LocalError):
                Server(self.store, workers=workers, waiters=waiters)

    def test_the_health_port_serves_only_liveness_and_a_readiness_boolean(self):
        server = health.LocalHealthServer(self.store.readiness)
        port = server.start()
        try:
            self.assertEqual(
                (200, b'{"status":"ok"}'), fetch(f"http://127.0.0.1:{port}/healthz")[:2]
            )
            self.assertEqual((200, b'{"ready":true}'), fetch(f"http://127.0.0.1:{port}/readyz")[:2])
            self.assertEqual(404, fetch(f"http://127.0.0.1:{port}/metrics")[0])
            self.assertEqual(
                {"database": True, "control": True, "schema": True}, self.store.readiness()
            )
            self.store.db.execute("PRAGMA user_version=3")
            try:
                self.assertFalse(self.store.readiness()["schema"])
                server.checks.at = float("-inf")  # the cache would hold the earlier answer
                status, body, _ = fetch(f"http://127.0.0.1:{port}/readyz")
                self.assertEqual((503, b'{"ready":false}'), (status, body))
            finally:
                self.store.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        finally:
            server.stop()

    def test_readiness_notices_a_fenced_or_missing_control_tree(self):
        anchor = self.store.control.read_anchor()
        self.store.control.anchor({**anchor, "state": "maintenance"})
        try:
            self.assertFalse(self.store.readiness()["control"])
        finally:
            self.store.control.anchor(anchor)
        self.assertTrue(self.store.readiness()["control"])

    def test_extra_gauges_report_journal_size_backup_age_readiness_and_the_daemons_limits(self):
        with Server(self.store, workers=4, waiters=2):
            gauges = {name: samples for name, _, samples in self.store.extra_gauges()}
        self.assertEqual(
            {
                "memoryking_journal_bytes",
                "memoryking_backup_age_seconds",
                "memoryking_ready",
                "memoryking_concurrency",
            },
            set(gauges),
        )
        self.assertEqual(-1.0, gauges["memoryking_backup_age_seconds"][0][1])
        self.store.backup(Path(self.temp.name) / "backup")
        age = {n: s for n, _, s in self.store.extra_gauges()}["memoryking_backup_age_seconds"][0][1]
        self.assertTrue(0 <= age < 60)
        limits = {
            tuple(sorted(labels.items())): value
            for labels, value in gauges["memoryking_concurrency"]
        }
        self.assertEqual(4, limits[(("kind", "limit"), ("resource", "local_workers"))])
        self.assertEqual(2, limits[(("kind", "limit"), ("resource", "local_waiters"))])


if __name__ == "__main__":
    unittest.main()
