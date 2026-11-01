"""One closed JSON Schema for P02 socket requests and capture payloads."""

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError

SCHEMA = json.loads(Path(__file__).with_name("local_wire.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)


def _operations(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        operation = (
            node.get("properties", {}).get("operation")
            if isinstance(node.get("properties"), dict)
            else None
        )
        if isinstance(operation, dict):
            found.update(
                [operation["const"]] if "const" in operation else operation.get("enum", [])
            )
        for value in node.values():
            found |= _operations(value)
    elif isinstance(node, list):
        for value in node:
            found |= _operations(value)
    return found


# The closed operation vocabulary of the wire schema: the only names metrics and trace
# lines may carry.
OPERATIONS = frozenset(_operations(SCHEMA))
CAPTURE = Draft202012Validator(SCHEMA["$defs"]["Capture"])


def validate_capture(value: dict[str, Any]) -> None:
    canonical_bytes(value)
    if not CAPTURE.is_valid(value) or len(value["text"].encode()) > 65536:
        raise LocalError("invalid_request")
