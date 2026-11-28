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

from memoryking.adapters.postgres_pool import busy_sqlstate
from memoryking.adapters.shared_service import SharedService
from memoryking.contracts.canonical import ContractError, canonical_bytes, load_strict
from memoryking.domain.hints import hint
from memoryking.domain.http_status import http_status
from memoryking.domain.local import LocalError, Released
from memoryking.observability import busy, failure


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


def error(
    code: str, status: int = 400, request_id: str | None = None, detail: str | None = None
) -> Response:
    body: dict[str, str] = {"error": code, "hint": detail or hint(code)}
    if request_id is not None:
        body["request_id"] = request_id
    return Response(
        canonical_bytes(body),
        # Pool pressure and outages are 503; a transient database conflict names its own 429.
        status_code=(
            429
            if status == 429
            else 503
            if code in {"service_busy", "service_unavailable"}
            else status
        ),
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
        readiness: Any = None,
        observe: Any = None,
    ):
        if not hosts or any("*" in value for value in [*hosts, *origins]):
            raise LocalError("explicit_shared_hosts_required")
        self.app, self.hosts, self.origins = app, set(hosts), set(origins)
        if not 1 <= capacity <= 1024:
            raise LocalError("invalid_concurrency")
        self.slots = threading.BoundedSemaphore(capacity)
        self.capacity = capacity
        self.in_flight = 0
        self.counter = threading.Lock()
        self.loopback_http = loopback_http
        self.readiness = readiness
        self.observe = observe

    def probe(self, path: str) -> Response:
        from memoryking.health import LIVE_BODY, readiness_response

        if path == "/healthz" or self.readiness is None:
            status, body = 200, LIVE_BODY
        else:
            status, body = readiness_response(self.readiness())
        return Response(
            body,
            status_code=status,
            media_type="application/json",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        real_send = send

        async def counted(message: Any) -> None:
            if message["type"] == "http.response.start" and self.observe is not None:
                self.observe(int(message["status"]))
            await real_send(message)

        send = counted
        if scope["method"] in {"GET", "HEAD"} and scope["path"] in {"/healthz", "/readyz"}:
            # Probes come from a load balancer or orchestrator by address, so the host and
            # origin allowlists do not apply; they answer a fixed boolean and take no credential.
            await self.probe(scope["path"])(scope, receive, send)
            return
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
        with self.counter:
            self.in_flight += 1
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
            with self.counter:
                self.in_flight -= 1


def create_app(
    service: SharedService,
    *,
    hosts: list[str],
    origins: list[str],
    loopback_http: bool = False,
    dashboard_tenant: str | None = None,
    capacity: int = 32,
) -> Any:
    async def operations(request: Request) -> Response:
        request_started = time.monotonic()
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
            if (
                body.get("operation") in {"viewer_exchange", "viewer_read", "viewer_close"}
                and len(data) > 16384
            ):
                raise LocalError("bounds_exceeded")
            # The whole request, body included, shares one five-second budget.
            result = await anyio.to_thread.run_sync(
                service.execute,
                request.path_params["tenant"],
                credential,
                body,
                request_started + 5.0,
            )
            return ReleaseResponse(result)
        except (LocalError, ContractError) as exc:
            code = exc.code if isinstance(exc, LocalError) else "invalid_request"
            default = 403 if code in {"denied", "credential_required"} else 400
            detail = (
                exc.hint if isinstance(exc, LocalError) and code == "schema_unsupported" else None
            )
            return error(code, http_status(code, default), detail=detail)
        except TimeoutError:
            return error("request_timeout", 408)
        except Exception as exc:
            if (state := busy_sqlstate(exc)) is not None:
                busy("shared_http", state)
                return error("service_busy", 429)
            # No SQL, supplied content, filesystem paths or credentials in responses.
            return error("service_unavailable", 503, failure("shared_http", exc))

    from memoryking.health import CachedChecks

    checks = CachedChecks(lambda: service.readiness())

    async def metrics(request: Request) -> Response:
        """Prometheus text behind the tenant's admin credential, like the local `/metrics`."""
        started = time.monotonic()
        code: str | None = None
        try:
            credential = bearer(request.headers)
            if request.headers.get("cookie"):
                raise LocalError("mixed_authentication")
            text = await anyio.to_thread.run_sync(
                service.metrics_text,
                request.path_params["tenant"],
                credential,
                started + 5.0,
                checks(),
            )
            return Response(
                text.encode(),
                media_type="text/plain; version=0.0.4",
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )
        except LocalError as exc:
            code = exc.code
            return error(
                code, http_status(code, 403 if code in {"denied", "credential_required"} else 400)
            )
        except Exception as exc:
            code = "internal_error"
            if (state := busy_sqlstate(exc)) is not None:
                busy("shared_http", state)
                return error("service_busy", 429)
            return error("service_unavailable", 503, failure("shared_http", exc))
        finally:
            service.metrics.observe("http_metrics", (time.monotonic() - started) * 1000, code)

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
        async with mcp.run():
            yield

    app = Starlette(
        lifespan=lifespan,
        routes=[
            *dashboard_routes,
            Route("/v1/tenants/{tenant}/operations", operations, methods=["POST"]),
            Route("/v1/tenants/{tenant}/metrics", metrics, methods=["GET"]),
            Route(
                "/v1/tenants/{tenant}/projects/{project}/mcp",
                mcp,
                methods=["POST", "GET", "DELETE"],
            ),
        ],
    )
    boundary = Boundary(
        app,
        hosts=hosts,
        origins=origins,
        capacity=capacity,
        loopback_http=loopback_http,
        readiness=checks,
        observe=service.http_response,
    )
    service.http_gauge = lambda: (boundary.capacity, boundary.in_flight)
    return boundary
