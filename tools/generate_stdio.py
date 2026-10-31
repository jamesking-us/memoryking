"""Compose the bounded local MCP tool surface from authoritative request contracts."""

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    capture["$defs"] = {"Capture": wire["$defs"]["Capture"]}
    tools = [
        {
            "name": "memory_status",
            "description": "Read current authenticated project identity, history and authorization epoch. This does not authorize an inference.",
            "inputSchema": context,
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        },
        {
            "name": "memory_capture",
            "description": "Capture an authorized observation with a stable retry key; no external tool is executed.",
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
            for branch in schema["oneOf"]:
                if branch["properties"]["action"]["const"] not in actions:
                    continue
                branch = copy.deepcopy(branch)
                # The bridge fills the incarnation it is bound to; a caller may still send
                # one, and a stale value is rejected as history_changed like everywhere else.
                if "incarnation" in branch.get("required", []):
                    branch["required"].remove("incarnation")
                branches.append(branch)
            selected = {"type": "object", "oneOf": branches, "$defs": schema["$defs"]}
            tools.append(
                {
                    "name": name,
                    "description": "Authorized MemoryKing "
                    + name.removeprefix("memory_")
                    + ". JSON text is the counted representation; protocol and host wrappers cost additional tokens.",
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
