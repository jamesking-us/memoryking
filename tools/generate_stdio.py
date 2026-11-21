"""Compose the bounded local MCP tool surface from authoritative request contracts."""

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


DEFAULTED = ("project", "bindings", "max_output_tokens", "tokenizer")
# Per tool, further fields the bridge fills: a checkpoint saves from a minimal body.
TOOL_DEFAULTED = {
    "memory_checkpoint": ("run", "readers", "expected_revision", "checkpoint_sections"),
    "memory_resume": ("run", "checkpoint"),
    # Nullable fields whose null means "none" or "the latest": a call that leaves them out is
    # completed with null (src/memoryking/sdk.py NULL_DEFAULTS), not refused.
    "memory_handoff": ("run",),
    "memory_plan": ("decision", "revision", "cursor"),
    "memory_feedback": ("comment",),
}

# What a model needs to know to fill a field, by name. Applied where the contract schema has
# no description of its own, to the fields directly under a tool call and its `args`.
FIELD_DESCRIPTIONS = {
    "key": "Retry key: repeat it to replay the first result instead of repeating the write. "
    "Minted when omitted.",
    "args": "Arguments of the chosen action.",
    "request": "The observation to record.",
    "project": "Project id. Defaults to this session's project.",
    "incarnation": "History id. Defaults to this session's.",
    "version": "Contract version, 1. Defaults to 1.",
    "query": "What you are about to work on, as a short phrase.",
    "task": "Task id, stable across sessions.",
    "run": "Run id within the task, or null.",
    "bindings": "Where this session works (environment, role, path ...). Defaults to the session's.",
    "max_output_tokens": "Token budget for the reply. Defaults to 6000.",
    "tokenizer": "Tokenizer used to count the budget. Defaults to the installed one.",
    "as_of_ms": "Brief as the project stood at this Unix-millisecond time; omit for now.",
    "format": "json for a program, text for a Markdown reply.",
    "consistency": "Whether to wait for recent writes before answering.",
    "freshness": "How recent the indexed data must be.",
    "dense": "Whether to use meaning-based search when it is available.",
    "salience": "Let your own recent use of memories nudge the ranking.",
    "rerank": "Let the local cross-encoder reorder the top candidates.",
    "top_k": "How many candidates to consider.",
    "recorded_position": "Answer as of this recorded history position.",
    "valid_ms": "Judge validity at this Unix-millisecond time.",
    "status": "Filter by status.",
    "brief": "The briefing request to run again with a new query.",
    "predecessor": "Receipt id of the earlier briefing this one follows.",
    "receipt": "Receipt id returned with an earlier briefing or fetch.",
    "reference": "Id of the record: an obs_ observation id or a claim revision id.",
    "kind": "observation or revision (claim).",
    "start_byte": "First byte of the range to read.",
    "end_byte": "End byte of the range to read.",
    "rendered_hash": "Hash of the rendered text being replayed.",
    "checkpoint": "The progress to save: objective and current_state are required, the rest optional.",
    "expected_revision": "The revision you last saw, so a concurrent save conflicts instead of "
    "overwriting; null accepts the current one.",
    "readers": "Principals allowed to read it; null means everyone in the project.",
    "handle": "Handoff handle returned by prepare.",
    "materialization": "Id of the materialized handoff.",
    "recipient": "Principal who will receive the handoff.",
    "role": "The recipient's role for this handoff.",
    "stage": "The stage of work being handed off.",
    "ttl_ms": "How long the handoff stays valid, in milliseconds.",
    "plan": "The whole plan: phases and steps with stable ids.",
    "step": "Id of the plan step to update.",
    "decision": "A recorded decision for the step, or null.",
    "evidence": "Evidence for the step: observation ids and spans.",
    "notes": "Notes on the step.",
    "revision": "Plan revision to read; null for the current one.",
    "cursor": "Page cursor from the previous listing; null for the first page.",
    "limit": "How many to return.",
    "logical_key": "Stable name of the record across revisions.",
    "body": "The outcome record: task result with its acceptance evidence.",
    "use": "Whether the item was read or used: unknown, read or used.",
    "usefulness": "unknown, helpful, irrelevant, misleading or harmful.",
    "comment": "A short note, or null.",
    "source": "Where it came from, such as pytest or git.",
    "source_event": "Stable id of the event, so a retry replays.",
    "text": "What you observed.",
    "classification": "public, internal or restricted.",
    "observed_at_ms": "When you observed it, in Unix milliseconds.",
    "supersedes": "Id of an earlier memory you can read that this replaces.",
}
ACTION_DESCRIPTIONS = {
    "brief": "Ask for a briefing.",
    "rerun": "Run an earlier briefing again with a new query.",
    "fetch": "Read a cited source in full or by byte range.",
    "receipt": "Show the diagnostics behind a receipt.",
    "replay": "Return the exact bytes delivered for a receipt.",
    "receipts": "List recent receipts.",
    "save": "Save a checkpoint.",
    "resume": "Pick up a saved task.",
    "plan_save": "Create or revise the whole plan.",
    "plan_step": "Update one step.",
    "plan_get": "Read a plan.",
    "plan_list": "List the project's plans.",
    "prepare": "Prepare a handoff from a checkpoint.",
    "preview": "Preview what the recipient would receive.",
    "materialize": "Materialize the handoff as the recipient.",
    "ack": "Acknowledge a materialized handoff.",
    "cancel": "Cancel a handoff.",
    "outcome": "Record the result of a run with its evidence.",
    "feedback": "Rate whether a specific memory helped.",
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
    "memory_plan": (
        "Track a multi-step plan on a task: `plan_save` creates or revises the whole plan "
        "(phases, steps with stable ids, prerequisites), `plan_step` updates one step's "
        "status, notes and evidence, `plan_get` reads the current or an earlier revision "
        "with computed progress, `plan_list` pages the project's plans. A step counts as "
        "done only with evidence; a changed definition needs fresh evidence. Progress is "
        "recorded work, not independent certification."
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


def reachable(node, defs, found=None):
    """The names of the $defs a schema node uses, following references transitively."""
    found = found if found is not None else set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/")
            if name not in found:
                found.add(name)
                reachable(defs[name], defs, found)
        for value in node.values():
            reachable(value, defs, found)
    elif isinstance(node, list):
        for value in node:
            reachable(value, defs, found)
    return found


def prune(schema):
    """Drop the $defs that no branch of the tool's schema refers to."""
    defs = schema.get("$defs")
    if not defs:
        return schema
    body = {k: v for k, v in schema.items() if k != "$defs"}
    used = reachable(body, defs)
    schema["$defs"] = {name: defs[name] for name in defs if name in used}
    if not schema["$defs"]:
        del schema["$defs"]
    return schema


def describe(node, defs, names):
    """Give the properties of `node` (following $ref) the description for their name."""
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        node = defs[ref.removeprefix("#/$defs/")]
    for field, value in node.get("properties", {}).items():
        if isinstance(value, dict) and "description" not in value and field in names:
            value["description"] = names[field]


def document(tools):
    """Describe every field a model fills directly under a call and under its `args`."""
    for tool in tools:
        schema = tool["inputSchema"]
        defs = schema.get("$defs", {})
        for branch in schema.get("oneOf") or [schema]:
            describe(branch, defs, FIELD_DESCRIPTIONS)
            action = branch.get("properties", {}).get("action")
            if isinstance(action, dict) and action.get("const") in ACTION_DESCRIPTIONS:
                action["description"] = ACTION_DESCRIPTIONS[action["const"]]
            args = branch.get("properties", {}).get("args")
            if isinstance(args, dict):
                describe(args, defs, FIELD_DESCRIPTIONS)
        for name in ("request",):
            nested = schema.get("properties", {}).get(name)
            if isinstance(nested, dict):
                describe(nested, defs, FIELD_DESCRIPTIONS)


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
        (
            "working",
            [
                ("memory_checkpoint", {"save"}, False),
                ("memory_resume", {"resume"}, True),
                ("memory_plan", {"plan_save", "plan_step", "plan_get", "plan_list"}, False),
            ],
        ),
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
    for tool in tools:
        prune(tool["inputSchema"])
    document(tools)
    raw = (
        json.dumps(
            {"protocol_version": "2025-11-25", "transport": "stdio", "tools": tools}, indent=2
        )
        + "\n"
    ).encode()
    sys.path.insert(0, str(ROOT / "src"))
    from memoryking import lean

    lean_raw = (json.dumps(lean.CATALOG, indent=2) + "\n").encode()
    return {
        ROOT / "contracts/stdio.tools.json": raw,
        ROOT / "src/memoryking/adapters/stdio.tools.json": raw,
        ROOT / "contracts/stdio.tools.lean.json": lean_raw,
    }
