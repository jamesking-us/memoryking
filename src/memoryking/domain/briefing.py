"""Deterministic lexical relevance and full-envelope budgeting; no storage/vendors."""

from __future__ import annotations

import copy
import re
from typing import Any, Callable

from memoryking.contracts.canonical import ContractError, canonical_bytes
from memoryking.domain.lexicon import query_window

CONFIGURATION = "lexical-v3"  # bm25 ranking over word forms, per passage
PREVIOUS_CONFIGURATION = "lexical-v2"  # the same ranking over whole observations
LEGACY_CONFIGURATION = "lexical-v1"  # term overlap; a store not rebuilt since then still ranks so
COMPILER = "brief-json-v1"


def term_window(text: str) -> tuple[list[str], bool]:
    """The first QUERY_TERM_LIMIT distinct searchable terms, sorted, plus a truncation flag.

    Earlier words win because a query reads most important first; the caller discloses
    truncation instead of failing a long query.
    """
    return query_window(text)


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


def budget_scope(output: dict[str, Any]) -> str:
    """What the budget was counted against, recorded in the receipt's diagnostics."""
    if output["usage"].get("counted") == "rendered_text":
        return "rendered_text"
    return "failure_envelope" if output["status"] == "budget_insufficient" else "context"


def pack(
    base: dict[str, Any],
    groups: list[list[dict[str, Any]]],
    count: Callable[[str], int],
    budget: int,
    render: Callable[[dict[str, Any]], str] | None = None,
) -> tuple[bytes, int, list[dict[str, Any]], list[str]]:
    """Essential fields are indivisible. Conflicting optional families are atomic groups.

    With `render`, the budget governs the text that function makes of the packet (what a
    model reads for `format: "text"`), not the JSON envelope; see `pack_rendered`.
    """
    if render is not None:
        return pack_rendered(base, groups, count, budget, render)
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


def _round_up(tokens: int, step: int = 50) -> int:
    return -(-tokens // step) * step


def pack_rendered(
    base: dict[str, Any],
    groups: list[list[dict[str, Any]]],
    count: Callable[[str], int],
    budget: int,
    render: Callable[[dict[str, Any]], str],
) -> tuple[bytes, int, list[dict[str, Any]], list[str]]:
    """Pack against the rendered text: `usage.output_tokens` is `count(render(packet))`.

    The text is a function of the packet, so there is no fixed point to search for. When
    optional groups are left out the packet records `omitted_for_budget` (items matched,
    shown, left out, and the budget that would include them) and the text says so; the note
    is part of what is counted, so the result never exceeds the budget it reports.
    """

    def measure(value: dict[str, Any]) -> tuple[bytes, int]:
        used = count(render(value))
        value["usage"]["output_tokens"] = used
        value["usage"]["counted"] = "rendered_text"
        return canonical_bytes(value), used

    value = copy.deepcopy(base)
    value["guidance"] = []
    try:
        raw, used = measure(value)
    except ContractError:
        value.update(status="budget_insufficient", constraints=[], guidance=[], conflicts=[])
        value.update(reason="essential_serialization_limit", minimum_required_tokens=None)
        value["usage"]["budget_compliant"] = False
        raw, used = measure(value)
        return raw, used, [], ["essential_serialization_limit"]
    if used > budget or len(raw) > 262144:
        value.update(status="budget_insufficient", constraints=[], guidance=[], conflicts=[])
        value["reason"] = "essential_set_does_not_fit"
        value["minimum_required_tokens"] = used
        value["usage"]["budget_compliant"] = False
        raw, used = measure(value)
        return raw, used, [], ["essential_set_does_not_fit"]
    if not groups:
        return raw, used, [], []
    everything = [item for group in groups for item in group]
    total = len(everything)
    whole = copy.deepcopy(value)
    whole["guidance"] = everything
    try:
        whole_raw, whole_used = measure(whole)
    except ContractError:
        whole_raw, whole_used = b"", budget + 1
    if whole_used <= budget and len(whole_raw) <= 262144:
        return whole_raw, whole_used, everything, []
    # Leave room for the one-line note about what was left out, then pack greedily.
    needed = _round_up(whole_used + 60)

    def noted(selected: list[dict[str, Any]]) -> dict[str, Any]:
        trial = copy.deepcopy(value)
        trial["guidance"] = list(selected)
        trial["omitted_for_budget"] = {
            "matched": total,
            "shown": len(selected),
            "omitted": total - len(selected),
            "budget_to_include": needed,
        }
        return trial

    selected: list[dict[str, Any]] = []
    omitted: list[str] = []
    best: tuple[bytes, int] | None = None
    for group in groups:
        trial = noted([*selected, *group])
        try:
            trial_raw, trial_used = measure(trial)
        except ContractError:
            omitted.append("optional_serialization_limit")
            continue
        if trial_used <= budget and len(trial_raw) <= 262144:
            selected.extend(group)
            best = (trial_raw, trial_used)
        else:
            omitted.append("optional_group_budget")
    if best is None:
        # Nothing fits beside the note: say that matches existed, in the fullest form that fits.
        empty = noted([])
        empty_raw, empty_used = measure(empty)
        if empty_used <= budget and len(empty_raw) <= 262144:
            return empty_raw, empty_used, [], omitted
        short = copy.deepcopy(value)
        short["omitted_for_budget"] = {"matched": total, "shown": 0, "omitted": total}
        short_raw, short_used = measure(short)
        if short_used <= budget and len(short_raw) <= 262144:
            return short_raw, short_used, [], omitted
        short.update(status="budget_insufficient", constraints=[], guidance=[], conflicts=[])
        short.pop("omitted_for_budget")
        short["reason"] = "essential_set_does_not_fit"
        short["minimum_required_tokens"] = short_used
        short["usage"]["budget_compliant"] = False
        failure_raw, failure_used = measure(short)
        return failure_raw, failure_used, [], [*omitted, "essential_set_does_not_fit"]
    return best[0], best[1], selected, omitted
