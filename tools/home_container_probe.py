"""Synthetic home-container probe, invoked only by the disposable rehearsal driver.

Never point this helper at an existing installation. It creates fixture policies
and false-for-real-use QA attestations; it never configures or invokes SMTP.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import os
import shutil
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from memoryking.adapters.local_files import Control, atomic
from memoryking.adapters.local_ipc import StoreOperations, request
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError, Released
from memoryking.domain.reporting import CHUNK_LIMIT

DATA, CONTROL, SECRETS = Path("/data/store"), Path("/control/security"), Path("/secrets")
PROJECT, ORIGIN = "home", "https://viewer.example.invalid"


class Probe:
    def __init__(self, backend, *, offline=False):
        self.backend, self.offline = backend, offline
        self.controller = Control(CONTROL / "main" if backend == "shared" else CONTROL)
        self.owner = (self.controller.path / "owner.credential").read_text()
        self.incarnation = self.controller.read_anchor()["incarnation"]
        self.pool = None
        self.mail = None
        if backend == "shared":
            from memoryking.adapters.postgres_pool import create_pool
            from memoryking.home import socket_dsn

            self.pool = create_pool(socket_dsn(SECRETS / "runtime.dsn"), size=2)
        self.cookies = {}
        settings = json.loads((SECRETS / "viewer.json").read_text())
        self.port = settings["listen_port"]
        self.tls = (
            ssl.create_default_context(cafile=settings["tls_cert_file"])
            if settings["transport"] == "direct_tls"
            else None
        )

    @contextmanager
    def store(self, credential=None):
        if self.pool:
            from memoryking.adapters.postgres_store import PostgresStore

            with PostgresStore(self.pool, DATA, CONTROL, "main", credential or self.owner) as store:
                if self.mail:
                    store.report_mail = self.mail
                yield store
        else:
            assert self.offline, "a second SQLite owner is forbidden"
            with LocalStore(DATA, CONTROL) as store:
                if self.mail:
                    store.report_mail = self.mail
                yield store

    def close(self):
        if self.pool:
            self.pool.close()

    def op(self, name, args, credential=None):
        credential = credential or self.owner
        if self.backend == "local" and not self.offline:
            return request(DATA / "service.sock", credential, name, args)
        with self.store(credential) as store:
            result = StoreOperations(store).execute(
                {"version": 1, "operation": name, "args": args, "credential": credential}
            )
            if isinstance(result, Released):
                output = []
                result.emit(output.append)
                return json.loads(output[0])
            return result

    def command(self, route, action, args):
        return self.op(
            route,
            {
                "command": {
                    "version": 1,
                    "incarnation": self.incarnation,
                    "key": uuid.uuid4().hex,
                    "action": action,
                    "args": ({"project": PROJECT} if route == "report" else {}) | args,
                }
            },
        )

    def report(self, action, args):
        return self.command("report", action, args)

    def prepare(self):
        with self.store() as store, store.transaction():
            assert store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        config = json.loads((SECRETS / "viewer.json").read_text())
        assert not config["enabled"] and not (SECRETS / "home-fixture.json").exists()
        self.command(
            "viewer_admin",
            "bind",
            {"principal": "phone", "issuer": "https://github.com", "user_id": "12345"},
        )
        self.command(
            "viewer_admin",
            "grant",
            {
                "principal": "phone",
                "project": PROJECT,
                "expected_version": 1,
                "starts": None,
                "expires": None,
            },
        )
        ns = self.op("namespace", {"project": PROJECT})["namespace"]
        captured = self.op(
            "capture",
            {
                "namespace": ns,
                "key": "home-rehearsal",
                "request": {
                    "version": 1,
                    "project": PROJECT,
                    "source": "synthetic-home-rehearsal",
                    "source_event": "one",
                    "text": "HOME_SYNTHETIC_VISIBLE",
                    "readers": ["owner", "phone"],
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            },
        )
        worker = self.op(
            "grant",
            {
                "principal": "report-worker",
                "project": PROJECT,
                "capabilities": ["read", "report_author"],
                "starts": None,
                "expires": None,
            },
        )
        atomic(SECRETS / "report.credential", worker["credential"].encode())
        self.report(
            "policy",
            {
                "expected_version": 0,
                "policy": {
                    "version": 1,
                    "project": PROJECT,
                    "program": "home-rehearsal",
                    "template": "phase-report",
                    "channel": "app",
                    "destination": None,
                    "recipient_principal": None,
                    "require_visual_qa": True,
                    "require_evidence": True,
                    "classification": "internal",
                    "readers": ["owner", "phone", "report-worker"],
                },
            },
        )
        source = "# Synthetic home report\n\n" + "\n\n".join(
            f"## {i}. Section {i}\n\nSynthetic measured result {i}."
            + (
                "\n\n" + "A synthetic paragraph exercises the confined renderer. " * 1800
                if i == 7
                else ""
            )
            for i in range(1, 18)
        )
        files = {
            "source.md": source.encode(),
            "readme.md": b"# Synthetic home installation",
            "evidence.json": b'{"fixture":true}',
        }
        dependencies = []
        for name, raw in files.items():
            digest = hashlib.sha256(raw).hexdigest()
            if name != "source.md":
                identity = name.split(".")[0]
                self.report(
                    "bind_source",
                    {
                        "expected_version": 0,
                        "binding": {
                            "id": identity,
                            "sha256": digest,
                            "readers": ["owner", "phone", "report-worker"],
                            "classification": "internal",
                            "enabled": True,
                            "starts": None,
                            "expires": None,
                        },
                    },
                )
                dependencies.append({"kind": "external_source", "id": identity, "sha256": digest})
            for offset in range(0, len(raw), CHUNK_LIMIT):
                self.report(
                    "upload",
                    {
                        "upload": "fixture",
                        "name": name,
                        "sha256": digest,
                        "bytes": len(raw),
                        "part": offset // CHUNK_LIMIT,
                        "parts": (len(raw) + CHUNK_LIMIT - 1) // CHUNK_LIMIT,
                        "data_base64": base64.b64encode(
                            raw[offset : offset + CHUNK_LIMIT]
                        ).decode(),
                    },
                )
        self.report(
            "submit",
            {
                "report": "home-report",
                "expected_revision": 0,
                "upload": "fixture",
                "program": "home-rehearsal",
                "phase": "S01",
                "task": None,
                "title": "Synthetic home report",
                "technical_status": "partial",
                "classification": "internal",
                "dependencies": dependencies,
            },
        )
        atomic(SECRETS / "github.secret", b"synthetic-never-used-for-real-github")
        config.update(
            enabled=True, public_origin=ORIGIN, github_client_id="synthetic-home-rehearsal"
        )
        atomic(SECRETS / "viewer.json", json.dumps(config).encode())
        reports = json.loads((SECRETS / "reports.json").read_text())
        reports.update(
            enabled=True,
            project=PROJECT,
            credential_file="/secrets/report.credential",
            mail_secret_file=None,
            sender_enabled=False,
            poll_seconds=1,
        )
        atomic(SECRETS / "reports.json", json.dumps(reports).encode())
        atomic(
            SECRETS / "home-fixture.json",
            json.dumps(
                {"incarnation": self.incarnation, "namespace": ns, "captured": captured}
            ).encode(),
        )
        return {"prepared": True, "fixture": True, "mail_configured": False}

    def http(self, path, *, method="GET", body=None, headers=None):
        request_headers = {
            "Host": "viewer.example.invalid",
            "Cookie": "; ".join(f"{key}={value}" for key, value in self.cookies.items()),
            **(headers or {}),
        }
        if body is not None:
            body = json.dumps(body)
            request_headers.update({"Content-Type": "application/json", "Origin": ORIGIN})
        connection = (
            http.client.HTTPSConnection("127.0.0.1", self.port, context=self.tls, timeout=6)
            if self.tls
            else http.client.HTTPConnection("127.0.0.1", self.port, timeout=6)
        )
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            for name, value in response.getheaders():
                if name.lower() == "set-cookie":
                    assert "Secure" in value and "HttpOnly" in value
                    cookie = SimpleCookie(value)
                    for key, item in cookie.items():
                        if item["max-age"] == "0":
                            self.cookies.pop(key, None)
                        else:
                            self.cookies[key] = item.value
            return response.status, raw, dict(response.getheaders())
        finally:
            connection.close()

    def login(self, *, denied=False):
        status, raw, _ = self.http("/viewer/auth/context")
        assert status == 200, (status, raw)
        status, raw, _ = self.http(
            "/viewer/auth/github/start", method="POST", body={"csrf": json.loads(raw)["csrf"]}
        )
        assert status == 200, (status, raw)
        state = parse_qs(urlsplit(json.loads(raw)["authorization_url"]).query)["state"][0]
        status, _, headers = self.http(
            "/viewer/auth/github/callback?"
            + urlencode(
                {
                    "state": state,
                    "code": "synthetic-home-code",
                    "iss": "https://github.com/login/oauth",
                }
            )
        )
        assert status == 303 and headers["Location"] == "/viewer/", (status, headers)
        status, raw, _ = self.http("/viewer/api/v1/session")
        if denied:
            assert status == 401, (status, raw)
            return
        assert status == 200 and json.loads(raw)["data"]["projects"] == [PROJECT], (status, raw)

    def live(self):
        self.login()
        assert self.http("/viewer/", headers={"Host": "attacker.invalid"})[0] == 400
        assert (
            self.http("/viewer/api/v1/session", headers={"Origin": "https://attacker.invalid"})[0]
            == 404
        )
        assert self.http("/v1/tenants/main/operations")[0] == 404
        self.report("build", {"report": "home-report", "revision": 1})
        samples, states = [], []
        deadline = time.monotonic() + 130
        while time.monotonic() < deadline:
            started = time.monotonic()
            status, raw, headers = self.http("/viewer/api/v1/projects/home/memories")
            samples.append(time.monotonic() - started)
            assert status == 200 and b"HOME_SYNTHETIC_VISIBLE" in raw, (status, raw)
            assert headers["Cache-Control"] == "no-store"
            current = self.report("status", {"report": "home-report", "revision": 1})
            states.append(current["state"])
            assert current["state"] != "failed", current
            if current["state"] == "built":
                break
            time.sleep(0.03)
        assert "building" in states and states[-1] == "built", states
        from pypdf import PdfReader

        artifacts = {a["name"]: a["sha256"] for a in current["artifacts"]}
        pdf = (DATA / "blobs/main" / artifacts["report.pdf"]).read_bytes()
        pages = len(PdfReader(io.BytesIO(pdf)).pages)
        self.report(
            "qa",
            {
                "report": "home-report",
                "revision": 1,
                "attestation": {
                    "version": 1,
                    "report": "home-report",
                    "revision": 1,
                    **{
                        key: current[key]
                        for key in ("source_sha256", "readme_sha256", "evidence_sha256")
                    },
                    "pdf_sha256": artifacts["report.pdf"],
                    "epub_sha256": artifacts["report.epub"],
                    "reader_sha256": artifacts["reader.json"],
                    "pdf_pages_reviewed": pages,
                    "pdf_reviewed": True,
                    "epub_reviewed": True,
                    "method": "Synthetic integration fixture; not actual phase or human visual QA",
                },
            },
        )
        self.report("publish", {"report": "home-report", "revision": 1})
        status, raw, _ = self.http("/viewer/api/v1/projects/home/reports")
        assert status == 200 and b"Synthetic home report" in raw
        status, raw, _ = self.http(
            "/viewer/api/v1/projects/home/reports/home-report/versions/1/sections/0"
        )
        assert status == 200 and b"Synthetic home report" in raw, (status, raw)
        assert not (SECRETS / "mail.json").exists()
        return {
            "passed": True,
            "controlled_provider": True,
            "report_pdf_pages": pages,
            "render_states_observed": sorted(set(states)),
            "concurrent_private_reads": len(samples),
            "read_seconds": {"median": statistics.median(samples), "max": max(samples)},
            "report_published_and_read": True,
            "mail_configured": False,
        }

    def backup(self):
        """Back up content, then retain later revocation and synthetic delivery evidence."""
        from memoryking.adapters.report_delivery import MailConfiguration
        from memoryking.reporting.smtp import GmailDestination

        reports = json.loads((SECRETS / "reports.json").read_text())
        reports.update(enabled=False, sender_enabled=False, credential_file=None)
        atomic(SECRETS / "reports.json", json.dumps(reports).encode())
        self.mail = MailConfiguration(
            GmailDestination(
                "synthetic",
                "sender@example.invalid",
                "recipient@example.invalid",
                "not-a-real-secret",
            ),
            enabled=False,
            project=PROJECT,
        )
        self.report(
            "policy",
            {
                "expected_version": 0,
                "policy": {
                    "version": 1,
                    "project": PROJECT,
                    "program": "synthetic-delivery-ledger",
                    "template": "phase-report",
                    "channel": "kindle",
                    "destination": "synthetic",
                    "recipient_principal": "owner",
                    "require_visual_qa": True,
                    "require_evidence": True,
                    "classification": "internal",
                    "readers": ["owner"],
                },
            },
        )
        proof = hashlib.sha256(b"Synthetic accepted receipt; no real submission").hexdigest()
        self.report(
            "bind_source",
            {
                "expected_version": 0,
                "binding": {
                    "id": "synthetic-receipt",
                    "sha256": proof,
                    "readers": ["owner"],
                    "classification": "internal",
                    "enabled": True,
                    "starts": None,
                    "expires": None,
                },
            },
        )
        archive = Path("/archives/content-before")
        subprocess.run(self.maintenance_command("backup", archive), check=True, capture_output=True)
        shutil.copytree(CONTROL, "/archives/control-before")
        damaged = Path("/archives/corrupt-content")
        shutil.copytree(archive, damaged)
        atomic(damaged / "manifest", b"corrupt synthetic signature")
        before = (self.controller.path / "anchor").read_bytes()
        try:
            self.restore(archive=damaged)
        except LocalError as error:
            assert error.code in {"invalid_token", "backup_invalid"}, error.code
        else:
            raise AssertionError("corrupt backup accepted")
        assert (self.controller.path / "anchor").read_bytes() == before
        imported = self.report(
            "import_delivery",
            {
                "report": "historical-fixture",
                "legacy": {
                    "version": 1,
                    "program": "synthetic-delivery-ledger",
                    "phase": "S00",
                    "revision": 1,
                    "legacy_identity": "a" * 64,
                    "receipt_sha256": proof,
                    "source_sha256": "b" * 64,
                    "artifact_sha256": "c" * 64,
                    "message_id": "<synthetic-only@reports.invalid>",
                    "accepted_ms": 1,
                    "recipient_fingerprint": self.mail.destination.fingerprint,
                    "destination": "synthetic",
                    "evidence": {
                        "kind": "external_source",
                        "id": "synthetic-receipt",
                        "sha256": proof,
                    },
                },
            },
        )
        self.command(
            "viewer_admin",
            "revoke",
            {"principal": "phone", "project": PROJECT, "expected_version": 2},
        )
        fixture = json.loads((SECRETS / "home-fixture.json").read_text())
        payload = {
            "version": 1,
            "project": PROJECT,
            "source": "post-backup-synthetic",
            "source_event": "acknowledged",
            "text": "HOME_SYNTHETIC_POST_BACKUP",
            "readers": ["owner"],
            "classification": "internal",
            "observed_at_ms": 2,
        }
        lost = self.op(
            "capture", {"namespace": fixture["namespace"], "key": "post-backup", "request": payload}
        )
        fixture.update(lost=lost, lost_request=payload, delivery=imported["delivery"])
        atomic(SECRETS / "home-fixture.json", json.dumps(fixture).encode())
        shutil.copytree(CONTROL, "/archives/control-current")
        shutil.copytree(SECRETS, "/archives/private-configuration")
        anchor = self.controller.read_anchor()
        atomic(
            Path("/archives/current-control-head.json"),
            json.dumps(
                {
                    "journal_head": anchor["journal_head"],
                    "journal_sequence": anchor["journal_sequence"],
                    "incarnation": anchor["incarnation"],
                    "synthetic": True,
                }
            ).encode(),
        )
        return {
            "passed": True,
            "manifest_sha256": hashlib.sha256((archive / "manifest").read_bytes()).hexdigest(),
            "later_revocation": True,
            "later_delivery_record": "synthetic imported acceptance",
            "control_snapshots_separate": True,
            "sender_disabled": True,
            "corrupt_backup_denied_without_changing_history": True,
            "documented_backup_cli": True,
        }

    def maintenance_command(self, operation, archive):
        if self.backend == "local":
            return [
                sys.executable,
                "-m",
                "memoryking",
                operation,
                "--store",
                str(DATA),
                "--control",
                str(CONTROL),
                "--backup",
                str(archive),
            ]
        command = [
            sys.executable,
            "-m",
            "memoryking.shared",
            operation,
            "--dsn-file",
            "/pgadmin/migration.dsn" if operation == "restore" else "/secrets/runtime.dsn",
            "--content",
            str(DATA),
            "--control",
            str(CONTROL),
            "--tenant",
            "main",
            "--archive",
            str(archive),
        ]
        if operation == "backup":
            command += [
                "--backup-dsn-file",
                "/pgadmin/backup.dsn",
                "--credential",
                str(CONTROL / "main/owner.credential"),
            ]
        return command

    def restore(self, *, interrupt=False, archive=None):
        def fault(name):
            if interrupt and name == "restore_fenced":
                os._exit(73)  # Actual process interruption after the durable fence.

        use_cli = not interrupt and archive is None
        archive = archive or Path("/archives/content-before")
        if use_cli:
            subprocess.run(
                self.maintenance_command("restore", archive), check=True, capture_output=True
            )
        elif self.backend == "shared":
            import psycopg

            from memoryking.adapters.postgres_recovery import restore
            from memoryking.home import socket_dsn

            with psycopg.connect(
                socket_dsn(Path("/pgadmin/migration.dsn")), autocommit=True
            ) as connection:
                restore(connection, DATA, CONTROL, "main", archive, fault=fault)
        else:
            LocalStore.restore(DATA, CONTROL, archive, fault=fault)
        anchor = self.controller.read_anchor()
        assert anchor["incarnation"] != self.incarnation and anchor["reconciliation_required"]
        return {
            "passed": True,
            "new_incarnation": True,
            "reconciliation_required": True,
            "current_control_preserved": True,
            "documented_restore_cli": use_cli,
        }

    def restore_cut(self):
        return self.restore(interrupt=True)

    def certificate(self, directory):
        import ipaddress
        from datetime import datetime, timedelta, timezone

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "viewer.example.invalid")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
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
        crt, private = directory / "test.crt", directory / "test.key"
        atomic(crt, cert.public_bytes(serialization.Encoding.PEM))
        atomic(
            private,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        return crt, private

    def enable_tls(self):
        crt, key = self.certificate(SECRETS)
        config = json.loads((SECRETS / "viewer.json").read_text())
        config.update(transport="direct_tls", tls_cert_file=str(crt), tls_key_file=str(key))
        atomic(SECRETS / "viewer.json", json.dumps(config).encode())
        return {"configured": "direct_tls", "certificate": "one-day synthetic local CA"}

    def surface(self):
        self.login()
        status, raw, _ = self.http("/viewer/api/v1/projects/home/reports")
        assert status == 200 and b"Synthetic home report" in raw
        assert self.http("/viewer/", method="HEAD")[0] == 200
        assert (
            self.http(
                "/viewer/",
                headers={"Host": "attacker.invalid", "X-Forwarded-Host": "viewer.example.invalid"},
            )[0]
            == 400
        )
        assert (
            self.http("/viewer/api/v1/session", headers={"Origin": "https://attacker.invalid"})[0]
            == 404
        )
        self.cookies.clear()
        assert (
            self.http(
                "/viewer/api/v1/session",
                headers={"Tailscale-User-Login": "owner", "X-Forwarded-User": "owner"},
            )[0]
            == 401
        )
        return {
            "passed": True,
            "trusted_local_tls": self.tls is not None,
            "current_private_read": True,
            "spoofed_host_origin_identity_denied": True,
        }

    def gateway(self):
        assert self.tls is None

        class Forward(BaseHTTPRequestHandler):
            def forward(handler):
                connection = http.client.HTTPConnection("127.0.0.1", 8772, timeout=6)
                try:
                    length = int(handler.headers.get("Content-Length", "0"))
                    assert 0 <= length <= 512
                    body = handler.rfile.read(length) if length else None
                    connection.request(
                        handler.command, handler.path, body=body, headers=dict(handler.headers)
                    )
                    response = connection.getresponse()
                    raw = response.read()
                    handler.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in {"connection", "server", "date"}:
                            handler.send_header(key, value)
                    handler.send_header("Connection", "close")
                    handler.end_headers()
                    if handler.command != "HEAD":
                        handler.wfile.write(raw)
                finally:
                    connection.close()

            do_GET = do_HEAD = do_POST = forward

            def log_message(handler, *_):
                pass

        with tempfile.TemporaryDirectory() as folder:
            crt, key = self.certificate(Path(folder))
            server = ThreadingHTTPServer(("127.0.0.1", 0), Forward)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            tls.load_cert_chain(crt, key)
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1})
            worker.start()
            self.port, self.tls = server.server_port, ssl.create_default_context(cafile=str(crt))
            try:
                result = self.surface()
                return {**result, "gateway": "controlled local TLS proxy; not real Tailscale Serve"}
            finally:
                server.shutdown()
                server.server_close()
                worker.join()

    def network(self):
        import socket

        listeners = []
        for filename in ("/proc/net/tcp", "/proc/net/tcp6"):
            for row in Path(filename).read_text().splitlines()[1:]:
                fields = row.split()
                if fields[3] != "0A":
                    continue
                address, port = fields[1].split(":")
                port = int(port, 16)
                if address == "0B00007F":  # Docker's namespace-local DNS resolver.
                    continue
                listeners.append({"address_hex": address, "port": port})
        assert listeners == [{"address_hex": "0100007F", "port": 8772}], listeners
        if self.backend == "local":
            try:
                LocalStore(DATA, CONTROL)
            except LocalError as error:
                assert error.code == "owner_busy_or_unsafe"
            else:
                raise AssertionError("second owner accepted")
        else:
            with self.pool.connection() as connection:
                assert connection.execute("SHOW listen_addresses").fetchone()[0] == ""
            assert Path("/socket/.s.PGSQL.5432").exists()
        for port in (5432, 8765, 8771):
            with socket.socket() as probe:
                probe.settimeout(0.2)
                assert probe.connect_ex(("127.0.0.1", port)) != 0
        return {
            "passed": True,
            "listeners": listeners,
            "no_database_or_admin_tcp": True,
            "namespace": os.readlink("/proc/self/ns/net"),
        }

    def restored(self):
        fixture = json.loads((SECRETS / "home-fixture.json").read_text())
        assert self.incarnation != fixture["incarnation"]
        self.login(denied=True)
        namespace = self.op("namespace", {"project": PROJECT})["namespace"]
        position = 0
        for number in range(20):
            payload = {**fixture["lost_request"], "source_event": f"replacement-{number}"}
            position = self.op(
                "capture",
                {"namespace": namespace, "key": f"replacement-{number}", "request": payload},
            )["position"]
        assert position > fixture["lost"]["position"]
        blocked = []
        for action, args in (
            ("status", {"project": PROJECT, "watermark": fixture["lost"]["watermark"]}),
            (
                "capture",
                {
                    "namespace": fixture["namespace"],
                    "key": "post-backup",
                    "request": fixture["lost_request"],
                },
            ),
        ):
            try:
                self.op(action, args)
            except LocalError as error:
                assert error.code == "history_changed", error.code
                blocked.append(action)
            else:
                raise AssertionError("old history unexpectedly accepted")
        status = self.report("delivery_status", {"delivery": fixture["delivery"]})
        assert status["state"] == "smtp_accepted" and status["attempts"] == 0
        sender = self.report("sender_status", {})
        assert not sender["enabled"]
        assert not json.loads((SECRETS / "reports.json").read_text())["enabled"]
        return {
            "passed": True,
            "revoked_viewer_denied": True,
            "old_history_blocked": blocked,
            "replacement_position_exceeds_lost": True,
            "synthetic_delivery_preserved": True,
            "sender_disabled": True,
            "actual_smtp_attempts": 0,
        }

    def sandbox(self):
        from memoryking.reporting.sandbox import isolated_python

        code = """import json,socket,subprocess
