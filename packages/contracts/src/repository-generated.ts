// Generated repository requests; runtime schema is authoritative.

export interface Register {
  project: string;
  remote: string;
  clone: string;
  path: string;
  readers: null | Array<string>;
}

export interface Capture {
  project: string;
  repository: string;
  clone: string;
  environment: string;
}

export interface Inspect {
  project: string;
  repository: string;
  snapshot: string;
}

export interface Dependency {
  kind: "file" | "symbol";
  target: string;
  policy: "invalidate" | "revalidate" | "rank_penalty";
}

export interface Bind {
  project: string;
  revision: string;
  repository: string;
  snapshot: string;
  dependencies: Array<Dependency>;
  impact: "high" | "low";
  coverage_reviewed: true;
  expected_policy: number;
  evidence: string;
}

export interface Verify {
  project: string;
  revision: string;
  snapshot: string;
  expected_policy: number;
  evidence: string;
  passed: boolean;
  expires_ms: number;
}

export interface Revalidate {
  project: string;
  repository: string;
  snapshot: string;
  expected_generation: number;
}

export interface Impact {
  project: string;
  repository: string;
  snapshot: string;
  paths: Array<string>;
  limit: number;
}

export interface Coverage {
  project: string;
  repository: string;
  snapshot: string;
  paths: Array<string>;
  bindings: Array<Record<string, unknown>>;
}

export interface Pending {
  project: string;
  repository: string;
}

export interface History {
  project: string;
  revision: string;
  snapshot: string;
}

export interface Command0 {
  version: 1;
  incarnation: string;
  action: "register";
  args: Register;
  key: string;
}

export interface Command1 {
  version: 1;
  incarnation: string;
  action: "capture";
  args: Capture;
  key: string;
}

export interface Command2 {
  version: 1;
  incarnation: string;
  action: "inspect";
  args: Inspect;
}

export interface Command3 {
  version: 1;
  incarnation: string;
  action: "bind";
  args: Bind;
  key: string;
}

export interface Command4 {
  version: 1;
  incarnation: string;
  action: "verify";
  args: Verify;
  key: string;
}

export interface Command5 {
  version: 1;
  incarnation: string;
  action: "revalidate";
  args: Revalidate;
  key: string;
}

export interface Command6 {
  version: 1;
  incarnation: string;
  action: "impact";
  args: Impact;
}

export interface Command7 {
  version: 1;
  incarnation: string;
  action: "coverage";
  args: Coverage;
}

export interface Command8 {
  version: 1;
  incarnation: string;
  action: "pending";
  args: Pending;
}

export interface Command9 {
  version: 1;
  incarnation: string;
  action: "history";
  args: History;
}

export type RepositoryCommand = Command0 | Command1 | Command2 | Command3 | Command4 | Command5 | Command6 | Command7 | Command8 | Command9;
