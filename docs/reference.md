# Feature and API reference

Use the [agent guide](agent-guide.md) for workflows and [deployment guide](deployment.md) for setup. This page maps implemented interfaces and their limits; schemas and `--help` provide exact required fields.

## Interfaces

| Interface | Entry point | Scope |
| --- | --- | --- |
| Local CLI | `python -m memoryking` / `memoryking` | Initialization, serving, JSON requests and offline recovery. |
| Python client | `memoryking.sdk.LocalClient` | Local Unix-socket client bound to identity/project/history. |
| Shared CLI/client | `python -m memoryking.shared`, `memoryking.shared_sdk.SharedClient` | PostgreSQL operators and authenticated HTTP requests. |
| MCP stdio | `python -m memoryking.adapters.stdio` | Eight model-facing memory tools; daemon must already run. |
| Shared MCP | `/v1/tenants/{tenant}/projects/{project}/mcp` | Authenticated Streamable HTTP transport. |
| Reference agent | `python -m memoryking.reference_agent --help` | Create-file/readback task harness, checkpoint/recovery and local recording provider. |
| Repository worker | `python -m memoryking.repository_worker --help` | One bounded passive repository validation job. |
| Embedding worker | `python -m memoryking.embedding_worker --help` | One configured local-model indexing job. |
| Migration | `python -m memoryking.migration --help` | Detached legacy snapshot inspection, planning, import and reconciliation. |

The local CLI reads `{"operation":"NAME","args":{...}}` from stdin. Shared operations add top-level `"version":1`. Structured domain commands inside `args.command` carry their own version, current `incarnation`, action and project-scoped arguments; mutations also require a stable key. Use current context values, not copied example history IDs.

Authoritative contracts:

| Area | Schema / complete example |
| --- | --- |
| Capture, grants, exports and jobs | [Local wire](../src/memoryking/adapters/local_wire.schema.json), [local example](../tools/local_demo.py). |
| Reviewed knowledge and constraints | [Knowledge](../src/memoryking/adapters/knowledge.schema.json), [review example](../tools/knowledge_demo.py). |
| Brief, fetch, receipt, replay, rerun and atlas | [Retrieval](../contracts/retrieval-v1.schema.json), [briefing example](../tools/briefing_demo.py). |
| Checkpoints, operations and activity | [Working state](../contracts/working-v1.schema.json), [working example](../tools/working_demo.py). |
| Recipient handoffs | [Handoff](../contracts/handoff-v1.schema.json), [handoff example](../tools/handoff_demo.py). |
| Authorized inference | [Inference](../contracts/inference-v1.schema.json), [context example](../tools/inference_demo.py). |
| Repository snapshots and dependencies | [Repository](../contracts/repository-v1.schema.json), [repository example](../tools/repository_demo.py). |
| Embeddings and source-backed processing | [Embeddings](../contracts/embedding-v1.schema.json), [processing](../contracts/processing-v1.schema.json). |
| Procedures, outcomes and ranking | [Experience](../contracts/experience-v1.schema.json), [experience example](../tools/experience_demo.py). |
| Browser inspection | [Inspection](../contracts/inspection-v1.schema.json), [atlas example](../tools/atlas_demo.py). |

`@memoryking/contracts` supplies generated TypeScript declarations and Node runtime validators. It is built/packed locally, not published to npm, and is not a TypeScript transport SDK. Python exposes `load_strict`, `validate_contract`, `canonical_bytes` and `canonical_hash`. Canonical JSON is limited to 1 MiB/depth 64 with safe integral numbers and deterministic keys; validation does not authenticate a caller.

## Feature behavior

**Knowledge.** Observations can be recalled without a model. Claims remain proposals until reviewed. Acceptance, quarantine, deprecation, attestation and partial replacement are explicit versioned actions. Constraint resolution is complete within its bounds and blocks on ambiguity, missing essential evidence or unknown applicability.

**Working state.** Checkpoints preserve hard constraints, failures, verification and next actions. Expected revisions prevent stale writers from overwriting newer state. Handoffs are recipient-bound. Operation intent/transition/reconciliation records track uncertain effects; the host implements the actual external tool and readback.

**Repository context.** Registration and snapshots are trusted-host operations. Git/dirty artifact identity and supported passive source projections inform dependency checks. No repository code is executed. Shallow or incomplete ancestry and unsupported coverage remain unknown rather than current.

