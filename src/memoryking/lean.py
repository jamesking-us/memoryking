"""The lean MCP profile: flat tools for any host that launches stdio servers.

Each tool maps onto the full catalog through the bound client, so authorization,
receipts, idempotency and budgets are exactly those of the full profile. The schemas are
small and have no `oneOf`, so a model learns them in a few hundred tokens.

`call` returns the full profile's envelope unchanged; `present` turns it into what a model
should read (the Markdown briefing, or a one-line confirmation with an id) plus the
envelope for machines.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from memoryking.contracts.canonical import canonical_bytes
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

TOOLS += [
    {
        "name": "fetch",
        "description": (
            "Read the exact text behind a citation: pass an id from a briefing's Sources "
            "(obs_... or a claim id), and a byte range to read part of a long record. "
            "Receipted like a briefing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {**IDENTIFIER, "description": "Id from a Sources line."},
                "start_byte": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 65536,
                    "description": "First byte. Default 0.",
                },
                "end_byte": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 65536,
                    "description": "End byte. Default 16384.",
                },
            },
            "required": ["reference"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "feedback",
        "description": (
            "Say whether a recalled item helped, after you used it: it tunes later "
            "briefings and shows in memory health. Pass the receipt id from the briefing's "
            "status line and the item's id or tag (g1)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "receipt": {**IDENTIFIER, "description": "rcpt_... id from the briefing."},
                "item": {**IDENTIFIER, "description": "Id from Sources, or a tag such as g1."},
                "rating": {"enum": ["helpful", "not_helpful", "harmful"]},
                "comment": {**TEXT, "maxLength": 2048},
                "task": {**IDENTIFIER, "description": "The task id the briefing was for, if any."},
            },
            "required": ["receipt", "item", "rating"],
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
        "name": "recent",
        "description": (
            "List the newest memories you may read in this project, with ids, so you can "
            "correct, forget or fetch one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "How many. Default 5.",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]

# `confirm` records a user's decision through the agent, which only works when the operator
# started the bridge with chat review; without it the tool is left out of the catalog.
CHAT_REVIEW_TOOLS = {"confirm"}


def tools(chat_review: bool = False) -> list[dict[str, Any]]:
    """The catalog a bridge serves: every tool, minus `confirm` unless chat review is on."""
    return [t for t in TOOLS if chat_review or t["name"] not in CHAT_REVIEW_TOOLS]


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


FETCH_SPAN = 16384  # the most one fetch returns


def _size(client: LocalClient, reference: str) -> int:
    """The record's size in bytes as the caller may read it (never beyond)."""
    observation = reference.startswith("obs_")
    found = client.inspection(
        {
            "version": 1,
            "incarnation": client.binding[-1],
            "action": "catalog",
            "args": {
                "project": client.project,
                "id": reference,
                "limit": 1,
                **({} if observation else {"kind": "claims"}),
            },
        }
    )["items"]
    if not found:
        raise LocalError("unavailable")
    item = found[0]
    return int(item["bytes"] if "bytes" in item else len(str(item.get("text", "")).encode()))


def _fetch(client: LocalClient, arguments: dict[str, Any]) -> dict[str, Any]:
    """Open one cited record; a range past the end is cut to the end, not refused."""
    reference = arguments["reference"]
    start = arguments.get("start_byte", 0)
    size = _size(client, reference)
    end = min(arguments.get("end_byte", start + FETCH_SPAN), size, start + FETCH_SPAN)
    if end <= start:
        raise LocalError("invalid_range")
    return client.tool(
        "memory_fetch",
        {
            "action": "fetch",
            "args": {
                "reference": reference,
                "kind": "observation" if reference.startswith("obs_") else "revision",
                "start_byte": start,
                "end_byte": end,
            },
        },
    )


RATINGS = {
    "helpful": ("used", "helpful"),
    "not_helpful": ("read", "irrelevant"),
    "harmful": ("used", "harmful"),
}
TAG = re.compile(r"^[gc][0-9]{1,3}$")


def _feedback(client: LocalClient, arguments: dict[str, Any]) -> dict[str, Any]:
    """Rate one item of a briefing the caller received, tied to that briefing's receipt."""
    receipt, item = arguments["receipt"], arguments["item"]
    if TAG.match(item):
        # `g1` means the first guidance item of that briefing; the retained briefing says which.
        packet = client.tool("memory_fetch", {"action": "replay", "args": {"receipt": receipt}})
        found = next((s for s in packet.get("sources", []) if s.get("ref") == item), None)
        if found is None:
            raise LocalError("feedback_reference_not_retrieved")
        item = found["id"]
    use, usefulness = RATINGS[arguments["rating"]]
    task, run = arguments.get("task", "general"), "main"
    body = {
        "receipt": receipt,
        "reference": item,
        "task": task,
        "run": run,
        "use": use,
        "usefulness": usefulness,
        "comment": arguments.get("comment"),
        "readers": [client.binding[2]],
    }
    key = "fb-" + _digest(receipt, item, task, arguments["rating"], arguments.get("comment"))
    expected = 0
    for _ in range(2):
        try:
            return client.tool(
                "memory_feedback",
                {
                    "action": "feedback",
                    "key": key + f"-{expected}",
                    "args": {**body, "expected_revision": expected},
                },
            )
        except LocalError as exc:
            if exc.code != "experience_revision_conflict" or expected:
                raise
            # This item was rated before in this task: revise that vote.
            expected = _feedback_revision(client, task, run, item)
    raise LocalError("experience_revision_conflict")


