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