**Dashboard.** Task activity, criteria/history, current authorized evidence, force-directed graph/list, source impact, receipts, usage inspection, review and retention previews are implemented. Nodes and counts represent bounded visible results, not the whole corpus. Layout distance is not confidence. The crowned cybernetic elephant has an optional 1.8-second depth tilt, with a static fallback when reduced motion is enabled. The animation stops when hidden or offscreen. Gunmetal panels, subtle CSS textures and cyan accents are available in dark and light themes; assets are served locally. Ordinary use requires no development reports.

**Procedures and learning.** Procedures require separate review and activation. Outcomes and helpfulness are actor reports tied to evidence/receipts. Feedback ranking is disabled by default; an administrator may run a frozen bounded comparison and explicitly activate or roll back a configuration. It cannot add candidates, override policy or prove a causal quality improvement. There is no autonomous learner or scheduler.

## Optional capabilities

| Capability | Enablement | Boundary |
| --- | --- | --- |
| Exact token counting | Install `requirements-tokenizers.lock`; request `cl100k_base`. | Missing runtime is explicit; `utf8-byte-estimate-v1` is a disclosed estimate. |
| Local semantic retrieval | Install `requirements-embeddings.lock`, explicitly install the pinned model, configure an experimental partition and run an embedding worker. | Default lexical retrieval remains available. No model download happens on startup. |
| pgvector exact search | Reviewed extension 0.8.7 in its dedicated schema, explicitly enabled by an operator. | Shared only; no approximate HNSW/IVFFlat index. Missing/wrong extension returns unavailable. |
| Adapter deltas | Retain a matching authorized base through the resume adapter. | Transport optimization; full model context is reconstructed. No automatic model-token savings claim. |
| Source-backed compilation | `processing` commands; deterministic extraction by default. | Candidate text must preserve validated whole source units and essential state or fall back. Remote generation is not included. |
| Feedback ranking | Explicit proposal, evaluation and activation. | Bounded development experiment; disabled by default. |

The model installer command is `python -m memoryking.model_install --destination NEW_MODEL_DIRECTORY`. It downloads the files pinned in the [model manifest](../src/memoryking/adapters/models/minilm-l6-v2-onnx.json), verifies hashes and never loads model-repository Python. Review its license and network requirements before opting in. The CPU model produces 384-dimensional vectors and reads only the first 256 tokens per input; truncation is disclosed.

Configure through the embedding API with a current history, expected generation and model fingerprint. The one-shot worker takes `--model`, `--credential`, `--project` and either `--socket` or `--shared-url` plus `--tenant`/`--ca-file`. Installing a runtime does not activate a partition, provision a provider account or authorize remote egress.

## Current limits

| Area | Implemented bound / behavior |
| --- | --- |
| Local request | 256 KiB wire body; 64 KiB captured text; one request per socket connection. |
| Briefing | Full response must fit requested budget; essentials are not silently truncated. A useful result can still be blocked or insufficient. |
| Authorization | Five-second evaluation-to-commit/first-byte deadline, with UTC and monotonic checks. |
| Idempotency | Replay bodies retained for 30 days; expired/deleted results do not free a consumed identity. |
| Generic jobs | 30-second leases, five attempts, bounded retry backoff, 1,000 unfinished jobs and four live leases per tenant. |
| Mandatory resolution | 256 candidate revisions/bindings and 65,536 atom-binding work units; overflow blocks. |
| Atlas | Default 200 nodes/400 edges; maxima 500/1,500; 196,608-byte response; cursor expires after 60 seconds. Counts describe only the returned window. |
| Dense retrieval | Exact authorized scan capped at 10,000 eligible vectors and 256 candidates; insufficient index/frontier is explicit. |
| Processing usage | Up to 100,000 retained attempts per tenant; unknown costs stay null, with known lower bounds. |
| Experience | 10,000 immutable records; up to 256 latest visible records per listing; no unbounded pagination/compaction. |
| Browser | 60-second bootstrap, 15-minute session, five-second polling and 15-second snapshot freshness; hidden/expired views clear. |
| Shared live updates | Eight streams, bounded samples/cursor history, resync/polling fallback. Not an unbounded event replay service. |

The product has been exercised on macOS with Python 3.12 and native PostgreSQL 18.6, and through a single-host Linux-container rehearsal. A full native Linux matrix, Windows, physical touch, complete OS reduced-motion/browser coverage and third-party agent hosts remain unqualified. Intermittent dashboard scope refresh loss needs further investigation.

Measured local/shared workload targets have not been met. No real-task success, noninferiority, longitudinal-learning or full-cost savings result is established. Automatic failover, production PITR and disaster RPO/RTO are not promised. Benchmark commands are available to measure your environment; their successful exit alone is not proof that a target passed.

