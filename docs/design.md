# How MemoryKing is designed

MemoryKing separates **what was observed**, **what has been accepted as guidance**, and **what an agent was doing**. That separation makes memory inspectable and prevents a saved sentence from quietly becoming an instruction or permission.

## The main pieces

```mermaid
flowchart TB
    A[Agent host / Python / MCP] --> B[Authenticated service boundary]
    U[Browser dashboard] --> B
    B --> C[Capture and reviewed knowledge]
    B --> D[Briefings and evidence]
    B --> E[Checkpoints and handoffs]
    C --> F[(Canonical local or shared store)]
    D --> F
    E --> F
    F --> G[Optional search and processing projections]
    H[Independent recovery control] --- F
```

The Python domain layer contains contracts and decisions independent of vendors. Application services coordinate them; adapters implement storage, transports, repository inspection and optional providers. TypeScript implements the browser interface and generated client contracts.

## Three kinds of memory

| Kind | Meaning | Important distinction |
| --- | --- | --- |
| Observation | An attributed event or captured source text. | It says what was recorded, not that the interpretation is correct. |
| Reviewed knowledge | A versioned claim or constraint linked to source evidence. | Proposals require authorized review before becoming accepted guidance. |
| Working state | A checkpoint of an unfinished task and its operations. | Pending tests and uncertain actions remain pending or uncertain after resume. |

Procedures extend reviewed knowledge into reusable steps. Activation is a separate authorized decision. Outcomes and feedback are attributed records; they do not automatically prove effectiveness or promote a procedure.

## Local and shared storage

The local profile uses one SQLite store per tenant, SQLite WAL with full synchronization, private content files and a Unix socket. One OS owner controls a store. Multiple machines must not write the SQLite files over a shared filesystem.

The shared profile uses native PostgreSQL, tenant-scoped transaction ordering and isolated migration/runtime/worker/backup roles. HTTP and MCP clients share the same authorization and domain rules. Tenant-wide ordering simplifies correctness but can create contention; the current implementation has not met its proposed workload targets.

An independent control tree records signing identity, recovery anchors and policy/deletion history. It is deliberately separate from ordinary content backups.

## How a briefing is assembled

1. Authenticate the caller and bind its tenant, project and current history.
2. Resolve mandatory constraints completely within explicit bounds.
3. Find relevant accessible observations and accepted guidance, using lexical retrieval by default and configured optional channels when available.
4. Check applicability, repository freshness, evidence visibility and conflicts.
5. Fit the complete response into the requested budget, preserving essential fields.
6. Persist the authorization decision and response identity before releasing bytes.

The result includes a status, guidance/evidence references and usage information. An unresolved essential constraint blocks rather than disappearing from a top-k list. Unknown token usage is not counted as zero; a byte estimate is labeled as an estimate.

Receipts preserve how a response was assembled. **Replay** returns retained exact response bytes if current authorization still permits it. **Rerun** performs a new retrieval and creates a new receipt. Neither operation implies a model response will be identical.

## Applicability and time

Knowledge can be limited to repositories, paths, environments, roles, task stages, dependency versions and commits. Selector v1 is a bounded OR of AND clauses, with exact sets, path-segment predicates, numeric version intervals and trusted commit ancestry. Arbitrary executable predicates, path globs and unproven ancestry do not enter this decision path.

Evaluation has three outcomes: true, false or unknown. Missing inputs do not silently mean “does not apply.” Unknown essential policy blocks. Partial replacement must be provably contained in the original and replacement claim's scope.

Valid time describes when a claim applies. Recorded position describes when the system learned or changed it. Historical inspection preserves that distinction while still applying today's permissions, purge and quarantine rules.

## Authorization and retained model context

Every private release records an authorization-evaluation instant, history, relevant policy epochs, response hash and deadline. Policy changes serialize against that decision. Validity at the evaluation instant allows the response to start within the bounded five-second window; crossing the deadline releases no old content and requires a fresh decision.

The consuming harness has an additional responsibility: check retained input before the next provider dispatch. `AuthorizedConversation` tracks authorization dependencies of source and derived messages, rebuilding permitted input or blocking dispatch after relevant changes. This protects future processing; it cannot undo prior deliveries.

Browser sessions bind identity, tenant, history and policy epoch. Private reads require current session authorization; mutations also require the configured Origin and CSRF protection. Public bootstrap assets carry no private content. The UI clears retained views after revocation, expiry or context changes.

## Provider adapters and egress

The local recording provider is the default and the only provider available without an explicit egress policy; with one, this is the single path by which retained context may leave the machine. It is built so that the store, not the host, decides what may go where, and so that every departure leaves an audit record.

### Three parts

**An adapter is small and dumb.** An adapter turns the frozen, canonical request bytes that `AuthorizedConversation` already produces into one provider call and returns the reply text. It has a name, the set of models it may be asked for, a declared destination host, whether it keeps server-side state (none of the shipped remote adapters do), and one method, `dispatch(raw) -> {content, request_hash, wire_hash, usage}`. It has no access to the store, the credential or the conversation; it cannot add, drop or reorder messages, because the mapping from the canonical request to the provider's wire body is a fixed function per adapter and the hash of the wire body is recorded. Three adapters ship: `anthropic-messages` (the Anthropic Messages API), `openai-chat` (chat completions, which also covers xAI Grok and self-hosted servers such as Ollama at an explicit loopback host) and `vertex-ai` (Google Cloud Vertex AI: Gemini through `generateContent`, or Anthropic models through `rawPredict`, with a bearer token from a file or an operator-named command). All use the standard library's HTTPS client with certificate verification, one attempt, a bounded timeout and a bounded response size. No retry, no streaming, no tool use, no prompt-caching handles in this revision.

