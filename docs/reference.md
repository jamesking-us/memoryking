# Feature and API reference

Use the [agent guide](agent-guide.md) for workflows and [deployment guide](deployment.md) for setup. This page maps implemented interfaces and their limits; schemas and `--help` provide exact required fields.

## Interfaces

| Interface | Entry point | Scope |
| --- | --- | --- |
| Local CLI | `python -m memoryking` / `memoryking` | Initialization, serving, status, doctor, credentials, capture and recall, host hooks (`hook`, `hooks install`), `influence` (where an observation went), `trust` (hold captures from untrusted sources), `vault` (Markdown export and import), dashboard sign-in, JSON requests and offline recovery. |
| Memory tool adapter | `memoryking.memory_tool.MemoryTool` | Anthropic's `memory_20250818` file commands answered from a project: notes as versioned observations, `claims/` as proposals, every write receipted. |
| Markdown vault | `memoryking vault export\|import` | The project as `<project>/<source>/<name>.md` files with provenance blocks, for hand editing and portability; import captures versions and proposals. |
| pytest plugin | `pytest -p memoryking.pytest_plugin` | Records failing tests as observations when the environment names a store. |
| Python client | `memoryking.sdk.LocalClient` | Local Unix-socket client bound to identity/project/history. |
| Shared CLI/client | `python -m memoryking.shared`, `memoryking.shared_sdk.SharedClient` | PostgreSQL operators and authenticated HTTP requests. |
| MCP stdio | `python -m memoryking.adapters.stdio` | Nine model-facing memory tools; daemon must already run. |
| Shared MCP | `/v1/tenants/{tenant}/projects/{project}/mcp` | Authenticated Streamable HTTP transport. |
| Reference agent | `python -m memoryking.reference_agent --help` | Create-file/readback task harness, checkpoint/recovery, the local recording provider or a policy adapter. |
| Provider adapters | `memoryking.providers.adapter(policy, name, model)` | Anthropic, OpenAI-compatible and Vertex AI dispatch for `AuthorizedConversation`, built from the daemon's egress policy. |
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

**Working state.** Checkpoints preserve hard constraints, failures, verification and next actions. Expected revisions prevent stale writers from overwriting newer state. A save with `readers: null` (the client default) keeps the stream's current readers; the first save is readable by its writer alone. Handoffs are recipient-bound. Operation intent/transition/reconciliation records track uncertain effects; the host implements the actual external tool and readback. Session summaries (`summarize`, hand-off, idle) are derived artifacts with their own receipt: the deterministic `extractive-v1` method quotes only the checkpoint, and a provider-written summary is admitted only as the exact reply in a delivered egress audit record. Resume packets and task cards carry the latest summary visible to the reader.

**Repository context.** Registration and snapshots are trusted-host operations. Git/dirty artifact identity and supported passive source projections inform dependency checks. No repository code is executed. Shallow or incomplete ancestry and unsupported coverage remain unknown rather than current.

**Dashboard.** Task activity, criteria/history, current authorized evidence, force-directed graph/list, source impact, receipts, usage inspection, review and retention previews are implemented. Nodes and counts represent bounded visible results, not the whole corpus. Layout distance is not confidence. In the memory graph, colour is the recorded authority of each node (observation, accepted, proposed, quarantined, rejected or deprecated, mandatory constraint, attestation, procedure, working state) and size grows with the number of the viewer's own retained receipts that cited the record; selecting a node lists those receipts and opens one in Retrieval history. A reviewer sees proposed and quarantined claims in the graph; a reader sees accepted ones only. On a phone the sidebar becomes a bar, cards stack and the graph shows its list unless the drawing is requested. The crowned cybernetic elephant has an optional 1.8-second depth tilt, with a static fallback when reduced motion is enabled. The animation stops when hidden or offscreen. Gunmetal panels, subtle CSS textures and cyan accents are available in dark and light themes; assets are served locally. Ordinary use requires no development reports.

