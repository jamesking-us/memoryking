"""Real loopback sockets, native stores and verified local TLS; no real OAuth bypass."""

import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_knowledge as knowledge
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from recovery_helpers import backup_fixture, restore_fixture
from test_postgres_parity import NativeFixture
from test_report_catalog import CatalogCases
from test_viewer_reads import ReadCases

from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.viewer_http import ViewerServer
from memoryking.adapters.viewer_reads import ViewerReads
from memoryking.domain.local import LocalError


def certificate(root):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "viewer.example.invalid")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("viewer.example.invalid"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = root / "viewer.crt", root / "viewer.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    return cert_file, key_file


class HttpCases:
    command = ReadCases.command
    policy = ReadCases.policy
    bind = ReadCases.bind
    grant_viewer = ReadCases.grant_viewer
    subject = ReadCases.subject
    actor_store = ReadCases.actor_store
    setup_viewer = ReadCases.setup_viewer
    body = ReadCases.body
    propose = ReadCases.propose
    review = ReadCases.review
    run_k = ReadCases.run_k

    @contextmanager
    def listener(
        self,
        *,
        tls=False,
        authenticated=True,
        request_seconds=5,
        browser_origin=False,
        public_origin=None,
    ):
        self.setup_viewer()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        settings = {**self.settings, "listen_port": port}
        if browser_origin or public_origin:
            settings["public_origin"] = public_origin or f"https://viewer.example.invalid:{port}"
        settings["limits"] = {**settings["limits"], "request_seconds": request_seconds}
        self.client_tls = None
        if tls:
            cert, key = certificate(self.root)
            settings.update(transport="direct_tls", tls_cert_file=str(cert), tls_key_file=str(key))
            self.client_tls = ssl.create_default_context(cafile=str(cert))

        def resolve(token):
            if token != "a" * 48:
                raise LocalError("unauthenticated")
            return self.session

        def request_store(session, deadline):
            if hasattr(self, "pool"):
                return PostgresStore(
                    self.pool,
                    self.data,
                    self.control,
                    self.tenant,
                    session.subject.credential,
                    clock=self.store.clock,
                    fault=self.store.fault,
                    request_deadline=deadline,
                )
            return self.actor_store(session.subject.credential)

        server = ViewerServer(
            settings,
            request_store,
            **({"resolve": resolve} if authenticated else {}),
        )
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        self.port = port
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=6)

    def get(self, path, *, method="GET", headers=None, body=None):
        client = (
            http.client.HTTPSConnection("127.0.0.1", self.port, context=self.client_tls, timeout=7)
            if self.client_tls
            else http.client.HTTPConnection("127.0.0.1", self.port, timeout=7)
        )
        try:
            client.request(
                method,
                path,
                body=body,
                headers={
                    "Host": "viewer.example.invalid",
                    "Cookie": "__Host-memoryking-viewer=" + "a" * 48,
                    **(headers or {}),
                },
            )
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def test_actual_get_head_private_reads_and_no_origin_in_both_modes(self):
        # Separate stores are retained across listeners; use this loop only for raw config mode.
        for tls in (False, True):
            with self.listener(tls=tls) as server:
                for path in (
                    "/viewer/",
                    "/viewer/shell.css",
                    "/viewer/api/v1/session",
                    "/viewer/api/v1/projects/p/overview",
                    "/viewer/api/v1/projects/p/memories",
                ):
                    for method in ("GET", "HEAD"):
                        status, headers, body = self.get(path, method=method)
                        self.assertEqual(status, 200, (path, method, body))
                        self.assertEqual(headers["Cache-Control"], "no-store")
                        self.assertNotIn("Access-Control-Allow-Origin", headers)
                        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                        if method == "HEAD":
                            self.assertEqual(body, b"")
                self.assertNotIn("internal_error", server.counts)
            if not tls:
                # Reuse the binding; the second listener must not create a duplicate invitation.
                self.setup_viewer = lambda: None

    def test_route_headers_mutation_boundary_and_non_disclosure(self):
        with self.listener() as server:
            before = self.store.status(self.owner, "p")["position"]
            for path in (
                "/api/status",
                "/v1/read",
                "/mcp",
                "/viewer/bootstrap",
                "/viewer/../owner.credential",
                "/viewer/%2e%2e/owner.credential",
                "/viewer/api/v1/projects/p/reports/x/versions/1/report.pdf",
            ):
                self.assertIn(self.get(path)[0], {400, 404})
            path = "/viewer/api/v1/projects/p/memories"
            for headers in (
                {"Origin": "https://hostile.invalid"},
                {"Host": "hostile.invalid"},
                {"Sec-Fetch-Site": "cross-site"},
                {"Sec-Fetch-Site": "same-site"},
                {"Authorization": "Bearer synthetic-owner"},
            ):
                self.assertIn(self.get(path, headers=headers)[0], {400, 404})
            for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"):
                self.assertNotEqual(self.get(path, method=method, body=b"{}")[0], 200)
            for suffix in (
                "?limit=51",
                "?limit=-1",
                "?limit=1&limit=2",
                "?operation=capture",
                "?q=%FF",
                "?q=%00",
            ):
                self.assertEqual(self.get(path + suffix)[0], 400)
            self.assertEqual(
                self.get(path, headers={"X-Forwarded-User": "owner", "X-Forwarded-Host": "other"})[
                    0
                ],
                200,
            )
            self.assertEqual(self.store.status(self.owner, "p")["position"], before)
            missing = self.get("/viewer/api/v1/projects/p/tasks/missing")
            denied = self.get("/viewer/api/v1/projects/hidden/tasks/private")
            self.assertEqual((missing[0], missing[2]), (denied[0], denied[2]))
            self.assertNotIn("internal_error", server.counts)

    def test_packaged_mobile_assets_are_public_closed_and_never_filesystem_fallback(self):
        from memoryking.adapters.viewer_http import ASSETS

        with self.listener(authenticated=False):
            for path, (_, expected_type) in ASSETS.items():
                for method in ("GET", "HEAD"):
                    status, headers, raw = self.get(path, method=method, headers={"Cookie": ""})
                    self.assertEqual(status, 200, path)
                    self.assertEqual(headers["Content-Type"], expected_type)
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    self.assertIn("worker-src 'none'", headers["Content-Security-Policy"])
                    if method == "HEAD":
                        self.assertEqual(raw, b"")
                    else:
                        self.assertTrue(raw)
                self.assertEqual(self.get(path, method="POST", body=b"{}")[0], 400)
                self.assertEqual(self.get(path, headers={"Origin": "https://evil.invalid"})[0], 404)
            for path in (
                "/viewer/client.ts",
                "/viewer/app.js.map",
                "/viewer/index.html",
                "/viewer/web/../owner.credential",
                "/viewer/app.js?v=1",
                "/viewer/manifest.webmanifest?tenant=main",
                "/viewer/sw.js",
                "/viewer/elephant.png/extra",
            ):
                self.assertIn(self.get(path)[0], (400, 404), path)
            self.assertEqual(self.get("/viewer/api/v1/session", headers={"Cookie": ""})[0], 401)
            manifest = json.loads(self.get("/viewer/manifest.webmanifest")[2])
            self.assertEqual(
                (manifest["id"], manifest["start_url"], manifest["scope"]), ("/viewer/",) * 3
            )
            self.assertEqual(manifest["display"], "standalone")
            self.assertEqual({item["sizes"] for item in manifest["icons"]}, {"192x192", "512x512"})

    def test_session_exposes_configured_poll_interval_without_extending_display_allowance(self):
        with self.listener() as server:
            for interval in (5, 10, 60):
                server.config["poll_seconds"] = interval
                status, _, raw = self.get("/viewer/api/v1/session")
                self.assertEqual(status, 200)
                value = json.loads(raw)
                self.assertEqual(value["data"]["poll_seconds"], interval)
                self.assertLessEqual(
                    value["binding"]["fresh_until_ms"] - value["binding"]["evaluated_ms"], 15000
                )

    def test_raw_duplicate_headers_and_oversized_line_close_bounded(self):
        with self.listener():
            for extra in (
                b"Host: hostile.invalid\r\n",
                b"Origin: https://viewer.example.invalid\r\nOrigin: https://hostile.invalid\r\n",
                b"X-Test: ok\r\n folded-value\r\n",
            ):
                with socket.create_connection(("127.0.0.1", self.port), timeout=3) as stream:
                    stream.sendall(
                        b"GET /viewer/ HTTP/1.1\r\nHost: viewer.example.invalid\r\n"
                        + extra
                        + b"\r\n"
                    )
                    response = stream.recv(8192)
                    self.assertIn(b" 400 ", response)
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as stream:
                stream.sendall(b"GET /" + b"a" * 9000 + b" HTTP/1.1\r\n\r\n")
                try:
                    response = stream.recv(8192)
                except ConnectionResetError:
                    response = b""
                self.assertNotIn(b" 200 ", response)
            self.assertEqual(self.get("/viewer/")[0], 200)

    def test_default_resolver_never_authenticates_and_capacity_recovers(self):
        with self.listener(authenticated=False) as server:
            self.assertEqual(self.get("/viewer/api/v1/session")[0], 401)
            sockets = []
            try:
                for _ in range(4):
                    stream = socket.create_connection(("127.0.0.1", self.port), timeout=3)
                    stream.sendall(b"GET ")
                    sockets.append(stream)
                deadline = time.monotonic() + 2
                while len(server.threads) < 4 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(server.threads), 4)
                with socket.create_connection(("127.0.0.1", self.port), timeout=3) as extra:
                    try:
                        response = extra.recv(100)
                    except ConnectionResetError:
                        response = b""
                    self.assertEqual(response, b"")
                self.assertEqual(server.counts["admission_rejected"], 1)
            finally:
                for stream in sockets:
                    stream.close()
            deadline = time.monotonic() + 2
            while server.threads and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.get("/viewer/")[0], 200)

    def test_report_http_full_reader_image_private_catalog_and_revocation(self):
        with self.listener() as server:

            class Fixture(CatalogCases):
                pass

            fixture = Fixture()
            fixture.store, fixture.owner, fixture.n = self.store, self.owner, 1000
            fixture.actor = self.actor_store
            files = fixture.publish("published", ["owner", "phone"], image=True)
            fixture.publish("private-title", ["owner"])
            self.session = replace(self.session, subject=self.subject())
            base = "/viewer/api/v1/projects/p/reports"
            status, _, raw = self.get(base)
            self.assertEqual(status, 200, raw)
            catalog = json.loads(raw)["data"]["reports"]
            self.assertEqual([r["report"] for r in catalog], ["published"])
            self.assertNotIn(b"private-title", raw)
            target = base + "/published/versions/1"
            status, _, raw = self.get(target)
            self.assertEqual(status, 200, raw)
            metadata = json.loads(raw)["data"]
            self.assertEqual(metadata["device_delivery"], "unverified")
            for secret in (
                b"recipient",
                b"destination",
                b".pdf",
                b".epub",
                str(self.root).encode(),
            ):
                self.assertNotIn(secret, raw)
            pieces = {}
            cursor = None
            first_cursor = None
            for page in range(metadata["sections"]):
                url = target + f"/sections/{page}" + ("?cursor=" + cursor if cursor else "")
                status, headers, raw = self.get(url)
                self.assertEqual(status, 200, raw)
                self.assertEqual(headers["Cache-Control"], "no-store")
                result = json.loads(raw)
                fragment = result["data"]["content"]
                pieces.setdefault(fragment["block"], []).append(fragment["content"])
                cursor = result["next_cursor"]
                if page == 0:
                    first_cursor = cursor
            self.assertIsNone(cursor)
            from memoryking.reporting.template import model

            expected = model(files["source.md"].decode(), "Synthetic phase")
            self.assertEqual(
                [json.loads("".join(pieces[n])) for n in sorted(pieces)], expected["blocks"]
            )
            self.assertEqual(self.get(target + "/sections/1")[0], 404)
            status, _, raw = self.get(target + "/images/diagram.png")
            self.assertEqual(status, 200, raw)
            import base64

            self.assertEqual(
                base64.b64decode(json.loads(raw)["data"]["data_base64"]), files["diagram.png"]
            )
            for name in ("report.pdf", "source.md", "owner.credential", "../diagram.png"):
                self.assertNotEqual(self.get(target + "/images/" + name)[0], 200)
            self.session = replace(self.session, identity="different-session")
            self.assertEqual(self.get(target + "/sections/1?cursor=" + first_cursor)[0], 409)
            fixture.source("published-evidence", files["evidence.json"], ["owner"], version=1)
            self.session = replace(self.session, subject=self.subject())
            self.assertEqual(json.loads(self.get(base)[2])["data"]["reports"], [])
            for suffix in ("", "/sections/0", "/images/diagram.png"):
                hidden = self.get(target + suffix)
                absent = self.get(base + "/absent/versions/1" + suffix)
                self.assertEqual((hidden[0], hidden[2]), (absent[0], absent[2]))
                self.assertEqual(hidden[0], 404)
            self.assertNotIn("internal_error", server.counts)

    def test_query_deadline_interrupts_work_and_connection_recovers(self):
        with self.listener(request_seconds=1) as server:
            original = ViewerReads._data

            def slow(reads, *args):
                if hasattr(reads.s, "connection"):
                    reads.db.execute("SELECT pg_sleep(4)")
                else:
                    reads.db.execute(
                        "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n"
                    ).fetchone()
                return original(reads, *args)

            before = time.monotonic()
            with patch.object(ViewerReads, "_data", slow):
                status, _, body = self.get("/viewer/api/v1/projects/p/memories")
            elapsed = time.monotonic() - before
            self.assertEqual(status, 503, body)
            self.assertEqual(json.loads(body)["error"], "service_unavailable")
            self.assertLess(elapsed, 3)
            self.assertEqual(self.get("/viewer/api/v1/projects/p/memories")[0], 200)
            self.assertNotIn("internal_error", server.counts)

    def test_report_revocation_serializes_after_release_decision(self):
        with self.listener():

            class Fixture(CatalogCases):
                pass

            fixture = Fixture()
            fixture.store, fixture.owner, fixture.n = self.store, self.owner, 1000
            fixture.actor = self.actor_store
            files = fixture.publish("race", ["owner", "phone"])
            self.session = replace(self.session, subject=self.subject())
            entered, release = threading.Event(), threading.Event()

            def stall(point):
                if point == "after_validation" and not entered.is_set():
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("fixture release timeout")

            self.store.fault = stall
            target = "/viewer/api/v1/projects/p/reports/race/versions/1"
            try:
                with ThreadPoolExecutor(max_workers=2) as workers:
                    reading = workers.submit(self.get, target)
                    self.assertTrue(entered.wait(2))
                    revoking = workers.submit(
                        fixture.source,
                        "race-evidence",
                        files["evidence.json"],
                        ["owner"],
                        version=1,
                    )
                    time.sleep(0.05)
                    self.assertFalse(revoking.done())
                    release.set()
                    self.assertEqual(reading.result(timeout=4)[0], 200)
                    revoking.result(timeout=4)
            finally:
                release.set()
                self.store.fault = lambda _: None
            self.session = replace(self.session, subject=self.subject())
            self.assertEqual(self.get(target)[0], 404)
            self.assertEqual(
                json.loads(self.get("/viewer/api/v1/projects/p/reports")[2])["data"]["reports"], []
            )

    def test_slow_header_total_deadline_and_recovery(self):
        with self.listener(request_seconds=1):
            before = time.monotonic()
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as connection:
                connection.sendall(b"GET /viewer/ HTTP/1.1\r\n")
                self.assertEqual(connection.recv(100), b"")
            self.assertLess(time.monotonic() - before, 2.5)
            self.assertEqual(self.get("/viewer/")[0], 200)

    def test_store_admission_pressure_uses_request_budget(self):
        with self.listener(request_seconds=1):
            held = []
            try:
                if hasattr(self, "pool"):
                    # One fixture connection is already borrowed; exhaust the real pool.
                    for _ in range(self.pool.max_size - 1):
                        held.append(self.pool.getconn())
                else:
                    self.store.lock.acquire()
                before = time.monotonic()
                status, _, body = self.get("/viewer/api/v1/projects/p/memories")
                self.assertEqual(status, 503, body)
                self.assertLess(time.monotonic() - before, 2.5)
            finally:
                if hasattr(self, "pool"):
                    for connection in held:
                        self.pool.putconn(connection)
                else:
                    self.store.lock.release()
            self.assertEqual(self.get("/viewer/api/v1/projects/p/memories")[0], 200)

    def test_restore_rejects_old_session_cursor_and_cache_conditions(self):
        with self.listener():
            base = "/viewer/api/v1/projects/p/memories"
            self.store.capture(
                self.owner,
                self.ns,
                "second",
                {
                    "version": 1,
                    "project": "p",
                    "source": "test",
                    "source_event": "second",
                    "text": "second",
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            )
            first = json.loads(self.get(base + "?limit=1")[2])
            cursor = first["next_cursor"]
            self.assertIsNotNone(cursor)
            handle = first["data"]["memories"][0]["handle"]
            archive = backup_fixture(self)
            old_position = self.store.status(self.owner, "p")["position"]
            restore_fixture(self, archive)
            namespace = self.store.namespace(self.owner, "p")
            for i in range(old_position + 2):
                self.store.capture(
                    self.owner,
                    namespace,
                    f"replacement-{i}",
                    {
                        "version": 1,
                        "project": "p",
                        "source": "test",
                        "source_event": f"replacement-{i}",
                        "text": "new history",
                        "readers": None,
                        "classification": "internal",
                        "observed_at_ms": 1,
                    },
                )
            self.assertEqual(self.get(base)[0], 409)
            self.session = replace(self.session, subject=self.subject())
            self.assertEqual(self.get(base + "?limit=1&cursor=" + cursor)[0], 409)
            self.assertEqual(self.get(base + "/" + handle)[0], 404)
            status, headers, raw = self.get(
                base,
                headers={
                    "If-None-Match": "*",
                    "If-Modified-Since": "Wed, 21 Oct 2099 07:28:00 GMT",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertNotEqual(
                json.loads(raw)["binding"]["incarnation"], first["binding"]["incarnation"]
            )
            self.policy("revoke", {"principal": "phone", "project": "p", "expected_version": 2})
            self.assertEqual(self.get(base, headers={"If-None-Match": "*"})[0], 409)

    def test_utf8_detail_configured_bounds_and_disconnect_recover(self):
        with self.listener() as server:
            base = "/viewer/api/v1/projects/p/memories"
            self.store.capture(
                self.owner,
                self.ns,
                "unicode",
                {
                    "version": 1,
                    "project": "p",
                    "source": "test",
                    "source_event": "unicode",
                    "text": "界" * 10000,
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            )
            status, _, raw = self.get(base + "?q=%E7%95%8C")
            self.assertEqual(status, 200, raw)
            item = json.loads(raw)["data"]["memories"][0]
            self.assertTrue(item["truncated"])
            self.assertLessEqual(len(item["text"].encode()), 16384)
            self.assertNotIn("\ufffd", item["text"])
            server.config["limits"]["list_items"] = 1
            bounded = json.loads(self.get(base + "?limit=2")[2])
            self.assertEqual(len(bounded["data"]["memories"]), 1)
            self.assertTrue(bounded["truncated"])
            server.config["limits"]["response_bytes"] = 16384
            self.assertEqual(self.get(base + "?limit=1&q=%E7%95%8C")[0], 503)
            self.assertEqual(self.get(base + "?limit=1&q=Observed")[0], 200)
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as stream:
                stream.sendall(
                    b"GET /viewer/api/v1/session HTTP/1.1\r\nHost: viewer.example.invalid\r\nCookie: __Host-memoryking-viewer="
                    + b"a" * 48
                    + b"\r\n\r\n"
                )
            until = time.monotonic() + 3
            while server.threads and time.monotonic() < until:
                time.sleep(0.01)
            self.assertFalse(server.threads)
            self.assertEqual(self.get(base + "?limit=1&q=Observed")[0], 200)


class LocalViewerHTTP(HttpCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresViewerHTTP(HttpCases, NativeFixture):
    def test_postgres_checkout_validation_uses_total_deadline(self):
        with self.listener(request_seconds=1) as server:

            def slow(connection):
                # Each statement fits the original per-statement timeout, but
                # their sum must not overrun the total admitted request budget.
                for _ in range(10):
                    connection.execute("SELECT pg_sleep(0.4)")

            before = time.monotonic()
            with patch("memoryking.adapters.postgres_pool.check", slow):
                status, _, raw = self.get("/viewer/api/v1/projects/p/memories")
            self.assertEqual(status, 503, raw)
            self.assertLess(time.monotonic() - before, 2.5)
            self.assertEqual(self.get("/viewer/api/v1/projects/p/memories")[0], 200)
            self.assertNotIn("internal_error", server.counts)