**The egress policy is the operator's, and the daemon enforces it.** A private policy file names the adapters an installation may use and the limits on each:

```toml
revision = "team-2026-10"            # becomes the binding's egress_policy_revision
[[providers]]
name = "anthropic-messages"
host = "api.anthropic.com"
models = ["claude-sonnet-5-5"]
key_file = "/private/anthropic.key"
max_classification = "internal"      # restricted content never leaves
max_request_bytes = 98304
```

`serve --egress-policy FILE` loads it; without the flag the daemon behaves exactly as today. The policy is enforced where it cannot be skipped: the `authorize` and `consume` actions of the inference contract. The daemon checks that the request's provider and model are listed, that the binding's `egress_policy_revision` is the loaded one (a policy change retires every open conversation, like a permission change), that the request is within the byte limit, and that every message's provenance is within the classification ceiling. Host-authorized segments carry their classification already; for memory segments the daemon resolves the receipt's recorded dependencies to their observations and takes the highest classification among them. A packet that cites a restricted source is refused for a provider whose ceiling is `internal`, with `egress_classification_denied`, before any permit is issued. The key file is read by the host process that runs the adapter, never by the daemon, and never appears in any record.

**Every manifest is audited.** When a permit is consumed the daemon writes an egress audit record: tenant, principal, project, provider, model, destination host, request hash, manifest hash, the segment manifest itself (origins, hashes, receipt ids and authorization ids, which is already content-free), highest classification, byte count, evaluated and deadline instants. After the provider answers, the host reports the outcome through the `report` action: wire hash, response hash, provider usage and a status of `delivered` or `failed`; a conversation that dies between consume and report leaves the record `pending`, which is the truth. Records carry no content and are not purged by source purge, because the delivery happened; they expire with the usage retention (30 days) and are listed by the `audit` action, `memoryking egress-audit --project P` and the dashboard's Jobs & health view. The audit is the answer to "what left this machine, when, citing what, and did it arrive".

### What stays the same

`AuthorizedConversation` keeps its guarantees unchanged: one frozen byte string per dispatch, authorize then consume then dispatch inside the five-second window, a full revalidation of the derived closure before the reply is admitted, and rebuild-or-block after a revocation or purge. It gains one check: the adapter's name, models and host must match an entry in the policy the daemon reported in the binding, else `provider_not_supported` as today. Memory still enters as user-role content; retrieved text is never a system instruction. The model-facing MCP tools still cannot reach the inference surface.

### What this does not promise

A host holding an `inference` credential can always copy bytes elsewhere by its own code; the policy governs what the store will authorize and record, not what an unreviewed process does. Delivery cannot be recalled: revocation and purge control future dispatches and the audit shows which past dispatches cited a source. Provider-side retention, training use and logging are the provider's terms, not MemoryKing's. The contract change (`provider` and `model` widened from constants to a bounded vocabulary and pattern, `egress_policy_revision` from a constant to an identifier, plus the `report` and `audit` actions) is additive: every existing local-recording request stays valid byte for byte.

## Retries and recovery

A request namespace and idempotency key bind a mutation to its canonical payload and history. Equal retries can reuse the recorded result; changed input conflicts. Expired result bodies leave consumed-key tombstones. Retired sources cannot be recreated by reusing their old event identity.

A restored backup creates a **new history incarnation**. Old credentials, namespaces, watermarks, leases, resumable handles and adapter bases do not silently bind to replacement history. Restore replays current independently retained revocations/deletions before reopening access. Missing control evidence keeps the store unavailable.

External tool effects need their own operation records and reconciliation. A local transaction cannot guarantee exactly-once execution of an unrelated external system.

## Optional work stays subordinate to canonical evidence

Embedding vectors, compressed summaries, graph projections and ranked procedures derive from current canonical sources. Workers receive bounded leases tied to history, source and policy. Stale completions are rejected. Disabling an optional model leaves the default lexical path available; a projection never grants authority or proves truth.

Repository inspection is passive: it records Git identity, tracked/dirty artifacts and supported source structure without executing repository code. Bounded or missing coverage stays explicit. The dashboard observes stored state and can submit authorized review actions; it does not schedule agents.

## What this architecture does not establish

Passing implementation tests does not establish better real-task outcomes, cost savings, production throughput, complete browser support, automatic failover or disaster-recovery service levels. The current [feature limits](reference.md#current-limits) preserve those distinctions.

## Dashboard appearance

The dashboard uses a crowned cybernetic elephant emblem with gunmetal panels, restrained circuit-grid and brushed-metal CSS textures, and cyan accents. Content panels keep solid surfaces for legible labels and evidence. Dark and light themes share the same controls and semantic status indicators. No fonts, textures or artwork are fetched from external services at runtime.

The optional logo interaction tilts a rendered image using perspective; it is not an interactive 3D model. It runs once for 1.8 seconds on request, stops when hidden or offscreen, and respects reduced motion and the Motion off control. The compact phone layout uses the small static emblem in a sticky bar that holds the views, stacks every card in one column and shows the memory graph as a list by default; the drawing is one tap away and the page never scrolls sideways.

The memory graph encodes recorded facts only: a node's colour is the authority the store holds for it (observation, accepted, proposed, quarantined, rejected or deprecated, mandatory constraint, attestation, procedure, working state), its size is the number of the viewer's own retained receipts that cited it, and selecting it lists those receipts. Layout distance carries no meaning, and a receipt trail is private to the principal that holds the receipts.

The main [elephant artwork](../packages/dashboard-foundation/web/elephant.png) was created with the built-in image-generation tool; its [generation prompt](images/memoryking-emblem-prompt.txt) is retained alongside the documentation. The favicon is a simplified vector mark for small sizes.

[Back to README](../README.md) · [Security](../SECURITY.md) · [Deployment](deployment.md)
