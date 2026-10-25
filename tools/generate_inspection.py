"""Package the closed browser-only inspection schema and its client types."""

import json
from pathlib import Path

from generate_working import typename

ROOT = Path(__file__).resolve().parents[1]


def generate() -> dict[Path, bytes]:
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
    return {
        ROOT / "src/memoryking/adapters/inspection.schema.json": raw,
        ROOT / "packages/dashboard-foundation/src/inspection-generated.ts": (
            "\n".join(ts) + "\n"
        ).encode(),
    }
