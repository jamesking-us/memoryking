"""Dedicated bounded HTTP listener. No shared/owner route or credential fallback."""

from __future__ import annotations

import re
import socket
import ssl
import threading
import time
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractContextManager
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from memoryking.adapters.local_files import private_file
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.viewer_config import validate_config
from memoryking.adapters.viewer_reads import ViewerReads, ViewerSession
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError, Released

IDENT = r"[A-Za-z0-9_-]{1,96}"
PREFIX = "/viewer/api/v1/"
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}
SHELL = b'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/viewer/shell.css"><title>MemoryKing viewer</title><main><h1>MemoryKing viewer</h1><p>Private dashboard access.</p><p>Sign-in is not configured in this build.</p></main></html>'
STYLE = b"body{font:1rem system-ui;background:#101b29;color:#e8f1fa;margin:2rem}main{max-width:42rem;margin:auto}"


def route(target: str) -> tuple[str, str | None, dict[str, Any]]:
    """Closed raw URL grammar; no encoded path aliases or caller-supplied operations."""
    if len(target.encode("utf-8")) > 8192 or not target.startswith("/") or target.startswith("//"):
        raise LocalError("invalid_request")
    parsed = urlsplit(target)
    path = parsed.path
    if parsed.scheme or parsed.netloc or parsed.fragment or "%" in path or "\\" in path:
        raise LocalError("invalid_request")
    if any(ord(c) < 32 or ord(c) >= 127 for c in target):
        raise LocalError("invalid_request")
    if re.search(r"%(?![a-fA-F0-9]{2})", parsed.query):
        raise LocalError("invalid_request")
    try:
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=4,
        )
    except (ValueError, UnicodeError) as exc:
        raise LocalError("invalid_request") from exc
    if len({k for k, _ in pairs}) != len(pairs):
        raise LocalError("invalid_request")
    query: dict[str, Any] = dict(pairs)
    if path in {"/viewer/", "/viewer/shell.css"} and not query:
        return ("shell" if path.endswith("/") else "style"), None, {}
    if path == PREFIX + "session" and not query:
        return "session", None, {}
    match = re.fullmatch(PREFIX + rf"projects/({IDENT})/(.+)", path)
    if match is None:
        raise LocalError("unavailable")
    project, suffix = match.groups()
    parameters: dict[str, Any] = {}
    if suffix in {"overview", "tasks", "memories", "graph", "reports"}:
        name = suffix
        allowed = {"limit", "cursor"} if name in {"tasks", "memories", "reports"} else set()
        if name == "memories":
            allowed.add("q")
    elif (match := re.fullmatch(rf"tasks/({IDENT})", suffix)) is not None:
        name, allowed = "task", set()
        parameters["task"] = match[1]
    elif (match := re.fullmatch(r"memories/([A-Za-z0-9_.-]{1,4096})", suffix)) is not None:
        name, allowed = "memory", set()
        parameters["handle"] = match[1]
    elif (
        match := re.fullmatch(rf"reports/({IDENT})/versions/([1-9][0-9]{{0,8}})(.*)", suffix)
    ) is not None:
        parameters = {"report": match[1], "revision": int(match[2])}
        tail = match[3]
        if tail == "":
            name, allowed = "report", set()
        elif (sub := re.fullmatch(r"/sections/(0|[1-9][0-9]{0,5})", tail)) is not None:
            name, allowed = "report_section", {"cursor"}
            parameters["section"] = int(sub[1])
        elif (
            sub := re.fullmatch(r"/images/([A-Za-z0-9_-]{1,96}\.(?:png|jpg|jpeg))", tail)
        ) is not None:
            name, allowed = "report_image", {"offset"}
            parameters["image"] = sub[1]
        else:
            raise LocalError("unavailable")
    else:
        raise LocalError("unavailable")
    if set(query) - allowed:
        raise LocalError("invalid_request")
    for key, value in query.items():
        if key in {"limit", "offset"}:
            if not re.fullmatch(r"0|[1-9][0-9]{0,8}", value):
                raise LocalError("invalid_request")
            value = int(value)
            if (key == "limit" and not 1 <= value <= 50) or (
                key == "offset" and value > 8 * 1024 * 1024
            ):
                raise LocalError("invalid_request")
        elif key == "cursor" and not re.fullmatch(r"[A-Za-z0-9_.-]{1,4096}", value):
            raise LocalError("invalid_cursor")
        elif key == "q" and (len(value) > 256 or any(ord(c) < 32 for c in value)):
            raise LocalError("invalid_request")
        parameters[key] = value
    return name, project, parameters


