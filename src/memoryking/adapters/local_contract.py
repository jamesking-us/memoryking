"""One closed JSON Schema for P02 socket requests and capture payloads."""

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError

SCHEMA = json.loads(Path(__file__).with_name("local_wire.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)
CAPTURE = Draft202012Validator(SCHEMA["$defs"]["Capture"])


def validate_capture(value: dict[str, Any]) -> None:
    canonical_bytes(value)
    if not CAPTURE.is_valid(value) or len(value["text"].encode()) > 65536:
        raise LocalError("invalid_request")
