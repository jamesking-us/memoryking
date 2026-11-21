"""Typed everyday methods for the Python clients: remember, recall, save_progress, resume, hand_off.

Each method is a thin wrapper over `lean.call`, the code path the lean MCP tool of the same
name runs, so authorization, receipts, idempotency, budgets and checkpoint merging are
identical whether an agent uses a tool or a program uses the client. Results are small
frozen dataclasses; the service's whole reply stays available as `.raw`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

from jsonschema import Draft202012Validator

from memoryking.domain.hints import hint
from memoryking.domain.local import LocalError

if TYPE_CHECKING:
    from memoryking.sdk import LocalClient

Classification = Literal["public", "internal", "restricted"]
Role = Literal["review", "implement", "verify", "operate"]


class Source(TypedDict):
    """One cited source of a briefing: `ref` is the `[g1]`-style label used in its text."""

    id: str
    kind: str
    ref: str


class FailedAttempt(TypedDict, total=False):
    action: str
    outcome: str
    do_not_repeat_when: str


@dataclass(frozen=True)
class Remembered:
    """A saved observation. Saving the same text from the same source on the same day replays."""

    observation: str
    position: int
    held: bool
    raw: dict[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class Briefing:
    """A cited briefing. `text` is Markdown for `format="text"` and empty for `"json"`.

    `status` is `ready`, `partial` or `blocked`; blocked means a decision is needed, not that
    the text was truncated.
    """

    text: str
    status: str
    receipt: str
    sources: list[Source]
    warnings: list[str]
    raw: dict[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class Progress:
    """A saved checkpoint: `revision` counts saves of the task, `replayed` marks a repeat."""

    checkpoint: str
    revision: int
    status: str
    receipt: str
    replayed: bool
    raw: dict[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class Resumed:
    """A Markdown handover note built from the task's latest saved progress."""

    text: str
    status: str
    receipt: str
    raw: dict[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class HandOff:
    """A prepared handoff; `handle` is what the recipient opens under their own permissions."""

    status: str
    handle: str
    raw: dict[str, Any] = field(repr=False, compare=False)


def _validated(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from memoryking import lean

    if name not in _VALIDATORS:
        for tool in lean.TOOLS:
            _VALIDATORS[tool["name"]] = Draft202012Validator(tool["inputSchema"])
    if not _VALIDATORS[name].is_valid(arguments):
        raise LocalError("invalid_tool_arguments", hint("invalid_tool_arguments"))
    return arguments


_VALIDATORS: dict[str, Draft202012Validator] = {}


def _present(**values: Any) -> dict[str, Any]:
    """Only the arguments the caller set, so each tool keeps its own defaults."""
    return {key: value for key, value in values.items() if value is not None}


def _as_of(value: str | datetime | date | None) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _text(reply: dict[str, Any], key: str) -> str:
    value = reply.get(key)
    return value if isinstance(value, str) else ""


def remembered(reply: dict[str, Any]) -> Remembered:
    return Remembered(reply["observation"], reply["position"], bool(reply["held"]), reply)


def briefing(reply: dict[str, Any]) -> Briefing:
    return Briefing(
        _text(reply, "text"),
        reply["status"],
        reply["receipt"],
        list(reply.get("sources", [])),
        list(reply.get("warnings", [])),
        reply,
    )


def progress(reply: dict[str, Any]) -> Progress:
    return Progress(
        reply["checkpoint"],
        reply["revision"],
        reply["status"],
        reply["receipt"],
        bool(reply.get("replayed", False)),
        reply,
    )


def resumed(reply: dict[str, Any]) -> Resumed:
    return Resumed(_text(reply, "text"), reply["status"], reply["receipt"], reply)


def handoff(reply: dict[str, Any]) -> HandOff:
    return HandOff(reply["status"], str(reply.get("handle", "")), reply)


def _bound(client: object) -> LocalClient:
    return cast("LocalClient", client)


class MemoryMethods:
    """Mixin for `LocalClient`: the same five operations as the lean MCP tools."""

    def remember(
        self,
        text: str,
        *,
        source: str = "agent",
        event: str | None = None,
        readers: list[str] | None = None,
        classification: Classification = "internal",
        observed_ms: int | None = None,
    ) -> Remembered:
        """Save one observation as evidence. Pass a stable `event` to make retries replay."""
        from memoryking import lean

        arguments = _present(
            text=text,
            source=source,
            event=event,
            readers=readers,
            classification=classification,
            observed_ms=observed_ms,
        )
        return remembered(lean.call(_bound(self), "remember", _validated("remember", arguments)))

    def recall(
        self,
        query: str,
        *,
        budget: int = 6000,
        as_of: str | datetime | date | None = None,
        rerank: bool = False,
        salience: bool = False,
        task: str | None = None,
        format: Literal["text", "json"] = "text",
    ) -> Briefing:
        """A cited briefing for what you are about to do; `as_of` briefs as the project stood."""
        from memoryking import lean

        arguments = _present(
            query=query,
            budget=budget,
            as_of=_as_of(as_of),
            rerank=rerank or None,
            salience=salience or None,
            task=task,
        )
        reply = lean.call(
            _bound(self), "recall", {**_validated("recall", arguments), "format": format}
        )
        return briefing(reply)

    def save_progress(
        self,
        task: str,
        objective: str,
        current_state: str,
        *,
        next_actions: list[str] | None = None,
        findings: list[str] | None = None,
        failed_attempts: list[FailedAttempt] | None = None,
        blockers: list[str] | None = None,
        hard_constraints: list[str] | None = None,
        run: str | None = None,
        share_with: list[str] | None = None,
    ) -> Progress:
        """Save where a task stands. A section left out keeps the previous save's content."""
        from memoryking import lean

        arguments = _present(
            task=task,
            objective=objective,
            current_state=current_state,
            next_actions=next_actions,
            findings=findings,
            failed_attempts=failed_attempts,
            blockers=blockers,
            hard_constraints=hard_constraints,
            run=run,
            share_with=share_with,
        )
        return progress(
            lean.call(_bound(self), "save_progress", _validated("save_progress", arguments))
        )

    def resume(self, task: str, *, run: str | None = None, budget: int = 12000) -> Resumed:
        """The handover note for a task's latest saved progress."""
        from memoryking import lean

        arguments = _present(task=task, run=run, budget=budget)
        return resumed(lean.call(_bound(self), "resume", _validated("resume", arguments)))

    def hand_off(
        self,
        task: str,
        to: str,
        *,
        role: Role = "review",
        stage: str = "handoff",
        ttl_minutes: int = 60,
        run: str | None = None,
    ) -> HandOff:
        """Prepare a task's latest progress for another principal, under their permissions."""
        from memoryking import lean

        arguments = _present(
            task=task, to=to, role=role, stage=stage, ttl_minutes=ttl_minutes, run=run
        )
        return handoff(lean.call(_bound(self), "hand_off", _validated("hand_off", arguments)))