## Importing legacy memory

The importer recognizes `kingforge-v1`, `standalone` and `universal` SQLite profiles. Start with a verified detached backup; do not copy a live main file and discard its WAL. Snapshot copying rejects sidecars, symlinks and files over 512 MiB.

Workflow: `memoryking.migration snapshot` → `inspect` → `plan` → bounded `apply` batches → `reconcile`. Run each subcommand with `--help` for the exact options. [The migration example](../tools/migration_demo.py) executes the entire local flow; shared import uses the same commands with `--url`, `--tenant` and verified `--ca-file` instead of `--socket`.

Inspection is bounded to 128 ordinary tables, 10,000 rows, 60 KiB per canonical row and 64 MiB total canonical row data. Unknown schemas, unresolved references, unstable keys or hash mismatches block. Apply defaults to 100 rows, maximum 500. Keep its source ID, snapshot and journal stable across retries.

Each imported row becomes restricted historical evidence; legacy approval and role labels do not become current permission. Lessons become unaccepted, nonexecutable proposals. Retired rows/dependents become tombstones. Reconciliation checks current target records and references but is not a lossless reverse importer, two-way sync or live cutover procedure. Production datasets and client behavior require their own rehearsal.

## Troubleshooting

| Result | What to do |
| --- | --- |
| `no_relevant_memory` | Check project, access, query and evidence; an empty result can be correct. |
| `budget_insufficient` | Increase the explicit budget or narrow task scope; do not strip required constraints. |
| `applicability_unknown`, `essential_policy_unavailable` | Supply valid context or resolve policy/evidence access with its owner. |
| `context_changed`, `history_changed` | Discard stale bindings, reconcile and create a fresh client/plan deliberately. |
| `idempotency_conflict`, `source_conflict` | Investigate mismatched bytes; do not invent a new key to bypass the conflict. |
| `source_retired`, `idempotency_result_expired` | The identity remains consumed. Reconcile instead of repeating the mutation. |
| `stale_lease`, `stale_source` | Discard stale output and resolve current source/worker state. |
| `queue_full`, `service_busy` | Back off and drain bounded work; inspect contention. No mutation retry is automatic. |
| `release_expired`, `clock_untrusted` | Restore trustworthy timing and request a fresh authorized response. |
| `recovery_required`, `journal_rollback`, `backup_invalid` | Stay offline and use verified independent recovery evidence. |
| `session_changed` | Sign in again; permissions changed and retained views were cleared. |

## Viewer and report contracts

[Viewer v1](../contracts/viewer-v1.schema.json) defines owner operations, fixed-issuer
subject metadata, configuration, bounded task/memory DTOs and error shapes.
[Reporting v1](../contracts/reporting-v1.schema.json) defines immutable revision,
artifact, QA, capability and channel-policy types for the upcoming built-in engine.
Generated Python/TypeScript and packaged schemas accompany these sources. Python
`memoryking.contracts.viewer.validate_viewer` validates a named definition; it does
not authenticate a provider subject or grant permissions.

