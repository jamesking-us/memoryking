"""Liveness and readiness answers that reveal nothing, shared by both profiles.

Liveness says the process answers: a fixed body, no work, no authentication. Readiness says the
store can serve: the database answers, the control tree is readable and active, and the
database's schema version is one this build accepts. Unauthenticated callers get only a
boolean (HTTP 200 or 503), never which check failed; the authenticated metrics endpoint
carries each check as a gauge with a fixed name.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CHECKS = ("database", "control", "schema")
LIVE_BODY = b'{"status":"ok"}'
READY_BODY = b'{"ready":true}'
NOT_READY_BODY = b'{"ready":false}'
SAFE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Type": "application/json",
}


def readiness_response(checks: dict[str, bool]) -> tuple[int, bytes]:
    ready = all(checks.get(name, False) for name in CHECKS)
    return (200, READY_BODY) if ready else (503, NOT_READY_BODY)


class CachedChecks:
    """Runs the checks at most once per `ttl` seconds, however many probes arrive, so an
    unauthenticated endpoint cannot be used to hammer the database."""

    def __init__(self, compute: Callable[[], dict[str, bool]], ttl: float = 2.0):
        self.compute, self.ttl = compute, ttl
        self.lock = threading.Lock()
        self.at = float("-inf")
        self.value: dict[str, bool] = dict.fromkeys(CHECKS, False)

    def __call__(self) -> dict[str, bool]:
        with self.lock:
            if time.monotonic() - self.at >= self.ttl:
                try:
                    self.value = {name: bool(v) for name, v in self.compute().items()}
                except Exception:
                    self.value = dict.fromkeys(CHECKS, False)
                self.at = time.monotonic()
            return dict(self.value)


class _Handler(BaseHTTPRequestHandler):
    server: Any
    server_version = "MemoryKing"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/healthz":
            status, body = 200, LIVE_BODY
        elif self.path == "/readyz":
            status, body = readiness_response(self.server.checks())
        else:
            status, body = 404, b'{"error":"not_found"}'
        self.send_response(status)
        for name, value in SAFE_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET


class LocalHealthServer(ThreadingHTTPServer):
    """`/healthz` and `/readyz` on a loopback port for the local daemon, which otherwise has
    no HTTP surface. It serves nothing else and takes no credentials."""

    daemon_threads = True

    def __init__(self, checks: Callable[[], dict[str, bool]], port: int = 0):
        self.checks = CachedChecks(checks)
        super().__init__(("127.0.0.1", port), _Handler)
        self.thread: threading.Thread | None = None

    def start(self) -> int:
        self.thread = threading.Thread(target=self.serve_forever, name="health", daemon=True)
        self.thread.start()
        return int(self.server_address[1])

    def stop(self) -> None:
        if self.thread is not None:
            self.shutdown()
            self.thread.join(5)
        self.server_close()


def describe(checks: dict[str, bool]) -> str:
    return json.dumps({name: checks.get(name, False) for name in CHECKS})
