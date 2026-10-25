"""Generated; source SHA256 9ce3283a06554e2e31517c52452db9f0d846ea378c4cd786d79be0b9418d2ce0. Do not edit."""
from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

Id = str

HistoryIncarnation = str

Counter = str

Digest = str

PhaseId = str

TimestampMs = int

class Scope(TypedDict):
    store_id: Id
    tenant_id: Id
    project_id: Id

class HistoryPosition(TypedDict):
    scope: Scope
    history_incarnation: HistoryIncarnation
    position: Counter

class AuthorizationContext(TypedDict):
    store_id: Id
    tenant_id: Id
    principal_id: Id
    history_incarnation: HistoryIncarnation
    authorization_generation: Counter
    audience: str

class EvidenceReference(TypedDict):
    evidence_id: Id
    digest: Digest
    source_revision: str
    classification: Literal["synthetic", "internal", "restricted"]

ErrorCode = Literal["invalid_request", "forbidden", "revision_conflict", "budget_insufficient", "constraint_resolution_incomplete", "constraint_conflict", "applicability_unknown", "operation_ambiguous", "index_not_ready", "provider_unavailable", "history_changed", "reconciliation_required", "authorization_release_expired", "context_rebuild_required", "selector_unsupported", "selector_limit_exceeded", "selector_relation_indeterminate", "replacement_out_of_range", "replay_expired", "replay_purged", "replay_artifact_missing", "unsupported_schema", "rate_limited", "internal_error"]

class ErrorEnvelope(TypedDict):
    schema_version: Literal["1"]
    code: ErrorCode
    request_id: Id
    retryable: bool
    retry_after_ms: int | None

class RequestIdentity(TypedDict):
    scope: Scope
    history_incarnation: HistoryIncarnation
    request_namespace: Id
    principal_id: Id
    operation: str
    idempotency_key: str
    canonicalization_version: Literal["MK-C14N-1"]
    request_hash: Digest

ExecutionStatus = Literal["planned", "ready", "running", "waiting", "blocked", "failed", "cancelled", "finished"]

class WorkItem(TypedDict):
    task_id: Id
    parent_task_id: Id | None
    dependencies: list[Id]
    execution_status: ExecutionStatus
    criterion_ids: list[Id]

class Plan(TypedDict):
    schema_version: Literal["1"]
    plan_id: Id
    plan_revision: Counter
    scope: Scope
    history_incarnation: HistoryIncarnation
    tasks: list[WorkItem]

class AcceptanceCriterion(TypedDict):
    criterion_id: Id
    task_id: Id
    plan_revision: Counter
    status: Literal["passed", "failed", "pending", "stale", "waived", "not_evaluated"]
    evidence: list[EvidenceReference]
    waiver_decision_id: Id | None

class PhaseStatus(TypedDict):
    schema_version: Literal["1"]
    phase_id: PhaseId
    plan_revision: Counter
    technical_status: Literal["not_started", "in_progress", "closeout_pending", "complete", "partial", "blocked"]
    approval_status: Literal["pending", "accepted", "rejected"]
    blocking_decisions: list[Id]
    report_status: Literal["not_generated", "draft", "qa_passed"]
    email_status: Literal["not_submitted", "unknown", "smtp_accepted", "failed"]
    device_status: Literal["unverified", "confirmed", "rejected"]
    report_digest: Digest | None

class ObservationDetail(TypedDict):
    execution_status: NotRequired[ExecutionStatus]
    verification_status: NotRequired[Literal["passed", "failed", "pending", "stale", "waived", "not_evaluated"]]
    summary: NotRequired[str]

class ObservationEvent(TypedDict):
    schema_version: Literal["1"]
    event_id: Id
    event_type: Literal["task.updated", "verification.recorded", "checkpoint.recorded", "operation.observed", "phase.updated", "heartbeat.observed"]
    scope: Scope
    history_incarnation: HistoryIncarnation
    plan_id: Id
    plan_revision: Counter
    phase_id: PhaseId | None
    task_id: Id | None
    run_id: Id | None
    producer_id: Id
    correlation_id: Id
    causation_id: Id | None
    observed_at_ms: TimestampMs
    recorded_at_ms: TimestampMs
    source_position: HistoryPosition
    entity_revision: Counter
    evidence: list[EvidenceReference]
    detail: ObservationDetail