**Procedures and learning.** Procedures require separate review and activation. Outcomes and helpfulness are actor reports tied to evidence/receipts. Feedback ranking is disabled by default; an administrator may run a frozen bounded comparison and explicitly activate or roll back a configuration. It cannot add candidates, override policy or prove a causal quality improvement. There is no autonomous learner or scheduler.

## Optional capabilities

| Capability | Enablement | Boundary |
| --- | --- | --- |
| Exact token counting | Install `requirements-tokenizers.lock`; request `cl100k_base`. | Missing runtime is explicit; `utf8-byte-estimate-v1` is a disclosed estimate. |
| Local semantic retrieval | Install `requirements-embeddings.lock`, install the pinned model once with the model installer, then start `serve --embedding-model DIR` and run `memoryking embeddings enable --project P`. The daemon indexes the project itself and embeds every briefing's query, so no worker or client-side vectors are needed; the explicit partition and worker commands still work. | Default lexical retrieval remains available and is the fallback whenever the model or partition is absent. No model download happens on startup. |
| pgvector exact search | Reviewed extension 0.8.7 in its dedicated schema, explicitly enabled by an operator. | Shared only; no approximate HNSW/IVFFlat index. Missing/wrong extension returns unavailable. |
| Adapter deltas | Retain a matching authorized base through the resume adapter. | Transport optimization; full model context is reconstructed. No automatic model-token savings claim. |
| Source-backed compilation | `processing` commands; deterministic extraction by default. | Candidate text must preserve validated whole source units and essential state or fall back. Remote generation is not included. |
| Feedback ranking | Explicit proposal, evaluation and activation. | Bounded development experiment; disabled by default. |
| Model providers | `serve --egress-policy FILE` naming providers, models, hosts and classification ceilings; the host builds adapters from the same file. | Off by default: only the local recorder is accepted. Enforced by the daemon at permit time; every dispatch is audited for 30 days. One HTTPS request per dispatch, no retry, no streaming, no provider-side state. |

The model installer command is `python -m memoryking.model_install --destination NEW_MODEL_DIRECTORY`. It downloads the files pinned in the [model manifest](../src/memoryking/adapters/models/minilm-l6-v2-onnx.json), verifies hashes and never loads model-repository Python. Review its license and network requirements before opting in. The CPU model produces 384-dimensional vectors and reads only the first 256 tokens per input; truncation is disclosed.

`memoryking embeddings enable --project P` configures the project's partition for the pinned model, and `embeddings status` reports its state, frontier and readiness. With `serve --embedding-model DIR` (or `embedding_model` in a profile) the daemon loads the model once, completes each enabled project's indexing jobs through the ordinary claim and complete path using its own owner credential, and embeds the query of any briefing that did not supply a `dense` request; an explicit request is never overridden. The embedding API and the one-shot worker (`--model`, `--credential`, `--project`, `--socket` or `--shared-url` with `--tenant`/`--ca-file`) remain available for the shared profile and for external indexing. Installing a runtime does not activate a partition, provision a provider account or authorize remote egress.

## Current limits

