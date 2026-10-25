// Generated; source SHA256 9ce3283a06554e2e31517c52452db9f0d846ea378c4cd786d79be0b9418d2ce0. Do not edit.

export type Id = string;

export type HistoryIncarnation = string;

export type Counter = string;

export type Digest = string;

export type PhaseId = string;

export type TimestampMs = number;

export interface Scope {
  store_id: Id;
  tenant_id: Id;
  project_id: Id;
}

export interface HistoryPosition {
  scope: Scope;
  history_incarnation: HistoryIncarnation;
  position: Counter;
}

export interface AuthorizationContext {
  store_id: Id;
  tenant_id: Id;
  principal_id: Id;
  history_incarnation: HistoryIncarnation;
  authorization_generation: Counter;
  audience: string;
}

export interface EvidenceReference {
  evidence_id: Id;
  digest: Digest;
  source_revision: string;
  classification: "synthetic" | "internal" | "restricted";
}

export type ErrorCode = "invalid_request" | "forbidden" | "revision_conflict" | "budget_insufficient" | "constraint_resolution_incomplete" | "constraint_conflict" | "applicability_unknown" | "operation_ambiguous" | "index_not_ready" | "provider_unavailable" | "history_changed" | "reconciliation_required" | "authorization_release_expired" | "context_rebuild_required" | "selector_unsupported" | "selector_limit_exceeded" | "selector_relation_indeterminate" | "replacement_out_of_range" | "replay_expired" | "replay_purged" | "replay_artifact_missing" | "unsupported_schema" | "rate_limited" | "internal_error";

export interface ErrorEnvelope {
  schema_version: "1";
  code: ErrorCode;
  request_id: Id;
  retryable: boolean;
  retry_after_ms: number | null;
}

export interface RequestIdentity {
  scope: Scope;
  history_incarnation: HistoryIncarnation;
  request_namespace: Id;
  principal_id: Id;
  operation: string;
  idempotency_key: string;
  canonicalization_version: "MK-C14N-1";
  request_hash: Digest;
}

export type ExecutionStatus = "planned" | "ready" | "running" | "waiting" | "blocked" | "failed" | "cancelled" | "finished";

export interface WorkItem {
  task_id: Id;
  parent_task_id: Id | null;
  dependencies: Array<Id>;
  execution_status: ExecutionStatus;
  criterion_ids: Array<Id>;
}

export interface Plan {
  schema_version: "1";
  plan_id: Id;
  plan_revision: Counter;
  scope: Scope;
  history_incarnation: HistoryIncarnation;
  tasks: Array<WorkItem>;
}

export interface AcceptanceCriterion {
  criterion_id: Id;
  task_id: Id;
  plan_revision: Counter;
  status: "passed" | "failed" | "pending" | "stale" | "waived" | "not_evaluated";
  evidence: Array<EvidenceReference>;
  waiver_decision_id: Id | null;
}

export interface PhaseStatus {
  schema_version: "1";
  phase_id: PhaseId;
  plan_revision: Counter;
  technical_status: "not_started" | "in_progress" | "closeout_pending" | "complete" | "partial" | "blocked";
  approval_status: "pending" | "accepted" | "rejected";
  blocking_decisions: Array<Id>;
  report_status: "not_generated" | "draft" | "qa_passed";
  email_status: "not_submitted" | "unknown" | "smtp_accepted" | "failed";
  device_status: "unverified" | "confirmed" | "rejected";
  report_digest: Digest | null;
}

export interface ObservationDetail {
  execution_status?: ExecutionStatus;
  verification_status?: "passed" | "failed" | "pending" | "stale" | "waived" | "not_evaluated";
  summary?: string;
}

export interface ObservationEvent {
  schema_version: "1";
  event_id: Id;
  event_type: "task.updated" | "verification.recorded" | "checkpoint.recorded" | "operation.observed" | "phase.updated" | "heartbeat.observed";
  scope: Scope;
  history_incarnation: HistoryIncarnation;
  plan_id: Id;
  plan_revision: Counter;
  phase_id: PhaseId | null;
  task_id: Id | null;
  run_id: Id | null;
  producer_id: Id;
  correlation_id: Id;
  causation_id: Id | null;
  observed_at_ms: TimestampMs;
  recorded_at_ms: TimestampMs;
  source_position: HistoryPosition;
  entity_revision: Counter;
  evidence: Array<EvidenceReference>;
  detail: ObservationDetail;
}