from pathlib import Path
results={}
for key,path in [('secret','/secrets/github.secret'),('control','/control/security/identity.json'),('data','/data/store'),('socket','/socket')]:
 try: Path(path).stat(); Path(path).read_bytes();results[key]='leaked'
 except (PermissionError,FileNotFoundError): results[key]='denied'
try: socket.socket();results['network']='leaked'
except PermissionError:results['network']='denied'
try: subprocess.run(['/bin/true']);results['exec']='leaked'
except PermissionError:results['exec']='denied'
Path('probe.json').write_text(json.dumps(results))
"""
        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder)
            isolated_python(stage, ["-c", code], protected=[DATA, CONTROL, SECRETS])
            values = json.loads((stage / "probe.json").read_text())
            assert all(value == "denied" for value in values.values()), values
            return {"passed": True, "denials": values}


def inject_fixture_fault(backend, action):
    """Deliberate offline corruption/reversal in this newly created fixture only."""
    assert (SECRETS / "home-fixture.json").is_file()
    if action.startswith("identity_"):
        directory = CONTROL / "main" if backend == "shared" else CONTROL
        original, quarantine = directory / "identity.json", directory / "identity.quarantined"
        if action == "identity_hide":
            assert original.exists() and not quarantine.exists()
            original.rename(quarantine)
        else:
            assert quarantine.exists() and not original.exists()
            quarantine.rename(original)
    elif action.startswith("tls_"):
        key, backup = SECRETS / "test.key", SECRETS / "test-key.backup"
        if action == "tls_break":
            atomic(backup, key.read_bytes())
            atomic(key, b"invalid synthetic private key")
        else:
            atomic(key, backup.read_bytes())
            backup.unlink()
    else:
        assert action in {"schema_break", "schema_repair"}
        if backend == "local":
            import sqlite3

            with sqlite3.connect(DATA / "store.sqlite") as connection:
                connection.execute(
                    "PRAGMA user_version=" + ("999" if action == "schema_break" else "10")
                )
        else:
            import psycopg

            from memoryking.home import socket_dsn

            with psycopg.connect(
                socket_dsn(Path("/pgadmin/migration.dsn")), autocommit=True
            ) as connection:
                connection.execute(
                    "UPDATE memoryking.shared_meta SET value=%s WHERE key='schema'",
                    ("999" if action == "schema_break" else "5",),
                )
    return {"fixture_fault": action, "applied": True}


def main():
    assert os.environ.get("MEMORYKING_HOME_REHEARSAL") == "1", "disposable rehearsal only"
    backend, action = sys.argv[1:]
    assert backend in {"local", "shared"}
    if action in {
        "schema_break",
        "schema_repair",
        "identity_hide",
        "identity_repair",
        "tls_break",
        "tls_repair",
    }:
        print(json.dumps(inject_fixture_fault(backend, action)))
        return
    assert backend in {"local", "shared"} and action in {
        "prepare",
        "live",
        "sandbox",
        "backup",
        "restore",
        "restore_cut",
        "restored",
        "network",
        "gateway",
        "enable_tls",
        "surface",
    }
    probe = Probe(
        backend, offline=action in {"prepare", "backup", "restore", "restore_cut", "enable_tls"}
    )
    try:
        if action != "prepare":
            assert (SECRETS / "home-fixture.json").is_file()
        print(json.dumps(getattr(probe, action)()))
    finally:
        probe.close()


if __name__ == "__main__":
    main()
