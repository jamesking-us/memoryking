"""Generate closed repository contracts and compose the latest local wire schema."""

import copy
import json
from pathlib import Path
from typing import Any

from generate_working import typename

ROOT = Path(__file__).resolve().parents[1]


def generate(wire_bytes: bytes) -> dict[Path, bytes]:
    raw = (ROOT / "contracts/repository-v1.schema.json").read_bytes()
    schema = json.loads(raw)
    py = [
        '"""Generated repository requests; runtime schema is authoritative."""',
        "from __future__ import annotations",
        "",
        "from typing import Any, Literal, TypedDict",
        "",
        "",
    ]
    ts = ["// Generated repository requests; runtime schema is authoritative.", ""]
    nodes = {**schema["$defs"], **{f"Command{i}": n for i, n in enumerate(schema["oneOf"])}}
    for name, node in nodes.items():
        py.append(f"class {name}(TypedDict):")
        ts.append(f"export interface {name} {{")
        for key, value in node["properties"].items():
            ptype = typename(value, "py").replace("Literal[true]", "Literal[True]")
            py.append(f"    {key}: {ptype}")
            ts.append(f"  {key}: {typename(value, 'ts')};")
        py.append("")
        ts.extend(["}", ""])
    names = " | ".join(f"Command{i}" for i in range(len(schema["oneOf"])))
    py.append("RepositoryCommand = " + names)
    ts.append("export type RepositoryCommand = " + names + ";")
    wire = json.loads(wire_bytes)
    wire["title"] = "Local wire request v1 with P06 repository extensions"
    for operation in wire["oneOf"]:
        if operation["properties"]["operation"]["const"] == "grant":
            caps = operation["properties"]["args"]["properties"]["capabilities"]["items"]["enum"]
            if "repository" not in caps:
                caps.append("repository")
    wire["oneOf"] = [
        n for n in wire["oneOf"] if n["properties"]["operation"]["const"] != "repository"
    ]
    embedded = copy.deepcopy(schema)
    embedded.pop("$id", None)
    embedded.pop("$schema", None)

    def refs(node: Any) -> None:
        if isinstance(node, dict):
            if "$ref" in node:
                node["$ref"] = node["$ref"].replace("#/$defs/", "#/$defs/Repository/$defs/")
            for child in node.values():
                refs(child)
        elif isinstance(node, list):
            for child in node:
                refs(child)

    refs(embedded)
    wire["$defs"]["Repository"] = embedded
    wire["oneOf"].append(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "version": {"const": 1},
                "credential": {"type": "string", "minLength": 1, "maxLength": 8192},
                "operation": {"const": "repository"},
                "args": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"command": {"$ref": "#/$defs/Repository"}},
                    "required": ["command"],
                },
            },
            "required": ["version", "credential", "operation", "args"],
        }
    )

    def dump(value: Any) -> bytes:
        return (json.dumps(value, indent=2) + "\n").encode()

    return {
        ROOT / "src/memoryking/adapters/repository.schema.json": raw,
        ROOT / "src/memoryking/contracts/repository.schema.json": raw,
        ROOT / "packages/contracts/repository.schema.json": raw,
        ROOT / "src/memoryking/contracts/repository_generated.py": ("\n".join(py) + "\n").encode(),
        ROOT / "packages/contracts/src/repository-generated.ts": ("\n".join(ts) + "\n").encode(),
        ROOT / "contracts/repository.cli.json": dump(
            {"operation": "repository", "command_schema": schema}
        ),
        ROOT / "src/memoryking/adapters/local_wire.schema.json": dump(wire),
    }
