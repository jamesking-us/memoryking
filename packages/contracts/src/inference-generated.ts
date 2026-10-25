// Generated inference requests; runtime schema is authoritative.

export interface Binding {
  store: string;
  tenant: string;
  principal: string;
  project: string;
  incarnation: string;
  authorization_epoch: number;
  purge_epoch: number;
  delegation_chain: Array<string>;
  egress_policy_revision: "local-recording-v1";
  model_session_id: string;
  context_epoch: number;
}

export interface Message {
  role: "system" | "user" | "assistant" | "tool";
  content: string;
}

export interface Handle {
  kind: "conversation";
  id: string;
}

export interface Request {
  provider: "local-recording";
  model: "echo-v1";
  messages: Array<Message>;
  state_handles: Array<Handle>;
}

export interface Artifact {
  receipt: string;
  sha256: string;
}

export interface HostAuthorization {
  id: string;
  classification: "public" | "internal" | "restricted";
  source_hash: string;
  expires_ms: number;
}

export interface Proof {
  origin: "host" | "memory" | "derived";
  sha256: string;
  artifacts: Array<Artifact>;
  host_authorizations: Array<HostAuthorization>;
}

export interface Manifest {
  segments: Array<Proof>;
  handles: Array<Proof>;
}

export interface Authorize {
  binding: Binding;
  request: Request;
  manifest: Manifest;
}

export interface Consume {
  binding: Binding;
  request: Request;
  manifest: Manifest;
  permit: string;
}

export interface Command0 {
  version: 1;
  action: "authorize";
  args: Authorize;
}

export interface Command1 {
  version: 1;
  action: "consume";
  args: Consume;
}

export interface Command2 {
  version: 1;
  action: "validate";
  args: Authorize;
}

export type InferenceCommand = Command0 | Command1 | Command2;
