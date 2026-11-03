"""Opt-in owner dashboard; inspection and explicit domain review, never a scheduler."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from memoryking.adapters.local_store import uid
from memoryking.application.store_core import StoreCore
from memoryking.contracts.canonical import canonical_bytes, load_strict
from memoryking.domain.hints import hint
from memoryking.domain.http_status import http_status
from memoryking.domain.local import LocalError, Released
from memoryking.observability import failure, summarize, timings, trace, tracing

STATIC = Path(__file__).with_name("web")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css"),
    "/elephant.png": ("elephant.png", "image/png"),
    **{
        f"/{name}.js": (f"{name}.js", "text/javascript")
        for name in ("app", "observatory", "graph", "graph-layout", "graph-worker", "brand", "team")
    },
    **{
        f"/{name}.svg": (f"{name}.svg", "image/svg+xml")
        for name in ("crown", "favicon", "wordmark", "crown-mono")
    },
}
SAFE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; worker-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def workspace_snapshot(at: int) -> dict[str, Any]:
    """The session's view of the store itself; every project view is loaded on demand."""
    return {"schema_version": 1, "observed_ms": at, "fresh_until_ms": at + 15000}


class Dashboard(ThreadingHTTPServer):
    request_queue_size = 8
    daemon_threads = True

    def __init__(self, store: StoreCore, port: int = 0):
        self.store = store
        self.codes: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        super().__init__(("127.0.0.1", port), DashboardHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.host = f"127.0.0.1:{self.server_port}"
        self.thread: threading.Thread | None = None

    def issue(self, credential: str) -> dict[str, Any]:
        with self.store.transaction() as decision:
            who = self.store._auth(credential, "admin", "*", decision.evaluated_ms)
            self.codes = {
                k: v for k, v in self.codes.items() if v["expires"] > decision.evaluated_ms
            }
            if len(self.codes) >= 32:
                raise LocalError("bootstrap_limit")
            code = secrets.token_urlsafe(32)
            self.codes[hashlib.sha256(code.encode()).hexdigest()] = {
                "credential": credential,
                "tenant": who["tenant"],
                "expires": decision.evaluated_ms + 60000,
                "epoch": self.epoch(who["tenant"]),
            }
        self.store._expose()
        return {"url": self.origin, "code": code, "expires_ms": decision.evaluated_ms + 60000}

    def epoch(self, tenant: str) -> int:
        return int(
            self.store.db.execute(
                "SELECT policy_epoch FROM tenants WHERE tenant=?", (tenant,)
            ).fetchone()[0]
        )

    def current(self, session: str, at: int) -> dict[str, Any]:
        value = self.sessions.get(session)
        if value is None or value["expires"] <= at:
            raise LocalError("session_expired")
        who = self.store._auth(value["credential"], "admin", "*", at)
        if (
            value["epoch"] != self.epoch(who["tenant"])
            or value["incarnation"] != self.store.incarnation
        ):
            self.sessions.pop(session, None)
            raise LocalError("session_changed")
        return value

    def start(self) -> None:
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.thread:
            self.shutdown()
            self.thread.join(timeout=5)
        self.server_close()


class DashboardHandler(BaseHTTPRequestHandler):
    server: Dashboard
    server_version = "MemoryKing"

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Never log bootstrap codes, cookies, paths or source content.

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def send(
        self,
        status: int,
        raw: bytes,
        content_type: str = "application/json",
        cookie: str | None = None,
        access_receipt: str | None = None,
    ) -> None:
        self.trace_status = status
        self.send_response(status)
        for name, value in SAFE_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if access_receipt:
            self.send_header("X-MemoryKing-Access-Receipt", access_receipt)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def guard(self) -> None:
        if self.headers.get_all("Host", []) != [self.server.host]:
            raise LocalError("host_rejected")
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [self.server.origin]:
            raise LocalError("origin_rejected")
        public_asset = self.command in {"GET", "HEAD"} and self.path in ASSETS
        if not public_asset and self.headers.get("Sec-Fetch-Site") not in {
            None,
            "same-origin",
            "none",
        }:
            raise LocalError("origin_rejected")
        agent_endpoint = self.path == "/api/v1/retrieval" and self.command == "POST"
        if (
            self.command not in {"GET", "HEAD"}
            and not agent_endpoint
            and origins != [self.server.origin]
        ):
            raise LocalError("origin_required")

    def session(self) -> str:
        if len(self.headers.get_all("Cookie", [])) != 1:
            raise LocalError("session_required")
        cookie = SimpleCookie()
        cookie.load(self.headers["Cookie"])
        if "mk_session" not in cookie:
            raise LocalError("session_required")
        return cookie["mk_session"].value

    def body(self, limit: int = 4096) -> dict[str, Any]:
        if self.headers.get_all("Content-Type", []) != ["application/json"] or self.headers.get(
            "Transfer-Encoding"
        ):
            raise LocalError("invalid_request")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit() or not 1 <= int(lengths[0]) <= limit:
            raise LocalError("invalid_request")
        value = load_strict(self.rfile.read(int(lengths[0])))
        if not isinstance(value, dict):
            raise LocalError("invalid_request")
        return value

    def route(self) -> None:
        self.guard()
        dashboard, store = self.server, self.server.store
        if self.path == "/metrics" and self.command == "GET":
            from memoryking.observability_metrics import render

            authorization = self.headers.get_all("Authorization", [])
            if len(authorization) != 1 or not authorization[0].startswith("Bearer "):
                raise LocalError("bearer_required")
            if self.headers.get("Cookie"):
                raise LocalError("mixed_authentication")
            status = store.status(authorization[0][7:], "metrics")
            self.send(200, render(store.counters, status).encode(), "text/plain; version=0.0.4")
            return
        if self.path == "/api/v1/retrieval" and self.command == "POST":
            from memoryking.adapters.retrieval import Retrieval

            authorization = self.headers.get_all("Authorization", [])
            if len(authorization) != 1 or not authorization[0].startswith("Bearer "):
                raise LocalError("bearer_required")
            # Cookies never authorize agent endpoints; reject mixed contexts.
            if self.headers.get_all("Cookie", []):
                raise LocalError("mixed_authentication")
            response = Retrieval(store).execute(authorization[0][7:], self.body(131072))
            response.emit(lambda b: self.send(200, b, access_receipt=response.access_receipt))
            return
        if self.command in {"GET", "HEAD"} and self.path in ASSETS:
            name, kind = ASSETS[self.path]
            self.send(200, (STATIC / name).read_bytes(), kind)
            return
        if self.command == "OPTIONS":
            if self.headers.get("Access-Control-Request-Method") not in {
                "GET",
                "HEAD",
                "POST",
            } or self.headers.get("Access-Control-Request-Headers", "").lower() not in {
                "",
                "content-type",
            }:
                raise LocalError("preflight_rejected")
            self.send(204, b"")
            return
        if self.command == "POST" and self.path == "/api/bootstrap":
            data = self.body()
            if set(data) != {"code"} or not isinstance(data["code"], str):
                raise LocalError("invalid_request")
            with store.transaction() as decision:
                code = dashboard.codes.pop(hashlib.sha256(data["code"].encode()).hexdigest(), None)
                if code is None or code["expires"] <= decision.evaluated_ms:
                    raise LocalError("bootstrap_invalid")
                who = store._auth(code["credential"], "admin", "*", decision.evaluated_ms)
                if code["epoch"] != dashboard.epoch(who["tenant"]):
                    raise LocalError("bootstrap_invalid")
                dashboard.sessions = {
                    k: v
                    for k, v in dashboard.sessions.items()
                    if v["expires"] > decision.evaluated_ms
                }
                if len(dashboard.sessions) >= 16:
                    raise LocalError("session_limit")
                session = secrets.token_urlsafe(32)
                dashboard.sessions[session] = {
                    **code,
                    "expires": decision.evaluated_ms + 900000,
                    "csrf": secrets.token_urlsafe(32),
                    "incarnation": store.incarnation,
                }
                raw = canonical_bytes({"ok": True})
            store._expose()
            Released(raw, decision, store.clock).emit(
                lambda b: self.send(
                    200,
                    b,
                    cookie=f"mk_session={session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=900",
                )
            )
            return
        session = self.session()
        if self.command == "POST" and self.path == "/api/admin/revoke":
            from memoryking.adapters.local_store import identifier

            data = self.body()
            # The same expiry, policy-epoch and incarnation checks as every other route:
            # a session made stale by an earlier revoke cannot keep revoking.
            with store.transaction() as decision:
                value = dashboard.current(session, decision.evaluated_ms)
            if (
                set(data) != {"csrf", "principal", "project"}
                or not isinstance(data["csrf"], str)
                or not secrets.compare_digest(data["csrf"], value["csrf"])
            ):
                raise LocalError("csrf_rejected")
            principal, project = identifier(data["principal"]), identifier(data["project"])
            # A policy change advances the epoch, so every browser session, including this
            # one, must sign in again afterwards; the reply says so.
            store.revoke(value["credential"], principal, project)
            self.send(
                200,
                canonical_bytes(
                    {"revoked": principal, "project": project, "sign_in_required": True}
                ),
            )
            return
        if self.command == "POST" and self.path == "/api/command":
            from memoryking.adapters.browser_commands import execute

            if self.headers.get_all("Authorization", []):
                raise LocalError("mixed_authentication")

            def current_session() -> dict[str, Any]:
                value = dashboard.sessions.get(session)
                if value is None:
                    raise LocalError("session_expired")
                return value

            response = execute(store, current_session(), self.body(131072), current_session)
            response.emit(lambda b: self.send(200, b, access_receipt=response.access_receipt))
            return
        with store.transaction() as decision:
            value = dashboard.current(session, decision.evaluated_ms)
            cookie = None
            content_type = "application/json"
            if self.command == "POST" and self.path == "/api/logout":
                data = self.body()
                if (
                    set(data) != {"csrf"}
                    or not isinstance(data["csrf"], str)
                    or not secrets.compare_digest(data["csrf"], value["csrf"])
                ):
                    raise LocalError("csrf_rejected")
                dashboard.sessions.pop(session, None)
                raw = b'{"ok":true}'
                cookie = "mk_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
            elif self.command in {"GET", "HEAD"} and self.path == "/api/snapshot":
                result = workspace_snapshot(decision.evaluated_ms)
                result.update(
                    csrf=value["csrf"],
                    incarnation=store.incarnation,
                    principal=store._decode(value["credential"], "credential")["principal"],
                    tenant=value["tenant"],
                    authorization_epoch=dashboard.epoch(value["tenant"]),
                    capacity=store._capacity(value["tenant"]),
                )
                raw = canonical_bytes(result)
            elif self.command in {"GET", "HEAD"} and self.path.startswith("/api/timeline/"):
                from memoryking.adapters.local_store import identifier
                from memoryking.adapters.timeline import timeline

                parts = self.path.split("/")
                if len(parts) not in {4, 5}:
                    raise LocalError("invalid_request")
                project = identifier(parts[3])
                before = None
                if len(parts) == 5:
                    if not parts[4].isdigit():
                        raise LocalError("invalid_request")
                    before = int(parts[4])
                who = store._auth(value["credential"], "read", project, decision.evaluated_ms)
                raw = canonical_bytes(timeline(store, who, project, before=before))
            elif self.command in {"GET", "HEAD"} and self.path == "/api/projects":
                from memoryking.adapters.projects import visible_projects

                raw = canonical_bytes(
                    {"items": visible_projects(store, value["credential"], decision.evaluated_ms)}
                )
            elif self.command in {"GET", "HEAD"} and self.path.startswith("/api/plans/"):
                from memoryking.adapters.plan_dashboard import read as read_plans

                raw = canonical_bytes(
                    read_plans(store, value["credential"], self.path, decision.evaluated_ms)
                )
            elif self.command in {"GET", "HEAD"} and self.path.startswith("/api/execution/"):
                from memoryking.adapters.execution import Execution
                from memoryking.adapters.local_store import identifier

                project = self.path.removeprefix("/api/execution/")
                identifier(project)
                who = store._auth(value["credential"], "read", project, decision.evaluated_ms)
                raw = canonical_bytes(
                    Execution(store).snapshot(who, project, decision.evaluated_ms)
                )
            elif self.command in {"GET", "HEAD"} and self.path.startswith(
                ("/api/receipts/", "/api/receipt/")
            ):
                from memoryking.adapters.local_store import identifier
                from memoryking.adapters.retrieval import Retrieval

                parts = self.path.split("/")
                if len(parts) not in {4, 5}:
                    raise LocalError("invalid_request")
                project = parts[3]
                identifier(project)
                who = store._auth(value["credential"], "read", project, decision.evaluated_ms)
                retrieval = Retrieval(store)
                if parts[2] == "receipts" and len(parts) == 4:
                    raw = canonical_bytes(
                        {"items": retrieval.list_receipts(who, project, decision.evaluated_ms)}
                    )
                elif parts[2] == "receipt" and len(parts) == 5:
                    identifier(parts[4])
                    row = retrieval._artifact(who, project, parts[4], decision.evaluated_ms)
                    raw = canonical_bytes(
                        {
                            "receipt": row["id"],
                            "rendered_hash": row["body_hash"],
                            "expires_ms": row["expires"],
                            "diagnostics": json.loads(row["diagnostics"]),
                        }
                    )
                else:
                    raise LocalError("route_unavailable")
            elif self.command in {"GET", "HEAD"} and self.path.startswith("/api/activity/"):
                from memoryking.adapters.local_store import identifier
                from memoryking.adapters.team import activity

                parts = self.path.split("/")
                if len(parts) not in {4, 5} or (len(parts) == 5 and not parts[4].isdigit()):
                    raise LocalError("invalid_request")
                project = identifier(parts[3])
                who = store._auth(value["credential"], "read", project, decision.evaluated_ms)
                raw = canonical_bytes(
                    activity(
                        store,
                        who,
                        project,
                        decision.evaluated_ms,
                        days=int(parts[4]) if len(parts) == 5 else 30,
                    )
                )
            elif self.command in {"GET", "HEAD"} and self.path.startswith("/api/export/"):
                from memoryking.adapters.local_store import identifier
                from memoryking.adapters.team import export

                project = identifier(self.path.removeprefix("/api/export/"))
                who = store._auth(value["credential"], "admin", "*", decision.evaluated_ms)
                store._auth(value["credential"], "read", project, decision.evaluated_ms)
                raw = canonical_bytes(export(store, who, project, decision.evaluated_ms))
            elif self.command in {"GET", "HEAD"} and self.path.startswith("/api/citations/"):
                from urllib.parse import unquote

                from memoryking.adapters.local_store import identifier
                from memoryking.adapters.retrieval import Retrieval

                parts = self.path.split("/")
                if len(parts) != 5:
                    raise LocalError("invalid_request")
                project = identifier(parts[3])
                who = store._auth(value["credential"], "read", project, decision.evaluated_ms)
                raw = canonical_bytes(
                    {
                        "node": unquote(parts[4]),
                        "items": Retrieval(store).list_citations(
                            who, project, unquote(parts[4]), decision.evaluated_ms
                        ),
                    }
                )
            else:
                raise LocalError("route_unavailable")
            who = store._decode(value["credential"], "credential")
            epochs = store.db.execute(
                "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
            ).fetchone()
            store.db.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    uid("rcpt"),
                    who["principal"],
                    store.incarnation,
                    decision.evaluated_ms,
                    decision.deadline_ms,
                    *epochs,
                    hashlib.sha256(raw).hexdigest(),
                    "[]",
                ),
            )
            store.fault("after_validation")
        store._expose()
        Released(raw, decision, store.clock).emit(lambda b: self.send(200, b, content_type, cookie))

    def do_GET(self) -> None:
        if not tracing():
            self._serve()
            return
        started = time.monotonic()
        self.trace_status = 0
        spans = timings(self.server.store)
        try:
            self._serve()
        finally:
            self.server.store.timings.items = None
            trace(
                "dashboard",
                method=self.command,
                route="/".join(self.path.split("?")[0].split("/")[:3]),
                status=self.trace_status,
                **summarize((time.monotonic() - started) * 1000, spans),
            )

    def _serve(self) -> None:
        try:
            self.route()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except LocalError as exc:
            self.send(
                http_status(exc.code, 403),
                canonical_bytes({"error": exc.code, "hint": hint(exc.code)}),
            )
        except Exception as exc:
            request_id = failure("dashboard", exc)
            self.send(
                503, canonical_bytes({"error": "dashboard_unavailable", "request_id": request_id})
            )

    do_HEAD = do_GET
    do_POST = do_GET
    do_OPTIONS = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET
    do_PATCH = do_GET
