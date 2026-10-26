"""Closed viewer/report contracts; validation never authenticates a subject."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError

SCHEMAS = {
    stem: json.loads(Path(__file__).with_name(f"{stem}.schema.json").read_text())
    for stem in ("viewer", "reporting")
}


def validate_viewer(name: str, value: dict[str, Any], *, reporting: bool = False) -> None:
    canonical_bytes(value)
    schema = SCHEMAS["reporting" if reporting else "viewer"]
    if name not in schema["$defs"]:
        raise LocalError("invalid_contract")
    selected = {"$defs": schema["$defs"], "$ref": f"#/$defs/{name}"}
    if not Draft202012Validator(selected).is_valid(value):
        raise LocalError("invalid_viewer_request")
