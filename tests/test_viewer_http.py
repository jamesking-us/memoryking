"""Real loopback sockets, native stores and verified local TLS; no real OAuth bypass."""

import http.client
import ipaddress
import socket
import ssl
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import test_knowledge as knowledge
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from test_postgres_parity import NativeFixture
from test_viewer_reads import ReadCases

from memoryking.adapters.viewer_http import ViewerServer
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

    @contextmanager
    def listener(self, *, tls=False, authenticated=True):
        self.setup_viewer()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        settings = {**self.settings, "listen_port": port}
        self.client_tls = None
        if tls:
            cert, key = certificate(self.root)
            settings.update(transport="direct_tls", tls_cert_file=str(cert), tls_key_file=str(key))
            self.client_tls = ssl.create_default_context(cafile=str(cert))

        def resolve(token):
            if token != "a" * 48:
                raise LocalError("unauthenticated")
            return self.session

        server = ViewerServer(
            settings,
            lambda session: self.actor_store(session.subject.credential),
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


class LocalViewerHTTP(HttpCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresViewerHTTP(HttpCases, NativeFixture):
    pass
