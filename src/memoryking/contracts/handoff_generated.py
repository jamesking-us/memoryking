"""Generated handoff requests; runtime schema is authoritative."""
from __future__ import annotations

from typing import Any, Literal, TypedDict


class Prepare(TypedDict):
    project: str
    task: str
    run: str | None
    checkpoint: str
    recipient: str
    role: str
    stage: str
    bindings: list[dict[str, Any]]
    ttl_ms: int

class View(TypedDict):
    project: str
    handle: str
    max_output_tokens: int
    tokenizer: str

class Acknowledge(TypedDict):
    project: str
    handle: str
    materialization: str
    rendered_hash: str

class Cancel(TypedDict):
    project: str
    handle: str

class Command0(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["prepare"]
    key: str
    args: Prepare

class Command1(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["materialize"]
    key: str
    args: View

class Command2(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["preview"]
    key: str
    args: View

class Command3(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["ack"]
    key: str
    args: Acknowledge

class Command4(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["cancel"]
    key: str
    args: Cancel

HandoffCommand = Command0 | Command1 | Command2 | Command3 | Command4
