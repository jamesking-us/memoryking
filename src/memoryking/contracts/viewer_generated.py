"""Generated from closed authoritative mobile contracts."""
from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


class BindArgs(TypedDict):
    principal: str
    issuer: Literal['https://github.com']
    user_id: str

class GrantArgs(TypedDict):
    principal: str
    project: str
    expected_version: int
    starts: int | None
    expires: int | None

class RevokeArgs(TypedDict):
    principal: str
    project: str
    expected_version: int

class PrincipalArgs(TypedDict):
    principal: str
    expected_version: int

class RebindArgs(TypedDict):
    principal: str
    user_id: str
    expected_version: int

class EmptyArgs(TypedDict):
    pass

class ViewerBind(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['bind']
    args: BindArgs

class ViewerGrant(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['grant']
    args: GrantArgs

class ViewerRevoke(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['revoke']
    args: RevokeArgs

class ViewerDisable(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['disable']
    args: PrincipalArgs

class ViewerRebind(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['rebind']
    args: RebindArgs

class ViewerLogoutAll(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['logout_all']
    args: PrincipalArgs

class ViewerList(TypedDict):
    version: Literal[1]
    incarnation: str
    key: str
    action: Literal['list']
    args: EmptyArgs

ViewerAdminCommand = ViewerBind | ViewerGrant | ViewerRevoke | ViewerDisable | ViewerRebind | ViewerLogoutAll | ViewerList

class ViewerLimits(TypedDict):
    active_sessions: int
    pending_logins: int
    list_items: int
    detail_bytes: int
    graph_nodes: int
    graph_edges: int
    response_bytes: int
    concurrency: int
    request_seconds: int

class ViewerConfig(TypedDict):
    version: Literal[1]
    enabled: bool
    tenant: str
    public_origin: str
    listen_host: Literal['127.0.0.1', '::1']
    listen_port: int
    transport: Literal['isolated_proxy', 'direct_tls']
    github_client_id: str
    github_secret_file: str
    tls_cert_file: str | None
    tls_key_file: str | None
    session_idle_seconds: int
    session_absolute_seconds: int
    login_seconds: int
    poll_seconds: int
    display_lifetime_seconds: int
    limits: ViewerLimits
    trusted_proxy: NotRequired[TrustedProxy]

class ViewerSubject(TypedDict):
    store: str
    tenant: str
    principal: str
    issuer: Literal['https://github.com']
    user_id: str
    incarnation: str
    generation: int
    policy_epoch: int

class ViewerError(TypedDict):
    schema_version: Literal[1]
    error: Literal['unauthenticated', 'unavailable', 'context_changed', 'invalid_cursor', 'invalid_request', 'rate_limited', 'service_unavailable']

class ViewerCriterion(TypedDict):
    id: str
    text: str
    state: Literal['pending', 'passed', 'failed', 'waived', 'unknown']
    evidence_available: bool
    evidence: list[ViewerEvidence]
    evidence_truncated: bool

class ViewerTask(TypedDict):
    id: str
    objective: str
    current_state: str
    criteria: list[ViewerCriterion]
    blockers: list[str]
    coverage: str
    checkpoint_revision: int
    progress: ViewerProgress
    text_truncated: bool
    plan_id: NotRequired[str]

class ViewerMemory(TypedDict):
    handle: str
    text: str
    authority: Literal['observed', 'proposed', 'accepted']
    observed_ms: int
    evidence_available: bool
    truncated: bool
    source: str
    classification: Literal['public', 'internal', 'restricted']

class ViewerBinding(TypedDict):
    store: str
    tenant: str
    principal: str
    project: str
    incarnation: str
    authorization_epoch: int
    evaluated_ms: int
    fresh_until_ms: int
    observed_position: int
    session_generation: int

class ViewerPage(TypedDict):
    schema_version: Literal[1]
    binding: ViewerBinding
    coverage: str
    truncated: bool
    next_cursor: str | None
    tasks: list[ViewerTask]
    memories: list[ViewerMemory]

class ViewerSessionData(TypedDict):
    kind: Literal['session']
    projects: list[str]
    csrf: str
    poll_seconds: int

class ViewerTaskData(TypedDict):
    kind: Literal['overview', 'tasks', 'task']
    tasks: list[ViewerTask]

class ViewerMemoryData(TypedDict):
    kind: Literal['memories', 'memory']
    memories: list[ViewerMemory]

class ViewerGraphNode(TypedDict):
    id: str
    label: str
    kind: Literal['task', 'criterion', 'observation', 'claim', 'plan', 'step']
    task: str | None
    handle: str | None
    phase: NotRequired[str]

class ViewerGraphEdge(TypedDict):
    source: str
    target: str
    kind: Literal['declares', 'uses', 'cites', 'depends_on']

class ViewerGraphData(TypedDict):
    kind: Literal['graph']
    nodes: list[ViewerGraphNode]
    edges: list[ViewerGraphEdge]

class ViewerReportItem(TypedDict):
    report: str
    revision: int
    title: str
    program: str
    phase: str
    task: str | None
    technical_status: Literal['complete', 'partial', 'blocked']
    review_status: Literal['pending', 'approved', 'changes_requested']
    channel: Literal['app', 'both']
    delivery_states: list[Literal['queued', 'submitting', 'smtp_accepted', 'rejected', 'unknown', 'cancelled']]
    device_delivery: Literal['unverified']
    created_ms: int

class ViewerReportListData(TypedDict):
    kind: Literal['reports']
    reports: list[ViewerReportItem]

class ViewerReportData(TypedDict):
    kind: Literal['report']
    report: str
    revision: int
    title: str
    program: str
    phase: str
    task: str | None
    technical_status: Literal['complete', 'partial', 'blocked']
    review_status: Literal['pending', 'approved', 'changes_requested']
    channel: Literal['app', 'both']
    delivery_states: list[Literal['queued', 'submitting', 'smtp_accepted', 'rejected', 'unknown', 'cancelled']]
    device_delivery: Literal['unverified']
    sections: int

class ViewerReportFragment(TypedDict):
    version: Literal[1]
    block: int
    part: int
    parts: int
    encoding: Literal['canonical-block-json']
    content: str

class ViewerReportSectionData(TypedDict):
    kind: Literal['report_section']
    page: int
    pages: int
    content: ViewerReportFragment

class ViewerReportImageData(TypedDict):
    kind: Literal['report_image']
    name: str
    sha256: str
    bytes: int
    offset: int
    data_base64: str
    next_offset: int | None

class ViewerPlanCounts(TypedDict):
    total: int
    todo: int
    doing: int
    done: int
    blocked: int
    skipped: int
    unknown: int
    completed: int
    resolved: int
    work_percent: int | None
    resolved_percent: int | None

class ViewerPlanPhase(TypedDict):
    id: str
    title: str
    state: Literal['empty', 'resolved', 'blocked', 'in_progress', 'not_started']
    progress: ViewerPlanCounts

class ViewerPlanStep(TypedDict):
    id: str
    phase: str
    description: str
    status: Literal['todo', 'doing', 'done', 'blocked', 'skipped', 'unknown']
    notes: str
    depends_on: list[str]
    evidence: list[ViewerEvidence]
    decision: str | None
    evidence_truncated: bool
    reported_status: Literal['todo', 'doing', 'done', 'blocked', 'skipped', 'unknown']

class ViewerPlanSummary(TypedDict):
    id: str
    title: str
    objective: str
    status: Literal['draft', 'active', 'done', 'abandoned']
    revision: int
    updated_ms: int
    progress: ViewerPlanCounts
    current_phase: str | None
    phase_count: int
    history_complete: bool

class ViewerPlanDetail(TypedDict):
    id: str
    title: str
    objective: str
    status: Literal['draft', 'active', 'done', 'abandoned']
    revision: int
    updated_ms: int
    progress: ViewerPlanCounts
    current_phase: str | None
    phase_count: int
    history_complete: bool
    context: str
    closure_note: str
    phases: list[ViewerPlanPhase]
    steps: list[ViewerPlanStep]
    lessons: list[ViewerEvidence]
    lessons_truncated: bool

class ViewerPlanListData(TypedDict):
    kind: Literal['plans']
    plans: list[ViewerPlanSummary]

class ViewerPlanData(TypedDict):
    kind: Literal['plan']
    plan: ViewerPlanDetail

ViewerData = ViewerSessionData | ViewerTaskData | ViewerMemoryData | ViewerGraphData | ViewerReportListData | ViewerReportData | ViewerReportSectionData | ViewerReportImageData | ViewerPlanListData | ViewerPlanData

class ViewerResponse(TypedDict):
    schema_version: Literal[1]
    binding: ViewerBinding
    coverage: str
    truncated: bool
    next_cursor: str | None
    data: ViewerData

class ViewerEvidence(TypedDict):
    kind: Literal['observation', 'knowledge']
    handle: str

class ViewerProgress(TypedDict):
    passed: int
    failed: int
    pending: int
    waived: int
    unknown: int
    total: int

class ViewerAuthContext(TypedDict):
    schema_version: Literal[1]
    csrf: str
    expires_ms: int

class ViewerAuthPost(TypedDict):
    csrf: str

class ViewerAuthStart(TypedDict):
    schema_version: Literal[1]
    authorization_url: str

class ViewerAuthResult(TypedDict):
    schema_version: Literal[1]
    status: Literal['signed_in', 'signed_out']

class TrustedProxy(TypedDict):
    kind: Literal['tailscale_serve_v1']
    source_ips: list[str]
