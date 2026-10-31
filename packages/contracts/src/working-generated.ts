// Generated working-state requests; runtime schema is authoritative.

export interface Dependency {
  kind: "observation" | "revision";
  id: string;
}

export interface Artifact {
  path: string;
  availability: "stored" | "external" | "missing";
  observation: string | null;
  content_hash: string | null;
  required: boolean;
}

export interface Repository {
  repository: string | null;
  base_commit: string | null;
  current_commit: string | null;
  dirty: boolean;
  artifacts: Array<Artifact>;
  snapshot_id?: string | null;
  environment?: string;
  change_scope?: Array<string>;
  deleted_paths?: Array<string>;
  clone?: string;
}

export interface Criterion {
  id: string;
  text: string;
  state: "pending" | "passed" | "failed" | "waived";
  evidence: Array<Dependency>;
  decision: string | null;
  weight?: number;
}

export interface Finding {
  text: string;
  kind: "decision" | "hypothesis";
  evidence: Array<Dependency>;
}

export interface FailedAttempt {
  action: string;
  outcome: string;
  do_not_repeat_when: string;
}

export interface Verification {
  name: string;
  target_revision: string | null;
  environment: string;
  result: "passed" | "failed" | "not_evaluated" | "inconclusive";
  evidence: Array<Dependency>;
}

export interface Blocker {
  text: string;
  owner: string | null;
}

export interface Capture {
  last_event: string | null;
  observed_ms: number;
  gaps: Array<string>;
}

export interface Checkpoint {
  objective: string;
  acceptance: Array<Criterion>;
  hard_constraints: Array<string>;
  current_state: string;
  repository: Repository;
  findings: Array<Finding>;
  failed_attempts: Array<FailedAttempt>;
  verification: Array<Verification>;
  blockers: Array<Blocker>;
  next_actions: Array<string>;
  references: Array<Dependency>;
  operation_ids: Array<string>;
  capture: Capture;
}

export interface Save {
  project: string;
  task: string;
  run: string | null;
  expected_revision: number | null;
  readers: Array<string>;
  checkpoint: Checkpoint;
}

export interface Resume {
  project: string;
  task: string;
  run: string | null;
  checkpoint: string | null;
  bindings: Array<Record<string, unknown>>;
  max_output_tokens: number;
  tokenizer: string;
  format?: "json" | "text";
}

export interface AssignWriter {
  project: string;
  task: string;
  run: string | null;
  expected_revision: number;
  writer: string;
  reason: string;
}

export interface OperationIntent {
  project: string;
  task: string;
  run: string | null;
  operation: string;
  target_hash: string;
  parameters_hash: string;
  method: "readback" | "idempotent_retry" | "manual";
  provider_key: string | null;
}

export interface OperationTransition {
  project: string;
  operation: string;
  expected_revision: number;
  state: "dispatching" | "unknown" | "succeeded" | "failed";
  reconciled: boolean;
  evidence: Array<Dependency>;
  result_hash: string | null;
}

export interface OperationList {
  project: string;
  task: string;
  run: string | null;
}

export interface AdapterResume {
  project: string;
  task: string;
  run: string | null;
  checkpoint: string | null;
  bindings: Array<Record<string, unknown>>;
  max_output_tokens: number;
  tokenizer: string;
  base: null | AdapterBase;
  adapter_context: AdapterContext;
}

export interface AdapterBase {
  id: string;
  hash: string;
  sequence: number;
  schema: string;
}

export interface AdapterContext {
  model_session: string;
  context_epoch: number;
}

export interface Command0 {
  version: 1;
  incarnation: string;
  action: "save";
  args: Save;
  key: string;
}

export interface Command1 {
  version: 1;
  incarnation: string;
  action: "resume";
  args: Resume;
}

export interface Command2 {
  version: 1;
  incarnation: string;
  action: "assign_writer";
  args: AssignWriter;
  key: string;
}

export interface Command3 {
  version: 1;
  incarnation: string;
  action: "operation_intent";
  args: OperationIntent;
  key: string;
}

export interface Command4 {
  version: 1;
  incarnation: string;
  action: "operation_transition";
  args: OperationTransition;
  key: string;
}

export interface Command5 {
  version: 1;
  incarnation: string;
  action: "operations";
  args: OperationList;
}

export interface Command6 {
  version: 1;
  incarnation: string;
  action: "adapter_resume";
  args: AdapterResume;
}

export type WorkingCommand = Command0 | Command1 | Command2 | Command3 | Command4 | Command5 | Command6;
