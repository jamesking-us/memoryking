#!/usr/bin/env python3
"""Emit actual Python validation/canonicalization results for cross-language comparison."""

import json
from pathlib import Path

from memoryking.contracts import (
    ContractError,
    canonical_bytes,
    canonical_hash,
    load_strict,
    validate_contract,
)


def results():
    vectors = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/contracts/vectors.json").read_text()
    )
    output = []
    for vector in vectors:
        try:
            value = load_strict(vector["raw"])
            if "contract" in vector:
                validate_contract(vector["contract"], value)
            encoded = canonical_bytes(value).decode()
            assert vector.get("valid", True), vector["id"]
            assert "canonical" not in vector or vector["canonical"] == encoded, vector["id"]
            output.append(
                {
                    "id": vector["id"],
                    "valid": True,
                    "canonical": encoded,
                    "hash": canonical_hash(value),
                }
            )
        except ContractError:
            assert vector.get("valid") is False, vector["id"]
            output.append({"id": vector["id"], "valid": False})
    return output


if __name__ == "__main__":
    print(json.dumps(results(), ensure_ascii=True, sort_keys=True))
