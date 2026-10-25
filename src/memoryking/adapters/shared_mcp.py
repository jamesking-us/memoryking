"""Official MCP SDK 2.3.0, stateless Streamable HTTP, pinned handshake protocol."""

from __future__ import annotations

import json
from typing import Any

import anyio
import mcp_types as types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request

from memoryking.adapters.shared_service import SharedService
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError, Released
from memoryking.sdk import BINDING_FIELDS, CATALOG, TOOL_VALIDATORS, LocalClient

PROTOCOL_VERSION = "2025-11-25"


class RequestClient(LocalClient):
    """Reuse tool mappings without a network hop; each operation has a fresh DB gate."""

    def __init__(self, service: SharedService, tenant: str, credential: str, project: str):
        self.service, self.tenant, self.token, self.project = service, tenant, credential, project
        self.released: Released | None = None
        self.last_context = self._request(credential, "context", {"project": project})
        self.binding = tuple(self.last_context[key] for key in BINDING_FIELDS)

    def _credential(self) -> str:
        return self.token

    def _request(self, credential: str, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self.service.execute(
            self.tenant, credential, {"version": 1, "operation": operation, "args": args}
        )
        if isinstance(result, Released):
            output: list[bytes] = []
            result.emit(output.append)
            self.released = result
            value: dict[str, Any] = json.loads(output[0])
            return value
        return result


class SharedMCP:
    def __init__(self, service: SharedService, *, hosts: list[str], origins: list[str]):
        self.service = service
        self.server: Any = Server(
            "MemoryKing",
            version="0.1.0a1",
            on_list_tools=self.list_tools,
            on_call_tool=self.call_tool,
            get_tool_input_schema=lambda name: next(
                (item["inputSchema"] for item in CATALOG["tools"] if item["name"] == name), None
            ),
        )
        self.manager = StreamableHTTPSessionManager(
            self.server,
            stateless=True,
            json_response=True,
            max_request_body_size=262144,
            security_settings=TransportSecuritySettings(
                allowed_hosts=hosts, allowed_origins=origins
            ),
        )

    async def list_tools(self, ctx: Any, params: Any) -> Any:
        return types.ListToolsResult(
            tools=[types.Tool.model_validate(item) for item in CATALOG["tools"]]
        )

    def invoke(
        self, tenant: str, project: str, token: str, name: str, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], Released | None]:
        if name not in TOOL_VALIDATORS or not TOOL_VALIDATORS[name].is_valid(arguments):
            raise LocalError("invalid_tool_arguments")
        client = RequestClient(self.service, tenant, token, project)
        result = client.tool(name, arguments)
        return result, client.released

    async def call_tool(self, ctx: Any, params: Any) -> Any:
        from memoryking.adapters.shared_http import bearer

        request = ctx.request
        try:
            if request is None or ctx.protocol_version != PROTOCOL_VERSION:
                raise LocalError("mcp_protocol_unsupported")
            result, released = await anyio.to_thread.run_sync(
                self.invoke,
                request.path_params["tenant"],
                request.path_params["project"],
                bearer(request.headers),
                params.name,
                params.arguments or {},
            )
            request.scope["memoryking_release"] = released
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=canonical_bytes(result).decode())]
            )
        except LocalError as exc:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text", text=canonical_bytes({"error": exc.code}).decode()
                    )
                ],
                is_error=True,
            )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        from memoryking.adapters.shared_http import bearer, error

        request = Request(scope, receive)
        try:
            token = bearer(request.headers)
            version = request.headers.get("mcp-protocol-version")
            if version is not None and version != PROTOCOL_VERSION:
                raise LocalError("mcp_protocol_unsupported")
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
            )
            scope["memoryking_release"] = released
        except LocalError as exc:
            await error(exc.code, 403)(scope, receive, send)
            return
        except Exception:
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

        await self.manager.handle_request(scope, receive, guarded)