| Area | Implemented bound / behavior |
| --- | --- |
| Local request | 256 KiB wire body; 64 KiB captured text; one request per socket connection; up to eight connections served concurrently, more wait up to five seconds and then receive `service_busy`. |
| Shared request | Every HTTP, MCP and dashboard request carries one five-second budget from its first byte: waiting for a pooled connection, the connection checks and every statement in the request's transactions stop at that deadline, so a saturated pool or a slow query returns `service_busy` or `service_unavailable` inside the budget rather than after it. |
| Query | Up to 32 distinct searchable terms. Longer queries are searched on their leading terms and the receipt diagnostics record `query_terms_truncated`. |
| Candidate window | Each query term contributes its 256 newest matches in the project; the merged set is ordered by how many terms a document matches, then by recency, and the top 256 are scored. A common word therefore cannot crowd out documents that match the rare terms. |
| Policy journal | 256 KiB per signed record, enforced before writing (`journal_record_limit`) and on reading. A torn final line beyond the anchored sequence is discarded on the next append; damage anywhere else stays fatal. |
| Conversation | 128 messages per conversation through `AuthorizedConversation` (a dispatch carries up to 127, leaving room for the reply); the request as a whole stays within 96 KiB, or the provider's smaller `max_request_bytes`. |
| Egress audit | One record per consumed permit for a remote provider, retained 30 days, 64 per listing page; a reply is admitted only up to 64 KiB of text and a provider response up to 1 MiB. |
| Session summaries | Extractive summaries are at most 1,200 characters from the checkpoint plus the 16 newest visible observations since it; provider-written ones at most 4,096 characters. Retained 30 days; the idle scan runs at most once a minute and derives up to 32 per pass. |
| Briefing | Full response must fit requested budget; essentials are not silently truncated. A useful result can still be blocked or insufficient. `format: "text"` returns a cited Markdown rendering in an envelope with the same receipt; the token count is of the rendered text. |
| Authorization | Five-second evaluation-to-commit/first-byte deadline, with UTC and monotonic checks. |
| Idempotency | Replay bodies retained for 30 days; expired/deleted results do not free a consumed identity. |
| Generic jobs | 30-second leases, five attempts, bounded retry backoff, 1,000 unfinished jobs and four live leases per tenant. The local daemon completes `audit_probe` jobs itself between requests, so a default installation never needs a worker to keep capturing. |
| Mandatory resolution | 256 candidate revisions/bindings and 65,536 atom-binding work units; overflow blocks. |
| Atlas | Default 200 nodes/400 edges; maxima 500/1,500; 196,608-byte response; cursor expires after 60 seconds. Counts describe only the returned window. Each node carries `citations`, the number of the caller's retained briefings, fetches and reruns that depended on it; atlas pages themselves never count. A new citation changes the view, so an open cursor then needs a resync. |
| Dense retrieval | Exact authorized scan capped at 10,000 eligible vectors and 256 candidates; insufficient index/frontier is explicit. |
| Processing usage | Up to 100,000 retained attempts per tenant; unknown costs stay null, with known lower bounds. Attempts past their 30-day retention are pruned by `gc` and when the cap is reached. |
| Experience | 10,000 immutable records; up to 256 latest visible records per listing; no unbounded pagination/compaction. |
| Browser | 60-second bootstrap, 15-minute session, five-second polling and 15-second snapshot freshness; an active Memory view re-reads every five seconds while the tab is visible, re-checks permission on every read, repaints only when the result changed, and clears when hidden, expired or after a clock rollback. A briefing is never re-read in the background, because each read retains a receipt. |
| Shared live updates | Eight streams, bounded samples/cursor history, resync/polling fallback. Not an unbounded event replay service. |

### Retrieval quality gate

`tools/retrieval_benchmark.py --gate` builds a deterministic synthetic corpus (400 documents, 200 labelled queries by default), runs real briefings and reports recall, precision, reciprocal rank, recall when a common word widens the candidate set, briefing tokens and latency. The thresholds in `fixtures/evaluation/retrieval-gate.json` fail the check when a ranking change regresses them. The corpus is synthetic, so the gate guards against regressions; it is not evidence of real-task retrieval quality.

### Capacity and release

Every per-tenant bound has a release path and is reported by `status` under `capacity` as `used`, `limit` and `warning` (true from 80 percent). The dashboard overview shows the same figures.

| Resource | Limit | Scope | Released by | Error when full |
| --- | --- | --- | --- | --- |
| `jobs` | 1,000 pending or leased | tenant | The daemon drains `audit_probe` jobs between requests; `claim`/`finish` workers also drain. | `queue_full` |
| `rendered_artifacts` | 1,024 retained briefings, replays and resume packets | tenant, evicting the caller's own oldest first | 30-day expiry, and eviction of the oldest retained artifact when the count or byte cap is reached; an evicted receipt replays as `replay_expired`. | `retention_capacity` only when a single artifact exceeds 128 MiB |
| `rendered_bytes` | 128 MiB of retained artifact bodies | tenant | As above. | As above |
| `operation_intents` | 4,096 open intents per tenant and 32 per task stream | tenant and stream | Transitioning an intent to `succeeded` or `failed`. Resolved intents keep their consumed identity but no longer count. | `operation_capacity`, `operation_scope_capacity` |
| `processing_usage` | 100,000 attempts | tenant | 30-day retention: expired rows are pruned by `gc` and when the cap is reached. | `usage_retention_limit` |
| access receipts | unbounded | tenant | 30-day retention, pruned by `gc` and by the daemon's hourly housekeeping, which also expires replay results and artifacts. | none |

