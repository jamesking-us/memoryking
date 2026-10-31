# Feature and API reference

Use the [agent guide](agent-guide.md) for workflows and [deployment guide](deployment.md) for setup. This page maps implemented interfaces and their limits; schemas and `--help` provide exact required fields.

## Interfaces

| Interface | Entry point | Scope |
| --- | --- | --- |
| Local CLI | `python -m memoryking` / `memoryking` | Initialization, serving, status, doctor, credentials, dashboard sign-in, JSON requests and offline recovery. |
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
| Local request | 256 KiB wire body; 64 KiB captured text; one request per socket connection; up to eight connections served concurrently, more wait up to five seconds and then receive `service_busy`. |
| Query | Up to 32 distinct searchable terms. Longer queries are searched on their leading terms and the receipt diagnostics record `query_terms_truncated`. |
| Candidate window | The 256 newest lexical matches in the project are scored; older matches beyond the window are not candidates. |
| Policy journal | 256 KiB per signed record, enforced before writing (`journal_record_limit`) and on reading. A torn final line beyond the anchored sequence is discarded on the next append; damage anywhere else stays fatal. |
| Conversation | 128 messages per dispatch through `AuthorizedConversation`; the request as a whole stays within 96 KiB. |
| Briefing | Full response must fit requested budget; essentials are not silently truncated. A useful result can still be blocked or insufficient. |
| Authorization | Five-second evaluation-to-commit/first-byte deadline, with UTC and monotonic checks. |
| Idempotency | Replay bodies retained for 30 days; expired/deleted results do not free a consumed identity. |
| Generic jobs | 30-second leases, five attempts, bounded retry backoff, 1,000 unfinished jobs and four live leases per tenant. The local daemon completes `audit_probe` jobs itself between requests, so a default installation never needs a worker to keep capturing. |
| Mandatory resolution | 256 candidate revisions/bindings and 65,536 atom-binding work units; overflow blocks. |
| Atlas | Default 200 nodes/400 edges; maxima 500/1,500; 196,608-byte response; cursor expires after 60 seconds. Counts describe only the returned window. |
| Dense retrieval | Exact authorized scan capped at 10,000 eligible vectors and 256 candidates; insufficient index/frontier is explicit. |
| Processing usage | Up to 100,000 retained attempts per tenant; unknown costs stay null, with known lower bounds. Attempts past their 30-day retention are pruned by `gc` and when the cap is reached. |
| Experience | 10,000 immutable records; up to 256 latest visible records per listing; no unbounded pagination/compaction. |
| Browser | 60-second bootstrap, 15-minute session, five-second polling and 15-second snapshot freshness; hidden/expired views clear. |
| Shared live updates | Eight streams, bounded samples/cursor history, resync/polling fallback. Not an unbounded event replay service. |

### Capacity and release

Every per-tenant bound has a release path and is reported by `status` under `capacity` as `used`, `limit` and `warning` (true from 80 percent). The dashboard overview shows the same figures.

| Resource | Limit | Scope | Released by | Error when full |
| --- | --- | --- | --- | --- |
| `jobs` | 1,000 pending or leased | tenant | The daemon drains `audit_probe` jobs between requests; `claim`/`finish` workers also drain. | `queue_full` |
| `rendered_artifacts` | 1,024 retained briefings, replays and resume packets | tenant, evicting the caller's own oldest first | 30-day expiry, and eviction of the oldest retained artifact when the count or byte cap is reached; an evicted receipt replays as `replay_expired`. | `retention_capacity` only when a single artifact exceeds 128 MiB |
| `rendered_bytes` | 128 MiB of retained artifact bodies | tenant | As above. | As above |
| `operation_intents` | 4,096 open intents per tenant and 32 per task stream | tenant and stream | Transitioning an intent to `succeeded` or `failed`. Resolved intents keep their consumed identity but no longer count. | `operation_capacity`, `operation_scope_capacity` |
| `processing_usage` | 100,000 attempts | tenant | 30-day retention: expired rows are pruned by `gc` and when the cap is reached. | `usage_retention_limit` |

