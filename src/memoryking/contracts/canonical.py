"""MK-C14N-1: a bounded, integer-only canonical JSON profile, not RFC 8785."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MAX_BYTES = 1_048_576
MAX_DEPTH = 64
MAX_INTEGER = 9_007_199_254_740_991

# C-level search preserves MK-C14N-1 rejection without a Python call per scalar.
_SURROGATE = re.compile(r"[\ud800-\udfff]")


class ContractError(ValueError):
    """Invalid input; messages contain no caller-supplied content."""


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ContractError("duplicate_key")
        result[key] = value
    return result


def _not_integer(value: str) -> None:
    raise ContractError("integer_profile_required")


def _check(value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ContractError("document_too_deep")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > MAX_INTEGER:
            raise ContractError("unsafe_integer")
    elif isinstance(value, str):
        if _SURROGATE.search(value) is not None:
            raise ContractError("invalid_unicode")
    elif isinstance(value, list):
        for item in value:
            _check(item, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError("non_string_key")
            _check(key, depth)
            _check(item, depth + 1)
    else:
        raise ContractError("unsupported_value")


def load_strict(raw: str | bytes) -> Any:
    """Reject duplicates, decimals, nonfinite values, unsafe integers and bad Unicode."""
    try:
        data = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(data) > MAX_BYTES:
            raise ContractError("document_too_large")
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_float=_not_integer,
            parse_constant=_not_integer,
        )
        _check(value)
        return value
    except ContractError:
        raise
    except (UnicodeError, RecursionError, ValueError) as exc:
        raise ContractError("invalid_json") from exc


def canonical_bytes(value: Any) -> bytes:
    """Preserve string content/array order; sort keys by Unicode scalar value."""
    _check(value)
    result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(result) > MAX_BYTES:
        raise ContractError("document_too_large")
    return result


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
