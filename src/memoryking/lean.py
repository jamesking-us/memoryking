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
                "supersedes": {
                    **IDENTIFIER,
                    "description": "Id of an earlier memory you can read that this replaces.",
                },
                "scope": {
                    "enum": ["project", "user"],
                    "description": "Where to keep it. `project` (default): this project's own facts. "
                    "`user`: a durable fact or preference about the person you work for that applies "
                    "in every project, such as the tools they want used, how they like changes "
                    "split, or the language to reply in. Never project facts, code or secrets. "
                    "Their other agents will read it, and it counts as a standing instruction only "
                    "once they review it.",
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

TOOLS += [
    {
        "name": "propose",
        "description": (
            "File a lasting fact or rule you believe is true, citing the memories it rests on, "
            "so the user can review it. It becomes a proposal, never accepted knowledge; "
            "nothing changes until the user confirms."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {**TEXT, "description": "The claim, as one clear statement."},
                "evidence": {
                    "type": "array",
                    "items": IDENTIFIER,
                    "minItems": 1,
                    "maxItems": 16,
                    "description": "Ids of the remembered observations that support it.",
                },
            },
            "required": ["text", "evidence"],
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
        "name": "confirm",
        "description": (
            "Record the user's decision on a proposal, after you asked them and they answered. "
            "Quote their answer; never decide for them. Works only when the operator enabled "
            "chat review and your credential may review."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal": {**IDENTIFIER, "description": "The id propose returned."},
                "decision": {"enum": ["accept", "reject"]},
                "user_said": {**TEXT, "maxLength": 1024, "description": "The user's own words."},
            },
            "required": ["proposal", "decision", "user_said"],
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
        "name": "correct",
        "description": (
            "Replace a memory that is wrong or out of date. The old one stays as evidence but "
            "briefings show the new text instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory": {**IDENTIFIER, "description": "Id of the memory to replace."},
                "text": {**TEXT, "maxLength": 65536, "description": "The corrected text."},
            },
            "required": ["memory", "text"],
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
        "name": "forget",
        "description": (
            "Ask for a memory to be retired. You cannot delete: an admin approves, and until "
            "then it is hidden from your briefings and marked for others."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory": {**IDENTIFIER, "description": "Id of the memory."},
                "why": {**TEXT, "maxLength": 2048},
            },
            "required": ["memory", "why"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
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


def _knowledge(
    client: LocalClient, action: str, args: dict[str, Any], key: str | None = None
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "version": 1,
        "incarnation": client.binding[-1],
        "action": action,
        "args": {"project": client.project, **args},
    }
    if key:
        command["key"] = key
    return client.knowledge(command)


def _digest(*parts: Any) -> str:
    return hashlib.sha256("\n".join(str(p) for p in parts).encode()).hexdigest()[:40]


def _evidence(client: LocalClient, observation: str) -> dict[str, Any]:
    """The whole observation as one evidence span, read as the caller (never beyond)."""
    found = client.inspection(
        {
            "version": 1,
            "incarnation": client.binding[-1],
            "action": "catalog",
            "args": {"project": client.project, "id": observation, "limit": 1},
        }
    )["items"]
    if not found:
        raise LocalError("evidence_unavailable")
    return {
        "observation": observation,
        "start_byte": 0,
        "end_byte": found[0]["bytes"],
        "content_hash": found[0]["hash"],
    }


def _propose(client: LocalClient, arguments: dict[str, Any]) -> dict[str, Any]:
    spans = [_evidence(client, o) for o in dict.fromkeys(arguments["evidence"])]
    key = "agent-" + _digest(arguments["text"], *(s["observation"] for s in spans))
    body = {
        "text": arguments["text"],
        "kind": "knowledge",
        "selector": {"selector_version": "1", "any_of": [{"all_of": [{"op": "any"}]}]},
        "valid_start": None,
        "valid_end": None,
        "task": None,
        "run": None,
        "readers": None,
        "evidence": spans,
        "authority": None,
        "rule_key": None,
        "effect": None,
        "rule_value": None,
        "provenance": {
            "method": "agent_proposal_v1",
            "proposed_by": client.binding[2],
            "support": len(spans),
        },
    }
    filed = _knowledge(
        client, "propose", {"logical_key": key, "expected_version": 0, "body": body}, key
    )
    return {
        "proposal": filed["revision"],
        "state": "proposed",
        "note": "Waiting for the user's review; it is not knowledge until accepted.",
    }


def _confirm(client: LocalClient, arguments: dict[str, Any]) -> dict[str, Any]:
    """The user's decision, relayed. Only when the operator enabled chat review, and only
    through the server's own review check; the record names the agent and the proposer."""
    found = _knowledge(
        client,
        "query",
        {
            "bindings": [{}],
            "diagnostic": True,
            "revision": arguments["proposal"],
            "kind": "knowledge",
        },
    )["items"]
    if not found:
        raise LocalError("unavailable")
    item = found[0]
    if item["state"] != "proposed":
        raise LocalError("invalid_transition")
    decision = arguments["decision"]
    key = "chat-" + _digest(arguments["proposal"], decision, arguments["user_said"])
    done = _knowledge(
        client,
        "review",
        {
            "revision": item["revision"],
            "expected_version": item["family_version"],
            "decision": decision,
            "reason": "confirmed_in_chat" if decision == "accept" else "rejected_in_chat",
            "note": "The user said: " + arguments["user_said"],
            "channel": "chat",
            "breadth_reviewed": True,
        },
        key,
    )
    return {"proposal": done["revision"], "state": done["state"]}


def call(
    client: LocalClient,
    name: str,
    arguments: dict[str, Any],
    *,
    chat_review: bool = False,
) -> dict[str, Any]:
    """Run a lean tool through the full-profile client.

    `chat_review` is the operator's switch (the bridge's `--allow-chat-review`); a model
    cannot set it, and `confirm` refuses without it."""
    if name == "propose":
        return _propose(client, arguments)
    if name == "confirm":
        if not chat_review:
            raise LocalError("chat_review_disabled")
        return _confirm(client, arguments)
    if name == "correct":
        return call(
            client,
            "remember",
            {"text": arguments["text"], "supersedes": arguments["memory"]},
        )
    if name == "forget":
        return _knowledge(
            client,
            "retire",
            {"observation": arguments["memory"], "note": arguments["why"]},
            "forget-" + _digest(arguments["memory"], arguments["why"]),
        )
    if name == "remember":
        source = arguments.get("source", "agent")
        # Replay needs identical bytes, so the default observed time is the day, not the
        # millisecond, and the derived event id folds the day in; the store's own event
        # log keeps the exact time of the capture.
        observed = arguments.get("observed_ms")
        if observed is None:
            observed = int(time.time() * 1000) // DAY_MS * DAY_MS
        project = client.user_project() if arguments.get("scope") == "user" else client.project
        event = arguments.get("event") or _event(
            source,
            arguments["text"]
            + (f"\n{arguments['supersedes']}" if "supersedes" in arguments else ""),
            observed,
            project,
        )
        return client.tool(
            "memory_capture",
            {
                "key": event,
                "request": {
                    **({"project": project} if project != client.project else {}),
                    "source": source,
                    "source_event": event,
                    "text": arguments["text"],
                    "readers": arguments.get("readers"),
                    "classification": arguments.get("classification", "internal"),
                    "observed_at_ms": observed,
                    **(
                        {"supersedes": arguments["supersedes"]} if "supersedes" in arguments else {}
                    ),
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
