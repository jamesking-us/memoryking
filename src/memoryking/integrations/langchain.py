"""LangChain and LangGraph (`langchain-core`, `langgraph`): tools and a `BaseStore`.

`tools(client)` returns `langchain_core.tools.StructuredTool` objects for the lean tools; they
work with any LangChain agent or a LangGraph `ToolNode`.

`MemoryKingStore` is a LangGraph `BaseStore` that honours only what MemoryKing really does:

* `put(namespace, key, value)` is a capture. The key and the value are saved together as one
  verbatim observation, so a changed value is a new version and nothing is overwritten. The
  namespace is `(project, *labels)`: the first label is the client's project, the rest become
  the observation's `source`.
* `search((project,), query=...)` is a briefing. It returns one `SearchItem` whose value holds
  the cited Markdown, its status, receipt and sources. The briefing is project-wide, so a
  narrower prefix, `filter` and `offset` are refused.
* `delete(namespace, key)` is a retirement request, and `key` is the id of the memory to
  retire (from a briefing's sources), not the key you put. An admin decides; until then the
  memory is hidden from your briefings. `put` with a `None` value is the same request.
* `get`, `list_namespaces` and TTLs raise `NotImplementedError`: there is no read by key and no
  namespace listing, and a briefing cannot stand in for them without pretending.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from memoryking import lean
from memoryking.integrations.function_calling import EVERYDAY, FunctionTools
from memoryking.sdk import LocalClient

_SOURCE = re.compile(r"[A-Za-z0-9_-]{1,96}")
_MISSING = (
    "memoryking.integrations.langchain needs `langchain-core` (tested with 1.8.4) and, for "
    "MemoryKingStore, `langgraph` (tested with 1.2.24); install them in your own environment"
)


def tools(
    client: LocalClient,
    names: Sequence[str] = EVERYDAY,
    *,
    chat_review: bool = False,
) -> list[Any]:
    """The lean tools bound to `client`, as `langchain_core.tools.StructuredTool` objects."""
    try:
        from langchain_core.tools import StructuredTool
    except ImportError as exc:
        raise ImportError(_MISSING) from exc
    bound = FunctionTools(client, names, chat_review=chat_review)

    def build(spec: dict[str, Any]) -> Any:
        name = spec["name"]

        def run(**arguments: Any) -> str:
            return bound.run(name, arguments)

        async def arun(**arguments: Any) -> str:
            return await asyncio.to_thread(bound.run, name, arguments)

        return StructuredTool(
            name=name,
            description=spec["description"],
            args_schema=spec["parameters"],
            func=run,
            coroutine=arun,
        )

    return [build(spec) for spec in bound.specs()]


def _build_store() -> Any:
    try:
        from langgraph.store.base import (
            BaseStore,
            GetOp,
            InvalidNamespaceError,
            ListNamespacesOp,
            PutOp,
            SearchItem,
            SearchOp,
        )
    except ImportError as exc:
        raise ImportError(_MISSING) from exc

    class MemoryKingStore(BaseStore):  # type: ignore[misc]
        """A LangGraph `BaseStore` over a MemoryKing client; see the module docstring."""

        def __init__(
            self,
            client: LocalClient,
            *,
            budget: int = 6000,
            retire_reason: str = "Retirement requested through the LangGraph store.",
        ):
            self.client = client
            self.budget = budget
            self.retire_reason = retire_reason
            self.root = (client.project,)

        def _source(self, namespace: tuple[str, ...]) -> str:
            if namespace[: len(self.root)] != self.root:
                raise InvalidNamespaceError(
                    f"namespaces start with the project {self.root[0]!r}; got {namespace!r}"
                )
            source = "-".join(namespace[len(self.root) :]) or "langgraph"
            if not _SOURCE.fullmatch(source):
                raise InvalidNamespaceError(
                    "labels after the project become the memory's source and may use only "
                    f"letters, digits, '_' and '-' (96 characters in all); got {namespace!r}"
                )
            return source

        def batch(self, ops: Iterable[Any]) -> list[Any]:
            return [self._one(op) for op in ops]

        async def abatch(self, ops: Iterable[Any]) -> list[Any]:
            return await asyncio.to_thread(self.batch, list(ops))

        def _one(self, op: Any) -> Any:
            if isinstance(op, GetOp):
                raise NotImplementedError(
                    "MemoryKing has no read by key; use search(project namespace, query=...)"
                )
            if isinstance(op, ListNamespacesOp):
                raise NotImplementedError("MemoryKing does not list namespaces")
            if isinstance(op, SearchOp):
                return self._search(op)
            if isinstance(op, PutOp):
                if op.ttl is not None:
                    raise NotImplementedError("MemoryKing memories do not expire by TTL")
                return self._delete(op) if op.value is None else self._put(op)
            raise NotImplementedError(f"unsupported store operation {type(op).__name__}")

        def _put(self, op: Any) -> None:
            source = self._source(op.namespace)
            try:
                text = json.dumps(
                    {"key": op.key, "value": op.value}, sort_keys=True, ensure_ascii=False
                )
            except TypeError as exc:
                raise ValueError("value must be JSON-serializable") from exc
            self.client.remember(text, source=source)

        def _delete(self, op: Any) -> None:
            self._source(op.namespace)
            lean.call(self.client, "forget", {"memory": op.key, "why": self.retire_reason})

        def _search(self, op: Any) -> list[Any]:
            prefix = tuple(op.namespace_prefix)
            if prefix not in ((), self.root):
                raise NotImplementedError(
                    "a briefing covers the whole project; search with the namespace "
                    f"{self.root!r} (a narrower prefix cannot be honoured)"
                )
            if not op.query:
                raise NotImplementedError("MemoryKing search needs a query; it cannot list")
            if op.filter:
                raise NotImplementedError("MemoryKing search does not filter on value fields")
            if op.offset:
                raise NotImplementedError("a briefing has no pages; offset must be 0")
            if op.limit < 1:
                return []
            briefing = self.client.recall(op.query, budget=self.budget)
            now = datetime.now(UTC)
            return [
                SearchItem(
                    namespace=self.root,
                    key=briefing.receipt,
                    value={
                        "briefing": briefing.text,
                        "status": briefing.status,
                        "receipt": briefing.receipt,
                        "sources": briefing.sources,
                        "warnings": briefing.warnings,
                    },
                    created_at=now,
                    updated_at=now,
                    score=None,
                )
            ]

    MemoryKingStore.__module__ = __name__
    return MemoryKingStore


_STORE: list[Any] = []


def __getattr__(name: str) -> Any:
    """`MemoryKingStore` is built on first access so importing this module needs no langgraph."""
    if name == "MemoryKingStore":
        if not _STORE:
            _STORE.append(_build_store())
        return _STORE[0]
    raise AttributeError(name)