The product has been exercised on macOS with Python 3.12 and native PostgreSQL 18.6, and through a single-host Linux-container rehearsal. A full native Linux matrix, Windows, physical touch, complete OS reduced-motion/browser coverage and third-party agent hosts remain unqualified. Intermittent dashboard scope refresh loss needs further investigation.

Measured local/shared workload targets have not been met. No real-task success, noninferiority, longitudinal-learning or full-cost savings result is established. Automatic failover, production PITR and disaster RPO/RTO are not promised. Benchmark commands are available to measure your environment; their successful exit alone is not proof that a target passed.

## Importing legacy memory

The importer recognizes `kingforge-v1`, `standalone` and `universal` SQLite profiles. Start with a verified detached backup; do not copy a live main file and discard its WAL. Snapshot copying rejects sidecars, symlinks and files over 512 MiB.

Workflow: `memoryking.migration snapshot` → `inspect` → `plan` → bounded `apply` batches → `reconcile`. Run each subcommand with `--help` for the exact options. [The migration example](../tools/migration_demo.py) executes the entire local flow; shared import uses the same commands with `--url`, `--tenant` and verified `--ca-file` instead of `--socket`.

Inspection is bounded to 128 ordinary tables, 10,000 rows, 60 KiB per canonical row and 64 MiB total canonical row data. Unknown schemas, unresolved references, unstable keys or hash mismatches block. Apply defaults to 100 rows, maximum 500. Keep its source ID, snapshot and journal stable across retries.

Each imported row becomes restricted historical evidence; legacy approval and role labels do not become current permission. Lessons become unaccepted, nonexecutable proposals. Retired rows/dependents become tombstones. Reconciliation checks current target records and references but is not a lossless reverse importer, two-way sync or live cutover procedure. Production datasets and client behavior require their own rehearsal.

### Dashboard project listing

`GET /api/projects` on a signed-in dashboard session returns the projects the session's principal may read now, each with tenant-wide counts of observations, claims, task streams and retained receipts, and the instant of the latest recorded observation event (`last_activity_ms`). A tenant-wide grant lists every project holding records; a scoped grant lists its own project even when empty.

### Dashboard timeline

`GET /api/timeline/{project}` and `GET /api/timeline/{project}/{before}` on a signed-in session list the project's events newest first (captures, claims, reviews, checkpoints, handoffs, purges) with the principal that caused each, up to 100 per page; `next_before` pages further back. The Memory view's **Timeline** shows the same.

### Dashboard receipt trail

`GET /api/citations/{project}/{node}` on a signed-in session lists the session principal's retained receipts (briefings, fetches and reruns, newest first, up to 32) whose recorded dependencies include the node: an observation id, a claim revision or a checkpoint id, URL-encoded. Each item has the same fields as the receipts listing plus `dependency_kind`. A receipt the principal may no longer replay is omitted, exactly as in the receipts listing. The memory graph uses this route when a node is selected.

### Influence report

`memoryking influence OBSERVATION --project P` (or `--json`), the Memory view **Where did this go**, and the `inspection` socket operation's `influence` action list every record built on one observation and who received them: the claims that cite it; the checkpoints, handoffs, summaries and experience records that recorded a dependency on it or on anything derived from it (the same fixed point a purge follows, read without changing anything); the retained briefings, fetches, reruns and graph pages that included it, each with its principal and time; and the sorted set of principals those reached. Lists are bounded to 256 entries each and truncation is named. It requires the admin capability, because it reveals who read what. It covers recorded dependencies in this project and retained deliveries only: a delivery whose retention expired, a prior export, a provider dispatch or a backup leaves no record here, and the report says so in its coverage field. This is the causal-attribution view the memory-poisoning literature asks for, built from the receipts MemoryKing already keeps.

