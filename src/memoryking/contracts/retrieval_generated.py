"""Generated retrieval request types. Runtime schema validation is authoritative."""
from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class Brief(TypedDict):
    project: str
    query: str
    bindings: list[dict[str, Any]]
    max_output_tokens: int
    tokenizer: str
    top_k: NotRequired[int]
    task: NotRequired[str]
    run: NotRequired[str]
    valid_ms: NotRequired[int]
    recorded_position: NotRequired[int]
    consistency: NotRequired[dict[str, Any]]
    freshness: NotRequired[Literal["any", "attested"]]
    dense: NotRequired[DenseQuery]
    format: NotRequired[Literal["json", "text"]]

class Fetch(TypedDict):
    project: str
    reference: str
    kind: Literal["observation", "revision"]
    start_byte: int
    end_byte: int
    max_output_tokens: int
    tokenizer: str

class Receipt(TypedDict):
    project: str
    receipt: str

class ReceiptList(TypedDict):
    project: str

class Rerun(TypedDict):
    project: str
    predecessor: str
    brief: Brief

class Atlas(TypedDict):
    project: str
    bindings: list[dict[str, Any]]
    task: NotRequired[str]
    run: NotRequired[str]
    valid_ms: NotRequired[int]
    recorded_position: NotRequired[int]
    query: NotRequired[str]
    types: NotRequired[list[Literal["observation", "claim", "attestation", "procedure", "checkpoint", "task", "run"]]]
    focus: NotRequired[list[str]]
    max_nodes: NotRequired[int]
    max_edges: NotRequired[int]
    cursor: NotRequired[str]

class DenseQuery(TypedDict):
    fingerprint: str
    query_hash: str
    vector: list[int]
    minimum_similarity_ppm: NotRequired[int]
    engine: NotRequired[Literal["python_exact", "pgvector_exact"]]

class Command0(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["brief"]
    args: Brief

class Command1(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["fetch"]
    args: Fetch

class Command2(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["receipt"]
    args: Receipt

class Command3(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["replay"]
    args: Receipt

class Command4(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["receipts"]
    args: ReceiptList

class Command5(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["rerun"]
    args: Rerun

class Command6(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["atlas"]
    args: Atlas

RetrievalCommand = Command0 | Command1 | Command2 | Command3 | Command4 | Command5 | Command6
