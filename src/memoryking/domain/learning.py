"""Bounded experimental feedback ranking; no online updates or causal inference."""

from __future__ import annotations

from typing import Any

from memoryking.domain.local import LocalError

POLICY = "bounded-feedback-v1"
MAX_ADJUSTMENT = 100000
LABELS = {"helpful": 100000, "irrelevant": -25000, "misleading": -75000, "harmful": -100000}


def freeze(feedback: list[dict[str, Any]], bound: int) -> dict[str, int]:
    if not 0 <= bound <= MAX_ADJUSTMENT or len(feedback) > 128:
        raise LocalError("ranking_limit")
    # One latest judgment per actor/run/item; average within each actor first.
    # Exposure, read/used without usefulness, and unknown are never positive votes.
    votes: dict[tuple[str, str], list[int]] = {}
    seen: set[tuple[str, str, str, str]] = set()
    for item in feedback:
        key = (item["actor"], item["task"], item["run"], item["reference"])
        if key in seen:
            raise LocalError("ranking_duplicate_vote")
        seen.add(key)
        label = item["usefulness"]
        if label not in LABELS:
            continue
        votes.setdefault((item["actor"], item["reference"]), []).append(LABELS[label])
    actors: dict[str, list[int]] = {}
    for (_, reference), values in votes.items():
        actors.setdefault(reference, []).append(sum(values) // len(values))
    if len(actors) > 64:
        raise LocalError("ranking_limit")
    return {
        reference: max(-bound, min(bound, sum(values) // len(values)))
        for reference, values in sorted(actors.items())
    }


def adjust(
    ranked: list[tuple[int, str, list[dict[str, Any]]]], adjustments: dict[str, int]
) -> list[tuple[int, str, list[dict[str, Any]]]]:
    result = []
    for score, reference, group in ranked:
        values = [adjustments.get(item["reference"], 0) for item in group]
        delta = sum(values) // len(values) if values else 0
        if abs(delta) > MAX_ADJUSTMENT:
            raise LocalError("ranking_limit")
        # No relevance floor is crossed, and conflict groups remain indivisible.
        updated = max(1, score * (1000000 + delta) // 1000000) if score > 0 else 0
        result.append((updated, reference, group))
    return sorted(result, key=lambda item: (-item[0], item[1]))


def measure(selected: list[str], relevant: list[str]) -> dict[str, int]:
    actual, expected = set(selected), set(relevant)
    hits = len(actual & expected)
    return {
        "recall_ppm": hits * 1000000 // len(expected) if expected else int(not actual) * 1000000,
        "precision_ppm": hits * 1000000 // len(actual) if actual else int(not expected) * 1000000,
    }
