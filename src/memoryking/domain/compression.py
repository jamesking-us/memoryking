"""Conservative whole-unit validation; no model can certify its own semantics."""

from __future__ import annotations

from typing import Any

from memoryking.contracts.canonical import canonical_bytes, canonical_hash

VALIDATOR = "whole-authorized-unit-v1"


def critical(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        key: packet.get(key)
        for key in ("checkpoint", "constraints", "operations", "recovery_gaps", "status", "reason")
    }


def extractive(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate exact text, retaining every source attribution and unit order."""
    quotes: list[dict[str, Any]] = []
    by_text: dict[str, dict[str, Any]] = {}
    for unit in units:
        text = unit["text"]
        if text not in by_text:
            quote = {"observations": [], "text": text}
            quotes.append(quote)
            by_text[text] = quote
        by_text[text]["observations"].append(unit["observation"])
    return quotes


def validate(
    units: list[dict[str, Any]], packet: dict[str, Any], candidate: dict[str, Any] | None
) -> dict[str, Any]:
    quotes = extractive(units)
    state_hash, source_hash = canonical_hash(critical(packet)), canonical_hash(units)
    reason = "no_candidate"
    if candidate is not None:
        provenance = candidate["provenance"]
        if candidate["critical_hash"] != state_hash:
            reason = "critical_state_mismatch"
        elif provenance["input_hash"] != source_hash:
            reason = "source_manifest_mismatch"
        elif provenance["output_hash"] != canonical_hash(candidate["quotes"]):
            reason = "output_hash_mismatch"
        elif canonical_bytes(candidate["quotes"]) != canonical_bytes(quotes):
            reason = "semantic_equivalence_unproven"
        else:
            reason = "validated_whole_units"
    return {
        "mode": "validated_extractive"
        if reason == "validated_whole_units"
        else "extractive_fallback",
        "reason": reason,
        "validator": VALIDATOR,
        "source_hash": source_hash,
        "critical_hash": state_hash,
        "output_hash": canonical_hash(quotes),
        "candidate_hash": canonical_hash(candidate) if candidate is not None else None,
        "quotes": quotes,
        "authority": "untrusted_source_material",
        "semantic_synthesis": "disabled",
    }
