"""Generated embedding requests; runtime schema is authoritative."""
from __future__ import annotations

from typing import Literal, TypedDict


class Configure(TypedDict):
    project: str
    model: Literal["bge-small-en-v1-5-onnx", "minilm-l6-v2-onnx"]
    state: Literal["experimental", "disabled"]
    expected_generation: int

class Partition(TypedDict):
    project: str
    fingerprint: str

class Complete(TypedDict):
    project: str
    lease: str
    fingerprint: str
    input_hash: str
    vector: list[int]
    input_tokens: int | None
    truncated: bool
    duration_ms: int

class Fail(TypedDict):
    project: str
    lease: str
    reason: Literal["provider_unavailable", "model_invalid", "interrupted", "provider_failure"]
    input_tokens: int | None
    duration_ms: int

class ClaimBatch(TypedDict):
    project: str
    fingerprint: str
    count: int

class CompleteItem(TypedDict):
    lease: str
    fingerprint: str
    input_hash: str
    vector: list[int]
    input_tokens: int | None
    truncated: bool
    duration_ms: int

class CompleteBatch(TypedDict):
    project: str
    items: list[CompleteItem]

class Command0(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["configure"]
    args: Configure
    key: str

class Command1(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["claim"]
    args: Partition

class Command2(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["complete"]
    args: Complete

class Command3(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["fail"]
    args: Fail

class Command4(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["claim_batch"]
    args: ClaimBatch

class Command5(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["complete_batch"]
    args: CompleteBatch

class Command6(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["status"]
    args: Partition

EmbeddingCommand = Command0 | Command1 | Command2 | Command3 | Command4 | Command5 | Command6
