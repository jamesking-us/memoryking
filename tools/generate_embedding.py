"""Generate closed embedding contracts and compose the latest local wire schema."""

import copy
import json
from pathlib import Path
from typing import Any

from generate_working import typename

ROOT = Path(__file__).resolve().parents[1]


def generate(wire_bytes: bytes) -> dict[Path, bytes]:
    raw = (ROOT / "contracts/embedding-v1.schema.json").read_bytes()
    schema = json.loads(raw)
    py = [
        '"""Generated embedding requests; runtime schema is authoritative."""',
        "from __future__ import annotations",
        "",
        "from typing import Literal, TypedDict",
        "",
        "",
    ]
    ts = ["// Generated embedding requests; runtime schema is authoritative.", ""]
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
    py.append("EmbeddingCommand = " + names)
    ts.append("export type EmbeddingCommand = " + names + ";")
    wire = json.loads(wire_bytes)
    wire["title"] = "Local wire request v1 with P08 embedding extensions"
    wire["oneOf"] = [
        n for n in wire["oneOf"] if n["properties"]["operation"]["const"] != "embedding"
    ]
    embedded = copy.deepcopy(schema)
    embedded.pop("$id", None)
    embedded.pop("$schema", None)

    def refs(node: Any) -> None:
        if isinstance(node, dict):
            if "$ref" in node:
                node["$ref"] = node["$ref"].replace("#/$defs/", "#/$defs/Embedding/$defs/")
            for child in node.values():
                refs(child)
        elif isinstance(node, list):
            for child in node:
                refs(child)

    refs(embedded)
    wire["$defs"]["Embedding"] = embedded
    wire["oneOf"].append(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "version": {"const": 1},
                "credential": {"type": "string", "minLength": 1, "maxLength": 8192},
                "operation": {"const": "embedding"},
                "args": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"command": {"$ref": "#/$defs/Embedding"}},
                    "required": ["command"],
                },
            },
            "required": ["version", "credential", "operation", "args"],
        }
    )

    def dump(value: Any) -> bytes:
        return (json.dumps(value, indent=2) + "\n").encode()

    return {
        ROOT / "src/memoryking/adapters/embedding.schema.json": raw,
        ROOT / "src/memoryking/contracts/embedding.schema.json": raw,
        ROOT / "packages/contracts/embedding.schema.json": raw,
        ROOT / "src/memoryking/contracts/embedding_generated.py": ("\n".join(py) + "\n").encode(),
        ROOT / "packages/contracts/src/embedding-generated.ts": ("\n".join(ts) + "\n").encode(),
        ROOT / "contracts/embedding.cli.json": dump(
            {"operation": "embedding", "command_schema": schema}
        ),
        ROOT / "src/memoryking/adapters/local_wire.schema.json": dump(wire),
    }
