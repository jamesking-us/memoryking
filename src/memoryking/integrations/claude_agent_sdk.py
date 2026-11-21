"""Claude Agent SDK (`claude-agent-sdk`): MCP configuration, in-process tools and hooks.

Three ways in, from least to most code:

* `mcp_server_config` returns the stdio configuration of the lean MCP bridge for
  `ClaudeAgentOptions.mcp_servers`; the SDK's Claude Code process starts and talks to it.
* `sdk_server` returns an in-process SDK MCP server whose tools are the lean tools bound to
  a client, so no bridge process is needed.
* `session_context` and `pre_compact_hook` load a briefing when a session starts and save
  what a compaction is about to drop, using the same code as `memoryking hook`.

Nothing here adds behavior: tools run `memoryking.lean.call` through `FunctionTools`, the hooks
call `memoryking.host_hooks`. The SDK is imported inside the functions that need it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from memoryking import host_hooks, profiles
from memoryking.domain.hints import hint
from memoryking.domain.local import LocalError
from memoryking.integrations.function_calling import EVERYDAY, FunctionTools
from memoryking.sdk import LocalClient

SERVER_NAME = "memoryking"


def _sdk() -> Any:
    try:
        import claude_agent_sdk
    except ImportError as exc:
        raise ImportError(
            "memoryking.integrations.claude_agent_sdk needs the `claude-agent-sdk` package "
            "(tested with 0.2.187); install it in your own environment"
        ) from exc
    return claude_agent_sdk


def mcp_server_config(
    project: str,
    *,
    profile: str | None = None,
    socket: Path | None = None,
    credential: Path | None = None,
    config: Path | None = None,
    bindings: dict[str, str] | None = None,
    allow_chat_review: bool = False,
    python: str | None = None,
) -> dict[str, Any]:
    """The stdio server entry for the lean bridge, for `ClaudeAgentOptions.mcp_servers`.

    Name a store `profile` (from `~/.memoryking/config.toml`, resolved now to absolute paths) or
    give `socket` and `credential`. `bindings` are the session's `KEY=VALUE` facts such as
    `{"environment": "prod"}`. The credential is passed as a file path, never its value. The
    bridge is started by the SDK's Claude Code process, using `python` (default: this
    interpreter), which must have `memoryking` installed.
    """
    if profile and (socket is None or credential is None):
        resolved = profiles.resolve(profile, config)
        socket = socket or Path(resolved["socket"])
        credential = credential or Path(resolved["credential"])
    if socket is None or credential is None:
        raise LocalError("socket_required" if socket is None else "credential_required")
    args = [
        "-m",
        "memoryking.adapters.stdio",
        "--socket",
        str(Path(socket).resolve()),
        "--credential",
        str(Path(credential).resolve()),
        "--project",
        project,
        "--profile",
        "lean",
    ]
    for key, value in sorted((bindings or {}).items()):
        args += ["--binding", f"{key}={value}"]
    if allow_chat_review:
        args.append("--allow-chat-review")
    return {"type": "stdio", "command": python or sys.executable, "args": args}


def allowed_tool_names(
    names: Sequence[str] = EVERYDAY, *, server_name: str = SERVER_NAME
) -> list[str]:
    """The `mcp__<server>__<tool>` names for `ClaudeAgentOptions.allowed_tools`."""
    return [f"mcp__{server_name}__{name}" for name in names]


def sdk_tools(
    client: LocalClient,
    *,
    names: Sequence[str] = EVERYDAY,
    chat_review: bool = False,
) -> list[Any]:
    """The lean tools bound to `client` as SDK tool definitions (`SdkMcpTool`).

    Each call runs the synchronous client on a worker thread; a refusal is returned to the
    model as an error result carrying the code and its hint."""
    sdk = _sdk()
    from mcp.types import ToolAnnotations

    from memoryking import lean

    tools = FunctionTools(client, names, chat_review=chat_review)
    catalog = {item["name"]: item for item in lean.TOOLS}

    def build(spec: dict[str, Any]) -> Any:
        name = spec["name"]

        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            text, failed = await asyncio.to_thread(tools.execute, name, dict(arguments))
            result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
            if failed:
                result["is_error"] = True
            return result

        annotations = ToolAnnotations(**catalog[name]["annotations"])
        return sdk.tool(name, spec["description"], spec["parameters"], annotations)(handler)

    return [build(spec) for spec in tools.specs()]


def sdk_server(
    client: LocalClient,
    *,
    names: Sequence[str] = EVERYDAY,
    server_name: str = SERVER_NAME,
    chat_review: bool = False,
) -> Any:
    """An in-process SDK MCP server with the lean tools bound to `client`.

    Pass it as `mcp_servers={server_name: sdk_server(client)}`; no bridge process is needed."""
    tools = sdk_tools(client, names=names, chat_review=chat_review)
    return _sdk().create_sdk_mcp_server(server_name, tools=tools)


def _where(client: LocalClient) -> dict[str, Any]:
    if not isinstance(client, LocalClient) or not hasattr(client, "socket"):
        # The hooks speak the local socket; a shared-service client has none.
        raise LocalError("socket_required", hint("socket_required"))
    return {
        "socket": client.socket,
        "credential": client.credential_file,
        "project": client.project,
    }


def session_context(
    client: LocalClient,
    *,
    task: str | None = None,
    query: str | None = None,
    budget: int = 6000,
    session: str = "",
) -> str:
    """Cited Markdown to load when a session starts: the task's resume packet, then a briefing.

    Append it to the system prompt, for example
    `system_prompt={"type": "preset", "preset": "claude_code", "append": session_context(...)}`.
    The same text `memoryking hook session-start` prints. Blocking; run it before the agent
    starts."""
    event = {"host": "claude-agent-sdk", "session": session, "kind": "session-start"}
    return host_hooks.session_start(
        event, query=query, task=task, budget=budget, binding=None, **_where(client)
    )


HookCallback = Callable[[dict[str, Any], str | None, Any], Awaitable[dict[str, Any]]]


def session_start_hook(
    client: LocalClient, *, task: str | None = None, query: str | None = None, budget: int = 6000
) -> HookCallback:
    """A `SessionStart` hook callback returning the briefing as `additionalContext`.

    Registered as `hooks={"SessionStart": [HookMatcher(hooks=[callback])]}`. The Python SDK's
    typed hook events do not list `SessionStart`, and this callback has been called directly
    but not driven by a live Claude Code session; `session_context` in the system prompt is the
    path that does not depend on it."""

    async def callback(
        event: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        text = await asyncio.to_thread(
            session_context,
            client,
            task=task,
            query=query,
            budget=budget,
            session=str(event.get("session_id", "")),
        )
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}

    return callback


def pre_compact_hook(client: LocalClient, *, task: str | None = None) -> HookCallback:
    """A `PreCompact` hook callback that saves what the compaction is about to drop.

    It captures the transcript tail (secret-shaped values are redacted first) and, with a
    `task`, saves a checkpoint, exactly as `memoryking hook pre-compact` does. A failure
    becomes a message to the user; it never blocks the compaction."""

    async def callback(
        event: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        normalized = host_hooks.read_event(json.dumps(event))
        try:
            await asyncio.to_thread(
                host_hooks.save,
                normalized,
                "pre-compact",
                host="claude-agent-sdk",
                task=task,
                **_where(client),
            )
        except (LocalError, OSError) as exc:
            code = exc.code if isinstance(exc, LocalError) else "transport_unavailable"
            return {"systemMessage": f"MemoryKing could not save before compaction: {code}"}
        return {}

    return callback


def hooks(client: LocalClient, *, task: str | None = None) -> dict[str, list[Any]]:
    """`ClaudeAgentOptions.hooks` with the pre-compaction save registered."""
    return {"PreCompact": [_sdk().HookMatcher(hooks=[pre_compact_hook(client, task=task)])]}
