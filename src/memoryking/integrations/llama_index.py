"""LlamaIndex (`llama-index-core`): tools and a read-only memory block.

`tools(client)` returns `llama_index.core.tools.FunctionTool` objects for the lean tools; their
parameter schema is the lean tool's own, passed through unchanged.

`MemoryKingBlock` is a `BaseMemoryBlock` for `Memory.from_defaults(memory_blocks=[...])`. Pulling
it briefs the latest user message and inserts the cited Markdown into the prompt. Pushing is a
deliberate no-op: the messages the memory ejects are a transcript, and MemoryKing saves what
an agent decides to `remember` or `save_progress`, not a conversation. It is not a retriever
over documents; it briefs from what was saved.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from memoryking.integrations.function_calling import EVERYDAY, FunctionTools
from memoryking.sdk import LocalClient

QUERY_LIMIT = 2048  # the `recall` tool's query length
_MISSING = (
    "memoryking.integrations.llama_index needs the `llama-index-core` package "
    "(tested with 0.14.26); install it in your own environment"
)


def tools(
    client: LocalClient,
    names: Sequence[str] = EVERYDAY,
    *,
    chat_review: bool = False,
) -> list[Any]:
    """The lean tools bound to `client`, as `llama_index.core.tools.FunctionTool` objects."""
    try:
        from llama_index.core.tools import FunctionTool, ToolMetadata
    except ImportError as exc:
        raise ImportError(_MISSING) from exc
    bound = FunctionTools(client, names, chat_review=chat_review)

    class LeanMetadata(ToolMetadata):  # type: ignore[misc]
        """Reports the lean tool's JSON schema as the parameters instead of a pydantic model."""

        schema: dict[str, Any]

        def get_parameters_dict(self) -> dict[str, Any]:
            return self.schema

    def build(spec: dict[str, Any]) -> Any:
        name = spec["name"]

        def run(**arguments: Any) -> str:
            return bound.run(name, arguments)

        async def arun(**arguments: Any) -> str:
            return await asyncio.to_thread(bound.run, name, arguments)

        metadata = LeanMetadata(description=spec["description"], name=name)
        metadata.schema = spec["parameters"]
        return FunctionTool.from_defaults(fn=run, async_fn=arun, tool_metadata=metadata)

    return [build(spec) for spec in bound.specs()]


def _build_block() -> Any:
    try:
        from llama_index.core.base.llms.types import ChatMessage
        from llama_index.core.memory import BaseMemoryBlock
    except ImportError as exc:
        raise ImportError(_MISSING) from exc

    class MemoryKingBlock(BaseMemoryBlock[str]):  # type: ignore[misc]
        """Briefs the latest user message from MemoryKing; never writes."""

        name: str = "MemoryKing"
        description: str | None = "Cited guidance from saved MemoryKing memory."
        accept_short_term_memory: bool = False
        client: Any
        budget: int = 6000

        async def _aget(
            self, messages: list[ChatMessage] | None = None, **block_kwargs: Any
        ) -> str:
            query = next(
                (
                    str(message.content or "")
                    for message in reversed(messages or [])
                    if message.role == "user" and message.content
                ),
                "",
            ).strip()[:QUERY_LIMIT]
            if not query:
                return ""
            briefing = await asyncio.to_thread(self.client.recall, query, budget=self.budget)
            return str(briefing.text)

        async def _aput(self, messages: list[ChatMessage]) -> None:
            return None

    MemoryKingBlock.__module__ = __name__
    return MemoryKingBlock


_BLOCK: list[Any] = []


def __getattr__(name: str) -> Any:
    """`MemoryKingBlock` is built on first access so importing this module needs no llama-index."""
    if name == "MemoryKingBlock":
        if not _BLOCK:
            _BLOCK.append(_build_block())
        return _BLOCK[0]
    raise AttributeError(name)