`viewer-admin` is an owner-only CLI/IPC operation, outside the eight MCP tools. Its
commands are listed in [viewer access](deployment.md#viewer-access-foundation).
`ViewerIdentity.resolve_authenticated_subject` is an internal trusted-adapter lookup,
not a browser login API. Its signed read-only context is never returned to browser
code; canonical project/content/dependency checks still execute on reads.

Identity-specific errors include `viewer_binding_conflict`, `revision_conflict`,
`viewer_policy_operation_required`, `invalid_viewer_request` and `viewer_limit`.
The operator can inspect its own bindings; callers without owner authority receive
no inventory. Login and viewer HTTP routes are not yet implemented. Do not expose the administrator dashboard as a substitute phone app.

## Experimental report commands

`LocalClient.report(command)` and `SharedClient.report(command)` use the closed
`ReportCommand` schema. The local CLI accepts the same JSON through stdin:

```sh
memoryking report --socket "$MK_DATA/service.sock" --credential "$MK_REPORT_CREDENTIAL" < report-command.json
```

A command contains `version: 1`, the current `incarnation`, a stable `key`, an
`action` and `args.project`. Keys bind canonical payloads; reuse with changed
content fails. The SDK does not automatically retry mutations.

| Action | Capability and purpose |
| --- | --- |
| `policy`, `bind_source` | `report_policy`: set a version-checked program policy or external source binding. |
| `upload`, `discard_upload` | `report_author`: upload ordered 64 KiB chunks or discard the caller's unfinished upload. |
| `submit`, `build` | `report_author`: create an immutable revision or queue its build. |
| `qa` | `report_qa`: attest exact source/artifact/README/evidence hashes and reviewed PDF page count. |
| `publish`, `withdraw` | `report_publish`: publish a verified revision or remove its current visibility. |
| `status`, `reader`, `list`, `image` | `read` plus all current report/dependency permissions. Drafts are visible only to their author or permitted QA/publish actors. |
| `artifact` | `export` plus report/dependency permissions: bounded operator PDF/EPUB/reader export. Read-only viewers cannot use it. |
| `delivery_status` | `report_delivery` plus project read: minimal outcome and reconciliation identity. |
| `reconcile` | `report_policy` plus project read: exact delivery/attempt, explicit outcome and a currently permitted evidence dependency. |
| `sender_status`, `import_delivery` | `report_policy` plus project read: inspect the fixed sender binding or reserve a verified accepted legacy delivery while sending is disabled. |

Report authoring never grants readers or chooses recipients. Policies default to `channel: app`, with null destination/recipient. Kindle/both
requires a fixed destination supplied by the trusted embedding host; the policy
records its recipient principal and fingerprint. The internal sender requires an
explicit `report_delivery` credential and enabled host configuration. The optional `serve --report-config` worker schedules builds and sends. Operator import preserves accepted legacy identities; the installation's trusted collector must verify old receipts and permanently disable its previous sender before activation. No report command enters the eight-tool model MCP
catalog.

Inputs are `source.md`, `readme.md`, `evidence.json`, and identifier-named PNG/JPEG
figures. There are at most 61 uploaded files, 50 MiB total and 2 MiB per text input;
each raster is at most 8 MiB/12 million pixels. Source must contain the 17 numbered
sections in order. Every non-narrative input hash must have a current dependency
binding. Unfinished inputs are bounded to 50 MiB per author/project; discard them
when abandoned. The service verifies bytes and permissions, not the truth of an
author's evidence claims.

The worker entry point is `build_one(store, credential, project)` in
`memoryking.adapters.report_build`. The embedding service owns the store and
supplies an explicitly granted credential. A process lease permits one renderer
per store root and the queue holds ten waiting builds per service tenant. Rendering
runs outside canonical request transactions. Interrupted builds become failed;
submit a new revision after correcting the cause. Both service profiles can schedule this worker from explicit private configuration.
A queued report unavailable to the worker does not block another permitted build.

Each reader response is at most 64 KiB and carries a history-, principal-, policy-
and revision-bound cursor. Fragments reassemble canonical block JSON in order;
content must be rendered as data, never raw HTML. The phone reader is not yet
implemented. `list` takes a limit of 1–50 and a nullable cursor; it returns only
currently permitted published app reports, ordered by report/revision. It exposes
no total or hidden identities. Cursors bind identity/history/policy and contain only
the last visible report. Each page scans at most 512 candidates; an exhausted scan
with no visible continuation fails explicitly. This is a live authorized page, not
a frozen catalog snapshot. `image` reads only referenced, validated static PNG/JPEG
inputs from a published app report in bounded chunks; it cannot export source files. Operator artifact exports carry a fixed hash, total length, offset
and at most 32 KiB of base64-decoded bytes per response.

Errors include `report_policy_required`, `report_source_binding_required`,
`report_dependency_unavailable`, `report_qa_mismatch`, `report_policy_changed`,
`report_worker_fenced`, `report_worker_busy`, `report_input_limit`,
`report_delivery_unavailable` and `report_sandbox_unavailable`, `report_sender_disabled`,
`report_destination_changed`, `report_delivery_backoff` and
`report_reconciliation_conflict`. None establishes
human approval or provider/device delivery.

The internal delivery adapter loads a protected operator-supplied JSON file with
`version`, `destination`, `GMAIL_FROM`, `GMAIL_APP_KEY` and `KINDLE_TO`. It never
reads shell startup files. Gmail uses verified TLS at the fixed host/port; authors
cannot change the envelope. Complete MIME is limited to 24,000,000 bytes. SMTP
acceptance means provider acceptance only. A connection failure after a possible
content write is `unknown` and is never automatically retried. `reconcile` accepts
`smtp_accepted` or independently evidenced `not_submitted`; the latter explicitly
allows a new bounded attempt cycle after current authority is rechecked. A stable
Message-ID alone is insufficient evidence.

`attempts` is a monotonically increasing delivery attempt identity; it does not
reset during reconciliation. `expected_attempts` must match that exact identity
(1–99). A separate private cycle counter enforces the three-attempt automatic
retry budget. A stale reconciliation cannot authorize or accept a later cycle.
Experimental pre-release records that already reset their counter without a cycle
marker fail closed with `report_delivery_attempt_history_required`; their attempt
identity must be reconstructed from retained independent evidence before migration.

Delivery status includes `outcome_ms` and `outcome_source`. The time records the
persisted observation, or the explicit reconciliation time; it is not a provider's
independent clock or device-arrival proof. `data_reply` identifies a directly
observed SMTP reply; `operator_reconciliation` identifies an evidenced operator
decision. Report status includes its delivery IDs and channel states, protected by
the same whole-report permissions as its other metadata.

`import_delivery` takes a report ID and a closed `ReportLegacyDelivery` record:
program/phase/revision, legacy identity, receipt/source/EPUB hashes, original
Message-ID, acceptance time, destination fingerprint and an authorized receipt
evidence dependency. The caller attests verification of the original files and
provider receipt; the service cannot infer their truth from hashes. An identical
import returns the original identity. Changed payloads, report aliases, occupied
revisions, wrong destinations and enabled senders fail. The independent ledger
reserves the revision across content restores. Import creates no app content,
artifacts or queued mail. Unknown historical outcomes need reconciliation before
transfer; importing them as accepted is invalid. See [sender transfer](deployment.md#replacing-an-existing-report-sender).

[Back to README](../README.md) · [Agent guide](agent-guide.md) · [Deployment](deployment.md)

## Restricted viewer API foundation

`memoryking serve --viewer-config FILE` and `memoryking-shared serve --viewer-config FILE` accept the closed `ViewerConfig` contract. Omit the option, or set `enabled` to false, to leave the listener disabled. The file must be a private regular file. The dedicated listener binds only configured loopback, requires its exact HTTPS public-origin Host, and supports either direct TLS with a private key or explicitly selected isolated proxy transport. This does not relax the shared agent transport's TLS rules. A production browser session cannot yet be issued in this build: public shell/assets work, while private requests remain unauthenticated. Do not expose this development foundation as a working phone installation.

The implemented read routes are GET/HEAD `/viewer/api/v1/session` and `/viewer/api/v1/projects/{project}/` followed by `overview`, `tasks`, `tasks/{task}`, `memories`, `memories/{handle}`, `graph`, `reports`, `reports/{report}/versions/{revision}`, and that report version's `sections/{index}` or `images/{name}`. No owner, agent, bootstrap, raw artifact, mutation or retry route is delegated. Internal integration tests supply a trusted synthetic session; no configuration field or HTTP header enables that test seam.

Lists accept `limit` (maximum 50) and `cursor`; memories also accept `q` (maximum 256 characters). Lists default to 25 tasks/reports or 20 memories, capped by configured `list_items`. Tasks inspect at most 64 current task-stream candidates, preserve unknown criteria and state explicit coverage. Memory inspection combines permitted observations with currently eligible accepted knowledge; missing applicability inputs or incomplete mandatory policy block the view. Handles and cursors bind the session, principal, project, query, tenant policy/purge epochs, source frontier and history. Refresh after a changed context. Report sections are ordered, lossless JSON block fragments; a continuation cursor is required after section zero. Figure responses contain bounded verified raster chunks; the app never receives a filesystem path or raw report export.

Private responses carry a closed `ViewerResponse` envelope with current authorization/freshness metadata, declared coverage and truncation. Maximum response size is 256 KiB; memory text is bounded to 16 KiB. Display lifetime is at most 15 seconds and is cut at known grant, policy, source and verification boundaries. The canonical persisted evaluation instant and five-second release deadline remain authoritative. A receipt is not human approval. The optional graph currently describes task/criterion containment and supplies an equivalent node list; it does not invent dependency edges or agent liveness.

Authenticated GET/HEAD may omit Origin. Supplied unexpected origins, cross-site private requests, ambiguous Host/cookie/origin headers, encoded path aliases, request bodies and unsupported parameters fail. Forwarded identity/host headers grant no authority. Responses are no-store with a restrictive same-origin CSP. Routine logging keeps only fixed outcome counters, never paths, queries, identities or cookies. Concurrent connections are bounded; excess connections close before request parsing, and backend pressure returns a neutral unavailable response where admission succeeds. Phone/browser qualification and the remaining deadline/pressure matrix are still in progress.
