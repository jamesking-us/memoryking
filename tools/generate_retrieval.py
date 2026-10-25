"""Generate transport contracts and lightweight types from retrieval-v1.schema.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def kind(node: dict[str, Any], language: str) -> str:
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[1]
    if "enum" in node:
        values = [json.dumps(v) for v in node["enum"]]
        return "Literal[" + ", ".join(values) + "]" if language == "py" else " | ".join(values)
    if node["type"] == "array":
        child = kind(node["items"], language)
        return f"list[{child}]" if language == "py" else f"Array<{child}>"
    return {
        "string": ("str", "string"),
        "integer": ("int", "number"),
        "object": ("dict[str, Any]", "Record<string, unknown>"),
    }[node["type"]][language == "ts"]


def generate() -> dict[Path, bytes]:
    raw = (ROOT / "contracts/retrieval-v1.schema.json").read_bytes()
    schema = json.loads(raw)
    py = [
        '"""Generated retrieval request types. Runtime schema validation is authoritative."""',
        "from __future__ import annotations",
        "",
        "from typing import Any, Literal, NotRequired, TypedDict",
        "",
        "",
    ]
    ts = ["// Generated retrieval types. Runtime schema validation is authoritative.", ""]
    for name, node in schema["$defs"].items():
        py.append(f"class {name}(TypedDict):")
        ts.append(f"export interface {name} {{")
        for key, value in node["properties"].items():
            required = key in node["required"]
            typename = kind(value, "py")
            py.append(f"    {key}: {typename if required else 'NotRequired[' + typename + ']'}")
            ts.append(f"  {key}{'' if required else '?'}: {kind(value, 'ts')};")
        py.append("")
        ts.extend(["}", ""])
    py.append(
        "RetrievalCommand = " + " | ".join("Command" + str(i) for i in range(len(schema["oneOf"])))
    )
    commands = []
    for i, node in enumerate(schema["oneOf"]):
        action = node["properties"]["action"]["const"]
        ref = node["properties"]["args"]["$ref"].rsplit("/", 1)[1]
        commands.extend(
            [
                f"class Command{i}(TypedDict):",
                "    version: Literal[1]",
                "    incarnation: str",
                f"    action: Literal[{json.dumps(action)}]",
                f"    args: {ref}",
                "",
            ]
        )
        ts.extend(
            [
                f"export interface Command{i} {{",
                "  version: 1;",
                "  incarnation: string;",
                f"  action: {json.dumps(action)};",
                f"  args: {ref};",
                "}",
                "",
            ]
        )
    # Runtime aliases are evaluated, so define discriminated branches before the union.
    union = py.pop()
    py.extend([*commands, union])
    ts.append(
        "export type RetrievalCommand = "
        + " | ".join("Command" + str(i) for i in range(len(schema["oneOf"])))
        + ";"
    )

    def standalone(node: dict[str, Any]) -> dict[str, Any]:
        return {"$schema": schema["$schema"], **node, "$defs": schema["$defs"]}

    mcp = {
        "schema_version": 1,
        "transport_status": "descriptors_only_stdio_runtime_P05",
        "tools": [],
    }
    for name, actions in [
        ("memory_brief", {"brief", "rerun"}),
        ("memory_fetch", {"fetch", "receipt", "replay", "receipts"}),
    ]:
        mcp["tools"].append(
            {
                "name": name,
                "description": "MemoryKing authorized "
                + name.removeprefix("memory_")
                + ". Returned JSON text is the counted representation; additional protocol wrappers cost extra tokens.",
                "inputSchema": standalone(
                    {
                        "oneOf": [
                            n
                            for n in schema["oneOf"]
                            if n["properties"]["action"]["const"] in actions
                        ]
                    }
                ),
            }
        )
    openapi = {
        "openapi": "3.1.0",
        "info": {"title": "MemoryKing local retrieval", "version": "1"},
        "paths": {
            "/api/v1/retrieval": {
                "post": {
                    "security": [{"bearer": []}],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": schema}},
                    },
                    "responses": {
                        "200": {
                            "description": "Canonical JSON; brief/fetch honor token budgets, atlas honors byte/node/edge limits; replay is original bytes. X-MemoryKing-Access-Receipt identifies the new access receipt."
                        },
                        "403": {"description": "Structured non-disclosing error"},
                    },
                }
            }
        },
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
    }

    def dump(v):
        return (json.dumps(v, indent=2) + "\n").encode()

    return {
        ROOT / "src/memoryking/adapters/retrieval.schema.json": raw,
        ROOT / "src/memoryking/contracts/retrieval.schema.json": raw,
        ROOT / "src/memoryking/contracts/retrieval_generated.py": (
            "\n".join(py).rstrip() + "\n"
        ).encode(),
        ROOT / "packages/contracts/src/retrieval-generated.ts": (
            "\n".join(ts).rstrip() + "\n"
        ).encode(),
        ROOT / "packages/contracts/retrieval.schema.json": raw,
        ROOT / "contracts/retrieval.openapi.json": dump(openapi),
        ROOT / "contracts/retrieval.mcp.json": dump(mcp),
        ROOT / "contracts/retrieval.cli.json": dump(
            {
                "operation": "retrieval",
                "args": {"command_schema": schema},
                "stdout": "canonical JSON plus newline framing",
            }
        ),
    }
