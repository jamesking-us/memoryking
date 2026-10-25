// Generated processing requests; runtime schema is authoritative.

export interface Proposal {
  text: string;
  kind: "knowledge";
  selector: Record<string, unknown>;
  valid_start: number | null;
  valid_end: number | null;
  task: string | null;
  run: string | null;
  readers: null | Array<string>;
  evidence: Array<Record<string, unknown>>;
  authority: null;
  rule_key: null;
  effect: null;
  rule_value: null;
}

export interface Provenance {
  provider: string;
  model: string;
  configuration_hash: string;
  prompt_revision: string;
  input_hash: string;
  output_hash: string;
}

export interface Extract {
  project: string;
  logical_key: string;
  expected_version: number;
  body: Proposal;
  provenance: Provenance;
}

export interface Quote {
  observations: Array<string>;
  text: string;
}

export interface Candidate {
  critical_hash: string;
  quotes: Array<Quote>;
  provenance: Provenance;
  attempt: string;
}

export interface Compile {
  project: string;
  task: string;
  run: string | null;
  checkpoint: string | null;
  bindings: Array<Record<string, unknown>>;
  max_output_tokens: number;
  tokenizer: string;
  sources: Array<string>;
  candidate: Candidate | null;
  claims: Array<string>;
}

export interface Usage {
  project: string;
  successful_tasks: number;
}

export interface Command0 {
  version: 1;
  incarnation: string;
  action: "extract";
  args: Extract;
  key: string;
}

export interface Command1 {
  version: 1;
  incarnation: string;
  action: "compile";
  args: Compile;
}

export interface Command2 {
  version: 1;
  incarnation: string;
  action: "usage";
  args: Usage;
}

export type ProcessingCommand = Command0 | Command1 | Command2;
