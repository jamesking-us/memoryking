"""Use the packaged authoritative schema; never infer authorization from validation."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator

from .canonical import ContractError, canonical_bytes


@lru_cache
def schema() -> dict[str, Any]:
    return json.loads(files(__package__).joinpath("schema.json").read_text())  # type: ignore[no-any-return]


def validate_contract(name: str, value: Any) -> None:
    """Validate a closed contract after applying the common JSON safety profile."""
    canonical_bytes(value)
    document = schema()
    if name not in document["$defs"]:
        raise ContractError("unknown_contract")
    validator = Draft202012Validator({**document, "$ref": f"#/$defs/{name}"})
    if next(validator.iter_errors(value), None) is not None:
        raise ContractError("schema_invalid")


def validate_retrieval(value: Any) -> None:
    """Shared request syntax only; services additionally apply bound/context/access rules."""
    canonical_bytes(value)
    document = json.loads(files(__package__).joinpath("retrieval.schema.json").read_text())
    if not Draft202012Validator(document).is_valid(value):
        raise ContractError("schema_invalid")


def validate_working(value: Any) -> None:
    """Closed checkpoint/operation syntax; authorization is a separate service gate."""
    canonical_bytes(value)
    document = json.loads(files(__package__).joinpath("working.schema.json").read_text())
    if not Draft202012Validator(document).is_valid(value):
        raise ContractError("schema_invalid")


def validate_handoff(value: Any) -> None:
    """Closed handoff syntax only; recipient and lifecycle checks belong to the service."""
    canonical_bytes(value)
    document = json.loads(files(__package__).joinpath("handoff.schema.json").read_text())
    if not Draft202012Validator(document).is_valid(value):
        raise ContractError("schema_invalid")


def validate_inference(value: Any) -> None:
    """Closed trusted dispatch syntax; a manifest alone is not authorization."""
    canonical_bytes(value)
    document = json.loads(files(__package__).joinpath("inference.schema.json").read_text())
    if not Draft202012Validator(document).is_valid(value):
        raise ContractError("schema_invalid")


def validate_repository(value: Any) -> None:
    """Closed repository syntax; trusted capture and evidence remain service concerns."""
    canonical_bytes(value)
    document = json.loads(files(__package__).joinpath("repository.schema.json").read_text())
    if not Draft202012Validator(document).is_valid(value):
        raise ContractError("schema_invalid")


def validate_embedding(value: Any) -> None:
    """Optional projection administration; this syntax never grants a worker capability."""
    canonical_bytes(value)
    document = json.loads(files(__package__).joinpath("embedding.schema.json").read_text())
    if not Draft202012Validator(document).is_valid(value):
        raise ContractError("schema_invalid")


def validate_processing(value: Any) -> None:
    """Optional proposal and compiler syntax; no provider authority is implied."""
    canonical_bytes(value)
    document = json.loads(files(__package__).joinpath("processing.schema.json").read_text())
    if not Draft202012Validator(document).is_valid(value):
        raise ContractError("schema_invalid")


def validate_experience(value: Any) -> None:
    """Optional proposal and compiler syntax; no provider authority is implied."""
    canonical_bytes(value)
    document = json.loads(files(__package__).joinpath("experience.schema.json").read_text())
    if not Draft202012Validator(document).is_valid(value):
        raise ContractError("schema_invalid")