### Proposal drafts

`knowledge` action `draft` (capture capability, keyed), `memoryking draft-proposals --project P [--limit N]` (default key one per project and day), and the dashboard's Review view. Window: the newest 512 visible observations; topics: anchors held by 2 to 32 of them, then terms of five or more characters shared by 2 to 8 of them, deduplicated by member set; a topic needs at least two distinct source events; a draft cites up to 16 spans (one sentence each, at most 512 characters); families are keyed `draft-<sha256(topic)>`; a draft whose evidence is already cited by the family's latest unpurged revision is skipped (`already_proposed`). Result: drafted revisions with topic, basis, support and text; `skipped` counts; `window`; `method` `repeated_evidence_v1`.

### Anchors and salience

Anchors: deterministic extraction at capture (paths, symbols, tickets, URLs, hosts, error codes; at most 32 per observation, each at most 128 characters) into the `anchors` table (SQLite 13 / PostgreSQL 8, archive schema 6). A query's anchors (at most 16) each get a newest-first candidate window and count double in coverage; an exact anchor overlap scores like full term overlap and is reported as `anchor_match`. The graph adds `anchors` to observation nodes and `shares_anchor` edges for anchors held by 2 to 8 loaded records, at most 512 such edges per page (`anchor_links` truncation). Purge removes a record's anchors; `reindex` rebuilds them from retained text.

Salience: per candidate, the caller's own deliveries in the last 90 days from retained non-graph receipts, the caller's own helpful feedback votes, and the last delivery time (another principal's use or opinion never changes a reader's order); strength is at most one million (millionths), the rank nudge at most 100,000 (a tenth of a full score), half-life 30 days; a record never delivered, never voted on and recorded more than 180 days ago is tier `archival` and ranked after every active record while remaining searchable. Applied only when the briefing asks (`salience: "on"`); disabled for any briefing with `recorded_position`, `valid_ms` or `as_of_ms`, during a ranking evaluation, and while a frozen ranking policy is active, each named in the trace stage `salience_v1`. Reported per item as `salience`.

Evidence recall: `tools/evidence_recall.py` (see the agent guide) with the gate `fixtures/evaluation/evidence-recall-gate.json` on the committed sample; on the pooled LongMemEval oracle set (500 questions, 10,866 observations) session recall at eight moved from 0.548 to 0.556 and MRR from 0.462 to 0.469 with anchors, while answer-turn recall moved from 0.562 to 0.538; chat transcripts rarely carry anchors, so a developer's own project is the corpus where they count.

### As-of briefings

`brief` accepts `as_of_ms` (Unix ms): the frontier is the last event recorded by that wall time and claim validity is judged at that time; the packet carries `as_of_ms`, `recorded_position` and `valid_ms` and the receipt replays byte for byte. Equivalent to passing `recorded_position` and `valid_ms` yourself, which remain available; combining the two is `invalid_request`. CLI `recall --as-of DATE|ISO|MS`, lean tool `recall(as_of=...)`, dashboard briefing form **As of**.

### Source trust

`memoryking trust set|list|held|release`, the **Review & conflicts** view, and the `inspection` operation's `trust`, `trust_list`, `held` and `release` actions (all admin). A rule names a project or `*`, a source by exact name or glob, and a level. A capture whose source resolves to `untrusted` (most specific rule wins) is stored but held: `_visible` is false for every principal until an admin releases it, so reads, briefings, the graph, resume evidence, claim proposals and the influence of later records exclude it; the capture reply carries `held: true`. `held` lists the oldest 64 held captures in a project with a 256-byte preview. Rules and releases are journal records (`trust`, `hold`) replayed on restore; `source_trust` and `observation_holds` are content tables in schema SQLite 12 / PostgreSQL 7 and archive schema 5.

### Memory tool and vault

