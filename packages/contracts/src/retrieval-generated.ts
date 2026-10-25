// Generated retrieval types. Runtime schema validation is authoritative.

export interface Brief {
  project: string;
  query: string;
  bindings: Array<Record<string, unknown>>;
  max_output_tokens: number;
  tokenizer: string;
  top_k?: number;
  task?: string;
  run?: string;
  valid_ms?: number;
  recorded_position?: number;
  consistency?: Record<string, unknown>;
  freshness?: "any" | "attested";
  dense?: DenseQuery;
}

export interface Fetch {
  project: string;
  reference: string;
  kind: "observation" | "revision";
  start_byte: number;
  end_byte: number;
  max_output_tokens: number;
  tokenizer: string;
}

export interface Receipt {
  project: string;
  receipt: string;
}

export interface ReceiptList {
  project: string;
}

export interface Rerun {
  project: string;
  predecessor: string;
  brief: Brief;
}

export interface Atlas {
  project: string;
  bindings: Array<Record<string, unknown>>;
  task?: string;
  run?: string;
  valid_ms?: number;
  recorded_position?: number;
  query?: string;
  types?: Array<"observation" | "claim" | "attestation" | "procedure" | "checkpoint" | "task" | "run">;
  focus?: Array<string>;
  max_nodes?: number;
  max_edges?: number;
  cursor?: string;
}

export interface DenseQuery {
  fingerprint: string;
  query_hash: string;
  vector: Array<number>;
  minimum_similarity_ppm?: number;
  engine?: "python_exact" | "pgvector_exact";
}

export interface Command0 {
  version: 1;
  incarnation: string;
  action: "brief";
  args: Brief;
}

export interface Command1 {
  version: 1;
  incarnation: string;
  action: "fetch";
  args: Fetch;
}

export interface Command2 {
  version: 1;
  incarnation: string;
  action: "receipt";
  args: Receipt;
}

export interface Command3 {
  version: 1;
  incarnation: string;
  action: "replay";
  args: Receipt;
}

export interface Command4 {
  version: 1;
  incarnation: string;
  action: "receipts";
  args: ReceiptList;
}

export interface Command5 {
  version: 1;
  incarnation: string;
  action: "rerun";
  args: Rerun;
}

export interface Command6 {
  version: 1;
  incarnation: string;
  action: "atlas";
  args: Atlas;
}

export type RetrievalCommand = Command0 | Command1 | Command2 | Command3 | Command4 | Command5 | Command6;
