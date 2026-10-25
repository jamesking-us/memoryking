"""Generated processing requests; runtime schema is authoritative."""
from __future__ import annotations

from typing import Any, Literal, TypedDict


class Proposal(TypedDict):
    text: str
    kind: Literal["knowledge"]
    selector: dict[str, Any]
    valid_start: int | None
    valid_end: int | None
    task: str | None
    run: str | None
    readers: None | list[str]
    evidence: list[dict[str, Any]]
    authority: None
    rule_key: None
    effect: None
    rule_value: None

class Provenance(TypedDict):
    provider: str
    model: str
    configuration_hash: str
    prompt_revision: str
    input_hash: str
    output_hash: str

class Extract(TypedDict):
    project: str
    logical_key: str
    expected_version: int
    body: Proposal
    provenance: Provenance

class Quote(TypedDict):
    observations: list[str]
    text: str

class Candidate(TypedDict):
    critical_hash: str
    quotes: list[Quote]
    provenance: Provenance
    attempt: str

class Compile(TypedDict):
    project: str
    task: str
    run: str | None
    checkpoint: str | None
    bindings: list[dict[str, Any]]
    max_output_tokens: int
    tokenizer: str
    sources: list[str]
    candidate: Candidate | None
    claims: list[str]

class Usage(TypedDict):
    project: str
    successful_tasks: int

class Command0(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["extract"]
    args: Extract
    key: str

class Command1(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["compile"]
    args: Compile

class Command2(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["usage"]
    args: Usage

ProcessingCommand = Command0 | Command1 | Command2