`MemoryTool(client)` answers `view`, `create`, `str_replace`, `insert`, `delete` and `rename` for paths under `/memories/<project>`; `DEFINITION` is the `tools` entry to send. Paths: `README.md` (generated), `<source>/<name>.md` (the newest version of the observation whose source event is `<name>`, versions `<name>__v<n>`), `claims/<key>.md` (the latest unpurged `knowledge` revision of family `<key>`). Names are mapped to identifiers of at most 80 characters; the version suffix is reserved; anything outside the root, hidden, or containing `..`, `\\` or `%` is refused. A file is a provenance block (`---` lines) and the stored text; `str_replace` and `insert` edit the text only, with line numbers counted as viewed, and record a new version through `capture` (key `mt-<sha256>`, day-granular observed time, namespace `sdk`, replay-safe) or a new revision through `propose` (expected version from the catalog; evidence from the file's `evidence` list, whole-record spans, at most 16; provenance method `memory_file_v1`). `view` of a file reads through `fetch` in 16 KiB ranges; directory listings show the newest 512 observations grouped by source and at most 256 entries, and say what they left out. `delete` purges every version of a note and `rename` captures the newest text under the new name before purging; both require the admin capability. Claims cannot be deleted or renamed. Reply texts follow the reference implementation; refusals are returned as `is_error` tool results.

`vault export --dir D` writes `D/<project>/<source>/<name>.md`, `D/<project>/claims/<key>.md` and `D/<project>/README.md` with mode 0600 in 0700 directories, replacing files in place; `vault import --dir D [--dry-run]` reads them back (notes before claims), captures a version for each changed or new note, proposes a revision for each changed or new claim file, and reports `captured`, `proposed`, `unchanged` and `errors`; the exit code is 1 when any file was skipped. Connection comes from `--socket`/`--credential`/`--project`, a profile, or `MEMORYKING_SOCKET`, `MEMORYKING_CREDENTIAL` and `MEMORYKING_PROJECT`; the vault client uses capture namespace `vault`.

Both read through the `inspection` operation's `catalog` action (read capability): `kind` `observations` (default) or `claims`; filters `source`, `name` (a file and its versions; `_` is literal) and `id`; `after` and `limit` (1 to 512, default 256) page newest first by position (`next_after` continues); `text: false` omits text. Observations are the visible ones only; claims are the latest unpurged knowledge revision per family, accepted ones for every reader and every state for a reviewer (`reviewer` in the reply). A page carries at most 192 KiB of text. Catalog reads leave no delivery record; `fetch` does.

### Dashboard plans

`GET /api/plans/{project}` (optional `cursor`) and `GET /api/plans/{project}/{plan}` on a signed-in session return the project's structured plans and one plan's detail with computed progress, under the same session, `Host`/`Origin` and project-access checks as the other dashboard reads; they accept no mutations and no other query parameters. **Task activity** shows the plans beside ordinary checkpoints.

### Metrics

`GET /metrics` on the dashboard port, authenticated with `Authorization: Bearer <admin credential>` and no cookie, returns Prometheus text: requests and errors by operation and code, a latency histogram per operation, capacity used and limit per resource, jobs by state, position and frontier. Counters live in the daemon's memory and reset on restart; nothing in them names content or principals. For the same figures per request rather than aggregated, start the daemon with `serve --trace` (see [deployment](deployment.md#local-configuration)).

### Dashboard agent endpoint

The local dashboard port also serves `POST /api/v1/retrieval` for an agent on the same machine: a retrieval command (`brief`, `fetch`, `receipt`, `replay`, `receipts`, `atlas`) in the request body, authenticated with `Authorization: Bearer <credential>` instead of a browser session, with exact `Host` and `Origin` checks and no cookies. It exists so a host can reuse the dashboard's loopback listener; the Unix socket remains the primary local transport and the shared service offers the same operations over HTTPS.

## Plan tracking contracts

`memory_plan` groups four working-state actions. `plan_save` creates or revises a typed plan; `plan_step` updates one step; `plan_get` reads the current or an earlier revision; `plan_list` pages the project's current plans. Read and checkpoint capabilities, current project and audience authorization, writer ownership, compare-and-set revisions, durable retry keys and the history id are enforced by the existing working-state transaction. A plan is stored as a checkpoint on its task stream, so no schema migration or second store is involved, and backup, restore and purge treat it like any checkpoint. An ordinary task stream cannot turn into a plan, or a plan into an ordinary stream.

Limits are 32 ordered phases, 256 steps, 64 prerequisite ids per step, 16 evidence references per step, 32 lesson references and the checkpoint's 128 distinct dependencies. The working request limit stays 128 KiB, so the maximal fields cannot all be used at once. The tenant's 4,096-checkpoint capacity also bounds plan revision history. A read verifies the available history up to that capacity; a historical revision the reader cannot open conservatively makes the affected completion `unknown`. A listing inspects at most 64 task streams per page, returns at most 50 plans and stops before the page exceeds 96 KiB; continue an opaque cursor even when a page is empty. A changed frontier, authorization or history invalidates the cursor; restart the listing.

The dashboard shows the agent-reported lifecycle separately from computed progress. Percent complete is not an estimate of elapsed time, cost or remaining effort. Nothing advances a plan automatically; agents call the plan commands with recorded results.

## Troubleshooting

Every error reply carries a `hint` beside its `error` code, and the complete list is in [error codes](error-codes.md), generated from the same table. The most common results:

| Result | What to do |
| --- | --- |
| `no_relevant_memory` | Check project, access, query and evidence; an empty result can be correct. |
| `budget_insufficient` | Increase the explicit budget or narrow task scope; do not strip required constraints. |
| `applicability_unknown`, `essential_policy_unavailable` | Supply valid context or resolve policy/evidence access with its owner. |
| `context_changed`, `history_changed` | Discard stale bindings, reconcile and create a fresh client/plan deliberately. |
| `idempotency_conflict`, `source_conflict` | Investigate mismatched bytes; do not invent a new key to bypass the conflict. |
| `source_retired`, `idempotency_result_expired` | The identity remains consumed. Reconcile instead of repeating the mutation. |
| `stale_lease`, `stale_source` | Discard stale output and resolve current source/worker state. |
| `queue_full`, `service_busy` | Back off and drain bounded work; inspect contention. No mutation retry is automatic. HTTP transports answer capacity that frees with time (`*_capacity`, queue, rate, concurrency and session limits) with 429 and unavailability with 503; conflicts such as `history_changed` or `session_changed` use 409. A fixed input bound such as `tool_input_limit` stays 400: waiting does not help. |
| `journal_record_limit` | The policy action was refused before any byte was written; the store stays usable. Reduce the action's payload. |
| `maintenance_operation_mismatch` | `end-maintenance` was given an operation id that is not the one recorded by the interrupted backup. Read the anchor's `maintenance_operation` and retry with it. |
| `internal_error`, `dashboard_unavailable`, `service_unavailable` with a `request_id` | Find the matching `request_id=` line in the daemon's stderr log (`serve --log-level`); it names the component, error type and code location without content. |
| `release_expired`, `clock_untrusted` | Restore trustworthy timing and request a fresh authorized response. |
| `recovery_required`, `journal_rollback`, `backup_invalid` | Stay offline and use verified independent recovery evidence. |
| `session_changed` | Sign in again; permissions changed and retained views were cleared. |
| `egress_provider_denied`, `egress_classification_denied` | The daemon's egress policy does not allow that provider or model, or a cited source is classified above the provider's ceiling; change the policy and restart, or rebuild the conversation without the source. |
| `provider_not_supported` | The adapter was built from a policy revision the daemon is not running; rebuild it from the current file. |
| `summary_source_invalid` | A provider-written summary was not the exact delivered reply for that permit; pass the unmodified text and the permit from the same dispatch, or use the extractive method. |
| `provider_unavailable`, `provider_response_invalid` | The provider failed, timed out or answered in an unexpected shape; nothing was admitted and the audit record says `failed`. The dispatch is never retried automatically. |

[Back to README](../README.md) · [Agent guide](agent-guide.md) · [Deployment](deployment.md)
