"""Compose the bounded local MCP tool surface from authoritative request contracts."""

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


DEFAULTED = ("project", "bindings", "max_output_tokens", "tokenizer")
# Per tool, further fields the bridge fills: a checkpoint saves from a minimal body.
TOOL_DEFAULTED = {
    "memory_checkpoint": ("run", "readers", "expected_revision", "checkpoint_sections"),
    "memory_resume": ("run",),
}

DESCRIPTIONS = {
    "memory_status": (
        "Check what this session is bound to: store, tenant, principal, project and the "
        "current history id. Call it first in a session, or when a call fails with "
        "history_changed, before retrying anything. Read-only; it never authorizes a model "
        "request."
    ),
    "memory_capture": (
        "Remember something you observed: an error, a test result, a decision, a fact "
        "with its source. Give a stable `key` and `request.source_event` for the same "
        "event so a retry replays instead of duplicating; the same key with different "
        "text is a conflict. Text is stored as untrusted evidence, not as an instruction."
    ),
    "memory_brief": (
        "Ask for a briefing before starting work: `brief` with a short query returns "
        "mandatory constraints, ranked guidance with sources and a receipt. Read "
        "`status` and `reason` first: `blocked` or `budget_insufficient` means decide, not "
        "truncate. `rerun` repeats an earlier receipt with a new query. Project, bindings, "
        "budget and tokenizer default to the session's."
    ),
    "memory_fetch": (
        "Follow up on a briefing you already have: `fetch` more of a source, `receipt` "
        "the diagnostics behind a receipt, `replay` the exact bytes that were delivered, "
        "`receipts` to list recent ones. Replays can report expired or purged; that is "
        "an answer, not an error to retry."
    ),
    "memory_checkpoint": (
        "Save progress before you stop or hand over: objective, current state, next "
        "actions, failed attempts, blockers and verification gaps, under a project, task "
        "and expected revision. Use the exact revision you last saw; a conflict means "
        "someone else saved first, so resume and merge rather than overwrite."
    ),
    "memory_resume": (
        "Pick up a saved task: returns the checkpoint, open operations and the context "
        "you are still authorized to see, within a token budget. Read `status`; a "
        "`blocked` or `partial` packet lists what is missing. Budget and tokenizer "
        "default to the session's."
    ),
    "memory_handoff": (
        "Pass a task to another principal with their current permissions: `prepare` a "
        "handoff from a checkpoint, `preview` what the recipient would receive, "
        "`materialize` it as the recipient, `ack` or `cancel`. A handoff never widens "
        "access; what the recipient cannot see is left out and reported."
    ),
    "memory_feedback": (
        "Report what happened after using memory: `outcome` records the result of a run "
        "with its evidence, `feedback` rates whether a specific memory helped. This "
        "informs ranking; it cannot activate a procedure or change policy."
    ),
}


def relax(node: dict, defs: dict, extra: tuple = (), seen: set | None = None) -> None:
    """Make fields the bridge can default optional, following $ref and a nested brief."""
    seen = seen if seen is not None else set()
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        name = ref.removeprefix("#/$defs/")
        if name in seen:
            return
        seen.add(name)
        relax(defs[name], defs, extra, seen)
        return
    required = node.get("required")
    if isinstance(required, list):
        for field in DEFAULTED + extra:
            if field in required:
                required.remove(field)
    nested = node.get("properties", {}).get("brief")
    if isinstance(nested, dict):
        relax(nested, defs, extra, seen)
    if "checkpoint_sections" in extra:
        checkpoint = node.get("properties", {}).get("checkpoint")
        if isinstance(checkpoint, dict):
            relax_checkpoint(checkpoint, defs)


def relax_checkpoint(node: dict, defs: dict) -> None:
    """Only objective and current_state stay required; the bridge completes the rest."""
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        node = defs[ref.removeprefix("#/$defs/")]
    if isinstance(node.get("required"), list):
        node["required"] = [f for f in node["required"] if f in ("objective", "current_state")]


def generate():
    wire = json.loads((ROOT / "src/memoryking/adapters/local_wire.schema.json").read_text())
    context = json.loads((ROOT / "contracts/context-v1.schema.json").read_text())
    capture = next(
        b["properties"]["args"]
        for b in wire["oneOf"]
        if b["properties"]["operation"]["const"] == "capture"
    )
    capture = copy.deepcopy(capture)
    del capture["properties"]["namespace"]
    capture["required"].remove("namespace")
    capture["$defs"] = {"Capture": copy.deepcopy(wire["$defs"]["Capture"])}
    # The bridge fills `request.version` and `request.project` from its binding.
    for field in ("version", "project"):
        capture["$defs"]["Capture"]["required"].remove(field)
    context = copy.deepcopy(context)
    if "project" in context.get("required", []):
        context["required"].remove("project")
    tools = [
        {
            "name": "memory_status",
            "description": DESCRIPTIONS["memory_status"],
            "inputSchema": context,
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        },
        {
            "name": "memory_capture",
            "description": DESCRIPTIONS["memory_capture"],
            "inputSchema": capture,
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        },
    ]
    for source, groups in [
        (
            "handoff",
            [("memory_handoff", {"prepare", "materialize", "preview", "ack", "cancel"}, False)],
        ),
        (
            "retrieval",
            [
                ("memory_brief", {"brief", "rerun"}, True),
                ("memory_fetch", {"fetch", "receipt", "replay", "receipts"}, True),
            ],
        ),
        ("working", [("memory_checkpoint", {"save"}, False), ("memory_resume", {"resume"}, True)]),
        ("experience", [("memory_feedback", {"outcome", "feedback"}, False)]),
    ]:
        schema = json.loads((ROOT / f"contracts/{source}-v1.schema.json").read_text())
        for name, actions, read_only in groups:
            branches = []
            defs = copy.deepcopy(schema["$defs"])
            for branch in schema["oneOf"]:
                if branch["properties"]["action"]["const"] not in actions:
                    continue
                branch = copy.deepcopy(branch)
                # The bridge fills the incarnation it is bound to; a caller may still send
                # one, and a stale value is rejected as history_changed like everywhere else.
                # Likewise version, project, bindings, token budget and tokenizer default
                # to the binding and the installed tokenizer when omitted.
                # A missing retry key is minted by the bridge; callers who need an exact
                # replay on retry still pass their own.
                for field in ("incarnation", "version", "key"):
                    if field in branch.get("required", []):
                        branch["required"].remove(field)
                relax(branch["properties"].get("args", {}), defs, TOOL_DEFAULTED.get(name, ()))
                branches.append(branch)
            selected = {"type": "object", "oneOf": branches, "$defs": defs}
            tools.append(
                {
                    "name": name,
                    "description": DESCRIPTIONS[name],
                    "inputSchema": selected,
                    "annotations": {
                        "readOnlyHint": read_only,
                        "destructiveHint": False,
                        "idempotentHint": not read_only,
                        "openWorldHint": False,
                    },
                }
            )
    raw = (
        json.dumps(
            {"protocol_version": "2025-11-25", "transport": "stdio", "tools": tools}, indent=2
        )
        + "\n"
    ).encode()
    return {
        ROOT / "contracts/stdio.tools.json": raw,
        ROOT / "src/memoryking/adapters/stdio.tools.json": raw,
    }
