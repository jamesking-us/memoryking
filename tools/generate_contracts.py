#!/usr/bin/env python3
"""Generate foundation types and packaged schemas; --check rejects source drift."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from generate_embedding import generate as generate_embedding
from generate_experience import generate as generate_experience
from generate_handoff import generate as generate_handoff
from generate_inference import generate as generate_inference
from generate_inspection import generate as generate_inspection
from generate_processing import generate as generate_processing
from generate_repository import generate as generate_repository
from generate_retrieval import generate as generate_retrieval
from generate_stdio import generate as generate_stdio
from generate_viewer import generate as generate_viewer
from generate_working import generate as generate_working

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "contracts/foundation-v1.schema.json"


def type_name(node: dict, language: str) -> str:
    if "$ref" in node:
        return node["$ref"].split("/")[-1]
    if "const" in node or "enum" in node:
        values = node.get("enum", [node.get("const")])
        literals = ", ".join(json.dumps(v) for v in values)
        return f"Literal[{literals}]" if language == "py" else " | ".join(map(json.dumps, values))
    if "anyOf" in node:
        return " | ".join(type_name(part, language) for part in node["anyOf"])
    kind = node.get("type")
    if kind == "array":
        child = type_name(node["items"], language)
        return f"list[{child}]" if language == "py" else f"Array<{child}>"
    primitives = {
        "string": ("str", "string"),
        "integer": ("int", "number"),
        "boolean": ("bool", "boolean"),
        "null": ("None", "null"),
    }
    if kind not in primitives:
        raise ValueError(f"Unsupported schema type: {kind}")
    return primitives[kind][language == "ts"]


def generate() -> dict[Path, bytes]:
    raw = SOURCE.read_bytes()
    schema = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    py = [
        f'"""Generated; source SHA256 {digest}. Do not edit."""',
        "from __future__ import annotations",
        "",
        "from typing import Literal, NotRequired, TypedDict",
        "",
    ]
    ts = [f"// Generated; source SHA256 {digest}. Do not edit.", ""]
    for name, node in schema["$defs"].items():
        if node.get("type") == "object":
            py.append(f"class {name}(TypedDict):")
            ts.append(f"export interface {name} {{")
            for key, prop in node["properties"].items():
                optional = key not in node["required"]
                ptype = type_name(prop, "py")
                py.append(f"    {key}: {'NotRequired[' + ptype + ']' if optional else ptype}")
                ts.append(f"  {key}{'?' if optional else ''}: {type_name(prop, 'ts')};")
            ts.append("}")
        else:
            py.append(f"{name} = {type_name(node, 'py')}")
            ts.append(f"export type {name} = {type_name(node, 'ts')};")
        py.append("")
        ts.append("")
    working = generate_working()
    handoff = generate_handoff(working[ROOT / "src/memoryking/adapters/local_wire.schema.json"])
    inference = generate_inference(handoff[ROOT / "src/memoryking/adapters/local_wire.schema.json"])
    repository = generate_repository(
        inference[ROOT / "src/memoryking/adapters/local_wire.schema.json"]
    )
    embedding = generate_embedding(
        repository[ROOT / "src/memoryking/adapters/local_wire.schema.json"]
    )
    processing = generate_processing(
        embedding[ROOT / "src/memoryking/adapters/local_wire.schema.json"]
    )
    experience = generate_experience(
        processing[ROOT / "src/memoryking/adapters/local_wire.schema.json"]
    )
    return {
        **generate_inspection(),
        **generate_retrieval(),
        **working,
        **handoff,
        **inference,
        **repository,
        **embedding,
        **processing,
        **experience,
        **generate_viewer(experience[ROOT / "src/memoryking/adapters/local_wire.schema.json"]),
        **generate_stdio(),
        ROOT / "src/memoryking/contracts/generated.py": ("\n".join(py).rstrip() + "\n").encode(),
        ROOT / "packages/contracts/src/generated.ts": ("\n".join(ts).rstrip() + "\n").encode(),
        ROOT / "src/memoryking/contracts/schema.json": raw,
        ROOT / "packages/contracts/schema.json": raw,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for path, data in generate().items():
        if args.check:
            if not path.exists() or path.read_bytes() != data:
                raise SystemExit(f"Generated artifact drift: {path.relative_to(ROOT)}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    print("Contract artifacts match" if args.check else "Contract artifacts generated")