def _feedback_revision(client: LocalClient, task: str, run: str, item: str) -> int:
    listing = client.experience(
        {
            "version": 1,
            "incarnation": client.binding[-1],
            "action": "records",
            "args": {"project": client.project, "kind": "feedback"},
        }
    )
    revisions = [
        entry["revision"]
        for entry in listing.get("items", [])
        if entry["actor"] == client.binding[2]
        and entry["body"].get("reference") == item
        and entry["body"].get("task") == task
        and entry["body"].get("run") == run
    ]
    return max(revisions, default=0)


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
    if name == "fetch":
        return _fetch(client, arguments)
    if name == "recent":
        return client.inspection(
            {
                "version": 1,
                "incarnation": client.binding[-1],
                "action": "catalog",
                "args": {"project": client.project, "limit": arguments.get("limit", 5)},
            }
        )
    if name == "feedback":
        return _feedback(client, arguments)
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
        event = arguments.get("event") or _event(
            source,
            arguments["text"]
            + (f"\n{arguments['supersedes']}" if "supersedes" in arguments else ""),
            observed,
            client.project,
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


# --- What a model reads ------------------------------------------------------------------

EXCERPT = 160


def _line(text: str) -> str:
    return " ".join(text.split())


def present(
    name: str, result: dict[str, Any], *, replayed: bool = False
) -> tuple[str, dict[str, Any] | None]:
    """The text a model reads and the envelope for machines (`structuredContent`).

    A briefing or a resume is its own Markdown, with the envelope (receipt, status, usage,
    warnings, sources) beside it, not repeated inside it. A write is one line with an id and
    a `replayed` flag. Anything else keeps the envelope as its text."""
    if name in {"recall", "resume"} and isinstance(result.get("text"), str):
        return result["text"], {k: v for k, v in result.items() if k != "text"}
    if name in {"remember", "correct"} and "observation" in result:
        ident = result["observation"]
        verb = "correction" if name == "correct" else "memory"
        line = (
            f"Already saved as {ident}; the same text from the same source today replays."
            if replayed
            else f"Saved {verb} as {ident}."
        )
        return line, {"id": ident, "replayed": replayed, **result}
    if name == "save_progress" and "checkpoint" in result:
        ident, replayed = result["checkpoint"], bool(result.get("replayed"))
        line = (
            f"Progress {ident} was already saved (revision {result.get('revision')})."
            if replayed
            else f"Saved progress as {ident}, revision {result.get('revision')}."
        )
        return line, {"id": ident, "replayed": replayed, **result}
    if name == "fetch" and isinstance(result.get("excerpt"), str):
        head = (
            f"{result.get('reference')} bytes {result.get('start_byte')}-{result.get('end_byte')}"
            f" of {result.get('source_bytes')}"
        )
        return head + "\n\n" + result["excerpt"], result
    if name == "feedback" and "record" in result:
        return f"Recorded feedback {result['record']} (revision {result.get('revision')}).", result
    if name == "recent" and "items" in result:
        rows = [
            {
                "id": item["id"],
                "source": item.get("source"),
                "recorded_ms": item.get("recorded_ms"),
                "bytes": item.get("bytes"),
                "excerpt": _line(str(item.get("text", "")))[:EXCERPT],
            }
            for item in result["items"]
        ]
        lines = [f"- {row['id']} ({row['source']}): {row['excerpt']}" for row in rows]
        return (
            "\n".join(lines) if lines else "No memories yet in this project.",
            {"items": rows},
        )
    return canonical_bytes(result).decode(), None  # the text is already the envelope


def invoke(
    client: LocalClient, name: str, arguments: dict[str, Any], *, chat_review: bool = False
) -> tuple[str, dict[str, Any] | None]:
    """`call` and `present` together, with the one fact `present` cannot see: whether a
    `remember` was a replay (the observation was recorded before this call began)."""
    started = int(time.time() * 1000)
    result = call(client, name, arguments, chat_review=chat_review)
    replayed = False
    if name in {"remember", "correct"} and "observation" in result:
        found = client.inspection(
            {
                "version": 1,
                "incarnation": client.binding[-1],
                "action": "catalog",
                "args": {
                    "project": client.project,
                    "id": result["observation"],
                    "limit": 1,
                    "text": False,
                },
            }
        )["items"]
        replayed = bool(found) and found[0]["recorded_ms"] < started - 5
    return present(name, result, replayed=replayed)
