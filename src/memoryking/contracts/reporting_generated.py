"""Generated from closed authoritative mobile contracts."""
from __future__ import annotations

from typing import Literal, TypedDict


class ReportArtifact(TypedDict):
    kind: Literal['pdf', 'epub', 'reader']
    sha256: str
    bytes: int
    renderer: str

class ReportDependency(TypedDict):
    kind: Literal['observation', 'revision', 'external_source']
    id: str
    sha256: str

class ReportRevision(TypedDict):
    version: Literal[1]
    project: str
    report: str
    revision: int
    template: str
    template_version: int
    program: str
    phase: str
    task: str | None
    title: str
    technical_status: Literal['complete', 'partial', 'blocked']
    review_status: Literal['pending', 'approved', 'changes_requested']
    lifecycle: Literal['draft', 'building', 'built', 'qa_verified', 'published', 'withdrawn', 'failed']
    source_sha256: str
    model_sha256: str
    readme_sha256: str
    evidence_sha256: str
    dependencies: list[ReportDependency]
    artifacts: list[ReportArtifact]
    readers: list[str]
    classification: Literal['public', 'internal', 'restricted']

class ReportPolicy(TypedDict):
    version: Literal[1]
    project: str
    program: str
    template: str
    channel: Literal['app', 'kindle', 'both']
    destination: str | None
    recipient_principal: str | None
    require_visual_qa: Literal[True]
    require_evidence: Literal[True]
    classification: Literal['public', 'internal', 'restricted']

class ReportQA(TypedDict):
    version: Literal[1]
    report: str
    revision: int
    source_sha256: str
    pdf_sha256: str
    epub_sha256: str
    reader_sha256: str
    readme_sha256: str
    evidence_sha256: str
    pdf_pages_reviewed: int
    pdf_reviewed: Literal[True]
    epub_reviewed: Literal[True]
    method: str

class ReportDelivery(TypedDict):
    version: Literal[1]
    report: str
    revision: int
    artifact_sha256: str
    destination: str
    recipient_fingerprint: str
    policy_version: int
    state: Literal['queued', 'submitting', 'smtp_accepted', 'rejected', 'unknown', 'cancelled']
    device_delivery: Literal['unverified', 'observed']

ReportCapabilities = Literal['report_author', 'report_qa', 'report_publish', 'report_policy', 'report_delivery']
