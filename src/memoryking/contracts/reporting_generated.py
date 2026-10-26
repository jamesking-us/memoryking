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

class ReportSourceBinding(TypedDict):
    id: str
    sha256: str
    readers: list[str]
    classification: Literal['public', 'internal', 'restricted']
    enabled: bool
    starts: int | None
    expires: int | None

class ReportPolicySettings(TypedDict):
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
    readers: list[str]

class ReportBindSourceArgs(TypedDict):
    project: str
    expected_version: int
    binding: ReportSourceBinding

class ReportPolicyArgs(TypedDict):
    project: str
    expected_version: int
    policy: ReportPolicySettings

class ReportUploadArgs(TypedDict):
    project: str
    upload: str
    name: str
    sha256: str
    bytes: int
    part: int
    parts: int
    data_base64: str

class ReportSubmitArgs(TypedDict):
    project: str
    report: str
    expected_revision: int
    upload: str
    program: str
    phase: str
    task: str | None
    title: str
    technical_status: Literal['complete', 'partial', 'blocked']
    classification: Literal['public', 'internal', 'restricted']
    dependencies: list[ReportDependency]

class ReportTargetArgs(TypedDict):
    project: str
    report: str
    revision: int

class ReportQAArgs(TypedDict):
    project: str
    report: str
    revision: int
    attestation: ReportQA

class ReportReaderArgs(TypedDict):
    project: str
    report: str
    revision: int
    page: int
    cursor: str | None

class ReportBindSourceCommand(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['bind_source']
    args: ReportBindSourceArgs

class ReportPolicyCommand(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['policy']
    args: ReportPolicyArgs

class ReportUploadCommand(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['upload']
    args: ReportUploadArgs

class ReportSubmitCommand(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['submit']
    args: ReportSubmitArgs

class ReportBuildCommand(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['build']
    args: ReportTargetArgs

class ReportQaCommand(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['qa']
    args: ReportQAArgs

class ReportPublishCommand(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['publish']
    args: ReportTargetArgs

class ReportWithdrawCommand(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['withdraw']
    args: ReportTargetArgs

class ReportStatusCommand(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['status']
    args: ReportTargetArgs

class ReportReaderCommand(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['reader']
    args: ReportReaderArgs

class ReportArtifactReadArgs(TypedDict):
    project: str
    report: str
    revision: int
    name: Literal['report.pdf', 'report.epub', 'reader.json', 'reader-pages.json']
    offset: int

class ReportDiscardArgs(TypedDict):
    project: str
    upload: str

class ReportArtifactCommand(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['artifact']
    args: ReportArtifactReadArgs

class ReportDiscardUploadCommand(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['discard_upload']
    args: ReportDiscardArgs

ReportCommand = ReportBindSourceCommand | ReportPolicyCommand | ReportUploadCommand | ReportSubmitCommand | ReportBuildCommand | ReportQaCommand | ReportPublishCommand | ReportWithdrawCommand | ReportStatusCommand | ReportReaderCommand | ReportArtifactCommand | ReportDiscardUploadCommand
