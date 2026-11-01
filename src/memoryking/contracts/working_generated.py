"""Generated working-state request types; runtime schema is authoritative."""
from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class Dependency(TypedDict):
    kind: Literal["observation", "revision"]
    id: str

class Artifact(TypedDict):
    path: str
    availability: Literal["stored", "external", "missing"]
    observation: str | None
    content_hash: str | None
    required: bool

class Repository(TypedDict):
    repository: str | None
    base_commit: str | None
    current_commit: str | None
    dirty: bool
    artifacts: list[Artifact]
    snapshot_id: NotRequired[str | None]
    environment: NotRequired[str]
    change_scope: NotRequired[list[str]]
    deleted_paths: NotRequired[list[str]]
    clone: NotRequired[str]

class Criterion(TypedDict):
    id: str
    text: str
    state: Literal["pending", "passed", "failed", "waived"]
    evidence: list[Dependency]
    decision: str | None
    weight: NotRequired[int]

class Finding(TypedDict):
    text: str
    kind: Literal["decision", "hypothesis"]
    evidence: list[Dependency]

class FailedAttempt(TypedDict):
    action: str
    outcome: str
    do_not_repeat_when: str

class Verification(TypedDict):
    name: str
    target_revision: str | None
    environment: str
    result: Literal["passed", "failed", "not_evaluated", "inconclusive"]
    evidence: list[Dependency]

class Blocker(TypedDict):
    text: str
    owner: str | None

class Capture(TypedDict):
    last_event: str | None
    observed_ms: int
    gaps: list[str]

class Checkpoint(TypedDict):
    objective: str
    acceptance: list[Criterion]
    hard_constraints: list[str]
    current_state: str
    repository: Repository
    findings: list[Finding]
    failed_attempts: list[FailedAttempt]
    verification: list[Verification]
    blockers: list[Blocker]
    next_actions: list[str]
    references: list[Dependency]
    operation_ids: list[str]
    capture: Capture

class Save(TypedDict):
    project: str
    task: str
    run: str | None
    expected_revision: int | None
    readers: list[str]
    checkpoint: Checkpoint

class Resume(TypedDict):
    project: str
    task: str
    run: str | None
    checkpoint: str | None
    bindings: list[dict[str, Any]]
    max_output_tokens: int
    tokenizer: str
    format: NotRequired[Literal["json", "text"]]

class AssignWriter(TypedDict):
    project: str
    task: str
    run: str | None
    expected_revision: int
    writer: str
    reason: str

class OperationIntent(TypedDict):
    project: str
    task: str
    run: str | None
    operation: str
    target_hash: str
    parameters_hash: str
    method: Literal["readback", "idempotent_retry", "manual"]
    provider_key: str | None

class OperationTransition(TypedDict):
    project: str
    operation: str
    expected_revision: int
    state: Literal["dispatching", "unknown", "succeeded", "failed"]
    reconciled: bool
    evidence: list[Dependency]
    result_hash: str | None

class OperationList(TypedDict):
    project: str
    task: str
    run: str | None

class AdapterResume(TypedDict):
    project: str
    task: str
    run: str | None
    checkpoint: str | None
    bindings: list[dict[str, Any]]
    max_output_tokens: int
    tokenizer: str
    base: None | AdapterBase
    adapter_context: AdapterContext

class AdapterBase(TypedDict):
    id: str
    hash: str
    sequence: int
    schema: str

class AdapterContext(TypedDict):
    model_session: str
    context_epoch: int

class Summarize(TypedDict):
    project: str
    task: str
    run: str | None
    checkpoint: str | None
    source: Literal["extractive", "provider"]
    text: str | None
    permit: str | None
    readers: list[str] | None

class Command0(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["save"]
    args: Save
    key: str

class Command1(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["resume"]
    args: Resume

class Command2(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["assign_writer"]
    args: AssignWriter
    key: str

class Command3(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["operation_intent"]
    args: OperationIntent
    key: str

class Command4(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["operation_transition"]
    args: OperationTransition
    key: str

class Command5(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["operations"]
    args: OperationList

class Command6(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["adapter_resume"]
    args: AdapterResume

class Command7(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["summarize"]
    args: Summarize
    key: str

WorkingCommand = Command0 | Command1 | Command2 | Command3 | Command4 | Command5 | Command6 | Command7
