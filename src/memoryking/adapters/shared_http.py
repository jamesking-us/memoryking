"""Bounded bearer-only HTTP surface. Cookie sessions never authorize agent requests."""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from memoryking.adapters.shared_service import SharedService
from memoryking.contracts.canonical import ContractError, canonical_bytes, load_strict
from memoryking.domain.hints import hint
from memoryking.domain.http_status import http_status
from memoryking.domain.local import LocalError, Released
from memoryking.observability import failure


class ReleaseResponse(Response):
    def __init__(self, result: dict[str, Any] | Released):
        self.released = result if isinstance(result, Released) else None
        super().__init__(
            result.body if isinstance(result, Released) else canonical_bytes(result),
            media_type="application/json",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
        if isinstance(result, Released) and result.access_receipt:
            self.headers["X-MemoryKing-Access-Receipt"] = result.access_receipt

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            if self.released:
                self.released.decision.check(self.released.clock)
        except LocalError as exc:
            await error(exc.code, 409)(scope, receive, send)
            return

        async def guarded(message: Any) -> None:
            if self.released and message["type"] == "http.response.body" and message.get("body"):
                try:
                    self.released.decision.check(self.released.clock)
                except LocalError:
                    # Headers may already have left; close without any protected bytes.
                    message = {"type": "http.response.body", "body": b"", "more_body": False}
            await send(message)

        await super().__call__(scope, receive, guarded)


def error(code: str, status: int = 400, request_id: str | None = None) -> Response:
    body: dict[str, str] = {"error": code, "hint": hint(code)}
    if request_id is not None:
        body["request_id"] = request_id
    return Response(
        canonical_bytes(body),
        status_code=503 if code in {"service_busy", "service_unavailable"} else status,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def bearer(headers: Any) -> str:
    authorization = headers.get("authorization", "")
    if not authorization.startswith("Bearer ") or len(authorization) > 8199:
        raise LocalError("credential_required")
    token = authorization[7:]
    if not token or any(c.isspace() for c in token):
        raise LocalError("credential_required")
    return str(token)


class Boundary:
    def __init__(
        self,
        app: Any,
        *,
        hosts: list[str],
        origins: list[str],
        capacity: int = 32,
        loopback_http: bool = False,
    ):
        if not hosts or any("*" in value for value in [*hosts, *origins]):
            raise LocalError("explicit_shared_hosts_required")
        self.app, self.hosts, self.origins = app, set(hosts), set(origins)
        self.slots = threading.BoundedSemaphore(capacity)
        self.loopback_http = loopback_http

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if scope.get("scheme") != "https" and not (
            self.loopback_http and scope.get("client", (None,))[0] in {"127.0.0.1", "::1"}
        ):
            await error("shared_tls_required", 403)(scope, receive, send)
            return
        # Duplicate authority/identity headers must not be interpreted differently downstream.
        for key in (b"host", b"authorization", b"origin", b"content-length"):
            if sum(name.lower() == key for name, _ in scope["headers"]) > 1:
                await error("ambiguous_headers")(scope, receive, send)
                return
        if request.headers.get("host") not in self.hosts:
            await error("invalid_host", 421)(scope, receive, send)
            return
        origin = request.headers.get("origin")
        if origin is not None and origin not in self.origins:
            await error("invalid_origin", 403)(scope, receive, send)
            return
        if not self.slots.acquire(blocking=False):
            await error("service_busy", 503)(scope, receive, send)
            return
        body_started = time.monotonic()
        body_bytes = 0
        body_done = False

        async def bounded_receive() -> Any:
            nonlocal body_bytes, body_done
            if body_done:
                return await receive()
            remaining = 5 - (time.monotonic() - body_started)
            if remaining <= 0:
                raise TimeoutError
            with anyio.fail_after(remaining):
                message = await receive()
            if message["type"] == "http.request":
                body_bytes += len(message.get("body", b""))
                if body_bytes > 262144:
                    raise LocalError("request_too_large")
                body_done = not message.get("more_body", False)
            return message

        try:
            await self.app(scope, bounded_receive, send)
        finally:
            self.slots.release()


def create_app(
    service: SharedService,
    *,
    hosts: list[str],
    origins: list[str],
    loopback_http: bool = False,
    dashboard_tenant: str | None = None,
) -> Any:
    async def operations(request: Request) -> Response:
        try:
            credential = bearer(request.headers)
            if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                raise LocalError("invalid_content_type")
            data = bytearray()
            with anyio.fail_after(5):
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > 262144:
                        raise LocalError("request_too_large")
            body = load_strict(bytes(data))
            if not isinstance(body, dict):
                raise LocalError("invalid_request")
            result = await anyio.to_thread.run_sync(
                service.execute, request.path_params["tenant"], credential, body
            )
            return ReleaseResponse(result)
        except (LocalError, ContractError) as exc:
            code = exc.code if isinstance(exc, LocalError) else "invalid_request"
            default = 403 if code in {"denied", "credential_required"} else 400
            return error(code, http_status(code, default))
        except TimeoutError:
            return error("request_timeout", 408)
        except Exception as exc:
            # No SQL, supplied content, filesystem paths or credentials in responses.
            return error("service_unavailable", 503, failure("shared_http", exc))

    from memoryking.adapters.shared_mcp import SharedMCP

    mcp = SharedMCP(service, hosts=hosts, origins=origins)
    dashboard_routes = []
    if dashboard_tenant is not None:
        from memoryking.adapters.shared_dashboard import SharedDashboard

        service.dashboard = SharedDashboard(
            service, dashboard_tenant, origins, secure=not loopback_http
        )
        dashboard_routes = service.dashboard.routes()

    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        async with mcp.manager.run():
            yield

    app = Starlette(
        lifespan=lifespan,
        routes=[
            *dashboard_routes,
            Route("/v1/tenants/{tenant}/operations", operations, methods=["POST"]),
            Route(
                "/v1/tenants/{tenant}/projects/{project}/mcp",
                mcp,
                methods=["POST", "GET", "DELETE"],
            ),
        ],
    )
    return Boundary(app, hosts=hosts, origins=origins, loopback_http=loopback_http)
