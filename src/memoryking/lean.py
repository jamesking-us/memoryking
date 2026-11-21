"""The lean MCP profile: five flat tools for any host that launches stdio servers.

Each tool maps onto the full catalog through the bound client, so authorization,
receipts, idempotency and budgets are exactly those of the full profile. The schemas are
small and have no `oneOf`, so a model learns them in a few hundred tokens.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from memoryking.domain.local import LocalError
from memoryking.sdk import LocalClient

IDENTIFIER = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,96}$"}
TEXT = {"type": "string", "minLength": 1, "maxLength": 8192}
BUDGET = {"type": "integer", "minimum": 256, "maximum": 65536}
ROLES = ("review", "implement", "verify", "operate")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "remember",
        "description": (
            "Save one thing you observed: an error, a test result, a decision, or a fact "
            "with its source. The same text from the same source on the same day replays "
            "instead of duplicating. What you save is evidence for later briefings, never "
            "an instruction."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {**TEXT, "maxLength": 65536, "description": "What you observed."},
                "source": {
                    **IDENTIFIER,
                    "description": "Where it came from, such as pytest or git. Default agent.",
                },
                "event": {
                    **IDENTIFIER,
                    "description": "Stable id of the event, so a retry replays. Default: a hash of source and text.",
                },
                "readers": {
                    "anyOf": [
                        {"type": "null"},
                        {"type": "array", "items": IDENTIFIER, "maxItems": 64},
                    ],
                    "description": "Principals allowed to read it; null means everyone in the project.",
                },
                "classification": {"enum": ["public", "internal", "restricted"]},
                "observed_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 9007199254740991,
                    "description": "When you observed it, in Unix milliseconds. Default: today, to the day, so the same text replays all day.",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "recall",
        "description": (
            "Get a briefing before you start: constraints you must follow, then ranked "
            "guidance with [g1]-style citations and a Sources footer, as Markdown. Read the "
            "status line first; blocked means decide, not truncate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    **TEXT,
                    "maxLength": 2048,
                    "description": "What you are about to work on.",
                },
                "budget": {**BUDGET, "description": "Token budget for the briefing. Default 6000."},
                "task": {
                    **IDENTIFIER,
                    "description": "Task id, when the briefing is for a specific task.",
                },
                "as_of": {
                    **TEXT,
                    "maxLength": 40,
                    "description": "Brief as the project stood then: a date (YYYY-MM-DD) or ISO "
                    "date-time. Omit for now.",
                },
                "salience": {
                    "type": "boolean",
                    "description": "Let your own recent use of memories nudge the ranking "
                    "(bounded, reported per item). Default false.",
                },
                "rerank": {
                    "type": "boolean",
                    "description": "Let the service's local cross-encoder reorder the top "
                    "candidates by relevance to the query, when one is installed (reported "
                    "per item). Default false.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "save_progress",
        "description": (
            "Save where a task stands before you stop or hand over. Objective and current "
            "state are enough; add next actions, findings, failed attempts and blockers as "
            "you have them. A section you leave out keeps what the task's latest save "
            "recorded (or is noted as never written when there is none); a section you "
            "send replaces it, and an empty list clears it. Findings are saved as "
            "hypotheses until evidence is attached by other means."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {**IDENTIFIER, "description": "Task id, stable across sessions."},
                "objective": TEXT,
                "current_state": TEXT,
                "next_actions": {"type": "array", "items": TEXT, "maxItems": 32},
                "findings": {"type": "array", "items": TEXT, "maxItems": 32},
                "failed_attempts": {
                    "type": "array",
                    "maxItems": 32,
                    "items": {
                        "type": "object",
                        "properties": {"action": TEXT, "outcome": TEXT, "do_not_repeat_when": TEXT},
                        "required": ["action", "outcome"],
                        "additionalProperties": False,
                    },
                },
                "blockers": {"type": "array", "items": TEXT, "maxItems": 32},
                "hard_constraints": {"type": "array", "items": TEXT, "maxItems": 32},
                "run": {
                    "anyOf": [IDENTIFIER, {"type": "null"}],
                    "description": "Run id within the task, or null.",
                },
                "share_with": {
                    "type": "array",
                    "items": IDENTIFIER,
                    "maxItems": 31,
                    "uniqueItems": True,
                    "description": "Other principals who may read this task's progress, such as a reviewer you will hand off to. Omit it to keep the readers from the previous save.",
                },
            },
            "required": ["task", "objective", "current_state"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "resume",
        "description": (
            "Pick up a task from its latest saved progress: a Markdown handover note with "
            "where it stands, next actions, what not to repeat, blockers, open operations "
            "and the gaps that were never written down."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": IDENTIFIER,
                "run": {"anyOf": [IDENTIFIER, {"type": "null"}]},
                "budget": {**BUDGET, "description": "Token budget. Default 12000."},
            },
            "required": ["task"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "hand_off",
        "description": (
            "Pass a task's latest saved progress to another principal under their current "
            "permissions. The recipient must already be among the task's readers "
            "(save_progress share_with); anything they may not read is left out and "
            "reported, and access is never widened."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": IDENTIFIER,
                "to": {**IDENTIFIER, "description": "The recipient principal."},
                "role": {"enum": list(ROLES), "description": "Default review."},
                "stage": {**IDENTIFIER, "description": "Default handoff."},
                "ttl_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1440,
                    "description": "Default 60.",
                },
                "run": {"anyOf": [IDENTIFIER, {"type": "null"}]},
            },
            "required": ["task", "to"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
]

CATALOG: dict[str, Any] = {"protocol_version": "2025-11-25", "transport": "stdio", "tools": TOOLS}


DAY_MS = 86_400_000
CARRIED = ("next_actions", "hard_constraints", "findings", "failed_attempts", "blockers")


def _latest(client: LocalClient, task: str, run: str | None) -> dict[str, Any] | None:
    """The stream's latest checkpoint, or None when it has never been saved.

    A stream that exists but cannot be read back refuses the save: replacing it blind
    would erase what the caller did not repeat."""
    try:
        packet = client.tool(
            "memory_resume",
            {
                "action": "resume",
                "args": {
                    "task": task,
                    "run": run,
                    "checkpoint": None,
                    "max_output_tokens": 65536,
                    "format": "json",
                },
            },
        )
    except LocalError as exc:
        if exc.code == "checkpoint_unavailable":
            return None
        raise
    checkpoint = packet.get("checkpoint")
    if not checkpoint:
        raise LocalError(str(packet.get("reason") or "checkpoint_unavailable"))
    return {"revision": checkpoint["revision"], "state": checkpoint["state"]}


def _event(source: str, text: str, observed_ms: int, project: str) -> str:
    return (
        "ev-"
        + hashlib.sha256(f"{source}\n{project}\n{observed_ms}\n{text}".encode()).hexdigest()[:40]
    )


def call(client: LocalClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run a lean tool through the full-profile client."""
    if name == "remember":
        source = arguments.get("source", "agent")
        # Replay needs identical bytes, so the default observed time is the day, not the
        # millisecond, and the derived event id folds the day in; the store's own event
        # log keeps the exact time of the capture.
        observed = arguments.get("observed_ms")
        if observed is None:
            observed = int(time.time() * 1000) // DAY_MS * DAY_MS
        event = arguments.get("event") or _event(
            source, arguments["text"], observed, client.project
        )
        return client.tool(
            "memory_capture",
            {
                "key": event,
                "request": {
                    "source": source,
                    "source_event": event,
                    "text": arguments["text"],
                    "readers": arguments.get("readers"),
                    "classification": arguments.get("classification", "internal"),
                    "observed_at_ms": observed,
                },
            },
        )
    if name == "recall":
        args: dict[str, Any] = {
            "query": arguments["query"],
            "max_output_tokens": arguments.get("budget", 6000),
            "format": arguments.get("format", "text"),
        }
        if "task" in arguments:
            args["task"] = arguments["task"]
        if "as_of" in arguments:
            from memoryking.hooks import as_of_ms

            args["as_of_ms"] = as_of_ms(arguments["as_of"])
        if arguments.get("salience"):
            args["salience"] = "on"
        if arguments.get("rerank"):
            args["rerank"] = "on"
        return client.tool("memory_brief", {"action": "brief", "args": args})
    if name == "save_progress":
        principal = client.binding[2]
        checkpoint: dict[str, Any] = {
            "objective": arguments["objective"],
            "current_state": arguments["current_state"],
        }
        if "next_actions" in arguments:
            checkpoint["next_actions"] = arguments["next_actions"]
        if "hard_constraints" in arguments:
            checkpoint["hard_constraints"] = arguments["hard_constraints"]
        if "findings" in arguments:
            checkpoint["findings"] = [
                {"text": text, "kind": "hypothesis", "evidence": []}
                for text in arguments["findings"]
            ]
        if "failed_attempts" in arguments:
            checkpoint["failed_attempts"] = [
                {
                    "action": item["action"],
                    "outcome": item["outcome"],
                    "do_not_repeat_when": item.get(
                        "do_not_repeat_when", "conditions are unchanged"
                    ),
                }
                for item in arguments["failed_attempts"]
            ]
        if "blockers" in arguments:
            checkpoint["blockers"] = [
                {"text": text, "owner": principal} for text in arguments["blockers"]
            ]
        save_args: dict[str, Any] = {
            "task": arguments["task"],
            "run": arguments.get("run"),
            "checkpoint": checkpoint,
        }
        previous = _latest(client, arguments["task"], arguments.get("run"))
        if previous is not None:
            # Sections the caller left out carry over; sections it sent replace them.
            # Only recorded content carries, so a never-written section stays a gap.
            for section in CARRIED:
                if section not in checkpoint and previous["state"].get(section):
                    checkpoint[section] = previous["state"][section]
            save_args["expected_revision"] = previous["revision"]
        if arguments.get("share_with"):
            save_args["readers"] = sorted({principal, *arguments["share_with"]})
        return client.tool("memory_checkpoint", {"action": "save", "args": save_args})
    if name == "resume":
        return client.tool(
            "memory_resume",
            {
                "action": "resume",
                "args": {
                    "task": arguments["task"],
                    "run": arguments.get("run"),
                    "checkpoint": None,
                    "max_output_tokens": arguments.get("budget", 12000),
                    "format": "text",
                },
            },
        )
    if name == "hand_off":
        latest = client.tool(
            "memory_resume",
            {
                "action": "resume",
                "args": {
                    "task": arguments["task"],
                    "run": arguments.get("run"),
                    "checkpoint": None,
                    "max_output_tokens": 2048,
                },
            },
        )
        # A blocked resume can still carry the caller's own checkpoint; a handoff needs
        # the constraints resolved, so it keeps refusing as before.
        if not latest.get("checkpoint") or latest.get("status") == "blocked":
            raise LocalError("checkpoint_unavailable")
        return client.tool(
            "memory_handoff",
            {
                "action": "prepare",
                "args": {
                    "task": arguments["task"],
                    "run": arguments.get("run"),
                    "checkpoint": latest["checkpoint"]["id"],
                    "recipient": arguments["to"],
                    "role": arguments.get("role", "review"),
                    "stage": arguments.get("stage", "handoff"),
                    "ttl_ms": arguments.get("ttl_minutes", 60) * 60_000,
                },
            },
        )
    raise LocalError("unknown_tool")
