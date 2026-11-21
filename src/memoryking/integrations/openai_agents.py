"""OpenAI Agents SDK (`openai-agents`): the lean tools as function tools.

`tools(client)` returns `agents.FunctionTool` objects for `Agent(tools=...)`. Each one carries
the lean tool's own JSON schema and runs `memoryking.lean.call` on a worker thread through
`FunctionTools`, so authorization, receipts, idempotency and budgets are those of the MCP tool
of the same name. A refusal is returned to the model as text with the error code and its hint.

No `Session` is provided. The SDK's session protocol stores a conversation: ordered items,
`pop_item` and `clear_session`. MemoryKing is not a transcript store. It keeps verbatim
observations as evidence, versions checkpoints, and never deletes on request (retirement is
approved by an admin), so `pop_item` and `clear_session` could not be honoured and replaying
a transcript from observations would not be a faithful history. Keep the SDK's own session for
the conversation and give the agent these tools (and `resume` at the start of a task) for
what should outlive it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from memoryking.integrations.function_calling import EVERYDAY, FunctionTools
from memoryking.sdk import LocalClient


def tools(
    client: LocalClient,
    names: Sequence[str] = EVERYDAY,
    *,
    chat_review: bool = False,
) -> list[Any]:
    """The lean tools bound to `client`, as `agents.FunctionTool` objects."""
    try:
        from agents import FunctionTool
    except ImportError as exc:
        raise ImportError(
            "memoryking.integrations.openai_agents needs the `openai-agents` package "
            "(tested with 0.25.0); install it in your own environment"
        ) from exc
    bound = FunctionTools(client, names, chat_review=chat_review)

    def build(spec: dict[str, Any]) -> Any:
        name = spec["name"]

        async def invoke(context: Any, arguments_json: str) -> str:
            return await asyncio.to_thread(bound.call, name, arguments_json)

        return FunctionTool(
            name=name,
            description=spec["description"],
            params_json_schema=spec["parameters"],
            on_invoke_tool=invoke,
            # The lean schemas have optional fields with defaults, which strict mode forbids.
            strict_json_schema=False,
        )

    return [build(spec) for spec in bound.specs()]
