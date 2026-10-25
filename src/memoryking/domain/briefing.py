"""Deterministic lexical relevance and full-envelope budgeting; no storage/vendors."""

from __future__ import annotations

import copy
import re
from typing import Any, Callable

from memoryking.contracts.canonical import ContractError, canonical_bytes
from memoryking.domain.local import LocalError

CONFIGURATION = "lexical-v1"
COMPILER = "brief-json-v1"
STOP = frozenset(
    "a an and are as at be by for from in is it of on or that the this to with".split()
)


def terms(text: str) -> list[str]:
    result = sorted({t for t in re.findall(r"[^\W_]+", text.casefold()) if t not in STOP})
    if len(result) > 32:
        raise LocalError("query_term_limit")
    return result


def relevance(query: list[str], text: str) -> int:
    """Literal normalized term overlap in millionths, never a calibrated probability."""
    words = set(re.findall(r"[^\W_]+", text.casefold()))
    return 0 if not query else len(set(query) & words) * 1000000 // len(query)


def counted(value: dict[str, Any], count: Callable[[str], int]) -> tuple[bytes, int]:
    """Count exact serialized bytes, including usage; bounded fixed point with honest fallback."""
    value = copy.deepcopy(value)
    value["usage"]["output_tokens"] = 0
    seen: set[int] = set()
    for _ in range(32):
        raw = canonical_bytes(value)
        actual = count(raw.decode())
        if actual == value["usage"]["output_tokens"]:
            return raw, actual
        if actual in seen:
            break
        seen.add(actual)
        value["usage"]["output_tokens"] = actual
    # BPE counts need not be monotonic in the decimal counter. The receipt has the
    # final exact count; never publish a knowingly incorrect self-referential value.
    value["usage"]["output_tokens"] = None
    value["usage"]["count_location"] = "receipt"
    raw = canonical_bytes(value)
    return raw, count(raw.decode())


def pack(
    base: dict[str, Any],
    groups: list[list[dict[str, Any]]],
    count: Callable[[str], int],
    budget: int,
) -> tuple[bytes, int, list[dict[str, Any]], list[str]]:
    """Essential fields are indivisible. Conflicting optional families are atomic groups."""
    value = copy.deepcopy(base)
    value["guidance"] = []
    try:
        raw, used = counted(value, count)
    except ContractError:
        value.update(status="budget_insufficient", constraints=[], guidance=[], conflicts=[])
        value.update(reason="essential_serialization_limit", minimum_required_tokens=None)
        value["usage"]["budget_compliant"] = False
        raw, used = counted(value, count)
        return raw, used, [], ["essential_serialization_limit"]
    if used > budget or len(raw) > 262144:
        value.update(status="budget_insufficient", constraints=[], guidance=[], conflicts=[])
        value["reason"] = "essential_set_does_not_fit"
        value["minimum_required_tokens"] = used
        value["usage"]["budget_compliant"] = False
        raw, used = counted(value, count)
        return raw, used, [], ["essential_set_does_not_fit"]
    selected: list[dict[str, Any]] = []
    omitted: list[str] = []
    for group in groups:
        trial = copy.deepcopy(value)
        trial["guidance"] = [*selected, *group]
        try:
            trial_raw, trial_used = counted(trial, count)
        except ContractError:
            omitted.append("optional_serialization_limit")
            continue
        if trial_used <= budget and len(trial_raw) <= 262144:
            selected.extend(group)
            value = trial
            raw, used = trial_raw, trial_used
        else:
            omitted.append("optional_group_budget")
    # Status is chosen conservatively before packing; omitted relevant guidance
    # cannot convert a partial retrieval into a claim of calibrated sufficiency.
    return raw, used, selected, omitted