def public_error(error: LocalError) -> tuple[str, int]:
    if error.code == "history_changed":
        return "context_changed", 409
    if error.code in {"unauthenticated", "context_changed", "invalid_cursor", "invalid_request"}:
        return error.code, {
            "unauthenticated": 401,
            "context_changed": 409,
            "invalid_cursor": 409,
            "invalid_request": 400,
        }[error.code]
    if error.code in {
        "service_busy",
        "service_unavailable",
        "release_expired",
        "clock_untrusted",
        "response_limit",
        "report_catalog_scan_limit",
        "recovery_required",
    }:
        return "service_unavailable", 503
    return "unavailable", 404


class DeadlineInput:
    def __init__(self, stream: Any, connection: socket.socket, deadline: float):
        self.stream, self.connection, self.deadline, self.bytes = stream, connection, deadline, 0

    def readline(self, size: int = -1) -> bytes:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        self.connection.settimeout(remaining)
        value: bytes = self.stream.readline(min(8193, size) if size >= 0 else 8193)
        self.bytes += len(value)
        if self.bytes > 16384 or len(value) > 8192:
            raise LocalError("invalid_request")
        return value

    def close(self) -> None:
        self.stream.close()


class Handler(BaseHTTPRequestHandler):
    server: ViewerServer
    protocol_version = "HTTP/1.1"
    server_version = "MemoryKing"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.deadline = self.server.request_context.deadline
        self.response_started = False
        self.rfile = DeadlineInput(self.rfile, self.connection, self.deadline)  # type: ignore[assignment]

    def handle(self) -> None:
        try:
            super().handle()
        except (TimeoutError, ConnectionError, OSError, LocalError):
            # Malformed/slow/closed connections get no interpreter traceback or private path.
            self.server.count("transport_rejected")

    def log_message(self, format: str, *args: Any) -> None:
        pass  # No URLs, identifiers, search text, cookies or callback queries in access logs.

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.reply(canonical_bytes({"schema_version": 1, "error": "invalid_request"}), 400)

    def reply(
        self,
        raw: bytes,
        status: int = 200,
        content_type: str = "application/json",
        released: Released | None = None,
    ) -> None:
        if time.monotonic() >= self.deadline and status < 400:
            raise TimeoutError
        if released is not None:
            released.decision.check(released.clock)
        self.connection.settimeout(
            max(0.1, self.deadline - time.monotonic()) if status < 400 else 0.5
        )
        self.close_connection = True
        self.response_started = True
        self.send_response(status)
        for key, value in HEADERS.items():
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        if status == 503:
            self.send_header("Retry-After", "1")
        self.end_headers()
        if self.command != "HEAD":
            if released is not None:
                released.decision.check(released.clock)
            self.wfile.write(raw)
        self.server.count(str(status))

    def handle_expect_100(self) -> bool:
        self.send_error(400)
        return False

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        try:
            if any("\n" in value or "\r" in value for _, value in self.headers.items()):
                raise LocalError("invalid_request")
            for header in (
                "Host",
                "Origin",
                "Cookie",
                "Content-Length",
                "Authorization",
                "Sec-Fetch-Site",
            ):
                if len(self.headers.get_all(header, [])) > 1:
                    raise LocalError("invalid_request")
            if self.headers.get("Host") != self.server.authority:
                raise LocalError("invalid_request")
            origin = self.headers.get("Origin")
            if origin is not None and origin != self.server.config["public_origin"]:
                raise LocalError("unavailable")
            if (
                self.headers.get("Transfer-Encoding") is not None
                or self.headers.get("Content-Length", "0") != "0"
                or self.headers.get("Authorization") is not None
            ):
                raise LocalError("invalid_request")
            name, project, params = route(self.path)
            if name in {"shell", "style"}:
                self.reply(
                    SHELL if name == "shell" else STYLE,
                    content_type="text/html; charset=utf-8" if name == "shell" else "text/css",
                )
                return
            if self.headers.get("Sec-Fetch-Site") not in {None, "same-origin", "none"}:
                raise LocalError("unavailable")
            cookie = self.headers.get("Cookie", "")
            if len(cookie) > 4096 or cookie.count("__Host-memoryking-viewer=") != 1:
                raise LocalError("unauthenticated")
            parsed = SimpleCookie()
            try:
                parsed.load(cookie)
            except CookieError as exc:
                raise LocalError("unauthenticated") from exc
            token = parsed.get("__Host-memoryking-viewer")
            if token is None or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token.value):
                raise LocalError("unauthenticated")
            session = self.server.resolve(token.value)
            with self.server.store(session, self.deadline) as store:
                released = ViewerReads(store, self.server.config).read(
                    session, name, project, params, deadline=self.deadline
                )
            self.reply(released.body, released=released)
        except LocalError as exc:
            if self.response_started:
                self.close_connection = True
                return
            error, status = public_error(exc)
            self.reply(canonical_bytes({"schema_version": 1, "error": error}), status)


