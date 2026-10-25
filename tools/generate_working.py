"""Generate working-state validators/types/CLI contracts from one authoritative schema."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def typename(node: dict[str, Any], language: str) -> str:
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[1]
    if "anyOf" in node:
        return " | ".join(typename(n, language) for n in node["anyOf"])
    if "enum" in node or "const" in node:
        values = [json.dumps(v) for v in node.get("enum", [node.get("const")])]
        return "Literal[" + ", ".join(values) + "]" if language == "py" else " | ".join(values)
    if node["type"] == "array":
        child = typename(node["items"], language)
        return f"list[{child}]" if language == "py" else f"Array<{child}>"
    return {
        "string": ("str", "string"),
        "integer": ("int", "number"),
        "boolean": ("bool", "boolean"),
        "null": ("None", "null"),
        "object": ("dict[str, Any]", "Record<string, unknown>"),
    }[node["type"]][language == "ts"]


def generate() -> dict[Path, bytes]:
    raw = (ROOT / "contracts/working-v1.schema.json").read_bytes()
    schema = json.loads(raw)
    py = [
        '"""Generated working-state request types; runtime schema is authoritative."""',
        "from __future__ import annotations",
        "",
        "from typing import Any, Literal, NotRequired, TypedDict",
        "",
        "",
    ]
    ts = ["// Generated working-state requests; runtime schema is authoritative.", ""]
    nodes = {**schema["$defs"], **{f"Command{i}": n for i, n in enumerate(schema["oneOf"])}}
    for name, node in nodes.items():
        py.append(f"class {name}(TypedDict):")
        ts.append(f"export interface {name} {{")
        for key, value in node["properties"].items():
            optional = key not in node["required"]
            ptype = typename(value, "py")
            py.append(f"    {key}: {'NotRequired[' + ptype + ']' if optional else ptype}")
            ts.append(f"  {key}{'?' if optional else ''}: {typename(value, 'ts')};")
        py.append("")
        ts.extend(["}", ""])
    names = " | ".join(f"Command{i}" for i in range(len(schema["oneOf"])))
    py.append("WorkingCommand = " + names)
    ts.append("export type WorkingCommand = " + names + ";")
    wire_path = ROOT / "src/memoryking/adapters/local_wire.schema.json"
    wire = json.loads(wire_path.read_text())
    wire["oneOf"] = [n for n in wire["oneOf"] if n["properties"]["operation"]["const"] != "working"]
    embedded = copy.deepcopy(schema)
    embedded.pop("$id", None)
    embedded.pop("$schema", None)

    def refs(node: Any) -> None:
        if isinstance(node, dict):
            if "$ref" in node:
                node["$ref"] = node["$ref"].replace("#/$defs/", "#/$defs/Working/$defs/")
            for val in node.values():
                refs(val)
        elif isinstance(node, list):
            for val in node:
                refs(val)

    refs(embedded)
    wire.setdefault("$defs", {})["Working"] = embedded
    wire["oneOf"].append(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "version": {"const": 1},
                "credential": {"type": "string", "minLength": 1, "maxLength": 8192},
                "operation": {"const": "working"},
                "args": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"command": {"$ref": "#/$defs/Working"}},
                    "required": ["command"],
                },
            },
            "required": ["version", "credential", "operation", "args"],
        }
    )
    wire["oneOf"] = [n for n in wire["oneOf"] if n["properties"]["operation"]["const"] != "context"]
    wire["oneOf"].append(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "version": {"const": 1},
                "credential": {"type": "string", "minLength": 1, "maxLength": 8192},
                "operation": {"const": "context"},
                "args": json.loads((ROOT / "contracts/context-v1.schema.json").read_text()),
            },
            "required": ["version", "credential", "operation", "args"],
        }
    )
    wire["title"] = "Local wire request v1 with P05 working-state extensions"

    def dump(value: Any) -> bytes:
        return (json.dumps(value, indent=2) + "\n").encode()

    return {
        ROOT / "src/memoryking/adapters/working.schema.json": raw,
        ROOT / "src/memoryking/contracts/working.schema.json": raw,
        ROOT / "src/memoryking/contracts/working_generated.py": ("\n".join(py) + "\n").encode(),
        ROOT / "packages/contracts/working.schema.json": raw,
        ROOT / "packages/contracts/src/working-generated.ts": ("\n".join(ts) + "\n").encode(),
        ROOT / "contracts/working.cli.json": dump(
            {"operation": "working", "command_schema": schema}
        ),
        wire_path: dump(wire),
    }