The product has been exercised on macOS with Python 3.12 and native PostgreSQL 18.6, and through a single-host Linux-container rehearsal. A full native Linux matrix, Windows, physical touch, complete OS reduced-motion/browser coverage and third-party agent hosts remain unqualified. Intermittent dashboard scope refresh loss needs further investigation.

Measured local/shared workload targets have not been met. No real-task success, noninferiority, longitudinal-learning or full-cost savings result is established. Automatic failover, production PITR and disaster RPO/RTO are not promised. Benchmark commands are available to measure your environment; their successful exit alone is not proof that a target passed.

## Importing legacy memory

The importer recognizes `kingforge-v1`, `standalone` and `universal` SQLite profiles. Start with a verified detached backup; do not copy a live main file and discard its WAL. Snapshot copying rejects sidecars, symlinks and files over 512 MiB.

Workflow: `memoryking.migration snapshot` → `inspect` → `plan` → bounded `apply` batches → `reconcile`. Run each subcommand with `--help` for the exact options. [The migration example](../tools/migration_demo.py) executes the entire local flow; shared import uses the same commands with `--url`, `--tenant` and verified `--ca-file` instead of `--socket`.

Inspection is bounded to 128 ordinary tables, 10,000 rows, 60 KiB per canonical row and 64 MiB total canonical row data. Unknown schemas, unresolved references, unstable keys or hash mismatches block. Apply defaults to 100 rows, maximum 500. Keep its source ID, snapshot and journal stable across retries.

Each imported row becomes restricted historical evidence; legacy approval and role labels do not become current permission. Lessons become unaccepted, nonexecutable proposals. Retired rows/dependents become tombstones. Reconciliation checks current target records and references but is not a lossless reverse importer, two-way sync or live cutover procedure. Production datasets and client behavior require their own rehearsal.

### Dashboard project listing

`GET /api/projects` on a signed-in dashboard session returns the projects the session's principal may read now, each with tenant-wide counts of observations, claims, task streams and retained receipts. A tenant-wide grant lists every project holding records; a scoped grant lists its own project even when empty.

### Dashboard agent endpoint

The local dashboard port also serves `POST /api/v1/retrieval` for an agent on the same machine: a retrieval command (`brief`, `fetch`, `receipt`, `replay`, `receipts`, `atlas`) in the request body, authenticated with `Authorization: Bearer <credential>` instead of a browser session, with exact `Host` and `Origin` checks and no cookies. It exists so a host can reuse the dashboard's loopback listener; the Unix socket remains the primary local transport and the shared service offers the same operations over HTTPS.

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
| `queue_full`, `service_busy` | Back off and drain bounded work; inspect contention. No mutation retry is automatic. HTTP transports answer capacity with 429 and unavailability with 503; conflicts such as `history_changed` or `session_changed` use 409. |
| `journal_record_limit` | The policy action was refused before any byte was written; the store stays usable. Reduce the action's payload. |
| `maintenance_operation_mismatch` | `end-maintenance` was given an operation id that is not the one recorded by the interrupted backup. Read the anchor's `maintenance_operation` and retry with it. |
| `internal_error`, `dashboard_unavailable`, `service_unavailable` with a `request_id` | Find the matching `request_id=` line in the daemon's stderr log (`serve --log-level`); it names the component, error type and code location without content. |
| `release_expired`, `clock_untrusted` | Restore trustworthy timing and request a fresh authorized response. |
| `recovery_required`, `journal_rollback`, `backup_invalid` | Stay offline and use verified independent recovery evidence. |
| `session_changed` | Sign in again; permissions changed and retained views were cleared. |

[Back to README](../README.md) · [Agent guide](agent-guide.md) · [Deployment](deployment.md)
