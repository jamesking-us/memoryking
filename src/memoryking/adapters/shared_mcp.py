"""Official MCP SDK 2.3.0, stateless Streamable HTTP.

Two tool profiles are served at the same URL: `lean` (the default: a few flat tools whose
replies are the Markdown a model reads, with the machine envelope as structured content)
and `full` (`?profile=full`: the schema-heavy catalog, replies as canonical JSON). The
protocol revisions accepted are the handshake revisions of the pinned library.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, AsyncIterator

import anyio
import mcp_types as types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request

from memoryking.adapters import mcp_common
from memoryking.adapters.postgres_pool import busy_sqlstate
from memoryking.adapters.shared_service import SharedService
from memoryking.domain.local import LocalError, Released
from memoryking.observability import busy
from memoryking.sdk import BINDING_FIELDS, LocalClient

# The handshake revisions plus the stateless per-request one the pinned library also serves.
SUPPORTED_VERSIONS = (*mcp_common.HANDSHAKE_VERSIONS, "2026-07-28")
DEFAULT_PROFILE = "lean"


class RequestClient(LocalClient):
    """Reuse tool mappings without a network hop; each operation has a fresh DB gate."""

    def __init__(self, service: SharedService, tenant: str, credential: str, project: str):
        self.service, self.tenant, self.token, self.project = service, tenant, credential, project
        self.namespace = "sdk"
        self.bindings: list[dict[str, Any]] = [{}]
        self.released: Released | None = None
        self.last_context = self._request(credential, "context", {"project": project})
        self.binding = tuple(self.last_context[key] for key in BINDING_FIELDS)

    def _credential(self) -> str:
        return self.token

    def _request(self, credential: str, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self.service.execute(
            self.tenant,
            credential,
            {"version": 1, "operation": operation, "args": args},
            time.monotonic() + 5.0,
        )
        if isinstance(result, Released):
            output: list[bytes] = []
            result.emit(output.append)
            self.released = result
            value: dict[str, Any] = json.loads(output[0])
            return value
        return result


def input_schema(tools: list[dict[str, Any]], name: str) -> Any:
    return next((item["inputSchema"] for item in tools if item["name"] == name), None)


class SharedMCP:
    def __init__(self, service: SharedService, *, hosts: list[str], origins: list[str]):
        self.service = service
        self.servers: dict[str, Any] = {}
        self.managers: dict[str, StreamableHTTPSessionManager] = {}
        for profile in mcp_common.PROFILES:
            tools = mcp_common.catalog(profile)
            self.servers[profile] = Server(
                "MemoryKing",
                version=mcp_common.SERVER_VERSION,
                instructions=mcp_common.INSTRUCTIONS[profile],
                on_list_tools=partial(self.list_tools, profile),
                on_call_tool=partial(self.call_tool, profile),
                get_tool_input_schema=partial(input_schema, tools),
            )
            self.managers[profile] = StreamableHTTPSessionManager(
                self.servers[profile],
                stateless=True,
                json_response=True,
                max_request_body_size=262144,
                security_settings=TransportSecuritySettings(
                    allowed_hosts=hosts, allowed_origins=origins
                ),
            )

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        async with self.managers["lean"].run(), self.managers["full"].run():
            yield

    async def list_tools(self, profile: str, ctx: Any, params: Any) -> Any:
        return types.ListToolsResult(
            tools=[types.Tool.model_validate(item) for item in mcp_common.catalog(profile)]
        )

    def invoke(
        self,
        profile: str,
        tenant: str,
        project: str,
        token: str,
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], Released | None]:
        client = RequestClient(self.service, tenant, token, project)
        result = mcp_common.run_tool(client, profile, name, arguments)
        return result, client.released

    async def call_tool(self, profile: str, ctx: Any, params: Any) -> Any:
        from memoryking.adapters.shared_http import bearer

        request = ctx.request
        try:
            if request is None or ctx.protocol_version not in SUPPORTED_VERSIONS:
                raise LocalError("mcp_protocol_unsupported")
            result, released = await anyio.to_thread.run_sync(
                self.invoke,
                profile,
                request.path_params["tenant"],
                request.path_params["project"],
                bearer(request.headers),
                params.name,
                params.arguments or {},
            )
            request.scope["memoryking_release"] = released
            return types.CallToolResult.model_validate(result)
        except LocalError as exc:
            return types.CallToolResult.model_validate(mcp_common.refusal(exc))
        except Exception as exc:
            if (state := busy_sqlstate(exc)) is None:
                raise
            busy("shared_mcp", state)
            return types.CallToolResult.model_validate(
                mcp_common.refusal(LocalError("service_busy"))
            )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        from memoryking.adapters.shared_http import bearer, error

        request = Request(scope, receive)
        try:
            token = bearer(request.headers)
            version = request.headers.get("mcp-protocol-version")
            if version is not None and version not in SUPPORTED_VERSIONS:
                raise LocalError("mcp_protocol_unsupported")
            profile = request.query_params.get("profile", DEFAULT_PROFILE)
            if profile not in mcp_common.PROFILES:
                raise LocalError("unknown_profile")
            # Every MCP request, including tools/list and reconnect, requires a current grant.
            released = await anyio.to_thread.run_sync(
                self.service.execute,
                request.path_params["tenant"],
                token,
                {
                    "version": 1,
                    "operation": "context",
                    "args": {"project": request.path_params["project"]},
                },
                time.monotonic() + 5.0,
            )
            scope["memoryking_release"] = released
        except LocalError as exc:
            await error(exc.code, 403)(scope, receive, send)
            return
        except Exception as exc:
            if (state := busy_sqlstate(exc)) is not None:
                busy("shared_mcp", state)
                await error("service_busy", 429)(scope, receive, send)
                return
            await error("service_unavailable", 503)(scope, receive, send)
            return

        async def guarded(message: Any) -> None:
            packet = scope.get("memoryking_release")
            if isinstance(packet, Released) and message["type"] in {
                "http.response.start",
                "http.response.body",
            }:
                # A stalled SDK/transport must never emit protected content under an old gate.
                packet.decision.check(packet.clock)
            await send(message)

        await self.managers[profile].handle_request(scope, receive, guarded)
