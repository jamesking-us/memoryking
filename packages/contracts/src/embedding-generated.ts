// Generated embedding requests; runtime schema is authoritative.

export interface Configure {
  project: string;
  model: "minilm-l6-v2-onnx";
  state: "experimental" | "disabled";
  expected_generation: number;
}

export interface Partition {
  project: string;
  fingerprint: string;
}

export interface Complete {
  project: string;
  lease: string;
  fingerprint: string;
  input_hash: string;
  vector: Array<number>;
  input_tokens: number | null;
  truncated: boolean;
  duration_ms: number;
}

export interface Fail {
  project: string;
  lease: string;
  reason: "provider_unavailable" | "model_invalid" | "interrupted" | "provider_failure";
  input_tokens: number | null;
  duration_ms: number;
}

export interface Command0 {
  version: 1;
  incarnation: string;
  action: "configure";
  args: Configure;
  key: string;
}

export interface Command1 {
  version: 1;
  incarnation: string;
  action: "claim";
  args: Partition;
}

export interface Command2 {
  version: 1;
  incarnation: string;
  action: "complete";
  args: Complete;
}

export interface Command3 {
  version: 1;
  incarnation: string;
  action: "fail";
  args: Fail;
}

export interface Command4 {
  version: 1;
  incarnation: string;
  action: "status";
  args: Partition;
}

export type EmbeddingCommand = Command0 | Command1 | Command2 | Command3 | Command4;
