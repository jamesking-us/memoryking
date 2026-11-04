"""Package the inspection schema, its client types, and its socket operation."""

import copy
import json
from pathlib import Path
from typing import Any

from generate_working import typename

ROOT = Path(__file__).resolve().parents[1]


def generate(wire_bytes: bytes) -> dict[Path, bytes]:
    raw = (ROOT / "contracts/inspection-v1.schema.json").read_bytes()
    schema = json.loads(raw)
    ts = ["// Generated from inspection-v1.schema.json. Do not edit."]
    nodes = {**schema["$defs"], **{f"Inspection{i}": n for i, n in enumerate(schema["oneOf"])}}
    for name, node in nodes.items():
        ts.append(f"export interface {name} {{")
        for key, prop in node["properties"].items():
            optional = "" if key in node["required"] else "?"
            ts.append(f"  {key}{optional}: {typename(prop, 'ts')};")
        ts.extend(["}", ""])
    ts.append(
        "export type InspectionCommand = "
        + " | ".join(f"Inspection{i}" for i in range(len(schema["oneOf"])))
        + ";"
    )
    # The same closed command runs over the local socket as the `inspection`
    # operation, so the CLI and clients can ask what the dashboard can ask.
    wire = json.loads(wire_bytes)
    wire["oneOf"] = [
        n for n in wire["oneOf"] if n["properties"]["operation"]["const"] != "inspection"
    ]
    embedded = copy.deepcopy(schema)
    embedded.pop("$id", None)
    embedded.pop("$schema", None)

    def refs(node: Any) -> None:
        if isinstance(node, dict):
            if "$ref" in node:
                node["$ref"] = node["$ref"].replace("#/$defs/", "#/$defs/Inspection/$defs/")
            for child in node.values():
                refs(child)
        elif isinstance(node, list):
            for child in node:
                refs(child)

    refs(embedded)
    wire["$defs"]["Inspection"] = embedded
    wire["oneOf"].append(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "version": {"const": 1},
                "credential": {"type": "string", "minLength": 1, "maxLength": 8192},
                "operation": {"const": "inspection"},
                "args": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"command": {"$ref": "#/$defs/Inspection"}},
                    "required": ["command"],
                },
            },
            "required": ["version", "credential", "operation", "args"],
        }
    )
    return {
        ROOT / "src/memoryking/adapters/inspection.schema.json": raw,
        ROOT / "packages/dashboard-foundation/src/inspection-generated.ts": (
            "\n".join(ts) + "\n"
        ).encode(),
        ROOT / "contracts/inspection.cli.json": (
            json.dumps({"operation": "inspection", "command_schema": schema}, indent=2) + "\n"
        ).encode(),
        ROOT / "src/memoryking/adapters/local_wire.schema.json": (
            json.dumps(wire, indent=2) + "\n"
        ).encode(),
    }
