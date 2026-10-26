// Generated from closed authoritative mobile contracts.

export interface ReportArtifact {
  kind: "pdf" | "epub" | "reader";
  sha256: string;
  bytes: number;
  renderer: string;
}

export interface ReportDependency {
  kind: "observation" | "revision" | "external_source";
  id: string;
  sha256: string;
}

export interface ReportRevision {
  version: 1;
  project: string;
  report: string;
  revision: number;
  template: string;
  template_version: number;
  program: string;
  phase: string;
  task: string | null;
  title: string;
  technical_status: "complete" | "partial" | "blocked";
  review_status: "pending" | "approved" | "changes_requested";
  lifecycle: "draft" | "building" | "built" | "qa_verified" | "published" | "withdrawn" | "failed";
  source_sha256: string;
  model_sha256: string;
  readme_sha256: string;
  evidence_sha256: string;
  dependencies: Array<ReportDependency>;
  artifacts: Array<ReportArtifact>;
  readers: Array<string>;
  classification: "public" | "internal" | "restricted";
}

export interface ReportPolicy {
  version: 1;
  project: string;
  program: string;
  template: string;
  channel: "app" | "kindle" | "both";
  destination: string | null;
  recipient_principal: string | null;
  require_visual_qa: true;
  require_evidence: true;
  classification: "public" | "internal" | "restricted";
}

export interface ReportQA {
  version: 1;
  report: string;
  revision: number;
  source_sha256: string;
  pdf_sha256: string;
  epub_sha256: string;
  reader_sha256: string;
  readme_sha256: string;
  evidence_sha256: string;
  pdf_pages_reviewed: number;
  pdf_reviewed: true;
  epub_reviewed: true;
  method: string;
}

export interface ReportDelivery {
  version: 1;
  report: string;
  revision: number;
  artifact_sha256: string;
  destination: string;
  recipient_fingerprint: string;
  policy_version: number;
  state: "queued" | "submitting" | "smtp_accepted" | "rejected" | "unknown" | "cancelled";
  device_delivery: "unverified" | "observed";
}

export type ReportCapabilities = "report_author" | "report_qa" | "report_publish" | "report_policy" | "report_delivery";

export interface ReportSourceBinding {
  id: string;
  sha256: string;
  readers: Array<string>;
  classification: "public" | "internal" | "restricted";
  enabled: boolean;
  starts: number | null;
  expires: number | null;
}

export interface ReportPolicySettings {
  version: 1;
  project: string;
  program: string;
  template: string;
  channel: "app" | "kindle" | "both";
  destination: string | null;
  recipient_principal: string | null;
  require_visual_qa: true;
  require_evidence: true;
  classification: "public" | "internal" | "restricted";
  readers: Array<string>;
}

export interface ReportBindSourceArgs {
  project: string;
  expected_version: number;
  binding: ReportSourceBinding;
}

export interface ReportPolicyArgs {
  project: string;
  expected_version: number;
  policy: ReportPolicySettings;
}

export interface ReportUploadArgs {
  project: string;
  upload: string;
  name: string;
  sha256: string;
  bytes: number;
  part: number;
  parts: number;
  data_base64: string;
}

export interface ReportSubmitArgs {
  project: string;
  report: string;
  expected_revision: number;
  upload: string;
  program: string;
  phase: string;
  task: string | null;
  title: string;
  technical_status: "complete" | "partial" | "blocked";
  classification: "public" | "internal" | "restricted";
  dependencies: Array<ReportDependency>;
}

export interface ReportTargetArgs {
  project: string;
  report: string;
  revision: number;
}

export interface ReportQAArgs {
  project: string;
  report: string;
  revision: number;
  attestation: ReportQA;
}

export interface ReportReaderArgs {
  project: string;
  report: string;
  revision: number;
  page: number;
  cursor: string | null;
}

export interface ReportBindSourceCommand {
  version: 1;
  incarnation: string;
  key: string;
  action: "bind_source";
  args: ReportBindSourceArgs;
}

export interface ReportPolicyCommand {
  version: 1;
  incarnation: string;
  key: string;
  action: "policy";
  args: ReportPolicyArgs;
}

export interface ReportUploadCommand {
  version: 1;
  incarnation: string;
  key: string;
  action: "upload";
  args: ReportUploadArgs;
}

export interface ReportSubmitCommand {
  version: 1;
  incarnation: string;
  key: string;
  action: "submit";
  args: ReportSubmitArgs;
}

export interface ReportBuildCommand {
  version: 1;
  incarnation: string;
  key: string;
  action: "build";
  args: ReportTargetArgs;
}

export interface ReportQaCommand {
  version: 1;
  incarnation: string;
  key: string;
  action: "qa";
  args: ReportQAArgs;
}

export interface ReportPublishCommand {
  version: 1;
  incarnation: string;
  key: string;
  action: "publish";
  args: ReportTargetArgs;
}

export interface ReportWithdrawCommand {
  version: 1;
  incarnation: string;
  key: string;
  action: "withdraw";
  args: ReportTargetArgs;
}

export interface ReportStatusCommand {
  version: 1;
  incarnation: string;
  key: string;
  action: "status";
  args: ReportTargetArgs;
}

export interface ReportReaderCommand {
  version: 1;
  incarnation: string;
  key: string;
  action: "reader";
  args: ReportReaderArgs;
}

export interface ReportArtifactReadArgs {
  project: string;
  report: string;
  revision: number;
  name: "report.pdf" | "report.epub" | "reader.json" | "reader-pages.json";
  offset: number;
}

export interface ReportDiscardArgs {
  project: string;
  upload: string;
}

export interface ReportArtifactCommand {
  version: 1;
  incarnation: string;
  key: string;
  action: "artifact";
  args: ReportArtifactReadArgs;
}

export interface ReportDiscardUploadCommand {
  version: 1;
  incarnation: string;
  key: string;
  action: "discard_upload";
  args: ReportDiscardArgs;
}

export interface ReportDeliveryStatusArgs {
  project: string;
  delivery: string;
}

export interface ReportReconcileArgs {
  project: string;
  delivery: string;
  expected_attempts: number;
  outcome: "smtp_accepted" | "not_submitted";
  evidence: ReportDependency;
}

export interface ReportDeliveryStatusCommand {
  version: 1;
  incarnation: string;
  key: string;
  action: "delivery_status";
  args: ReportDeliveryStatusArgs;
}

export interface ReportReconcileCommand {
  version: 1;
  incarnation: string;
  key: string;
  action: "reconcile";
  args: ReportReconcileArgs;
}

export type ReportCommand = ReportBindSourceCommand | ReportPolicyCommand | ReportUploadCommand | ReportSubmitCommand | ReportBuildCommand | ReportQaCommand | ReportPublishCommand | ReportWithdrawCommand | ReportStatusCommand | ReportReaderCommand | ReportArtifactCommand | ReportDiscardUploadCommand | ReportDeliveryStatusCommand | ReportReconcileCommand;
