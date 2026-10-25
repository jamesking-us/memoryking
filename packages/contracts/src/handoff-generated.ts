// Generated handoff requests; runtime schema is authoritative.

export interface Prepare {
  project: string;
  task: string;
  run: string | null;
  checkpoint: string;
  recipient: string;
  role: string;
  stage: string;
  bindings: Array<Record<string, unknown>>;
  ttl_ms: number;
}

export interface View {
  project: string;
  handle: string;
  max_output_tokens: number;
  tokenizer: string;
}

export interface Acknowledge {
  project: string;
  handle: string;
  materialization: string;
  rendered_hash: string;
}

export interface Cancel {
  project: string;
  handle: string;
}

export interface Command0 {
  version: 1;
  incarnation: string;
  action: "prepare";
  key: string;
  args: Prepare;
}

export interface Command1 {
  version: 1;
  incarnation: string;
  action: "materialize";
  key: string;
  args: View;
}

export interface Command2 {
  version: 1;
  incarnation: string;
  action: "preview";
  key: string;
  args: View;
}

export interface Command3 {
  version: 1;
  incarnation: string;
  action: "ack";
  key: string;
  args: Acknowledge;
}

export interface Command4 {
  version: 1;
  incarnation: string;
  action: "cancel";
  key: string;
  args: Cancel;
}

export type HandoffCommand = Command0 | Command1 | Command2 | Command3 | Command4;
