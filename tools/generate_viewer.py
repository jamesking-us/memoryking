"""Generate mobile/report contract types and extend only the privileged local wire."""

import copy
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def kind(node: dict[str, Any], language: str) -> str:
    if "$ref" in node:
        return str(node["$ref"]).rsplit("/", 1)[-1]
    if "oneOf" in node or "anyOf" in node:
        return " | ".join(kind(n, language) for n in node.get("oneOf", node.get("anyOf", [])))
    if "const" in node or "enum" in node:
        values = node.get("enum", [node.get("const")])
        if language == "py":
            return "Literal[" + ", ".join(repr(x) for x in values) + "]"
        return " | ".join(json.dumps(x) for x in values)
    if node.get("type") == "array":
        child = kind(node["items"], language)
        return f"list[{child}]" if language == "py" else f"Array<{child}>"
    return {
        "string": ("str", "string"),
        "integer": ("int", "number"),
        "boolean": ("bool", "boolean"),
        "null": ("None", "null"),
    }[node["type"]][language == "ts"]


def generate(wire_bytes: bytes) -> dict[Path, bytes]:
    outputs: dict[Path, bytes] = {}
    schemas = {}
    for stem in ("viewer", "reporting"):
        raw = (ROOT / f"contracts/{stem}-v1.schema.json").read_bytes()
        schema = json.loads(raw)
        schemas[stem] = schema
        py = [
            '"""Generated from closed authoritative mobile contracts."""',
            "from __future__ import annotations",
            "",
            "from typing import Literal, NotRequired, TypedDict"
            if any(
                set(n.get("properties", {})) - set(n.get("required", []))
                for n in schema["$defs"].values()
            )
            else "from typing import Literal, TypedDict",
            "",
            "",
        ]
        ts = ["// Generated from closed authoritative mobile contracts.", ""]
        for name, node in schema["$defs"].items():
            if node.get("type") == "object":
                py.append(f"class {name}(TypedDict):")
                if not node["properties"]:
                    py.append("    pass")
                ts.append(f"export interface {name} {{")
                for field, value in node["properties"].items():
                    optional = field not in node.get("required", [])
                    pytype = kind(value, "py")
                    if optional:
                        pytype = f"NotRequired[{pytype}]"
                    py.append(f"    {field}: {pytype}")
                    ts.append(f"  {field}{'?' if optional else ''}: {kind(value, 'ts')};")
                ts.append("}")
            else:
                py.append(f"{name} = {kind(node, 'py')}")
                ts.append(f"export type {name} = {kind(node, 'ts')};")
            py.append("")
            ts.append("")
        outputs[ROOT / f"src/memoryking/contracts/{stem}.schema.json"] = raw
        outputs[ROOT / f"packages/contracts/{stem}.schema.json"] = raw
        outputs[ROOT / f"src/memoryking/contracts/{stem}_generated.py"] = (
            "\n".join(py).rstrip() + "\n"
        ).encode()
        outputs[ROOT / f"packages/contracts/src/{stem}-generated.ts"] = (
            "\n".join(ts).rstrip() + "\n"
        ).encode()
    wire = json.loads(wire_bytes)
    for node in wire["oneOf"]:
        if node["properties"]["operation"]["const"] == "grant":
            caps = node["properties"]["args"]["properties"]["capabilities"]["items"]["enum"]
            for cap in schemas["reporting"]["$defs"]["ReportCapabilities"]["enum"]:
                if cap not in caps:
                    caps.append(cap)
    wire["oneOf"] = [
        node
        for node in wire["oneOf"]
        if node["properties"]["operation"]["const"] not in {"viewer_admin", "report"}
    ]
    for stem, group, operation, command in (
        ("viewer", "Viewer", "viewer_admin", "ViewerAdminCommand"),
        ("reporting", "Reporting", "report", "ReportCommand"),
    ):
        embedded = copy.deepcopy(schemas[stem])
        embedded.pop("$id")
        embedded.pop("$schema")

        def refs(node: Any, group: str = group) -> None:
            if isinstance(node, dict):
                if "$ref" in node:
                    node["$ref"] = node["$ref"].replace("#/$defs/", f"#/$defs/{group}/$defs/")
                for child in node.values():
                    refs(child, group)
            elif isinstance(node, list):
                for child in node:
                    refs(child, group)

        refs(embedded)
        wire["$defs"][group] = embedded
        wire["oneOf"].append(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "version": {"const": 1},
                    "credential": {"type": "string", "minLength": 1, "maxLength": 8192},
                    "operation": {"const": operation},
                    "args": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"command": {"$ref": f"#/$defs/{group}/$defs/{command}"}},
                        "required": ["command"],
                    },
                },
                "required": ["version", "credential", "operation", "args"],
            }
        )
    outputs[ROOT / "src/memoryking/adapters/local_wire.schema.json"] = (
        json.dumps(wire, indent=2) + "\n"
    ).encode()
    return outputs
