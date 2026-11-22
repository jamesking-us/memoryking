"""Bounded MCP stdio bridge for the implemented local memory tools, with no provider calls.

The default `lean` profile serves a few flat tools and returns what a model should read
(the Markdown briefing, a one-line confirmation) as the text block alone, because some
hosts show a model only `structuredContent`; `--profile full` serves the schema-heavy catalog whose
text block is the canonical JSON envelope, as before.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from time import monotonic
from typing import Any, BinaryIO, Callable

from memoryking import daemon, profiles, session_bindings
from memoryking.adapters import mcp_common
from memoryking.adapters.mcp_common import LEAN_VALIDATORS
from memoryking.contracts.canonical import ContractError, canonical_bytes, load_strict
from memoryking.domain.hints import hint
from memoryking.domain.local import LocalError
from memoryking.observability import failure
from memoryking.sdk import LocalClient

INPUT_LIMIT = 262144
__all__ = ["INPUT_LIMIT", "LEAN_VALIDATORS", "StdioBridge", "main"]


class StdioBridge:
    def __init__(
        self,
        client: LocalClient,
        profile: str = "lean",
        chat_review: bool = False,
        restart: Callable[[], bool] | None = None,
    ):
        if profile not in mcp_common.PROFILES:
            raise LocalError("invalid_request")
        self.client = client
        self.profile = profile
        self.chat_review = chat_review
        # Called when the daemon is gone; True when it started it again, so a call that
        # never reached it can be retried once.
        self.restart = restart
        self.initialized = False
        self.ready = False

    def tools(self) -> list[dict[str, Any]]:
        return mcp_common.catalog(self.profile, self.chat_review)

    @staticmethod
    def error(identifier: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}

    @staticmethod
    def result(identifier: Any, value: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": identifier, "result": value}

    def handle(self, value: Any) -> dict[str, Any] | None:
        if (
            not isinstance(value, dict)
            or value.get("jsonrpc") != "2.0"
            or not isinstance(value.get("method"), str)
        ):
            return self.error(None, -32600, "invalid_request")
        identifier = value.get("id")
        notification = "id" not in value
        if not notification and (
            isinstance(identifier, bool) or not isinstance(identifier, (str, int))
        ):
            return self.error(None, -32600, "invalid_request")
        method, params = value["method"], value.get("params", {})
        if not isinstance(params, dict):
            return None if notification else self.error(identifier, -32602, "invalid_params")
        if notification:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            return None
        if method == "ping":
            return self.result(identifier, {})
        if method == "initialize":
            info = params.get("clientInfo")
            if (
                self.initialized
                or not isinstance(params.get("protocolVersion"), str)
                or not isinstance(params.get("capabilities"), dict)
                or not isinstance(info, dict)
                or not isinstance(info.get("name"), str)
                or not isinstance(info.get("version"), str)
            ):
                return self.error(identifier, -32602, "invalid_initialization")
            self.initialized = True
            return self.result(
                identifier,
                {
                    "protocolVersion": mcp_common.negotiate(params["protocolVersion"]),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": mcp_common.server_info("memoryking-local"),
                    "instructions": mcp_common.INSTRUCTIONS[self.profile],
                },
            )
        if not self.ready:
            return self.error(identifier, -32000, "not_initialized")
        if method == "tools/list":
            if params.get("cursor") is not None:
                return self.error(identifier, -32602, "invalid_cursor")
            return self.result(identifier, {"tools": self.tools()})
        if method != "tools/call":
            return self.error(identifier, -32601, "method_not_found")
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return self.error(identifier, -32602, "invalid_params")
        try:
            result = self.run(name, arguments)
        except LocalError as exc:
            return self.error(identifier, -32602, exc.code)
        return self.result(identifier, result)

    def run(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One tool call as an MCP result; a refusal is a result marked as an error."""
        result = mcp_common.run_tool(
            self.client, self.profile, name, arguments, chat_review=self.chat_review
        )
        if (
            result["isError"]
            and result["structuredContent"]["error"] == "transport_unavailable"
            and self.restart is not None
            and self.restart()
        ):
            result = mcp_common.run_tool(
                self.client, self.profile, name, arguments, chat_review=self.chat_review
            )
        return result

    def serve(self, incoming: BinaryIO, outgoing: BinaryIO) -> None:
        response: dict[str, Any] | None
        while raw := incoming.readline(INPUT_LIMIT + 1):
            started = monotonic()
            if len(raw) > INPUT_LIMIT or not raw.endswith(b"\n"):
                # Drain one overlong frame with bounded memory, never dispatch a partial frame.
                while not raw.endswith(b"\n") and raw:
                    raw = incoming.readline(INPUT_LIMIT + 1)
                response = self.error(None, -32600, "request_too_large_or_unframed")
            else:
                try:
                    value = load_strict(raw.removesuffix(b"\n"))
                    response = self.handle(value)
                except ContractError:
                    response = self.error(None, -32700, "parse_error")
                except Exception as exc:
                    failure("stdio", exc)
                    response = self.error(None, -32603, "internal_error")
            if response is not None:
                if (
                    monotonic() - started >= 5
                    and "content" in response.get("result", {})
                    and not response["result"].get("isError")
                ):
                    response = self.result(
                        response["id"],
                        {
                            "content": [{"type": "text", "text": '{"error":"release_expired"}'}],
                            "isError": True,
                        },
                    )
                outgoing.write(canonical_bytes(response) + b"\n")
                outgoing.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, help="Default: the store profile's socket")
    parser.add_argument("--credential", type=Path, help="Default: the store profile's credential")
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--profile",
        choices=("full", "lean"),
        default="lean",
        help="Tool set. lean (default): flat tools for a model (remember, recall, "
        "save_progress, resume, hand_off, propose, correct, forget, fetch, feedback, "
        "recent, and confirm with --allow-chat-review). full: the schema-heavy catalog "
        "for programs",
    )
    parser.add_argument(
        "--allow-chat-review",
        action="store_true",
        help="Let the lean `confirm` tool record the user's accept or reject of a proposal "
        "through this agent. Off by default; the credential must also hold the review "
        "capability. Decisions are recorded as confirmed in chat, naming the proposer.",
    )
    parser.add_argument(
        "--binding",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Where this session works, such as environment=prod; repeatable. Also "
        "MEMORYKING_BINDINGS and a named store profile (--store-profile)",
    )
    parser.add_argument(
        "--store-profile",
        help="Named store profile whose `bindings` apply (default MEMORYKING_PROFILE)",
    )
    args = parser.parse_args(argv)
    try:
        name = args.store_profile or os.environ.get("MEMORYKING_PROFILE")
        if name and (args.socket is None or args.credential is None):
            resolved = profiles.resolve(name)
            args.socket = args.socket or Path(resolved["socket"])
            args.credential = args.credential or Path(resolved["credential"])
        if args.socket is None or args.credential is None:
            raise LocalError("socket_required" if args.socket is None else "credential_required")
        bindings = session_bindings.resolve(args.binding, profile=args.store_profile)
        restart = None
        if name and profiles.resolve(name).get("autostart"):
            daemon.ensure(name)  # the profile lets the bridge start the daemon

            def restart() -> bool:
                return bool(daemon.ensure(name)["started"])

        client = LocalClient(args.socket, args.credential, args.project, bindings=bindings)
        StdioBridge(client, args.profile, args.allow_chat_review, restart).serve(
            sys.stdin.buffer, sys.stdout.buffer
        )
    except (LocalError, OSError, ContractError) as exc:
        code = exc.code if isinstance(exc, LocalError) else "transport_unavailable"
        detail = dict(exc.detail) if isinstance(exc, LocalError) else {}
        print(json.dumps({"error": code, "hint": hint(code), **detail}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
