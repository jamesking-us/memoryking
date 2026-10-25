"""Bounded dictionary deltas over canonical adapter state; arrays are atomic."""

from __future__ import annotations

import copy
from typing import Any

from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.local import LocalError

SCHEMA = "adapter-state-v1"
SERIALIZATION = "MK-C14N-1"
MAX_CHANGES = 256
MAX_BYTES = 262144


def changes(base: dict[str, Any], target: dict[str, Any]) -> list[dict[str, Any]]:
    """Sorted recursive dictionary updates, with no executable/path expressions."""
    result: list[dict[str, Any]] = []
    work = 0

    def walk(left: Any, right: Any, path: list[str]) -> None:
        nonlocal work
        work += 1
        if work > 16384 or len(path) > 32 or len(result) > MAX_CHANGES:
            raise LocalError("delta_limit")
        if canonical_bytes(left) == canonical_bytes(right):
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() - right.keys()):
                result.append({"op": "remove", "path": [*path, key]})
            for key in sorted(right):
                if key not in left:
                    result.append(
                        {"op": "set", "path": [*path, key], "value": copy.deepcopy(right[key])}
                    )
                else:
                    walk(left[key], right[key], [*path, key])
        else:
            result.append({"op": "set", "path": path, "value": copy.deepcopy(right)})

    if max(len(canonical_bytes(base)), len(canonical_bytes(target))) > MAX_BYTES:
        raise LocalError("delta_limit")
    walk(base, target, [])
    if len(result) > MAX_CHANGES or len(canonical_bytes(result)) > MAX_BYTES:
        raise LocalError("delta_limit")
    return result


def apply(base: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(operations, list):
        raise LocalError("delta_invalid")
    if len(operations) > MAX_CHANGES or len(canonical_bytes(operations)) > MAX_BYTES:
        raise LocalError("delta_limit")
    result = copy.deepcopy(base)
    seen: list[tuple[str, ...]] = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise LocalError("delta_invalid")
        kind, path = operation.get("op"), operation.get("path")
        fields = {"op", "path", "value"} if kind == "set" else {"op", "path"}
        if (
            kind not in {"set", "remove"}
            or set(operation) != fields
            or not isinstance(path, list)
            or not 1 <= len(path) <= 32
            or any(not isinstance(key, str) or len(key) > 256 for key in path)
        ):
            raise LocalError("delta_invalid")
        route = tuple(path)
        if any(
            route[: len(previous)] == previous or previous[: len(route)] == route
            for previous in seen
        ):
            raise LocalError("delta_invalid")
        seen.append(route)
        parent = result
        for key in path[:-1]:
            if (
                not isinstance(parent, dict)
                or key not in parent
                or not isinstance(parent[key], dict)
            ):
                raise LocalError("delta_invalid")
            parent = parent[key]
        if kind == "remove":
            if path[-1] not in parent:
                raise LocalError("delta_invalid")
            del parent[path[-1]]
        else:
            parent[path[-1]] = copy.deepcopy(operation["value"])
    if len(canonical_bytes(result)) > MAX_BYTES:
        raise LocalError("delta_limit")
    return result


def materialize(
    envelope: dict[str, Any], base: dict[str, Any] | None, binding: dict[str, Any]
) -> dict[str, Any]:
    if len(canonical_bytes(envelope)) > MAX_BYTES:
        raise LocalError("delta_limit")
    if (
        envelope.get("schema") != SCHEMA
        or envelope.get("serialization") != SERIALIZATION
        or canonical_hash(envelope.get("binding")) != canonical_hash(binding)
    ):
        raise LocalError("delta_binding_mismatch")
    if type(envelope.get("sequence")) is not int or envelope["sequence"] < 1:
        raise LocalError("delta_sequence_mismatch")
    if envelope.get("mode") == "full":
        target = copy.deepcopy(envelope["target"])
    elif envelope.get("mode") == "delta":
        if base is None or canonical_hash(base) != envelope["base_hash"]:
            raise LocalError("delta_base_mismatch")
        if envelope["sequence"] != base["sequence"] + 1:
            raise LocalError("delta_sequence_mismatch")
        target = apply(base, envelope["changes"])
    else:
        raise LocalError("delta_invalid")
    if (
        not isinstance(target, dict)
        or set(target) != {"sequence", "packet"}
        or not isinstance(target["packet"], dict)
        or type(target["sequence"]) is not int
        or target["sequence"] != envelope["sequence"]
        or canonical_hash(target) != envelope["target_hash"]
    ):
        raise LocalError("delta_target_mismatch")
    return target
