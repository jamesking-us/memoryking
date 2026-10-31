"""Deterministic lexical relevance and full-envelope budgeting; no storage/vendors."""

from __future__ import annotations

import copy
import re
from typing import Any, Callable

from memoryking.contracts.canonical import ContractError, canonical_bytes

CONFIGURATION = "lexical-v1"
COMPILER = "brief-json-v1"
STOP = frozenset(
    "a an and are as at be by for from in is it of on or that the this to with".split()
)


QUERY_TERM_LIMIT = 32


def term_window(text: str) -> tuple[list[str], bool]:
    """The first QUERY_TERM_LIMIT distinct searchable terms, sorted, plus a truncation flag.

    Earlier words win because a query reads most important first; the caller discloses
    truncation instead of failing a long query.
    """
    seen: dict[str, None] = {}
    for term in re.findall(r"[^\W_]+", text.casefold()):
        if term not in STOP:
            seen.setdefault(term, None)
    return sorted(list(seen)[:QUERY_TERM_LIMIT]), len(seen) > QUERY_TERM_LIMIT


def terms(text: str) -> list[str]:
    return term_window(text)[0]


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
