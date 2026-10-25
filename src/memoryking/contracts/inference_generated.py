"""Generated inference requests; runtime schema is authoritative."""
from __future__ import annotations

from typing import Literal, TypedDict


class Binding(TypedDict):
    store: str
    tenant: str
    principal: str
    project: str
    incarnation: str
    authorization_epoch: int
    purge_epoch: int
    delegation_chain: list[str]
    egress_policy_revision: Literal["local-recording-v1"]
    model_session_id: str
    context_epoch: int

class Message(TypedDict):
    role: Literal["system", "user", "assistant", "tool"]
    content: str

class Handle(TypedDict):
    kind: Literal["conversation"]
    id: str

class Request(TypedDict):
    provider: Literal["local-recording"]
    model: Literal["echo-v1"]
    messages: list[Message]
    state_handles: list[Handle]

class Artifact(TypedDict):
    receipt: str
    sha256: str

class HostAuthorization(TypedDict):
    id: str
    classification: Literal["public", "internal", "restricted"]
    source_hash: str
    expires_ms: int

class Proof(TypedDict):
    origin: Literal["host", "memory", "derived"]
    sha256: str
    artifacts: list[Artifact]
    host_authorizations: list[HostAuthorization]

class Manifest(TypedDict):
    segments: list[Proof]
    handles: list[Proof]

class Authorize(TypedDict):
    binding: Binding
    request: Request
    manifest: Manifest

class Consume(TypedDict):
    binding: Binding
    request: Request
    manifest: Manifest
    permit: str

class Command0(TypedDict):
    version: Literal[1]
    action: Literal["authorize"]
    args: Authorize

class Command1(TypedDict):
    version: Literal[1]
    action: Literal["consume"]
    args: Consume

class Command2(TypedDict):
    version: Literal[1]
    action: Literal["validate"]
    args: Authorize

InferenceCommand = Command0 | Command1 | Command2
