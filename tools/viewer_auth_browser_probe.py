"""Real browser login with a controlled GitHub fixture; no external provider requests."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import platform
import ssl
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


@contextmanager
def gateway(fixture):
    from test_viewer_http import certificate

    class Forward(BaseHTTPRequestHandler):
        def do_GET(self):
            connection = HTTPConnection("127.0.0.1", fixture.port, timeout=6)
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                connection.request(
                    self.command, self.path, body=body or None, headers=dict(self.headers)
                )
                response = connection.getresponse()
                raw = response.read()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in {"connection", "server", "date"}:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    try:
                        self.wfile.write(raw)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # Expected when the browser aborts an obsolete view.
            finally:
                connection.close()

        do_POST = do_HEAD = do_GET

        def log_message(self, *args):
            pass

    cert, key = certificate(fixture.root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Forward)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"https://viewer.example.invalid:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run_browser(fixture, transport, args):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    from memoryking.adapters.viewer_http import Handler
    from memoryking.domain.local import LocalError

    wire_callbacks = []

    class ObservedHandler(Handler):
        def do_GET(self):
            if self.path.startswith("/viewer/auth/github/callback?"):
                wire_callbacks.append(
                    {
                        "origin_present": "Origin" in self.headers,
                        "fetch_site": self.headers.get("Sec-Fetch-Site"),
                        "fetch_mode": self.headers.get("Sec-Fetch-Mode"),
                        "fetch_dest": self.headers.get("Sec-Fetch-Dest"),
                    }
                )
            super().do_GET()

    fixture.server.RequestHandlerClass = ObservedHandler
    origin = fixture.settings["public_origin"]
    cert = x509.load_pem_x509_certificate((fixture.root / "viewer.crt").read_bytes())
    spki = base64.b64encode(
        hashlib.sha256(
            cert.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        ).digest()
    ).decode()
    exchange = fixture.root / "controlled-exchange.json"
    calls = []

    class Provider:
        def authenticate(self, code, verifier, callback, deadline):
            expected = json.loads(exchange.read_text())
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            if (
                code != "controlled-code"
                or challenge != expected["code_challenge"]
                or callback != origin + "/viewer/auth/github/callback"
            ):
                raise LocalError("login_failed")
            calls.append("verified-numeric-identity")
            return "12345"

    fixture.auth.provider = Provider()
    config, output = fixture.root / "browser-input.json", fixture.root / "browser-result.json"
    config.write_text(
        json.dumps(
            {
                "chrome": str(args.chrome),
                "playwright": str(args.playwright),
                "origin": origin,
                "spki": spki,
                "transport": transport,
                "exchange": str(exchange),
            }
        )
    )
    subprocess.run(
        [
            str(args.node),
            str(ROOT / "tools/viewer-auth-browser-probe.mjs"),
            str(config),
            str(output),
        ],
        check=True,
        timeout=90,
    )
    result = json.loads(output.read_text())
    assert len(calls) == 2
    assert len(wire_callbacks) == 3, wire_callbacks
    assert all(
        not c["origin_present"] and c["fetch_mode"] == "navigate" and c["fetch_dest"] == "document"
        for c in wire_callbacks
    ), wire_callbacks
    assert [c["fetch_site"] for c in wire_callbacks] == ["cross-site", "cross-site", "same-site"], (
        wire_callbacks
    )
    assert not fixture.auth.sessions
    return {**result, "verified_provider_exchanges": len(calls), "wire_callbacks": wire_callbacks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "chrome", "playwright", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    for path in (args.node, args.chrome, args.playwright):
        if not path.is_absolute() or not path.is_file():
            raise SystemExit("Explicit local runtime paths required")
    import test_viewer_auth_http as http

    http.PostgresViewerAuthHTTP.setUpClass()
    results = []
    try:
        for backend in (http.LocalViewerAuthHTTP, http.PostgresViewerAuthHTTP):
            for transport in ("direct_tls", "isolated_proxy"):
                fixture = backend("test_actual_login_cookie_flags_private_read_and_logout")
                fixture.setUp()
                try:
                    if transport == "direct_tls":
                        with fixture.listener(tls=True):
                            result = run_browser(fixture, transport, args)
                    else:
                        with gateway(fixture) as origin, fixture.listener(public_origin=origin):
                            result = run_browser(fixture, transport, args)
                    results.append({"backend": backend.__name__, **result})
                finally:
                    fixture.tearDown()
    finally:
        http.PostgresViewerAuthHTTP.tearDownClass()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "passed": len(results) == 4 and all(r["passed"] for r in results),
                "platform": platform.platform(),
                "scope": "Desktop Chromium controlled-provider authentication; both actual stores and both transport modes. No real GitHub, Android, Tailscale or deployment qualification.",
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )
    print("Passed all four controlled-provider browser/store/transport combinations")


if __name__ == "__main__":
    main()
