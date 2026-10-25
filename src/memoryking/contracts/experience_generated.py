"""Generated experience requests; runtime schema is authoritative."""
from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class Procedure(TypedDict):
    prerequisites: list[str]
    steps: list[str]
    verification: list[str]
    expected_artifacts: list[str]
    exceptions: list[str]
    failure_signals: list[str]
    recovery: list[str]
    known_conditions: list[str]
    success_evidence: list[str]
    failure_evidence: list[str]
    failure_assessment: str
    dependency_mode: Literal["independent", "required"]

class Propose(TypedDict):
    project: str
    logical_key: str
    expected_version: int
    body: dict[str, Any]

class Review(TypedDict):
    project: str
    revision: str
    expected_version: int
    decision: Literal["accept", "reject", "deprecate", "quarantine", "release_quarantine"]
    reason: str
    breadth_reviewed: bool
    replacement: NotRequired[dict[str, Any]]

class Activate(TypedDict):
    project: str
    revision: str
    expected_version: int
    reason: str
    bindings: list[dict[str, Any]]
    policy: Literal["reviewed-procedure-v1"]

class Suspend(TypedDict):
    project: str
    revision: str
    expected_version: int
    reason: str

class Query(TypedDict):
    project: str
    bindings: list[dict[str, Any]]
    task: NotRequired[str]
    run: NotRequired[str]
    diagnostic: NotRequired[bool]

class OutcomeBody(TypedDict):
    task: str
    run: str
    result: Literal["success", "failure", "unknown"]
    acceptance_evidence: list[dict[str, Any]]
    failures: list[str]
    rework: list[str]
    elapsed_ms: int | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    limitations: list[str]
    readers: None | list[str]

class Outcome(TypedDict):
    project: str
    logical_key: str
    expected_revision: int
    body: OutcomeBody

class Feedback(TypedDict):
    project: str
    receipt: str
    reference: str
    task: str
    run: str
    expected_revision: int
    use: Literal["unknown", "read", "used"]
    usefulness: Literal["unknown", "helpful", "irrelevant", "misleading", "harmful"]
    comment: str | None
    readers: None | list[str]

class Records(TypedDict):
    project: str
    kind: Literal["outcome", "feedback", "ranking", "shadow"]

class ShadowBrief(TypedDict):
    project: str
    query: str
    bindings: list[dict[str, Any]]
    max_output_tokens: int
    tokenizer: str
    top_k: NotRequired[int]
    task: NotRequired[str]
    run: NotRequired[str]
    freshness: NotRequired[Literal["any", "attested"]]

class RankingPropose(TypedDict):
    project: str
    logical_key: str
    feedback: list[str]
    max_adjustment_ppm: int
    policy: Literal["bounded-feedback-v1"]

class ShadowCase(TypedDict):
    brief: ShadowBrief
    relevant: list[str]
    forbidden: list[str]

class RankingEvaluate(TypedDict):
    project: str
    configuration: str
    logical_key: str
    cases: list[ShadowCase]
    dataset_kind: Literal["development"]
    judgment_provenance: str

class RankingActivate(TypedDict):
    project: str
    configuration: str
    shadow: str
    expected_version: int
    reason: str

class RankingRollback(TypedDict):
    project: str
    configuration: str | None
    expected_version: int
    reason: str

class RankingInspect(TypedDict):
    project: str

class RecordLookup(TypedDict):
    project: str
    record: str
    kind: Literal["outcome", "feedback", "ranking", "shadow"]

class Command0(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["propose"]
    args: Propose
    key: str

class Command1(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["review"]
    args: Review
    key: str

class Command2(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["activate"]
    args: Activate
    key: str

class Command3(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["suspend"]
    args: Suspend
    key: str

class Command4(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["query"]
    args: Query

class Command5(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["outcome"]
    args: Outcome
    key: str

class Command6(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["feedback"]
    args: Feedback
    key: str

class Command7(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["records"]
    args: Records

class Command8(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["ranking_propose"]
    args: RankingPropose
    key: str

class Command9(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["ranking_evaluate"]
    args: RankingEvaluate
    key: str

class Command10(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["ranking_activate"]
    args: RankingActivate
    key: str

class Command11(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["ranking_rollback"]
    args: RankingRollback
    key: str

class Command12(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["ranking_inspect"]
    args: RankingInspect

class Command13(TypedDict):
    version: Literal[1]
    incarnation: str
    action: Literal["record"]
    args: RecordLookup

ExperienceCommand = Command0 | Command1 | Command2 | Command3 | Command4 | Command5 | Command6 | Command7 | Command8 | Command9 | Command10 | Command11 | Command12 | Command13
