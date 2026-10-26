// Generated from closed authoritative mobile contracts.

export interface BindArgs {
  principal: string;
  issuer: "https://github.com";
  user_id: string;
}

export interface GrantArgs {
  principal: string;
  project: string;
  expected_version: number;
  starts: number | null;
  expires: number | null;
}

export interface RevokeArgs {
  principal: string;
  project: string;
  expected_version: number;
}

export interface PrincipalArgs {
  principal: string;
  expected_version: number;
}

export interface RebindArgs {
  principal: string;
  user_id: string;
  expected_version: number;
}

export interface EmptyArgs {
}

export interface ViewerBind {
  version: 1;
  incarnation: string;
  key: string;
  action: "bind";
  args: BindArgs;
}

export interface ViewerGrant {
  version: 1;
  incarnation: string;
  key: string;
  action: "grant";
  args: GrantArgs;
}

export interface ViewerRevoke {
  version: 1;
  incarnation: string;
  key: string;
  action: "revoke";
  args: RevokeArgs;
}

export interface ViewerDisable {
  version: 1;
  incarnation: string;
  key: string;
  action: "disable";
  args: PrincipalArgs;
}

export interface ViewerRebind {
  version: 1;
  incarnation: string;
  key: string;
  action: "rebind";
  args: RebindArgs;
}

export interface ViewerLogoutAll {
  version: 1;
  incarnation: string;
  key: string;
  action: "logout_all";
  args: PrincipalArgs;
}

export interface ViewerList {
  version: 1;
  incarnation: string;
  key: string;
  action: "list";
  args: EmptyArgs;
}

export type ViewerAdminCommand = ViewerBind | ViewerGrant | ViewerRevoke | ViewerDisable | ViewerRebind | ViewerLogoutAll | ViewerList;

export interface ViewerLimits {
  active_sessions: number;
  pending_logins: number;
  list_items: number;
  detail_bytes: number;
  graph_nodes: number;
  graph_edges: number;
  response_bytes: number;
  concurrency: number;
  request_seconds: number;
}

export interface ViewerConfig {
  version: 1;
  enabled: boolean;
  tenant: string;
  public_origin: string;
  listen_host: "127.0.0.1" | "::1";
  listen_port: number;
  transport: "isolated_proxy" | "direct_tls";
  github_client_id: string;
  github_secret_file: string;
  tls_cert_file: string | null;
  tls_key_file: string | null;
  session_idle_seconds: number;
  session_absolute_seconds: number;
  login_seconds: number;
  poll_seconds: number;
  display_lifetime_seconds: number;
  limits: ViewerLimits;
}

export interface ViewerSubject {
  store: string;
  tenant: string;
  principal: string;
  issuer: "https://github.com";
  user_id: string;
  incarnation: string;
  generation: number;
  policy_epoch: number;
}

export interface ViewerError {
  schema_version: 1;
  error: "unauthenticated" | "unavailable" | "context_changed" | "invalid_cursor" | "invalid_request" | "rate_limited" | "service_unavailable";
}

export interface ViewerCriterion {
  id: string;
  text: string;
  state: "pending" | "passed" | "failed" | "waived" | "unknown";
  evidence_available: boolean;
  evidence: Array<ViewerEvidence>;
  evidence_truncated: boolean;
}

export interface ViewerTask {
  id: string;
  objective: string;
  current_state: string;
  criteria: Array<ViewerCriterion>;
  blockers: Array<string>;
  coverage: string;
  checkpoint_revision: number;
  progress: ViewerProgress;
  text_truncated: boolean;
}

export interface ViewerMemory {
  handle: string;
  text: string;
  authority: "observed" | "proposed" | "accepted";
  observed_ms: number;
  evidence_available: boolean;
  truncated: boolean;
  source: string;
  classification: "public" | "internal" | "restricted";
}

export interface ViewerBinding {
  store: string;
  tenant: string;
  principal: string;
  project: string;
  incarnation: string;
  authorization_epoch: number;
  evaluated_ms: number;
  fresh_until_ms: number;
  observed_position: number;
  session_generation: number;
}

export interface ViewerPage {
  schema_version: 1;
  binding: ViewerBinding;
  coverage: string;
  truncated: boolean;
  next_cursor: string | null;
  tasks: Array<ViewerTask>;
  memories: Array<ViewerMemory>;
}

export interface ViewerSessionData {
  kind: "session";
  projects: Array<string>;
  csrf: string;
  poll_seconds: number;
}

export interface ViewerTaskData {
  kind: "overview" | "tasks" | "task";
  tasks: Array<ViewerTask>;
}

export interface ViewerMemoryData {
  kind: "memories" | "memory";
  memories: Array<ViewerMemory>;
}

export interface ViewerGraphNode {
  id: string;
  label: string;
  kind: "task" | "criterion";
}

export interface ViewerGraphEdge {
  source: string;
  target: string;
  kind: "declares";
}

export interface ViewerGraphData {
  kind: "graph";
  nodes: Array<ViewerGraphNode>;
  edges: Array<ViewerGraphEdge>;
}

export interface ViewerReportItem {
  report: string;
  revision: number;
  title: string;
  program: string;
  phase: string;
  task: string | null;
  technical_status: "complete" | "partial" | "blocked";
  review_status: "pending" | "approved" | "changes_requested";
  channel: "app" | "both";
  delivery_states: Array<"queued" | "submitting" | "smtp_accepted" | "rejected" | "unknown" | "cancelled">;
  device_delivery: "unverified";
  created_ms: number;
}

export interface ViewerReportListData {
  kind: "reports";
  reports: Array<ViewerReportItem>;
}

export interface ViewerReportData {
  kind: "report";
  report: string;
  revision: number;
  title: string;
  program: string;
  phase: string;
  task: string | null;
  technical_status: "complete" | "partial" | "blocked";
  review_status: "pending" | "approved" | "changes_requested";
  channel: "app" | "both";
  delivery_states: Array<"queued" | "submitting" | "smtp_accepted" | "rejected" | "unknown" | "cancelled">;
  device_delivery: "unverified";
  sections: number;
}

export interface ViewerReportFragment {
  version: 1;
  block: number;
  part: number;
  parts: number;
  encoding: "canonical-block-json";
  content: string;
}

export interface ViewerReportSectionData {
  kind: "report_section";
  page: number;
  pages: number;
  content: ViewerReportFragment;
}

export interface ViewerReportImageData {
  kind: "report_image";
  name: string;
  sha256: string;
  bytes: number;
  offset: number;
  data_base64: string;
  next_offset: number | null;
}

export type ViewerData = ViewerSessionData | ViewerTaskData | ViewerMemoryData | ViewerGraphData | ViewerReportListData | ViewerReportData | ViewerReportSectionData | ViewerReportImageData;

export interface ViewerResponse {
  schema_version: 1;
  binding: ViewerBinding;
  coverage: string;
  truncated: boolean;
  next_cursor: string | null;
  data: ViewerData;
}

export interface ViewerEvidence {
  kind: "observation" | "knowledge";
  handle: string;
}

export interface ViewerProgress {
  passed: number;
  failed: number;
  pending: number;
  waived: number;
  unknown: number;
  total: number;
}

export interface ViewerAuthContext {
  schema_version: 1;
  csrf: string;
  expires_ms: number;
}

export interface ViewerAuthPost {
  csrf: string;
}

export interface ViewerAuthStart {
  schema_version: 1;
  authorization_url: string;
}

export interface ViewerAuthResult {
  schema_version: 1;
  status: "signed_in" | "signed_out";
}
