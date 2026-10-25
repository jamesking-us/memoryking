// Generated experience requests; runtime schema is authoritative.

export interface Procedure {
  prerequisites: Array<string>;
  steps: Array<string>;
  verification: Array<string>;
  expected_artifacts: Array<string>;
  exceptions: Array<string>;
  failure_signals: Array<string>;
  recovery: Array<string>;
  known_conditions: Array<string>;
  success_evidence: Array<string>;
  failure_evidence: Array<string>;
  failure_assessment: string;
  dependency_mode: "independent" | "required";
}

export interface Propose {
  project: string;
  logical_key: string;
  expected_version: number;
  body: Record<string, unknown>;
}

export interface Review {
  project: string;
  revision: string;
  expected_version: number;
  decision: "accept" | "reject" | "deprecate" | "quarantine" | "release_quarantine";
  reason: string;
  breadth_reviewed: boolean;
  replacement?: Record<string, unknown>;
}

export interface Activate {
  project: string;
  revision: string;
  expected_version: number;
  reason: string;
  bindings: Array<Record<string, unknown>>;
  policy: "reviewed-procedure-v1";
}

export interface Suspend {
  project: string;
  revision: string;
  expected_version: number;
  reason: string;
}

export interface Query {
  project: string;
  bindings: Array<Record<string, unknown>>;
  task?: string;
  run?: string;
  diagnostic?: boolean;
}

export interface OutcomeBody {
  task: string;
  run: string;
  result: "success" | "failure" | "unknown";
  acceptance_evidence: Array<Record<string, unknown>>;
  failures: Array<string>;
  rework: Array<string>;
  elapsed_ms: number | null;
  input_tokens: number | null;
  cached_input_tokens: number | null;
  output_tokens: number | null;
  limitations: Array<string>;
  readers: null | Array<string>;
}

export interface Outcome {
  project: string;
  logical_key: string;
  expected_revision: number;
  body: OutcomeBody;
}

export interface Feedback {
  project: string;
  receipt: string;
  reference: string;
  task: string;
  run: string;
  expected_revision: number;
  use: "unknown" | "read" | "used";
  usefulness: "unknown" | "helpful" | "irrelevant" | "misleading" | "harmful";
  comment: string | null;
  readers: null | Array<string>;
}

export interface Records {
  project: string;
  kind: "outcome" | "feedback" | "ranking" | "shadow";
}

export interface ShadowBrief {
  project: string;
  query: string;
  bindings: Array<Record<string, unknown>>;
  max_output_tokens: number;
  tokenizer: string;
  top_k?: number;
  task?: string;
  run?: string;
  freshness?: "any" | "attested";
}

export interface RankingPropose {
  project: string;
  logical_key: string;
  feedback: Array<string>;
  max_adjustment_ppm: number;
  policy: "bounded-feedback-v1";
}

export interface ShadowCase {
  brief: ShadowBrief;
  relevant: Array<string>;
  forbidden: Array<string>;
}

export interface RankingEvaluate {
  project: string;
  configuration: string;
  logical_key: string;
  cases: Array<ShadowCase>;
  dataset_kind: "development";
  judgment_provenance: string;
}

export interface RankingActivate {
  project: string;
  configuration: string;
  shadow: string;
  expected_version: number;
  reason: string;
}

export interface RankingRollback {
  project: string;
  configuration: string | null;
  expected_version: number;
  reason: string;
}

export interface RankingInspect {
  project: string;
}

export interface RecordLookup {
  project: string;
  record: string;
  kind: "outcome" | "feedback" | "ranking" | "shadow";
}

export interface Command0 {
  version: 1;
  incarnation: string;
  action: "propose";
  args: Propose;
  key: string;
}

export interface Command1 {
  version: 1;
  incarnation: string;
  action: "review";
  args: Review;
  key: string;
}

export interface Command2 {
  version: 1;
  incarnation: string;
  action: "activate";
  args: Activate;
  key: string;
}

export interface Command3 {
  version: 1;
  incarnation: string;
  action: "suspend";
  args: Suspend;
  key: string;
}

export interface Command4 {
  version: 1;
  incarnation: string;
  action: "query";
  args: Query;
}

export interface Command5 {
  version: 1;
  incarnation: string;
  action: "outcome";
  args: Outcome;
  key: string;
}

export interface Command6 {
  version: 1;
  incarnation: string;
  action: "feedback";
  args: Feedback;
  key: string;
}

export interface Command7 {
  version: 1;
  incarnation: string;
  action: "records";
  args: Records;
}

export interface Command8 {
  version: 1;
  incarnation: string;
  action: "ranking_propose";
  args: RankingPropose;
  key: string;
}

export interface Command9 {
  version: 1;
  incarnation: string;
  action: "ranking_evaluate";
  args: RankingEvaluate;
  key: string;
}

export interface Command10 {
  version: 1;
  incarnation: string;
  action: "ranking_activate";
  args: RankingActivate;
  key: string;
}

export interface Command11 {
  version: 1;
  incarnation: string;
  action: "ranking_rollback";
  args: RankingRollback;
  key: string;
}

export interface Command12 {
  version: 1;
  incarnation: string;
  action: "ranking_inspect";
  args: RankingInspect;
}

export interface Command13 {
  version: 1;
  incarnation: string;
  action: "record";
  args: RecordLookup;
}

export type ExperienceCommand = Command0 | Command1 | Command2 | Command3 | Command4 | Command5 | Command6 | Command7 | Command8 | Command9 | Command10 | Command11 | Command12 | Command13;