def deny_session(token: str) -> ViewerSession:
    raise LocalError("unauthenticated")


class ViewerServer(HTTPServer):
    request_queue_size = 16
    allow_reuse_address = True

    def __init__(
        self,
        config: dict[str, Any],
        store: Callable[[ViewerSession, float], AbstractContextManager[LocalStore]],
        *,
        resolve: Callable[[str], ViewerSession] = deny_session,
    ):
        self.config = validate_config(config)
        if not config["enabled"]:
            raise LocalError("viewer_disabled")
        self.authority = urlsplit(config["public_origin"]).netloc
        self.store, self.resolve = store, resolve
        self.slots = threading.BoundedSemaphore(config["limits"]["concurrency"])
        self.guard = threading.Lock()
        self.request_context = threading.local()
        self.counts: Counter[str] = Counter()
        self.threads: set[threading.Thread] = set()
        self.tls: ssl.SSLContext | None = None
        if config["transport"] == "direct_tls":
            from pathlib import Path

            private_file(Path(config["tls_key_file"]))
            self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.tls.minimum_version = ssl.TLSVersion.TLSv1_2
            self.tls.load_cert_chain(config["tls_cert_file"], config["tls_key_file"])
        self.address_family = socket.AF_INET6 if config["listen_host"] == "::1" else socket.AF_INET
        super().__init__((config["listen_host"], config["listen_port"]), Handler)

    def count(self, name: str) -> None:
        with self.guard:
            self.counts[name] += 1

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            self.count("admission_rejected")
            self.shutdown_request(request)
            return

        def work() -> None:
            connection = request
            self.request_context.deadline = (
                time.monotonic() + self.config["limits"]["request_seconds"]
            )
            try:
                connection.settimeout(self.config["limits"]["request_seconds"])
                if self.tls is not None:
                    connection = self.tls.wrap_socket(connection, server_side=True)
                self.finish_request(connection, client_address)
            except (OSError, TimeoutError):
                self.count("transport_rejected")
            except Exception:
                # Unexpected internal errors close safely; only a fixed aggregate is observed.
                self.count("internal_error")
            finally:
                self.shutdown_request(connection)
                self.slots.release()
                with self.guard:
                    self.threads.discard(threading.current_thread())

        thread = threading.Thread(target=work, name="memoryking-viewer-request", daemon=True)
        with self.guard:
            self.threads.add(thread)
        thread.start()

    def server_close(self) -> None:
        super().server_close()
        with self.guard:
            threads = list(self.threads)
        for thread in threads:
            thread.join(timeout=self.config["limits"]["request_seconds"] + 1)


class ViewerListener:
    """Explicit optional lifecycle owned by the one serving process."""

    def __init__(
        self,
        config: dict[str, Any],
        store: Callable[[ViewerSession, float], AbstractContextManager[LocalStore]],
    ):
        self.config, self.store = validate_config(config), store
        self.server: ViewerServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.config["enabled"] and self.server is None:
            self.server = ViewerServer(self.config, self.store)
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="memoryking-viewer",
                daemon=True,
            )
            self.thread.start()

    def stop(self) -> None:
        if self.server is not None:
            if self.thread is not None:
                self.server.shutdown()
            self.server.server_close()
            if self.thread is not None:
                self.thread.join(timeout=2)
