"""Exercise both actual viewer modes/stores in isolated desktop Chromium fixtures."""

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
    # Test-only HTTPS termination proxy. The product isolated_proxy mode itself
    # accepts only loopback; deployment isolation is separately qualified in M06.
    import test_viewer_http as http

    class Forward(BaseHTTPRequestHandler):
        def do_GET(self):
            connection = HTTPConnection("127.0.0.1", fixture.port, timeout=6)
            try:
                connection.request(self.command, self.path, headers=dict(self.headers))
                response = connection.getresponse()
                body = response.read()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in {"connection", "server", "date"}:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            finally:
                connection.close()

        do_HEAD = do_GET

        def log_message(self, *args):
            pass

    certificate, key = http.certificate(fixture.root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Forward)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    try:
        yield f"https://viewer.example.invalid:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run_browser(fixture, origin, transport, args):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate((fixture.root / "viewer.crt").read_bytes())
    spki = base64.b64encode(
        hashlib.sha256(
            cert.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        ).digest()
    ).decode()
    config, output = fixture.root / "browser-input.json", fixture.root / "browser-result.json"
    config.write_text(
        json.dumps(
            {
                "chrome": str(args.chrome),
                "playwright": str(args.playwright),
                "origin": origin,
                "spki": spki,
                "transport": transport,
            }
        )
    )
    subprocess.run(
        [str(args.node), str(ROOT / "tools/viewer-browser-probe.mjs"), str(config), str(output)],
        check=True,
        timeout=60,
    )
    return json.loads(output.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "chrome", "playwright", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    for path in (args.node, args.chrome, args.playwright):
        if not path.is_absolute() or not path.is_file():
            raise SystemExit("Browser probe requires explicit local executable/module paths")
    import test_viewer_http as http

    http.PostgresViewerHTTP.setUpClass()
    results = []
    try:
        for backend in (http.LocalViewerHTTP, http.PostgresViewerHTTP):
            for transport in ("direct_tls", "isolated_proxy"):
                fixture = backend("test_actual_get_head_private_reads_and_no_origin_in_both_modes")
                fixture.setUp()
                try:
                    if transport == "direct_tls":
                        with fixture.listener(tls=True, browser_origin=True) as server:
                            result = run_browser(
                                fixture, server.config["public_origin"], transport, args
                            )
                    else:
                        with gateway(fixture) as origin, fixture.listener(public_origin=origin):
                            result = run_browser(fixture, origin, transport, args)
                    results.append({"backend": backend.__name__, **result})
                finally:
                    fixture.tearDown()
    finally:
        http.PostgresViewerHTTP.tearDownClass()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "passed": len(results) == 4 and all(r["passed"] for r in results),
                "platform": platform.platform(),
                "scope": "Desktop Chrome, actual SQLite/native PostgreSQL and direct TLS/test TLS gateway; no phone, OAuth, Tailscale or deployment qualification",
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )
    print("Passed all four desktop-browser/store/transport combinations")


if __name__ == "__main__":
    main()
