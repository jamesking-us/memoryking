"""Package the closed delegated observation-read wire contracts."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def generate(wire_bytes: bytes) -> dict[Path, bytes]:
    raw = (ROOT / "contracts/viewer-v1.schema.json").read_bytes()
    schema = json.loads(raw)
    wire = json.loads(wire_bytes)
    operations = {n["properties"]["operation"]["const"] for n in schema["oneOf"]}
    wire["oneOf"] = [
        n for n in wire["oneOf"] if n["properties"]["operation"]["const"] not in operations
    ]
    wire["oneOf"].extend(schema["oneOf"])

    # Capability vocabulary is shared with existing grant/delegate schemas.
    def capabilities(node):
        if isinstance(node, dict):
            values = node.get("enum")
            if (
                isinstance(values, list)
                and "capture" in values
                and "read" in values
                and "viewer_exchange" not in values
            ):
                values.append("viewer_exchange")
            for value in node.values():
                capabilities(value)
        elif isinstance(node, list):
            for value in node:
                capabilities(value)

    capabilities(wire)
    return {
        ROOT / "src/memoryking/adapters/viewer.schema.json": raw,
        ROOT / "src/memoryking/adapters/local_wire.schema.json": (
            json.dumps(wire, indent=2) + "\n"
        ).encode(),
    }
