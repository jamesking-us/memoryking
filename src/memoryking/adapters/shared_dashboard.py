"""Owner browser sessions and authorized bounded snapshot/SSE for one configured tenant."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from memoryking.adapters.dashboard import ASSETS, SAFE_HEADERS, STATIC, PlanFiles
from memoryking.adapters.execution import Execution
from memoryking.adapters.local_store import identifier, uid
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.shared_service import SharedService
from memoryking.contracts.canonical import canonical_bytes, canonical_hash, load_strict
from memoryking.domain.http_status import http_status
from memoryking.domain.local import LocalError, Released
from memoryking.observability import failure


def stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: stable(v)
            for k, v in value.items()
            if k not in {"observed_ms", "fresh_until_ms", "csrf", "cursor"}
        }
    if isinstance(value, list):
        return [stable(item) for item in value]
    return value


class CursorHistory:
    """Bounded revision metadata only; never retains source content between authorizations."""

    def __init__(self) -> None:
        self.generation = secrets.token_hex(16)
        self.sequence = 0
        self.lock = threading.Lock()
        self.entries: OrderedDict[str, deque[tuple[int, str]]] = OrderedDict()

    def current(
        self, context: str, digest: str, previous: dict[str, Any] | None
    ) -> tuple[int, bool]:
        with self.lock:
            rows = self.entries.setdefault(context, deque(maxlen=64))
            self.entries.move_to_end(context)
            if not rows or rows[-1][1] != digest:
                # A context evicted from the LRU must never reuse a former cursor position.
                self.sequence += 1
                rows.append((self.sequence, digest))
            while len(self.entries) > 64:
                self.entries.popitem(last=False)
            sequence = rows[-1][0]
            gap = previous is not None and (
                previous.get("generation") != self.generation
                or previous.get("sequence") not in {row[0] for row in rows}
            )
            return sequence, gap


class SharedDashboard:
    def __init__(
        self, service: SharedService, root: Path, tenant: str, origins: list[str], *, secure: bool
    ):
        if not origins:
            raise LocalError("dashboard_origin_required")
        self.service, self.files, self.tenant = service, PlanFiles(root), identifier(tenant)
        self.origins, self.secure = set(origins), secure
        self.lock = threading.RLock()
        self.codes: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.history = CursorHistory()
        self.streams = threading.BoundedSemaphore(8)

    def issue(self, tenant: str, credential: str) -> dict[str, Any]:
        if tenant != self.tenant:
            raise LocalError("dashboard_unavailable")
        with self.service.store(tenant, credential, release_only=True) as store:
            with store.transaction() as decision:
                who = store._auth(credential, "admin", "*", decision.evaluated_ms)
                epoch = store.db.execute("SELECT policy_epoch FROM tenants").fetchone()[0]
                code = secrets.token_urlsafe(32)
                value = {
                    "credential": credential,
                    "tenant": tenant,
                    "principal": who["principal"],
                    "incarnation": store.incarnation,
                    "epoch": epoch,
                    "expires": decision.evaluated_ms + 60000,
                }
            store._expose()
        with self.lock:
            self.codes = {
                k: v for k, v in self.codes.items() if v["expires"] > decision.evaluated_ms
            }
            if len(self.codes) >= 32:
                raise LocalError("bootstrap_limit")
            self.codes[hashlib.sha256(code.encode()).hexdigest()] = value
        return {"code": code, "url": sorted(self.origins)[0], "expires_ms": value["expires"]}

    def session(self, request: Request) -> str:
        if len(request.headers.getlist("cookie")) != 1:
            raise LocalError("session_required")
        session = request.cookies.get("mk_session")
        if not session:
            raise LocalError("session_required")
        return session

    def current(self, session: str) -> dict[str, Any]:
        with self.lock:
            value = self.sessions.get(session)
            if value is None:
                raise LocalError("session_expired")
            return dict(value)

    def read(self, session: str, route: str, previous: str | None = None) -> Released:
        value = self.current(session)
        # Plan/report hashing is independent of the database. Prepare bounded bytes before
        # taking the authorization gate; only the freshly authorized result can leave it.
        prepared = self.files.snapshot(0) if route == "/api/snapshot" else None
        with self.service.store(value["tenant"], value["credential"], release_only=True) as store:
            with store.transaction() as decision:
                at = decision.evaluated_ms
                who = store._auth(value["credential"], "admin", "*", at)
                epoch = store.db.execute("SELECT policy_epoch,purge_epoch FROM tenants").fetchone()
                if (
                    value["expires"] <= at
                    or value["epoch"] != epoch[0]
                    or value["incarnation"] != store.incarnation
                ):
                    with self.lock:
                        self.sessions.pop(session, None)
                    raise LocalError("session_changed")
                if route == "/api/snapshot":
                    assert prepared is not None
                    result = prepared
                    result.update(
                        observed_ms=at,
                        fresh_until_ms=at + 15000,
                        csrf=value["csrf"],
                        incarnation=store.incarnation,
                        tenant=value["tenant"],
                        principal=who["principal"],
                        authorization_epoch=epoch[0],
                        capacity=store._capacity(value["tenant"]),
                        streaming=True,
                        adapter="shared-plan-files / authorized read only",
                    )
                    binding = canonical_hash(
                        {
                            "session": session,
                            "tenant": value["tenant"],
                            "principal": who["principal"],
                            "incarnation": store.incarnation,
                            "epoch": epoch[0],
                            "route": route,
                        }
                    )
                    old = None
                    if previous:
                        old = store._decode(previous, "dashboard_cursor")
                        if old.get("binding") != binding:
                            raise LocalError("cursor_context_changed")
                    sequence, gap = self.history.current(
                        binding, canonical_hash(stable(result)), old
                    )
                    result["cursor"] = store._token(
                        "dashboard_cursor",
                        binding=binding,
                        generation=self.history.generation,
                        sequence=sequence,
                        expires=at + 900000,
                    )
                    result["stream_sequence"] = sequence
                    result["resync_required"] = gap or (
                        old is not None and old.get("expires", 0) <= at
                    )
                    raw = canonical_bytes(result)
                elif route.startswith("/api/timeline/"):
                    from memoryking.adapters.timeline import timeline

                    parts = route.split("/")
                    if len(parts) not in {4, 5} or (len(parts) == 5 and not parts[4].isdigit()):
                        raise LocalError("invalid_request")
                    project = identifier(parts[3])
                    reader = store._auth(value["credential"], "read", project, at)
                    raw = canonical_bytes(
                        timeline(
                            store,
                            reader,
                            project,
                            before=int(parts[4]) if len(parts) == 5 else None,
                        )
                    )
                elif route == "/api/projects":
                    from memoryking.adapters.projects import visible_projects

                    raw = canonical_bytes(
                        {"items": visible_projects(store, value["credential"], at)}
                    )
                elif route.startswith("/api/execution/"):
                    project = identifier(route.removeprefix("/api/execution/"))
                    store._auth(value["credential"], "read", project, at)
                    raw = canonical_bytes(Execution(store).snapshot(who, project, at))
                elif route.startswith(("/api/receipts/", "/api/receipt/")):
                    parts = route.split("/")
                    if len(parts) not in {4, 5}:
                        raise LocalError("invalid_request")
                    project = identifier(parts[3])
                    store._auth(value["credential"], "read", project, at)
                    retrieval = Retrieval(store)
                    if parts[2] == "receipts" and len(parts) == 4:
                        raw = canonical_bytes({"items": retrieval.list_receipts(who, project, at)})
                    elif parts[2] == "receipt" and len(parts) == 5:
                        row = retrieval._artifact(who, project, identifier(parts[4]), at)
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
                elif route.startswith("/api/citations/"):
                    from urllib.parse import unquote

                    parts = route.split("/")
                    if len(parts) != 5:
                        raise LocalError("invalid_request")
                    project = identifier(parts[3])
                    store._auth(value["credential"], "read", project, at)
                    raw = canonical_bytes(
                        {
                            "node": unquote(parts[4]),
                            "items": Retrieval(store).list_citations(
                                who, project, unquote(parts[4]), at
                            ),
                        }
                    )
                elif route.startswith("/api/artifact/"):
                    artifact = self.files.artifacts().get(route.removeprefix("/api/artifact/"))
                    if artifact is None:
                        raise LocalError("unavailable")
                    path, _ = artifact
                    raw = self.files.read(path, 4194304)
                else:
                    raise LocalError("route_unavailable")
                receipt = uid("rcpt")
                store.db.execute(
                    "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        value["tenant"],
                        receipt,
                        who["principal"],
                        store.incarnation,
                        at,
                        decision.deadline_ms,
                        *epoch,
                        hashlib.sha256(raw).hexdigest(),
                        "[]",
                    ),
                )
            store._expose()
            packet = Released(raw, decision, store.clock, receipt)
            return packet

    def browser_guard(self, request: Request) -> None:
        if len(request.headers.getlist("origin")) > 1:
            raise LocalError("origin_rejected")
        origin = request.headers.get("origin")
        if origin is not None and origin not in self.origins:
            raise LocalError("origin_rejected")
        public_asset = request.method in {"GET", "HEAD"} and request.url.path in ASSETS
        if not public_asset and request.headers.get("sec-fetch-site") not in {
            None,
            "none",
            "same-origin",
        }:
            raise LocalError("origin_rejected")
        if request.method not in {"GET", "HEAD"} and origin not in self.origins:
            raise LocalError("origin_required")

    def exchange(self, code: str, previous_session: str) -> str:
        with self.lock:
            value = self.codes.pop(hashlib.sha256(code.encode()).hexdigest(), None)
        if value is None:
            raise LocalError("bootstrap_invalid")
        with self.service.store(value["tenant"], value["credential"], release_only=True) as store:
            with store.transaction() as decision:
                store._auth(value["credential"], "admin", "*", decision.evaluated_ms)
                epoch = store.db.execute("SELECT policy_epoch FROM tenants").fetchone()[0]
                if (
                    value["expires"] <= decision.evaluated_ms
                    or value["epoch"] != epoch
                    or value["incarnation"] != store.incarnation
                ):
                    raise LocalError("bootstrap_invalid")
            store._expose()
            decision.check(store.clock)
        session = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions = {
                k: v for k, v in self.sessions.items() if v["expires"] > decision.evaluated_ms
            }
            if len(self.sessions) >= 16:
                raise LocalError("session_limit")
            self.sessions.pop(previous_session, None)
            self.sessions[session] = {
                **value,
                "expires": decision.evaluated_ms + 900000,
                "csrf": secrets.token_urlsafe(32),
            }
        return session

    def command(self, session: str, body: dict[str, Any]) -> Released:
        from memoryking.adapters.browser_commands import execute

        value = self.current(session)
        # The domain mutation owns the exclusive tenant gate. Session validation
        # runs inside that gate at the persisted authorization evaluation instant.
        with self.service.store(value["tenant"], value["credential"]) as store:
            return execute(store, value, body, lambda: self.current(session))

    async def route(self, request: Request) -> Response:
        from memoryking.adapters.shared_http import ReleaseResponse, error

        try:
            self.browser_guard(request)
            route = request.url.path
            if request.method in {"GET", "HEAD"} and route in ASSETS:
                name, kind = ASSETS[route]
                content = (STATIC / name).read_bytes() if request.method == "GET" else b""
                if name == "index.html":
                    content = content.replace(
                        b"python -m memoryking dashboard-login --help",
                        b"python -m memoryking.shared request --help",
                    )
                return Response(
                    content,
                    media_type=kind,
                    headers=SAFE_HEADERS,
                )
            if request.method == "POST" and route in {"/api/bootstrap", "/api/logout"}:
                if request.headers.get("content-type") != "application/json":
                    raise LocalError("invalid_request")
                raw = await request.body()
                if len(raw) > 4096:
                    raise LocalError("invalid_request")
                body = load_strict(raw)
                if not isinstance(body, dict):
                    raise LocalError("invalid_request")
                if route == "/api/bootstrap":
                    if set(body) != {"code"} or not isinstance(body["code"], str):
                        raise LocalError("invalid_request")
                    session = await anyio.to_thread.run_sync(
                        self.exchange, body["code"], request.cookies.get("mk_session", "")
                    )
                    response = Response(
                        b'{"ok":true}', media_type="application/json", headers=SAFE_HEADERS
                    )
                    response.set_cookie(
                        "mk_session",
                        session,
                        httponly=True,
                        secure=self.secure,
                        samesite="strict",
                        max_age=900,
                    )
                    return response
                session = self.session(request)
                value = self.current(session)
                if (
                    set(body) != {"csrf"}
                    or not isinstance(body["csrf"], str)
                    or not secrets.compare_digest(body["csrf"], value["csrf"])
                ):
                    raise LocalError("csrf_rejected")
                # Fresh authorization precedes session mutation, including logout.
                await anyio.to_thread.run_sync(self.read, session, "/api/snapshot")
                with self.lock:
                    self.sessions.pop(session, None)
                response = Response(
                    b'{"ok":true}', media_type="application/json", headers=SAFE_HEADERS
                )
                response.delete_cookie(
                    "mk_session", httponly=True, secure=self.secure, samesite="strict"
                )
                return response
            session = self.session(request)
            if route == "/api/command" and request.method == "POST":
                if request.headers.getlist("authorization"):
                    raise LocalError("mixed_authentication")
                if request.headers.getlist("content-type") != ["application/json"]:
                    raise LocalError("invalid_request")
                raw = await request.body()
                if len(raw) > 131072:
                    raise LocalError("invalid_request")
                body = load_strict(raw)
                if not isinstance(body, dict):
                    raise LocalError("invalid_request")
                result = await anyio.to_thread.run_sync(self.command, session, body)
                response = ReleaseResponse(result)
                response.headers.update(SAFE_HEADERS)
                return response
            if route == "/api/events" and request.method == "GET":
                return await self.events(session, request.headers.get("last-event-id"))
            if request.method not in {"GET", "HEAD"}:
                raise LocalError("route_unavailable")
            result = await anyio.to_thread.run_sync(self.read, session, route)
            response = ReleaseResponse(result)
            response.headers.update(SAFE_HEADERS)
            if route.startswith("/api/artifact/"):
                entry = self.files.artifacts().get(route.removeprefix("/api/artifact/"))
                response.headers["content-type"] = entry[1] if entry else "application/octet-stream"
            if request.method == "HEAD":
                response.body = b""
                response.headers["content-length"] = "0"
            return response
        except LocalError as exc:
            return error(exc.code, http_status(exc.code, 403))
        except Exception as exc:
            return error("dashboard_unavailable", 503, failure("shared_dashboard", exc))

    async def events(self, session: str, cursor: str | None) -> Response:
        from memoryking.adapters.shared_http import error

        if not self.streams.acquire(blocking=False):
            return error("stream_limit", 503)
        try:
            first = await anyio.to_thread.run_sync(self.read, session, "/api/snapshot", cursor)
        except BaseException:
            self.streams.release()
            raise

        releasing: Released | None = first

        async def chunks() -> Any:
            nonlocal cursor, releasing
            packet = first
            last_sequence = None
            try:
                for index in range(15):
                    if index:
                        await anyio.sleep(2)
                        packet = await anyio.to_thread.run_sync(
                            self.read, session, "/api/snapshot", cursor
                        )
                    packet.decision.check(packet.clock)
                    result = json.loads(packet.body)
                    sequence = result["stream_sequence"]
                    event = (
                        "resync"
                        if result["resync_required"]
                        else "heartbeat"
                        if sequence == last_sequence
                        else "snapshot"
                    )
                    cursor, last_sequence = result["cursor"], sequence
                    payload = (
                        packet.body
                        if event != "heartbeat"
                        else canonical_bytes(
                            {"cursor": cursor, "fresh_until_ms": result["fresh_until_ms"]}
                        )
                    )
                    packet.decision.check(packet.clock)
                    releasing = packet
                    yield (
                        b"id: "
                        + cursor.encode()
                        + b"\nevent: "
                        + event.encode()
                        + b"\ndata: "
                        + payload
                        + b"\n\n"
                    )
            except Exception:
                releasing = None
                yield b'event: context-reset\ndata: {"reason":"reauthorization_required"}\n\n'
            finally:
                self.streams.release()

        class AuthorizedStream(StreamingResponse):
            async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
                async def guarded(message: Any) -> None:
                    if message["type"] == "http.response.body" and message.get("body"):
                        packet = releasing
                        if packet is not None:
                            packet.decision.check(packet.clock)
                            remaining = (
                                min(
                                    packet.decision.deadline_ms - packet.clock.utc_ms(),
                                    5000
                                    - (packet.clock.monotonic_ms() - packet.decision.monotonic_ms),
                                )
                                / 1000
                            )
                        else:
                            remaining = 5.0
                        # Bound transport backpressure; this does not revoke bytes already sent.
                        with anyio.fail_after(max(0, remaining)):
                            await send(message)
                    else:
                        await send(message)

                await super().__call__(scope, receive, guarded)

        return AuthorizedStream(
            chunks(),
            media_type="text/event-stream",
            headers={**SAFE_HEADERS, "X-Accel-Buffering": "no"},
        )

    def routes(self) -> list[Route]:
        return [
            Route(path, self.route, methods=methods)
            for path, methods in [
                *((path, ["GET", "HEAD"]) for path in ASSETS),
                ("/api/bootstrap", ["POST"]),
                ("/api/logout", ["POST"]),
                ("/api/command", ["POST"]),
                ("/api/snapshot", ["GET", "HEAD"]),
                ("/api/events", ["GET"]),
                ("/api/execution/{project}", ["GET", "HEAD"]),
                ("/api/receipts/{project}", ["GET", "HEAD"]),
                ("/api/receipt/{project}/{receipt}", ["GET", "HEAD"]),
                ("/api/citations/{project}/{node}", ["GET", "HEAD"]),
                ("/api/artifact/{artifact}", ["GET", "HEAD"]),
            ]
        ]
