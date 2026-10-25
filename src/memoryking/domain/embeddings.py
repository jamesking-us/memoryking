"""Provider-independent finite vectors and fingerprinted embedding results."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.local import LocalError

SCALE = 1_000_000


def validate_vector(vector: list[int], dimensions: int) -> None:
    if (
        type(dimensions) is not int
        or not 1 <= dimensions <= 4096
        or not isinstance(vector, list)
        or len(vector) != dimensions
        or any(type(value) is not int or abs(value) > SCALE for value in vector)
        or not any(vector)
    ):
        raise LocalError("embedding_vector_invalid")


def quantize(values: list[float], dimensions: int) -> list[int]:
    if len(values) != dimensions or any(
        type(v) not in (int, float) or not math.isfinite(v) for v in values
    ):
        raise LocalError("embedding_vector_invalid")
    norm = math.sqrt(math.fsum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0:
        raise LocalError("embedding_vector_invalid")
    vector = [round(value / norm * SCALE) for value in values]
    validate_vector(vector, dimensions)
    return vector


def cosine_ppm(left: list[int], right: list[int]) -> int:
    validate_vector(left, len(left))
    validate_vector(right, len(left))
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    denominator = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return max(-SCALE, min(SCALE, round(numerator / denominator * SCALE)))


def fingerprint(configuration: dict[str, Any]) -> str:
    """Includes weights, tokenizer, runtime, pooling, truncation and vector encoding."""
    return canonical_hash(configuration)


@dataclass(frozen=True)
class EmbeddingBatch:
    fingerprint: str
    dimensions: int
    vectors: list[list[int]]
    input_hashes: list[str]
    input_tokens: int | None
    truncated: list[bool]

    def validate(self, expected: str, dimensions: int, inputs: list[str]) -> None:
        if self.fingerprint != expected or self.dimensions != dimensions:
            raise LocalError("embedding_fingerprint_mismatch")
        if (
            not 1 <= len(inputs) <= 16
            or len(self.vectors) != len(inputs)
            or self.input_hashes != [canonical_hash(text) for text in inputs]
            or len(self.truncated) != len(inputs)
            or any(type(value) is not bool for value in self.truncated)
            or (
                self.input_tokens is not None
                and (type(self.input_tokens) is not int or not 0 <= self.input_tokens <= 1_000_000)
            )
        ):
            raise LocalError("embedding_batch_invalid")
        for vector in self.vectors:
            validate_vector(vector, dimensions)


def validate_inputs(texts: list[str]) -> None:
    if (
        not isinstance(texts, list)
        or not 1 <= len(texts) <= 16
        or any(
            not isinstance(text, str) or not text or len(text.encode()) > 16384 for text in texts
        )
        or sum(len(text.encode()) for text in texts) > 131072
    ):
        raise LocalError("embedding_input_limit")
