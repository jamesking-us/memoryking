# Using MemoryKing with agents

MemoryKing is the memory service around an agent, not the agent itself. Your host decides when to save information, request context and run tools. MemoryKing stores and returns that information under explicit access and recovery rules.

## The everyday flow

| Step | Agent behavior | What is kept or returned |
| --- | --- | --- |
| Capture | Save a useful observation or tool result. | Original text, source identity, project, audience and timestamp. |
| Brief | Ask what matters for the current task. | Applicable guidance, constraints, verification gaps and evidence references within an output budget. |
| Checkpoint | Save before pausing, switching roles or taking a risky action. | Goal, repository state, constraints, findings, failed attempts, tests, blockers and next actions. |
| Resume | Start a fresh session from the saved checkpoint. | Complete essential state, with missing evidence or uncertain operations called out. |
| Handoff | Prepare work for a named recipient. | A handle resolved under the recipient's current permissions. |
| Review | Decide whether an observed lesson should become reusable guidance. | An explicit accepted/rejected revision, with evidence and applicability. |

### Example: picking up a bug investigation

An agent captures “Increasing every timeout exceeded the total deadline.” It checkpoints the unfinished fix and the test still needed. A later session asks about the timeout problem and resumes the saved task. It receives the failed approach and pending test, so it can choose the next action without re-reading the entire conversation.

Run [the complete example](../examples/remember_and_resume.py):

```sh
.venv/bin/python examples/remember_and_resume.py
```

The example uses synthetic content, two client connections and real local storage. Its checkpoint fixture contains illustrative repository IDs; it is not evidence about your actual checkout. The full state round-trips without converting pending verification into a pass.

### Example: passing work to a reviewer

The implementing agent saves a checkpoint and prepares a handoff for a reviewer. The reviewer opens it using their own identity. MemoryKing checks what that recipient may access and retains missing verification. A “reviewer” label does not itself grant permission.

The executable [handoff example](../tools/handoff_demo.py) covers this path. Use the contributor environment, then run `.venv/bin/python tools/handoff_demo.py`.

### Example: recovering after an uncertain action

A host records an operation intent before writing a file. If it crashes after the write but before recording success, the resumed task reports the result as uncertain. The host reads the file back and reconciles the recorded operation instead of blindly writing it again.

Run `.venv/bin/python tools/harness_demo.py` in the contributor environment. The reference harness implements this specific create-file/readback workflow. Other external tools need their own reconciliation adapter; generic exactly-once execution is not promised.

## Python integration

Start a service as described in [getting started](getting-started.md). This client connects to project `demo` in that local store:

```python
from pathlib import Path
from memoryking.sdk import LocalClient

client = LocalClient(
    Path.home() / ".memoryking-data/service.sock",
    Path.home() / ".memoryking-control/owner.credential",
    "demo",
)
context = client.context()
```

Use a scoped agent credential for real integrations; the owner credential is for initial setup. The client binds store, tenant, principal, project and history. It refuses to silently move to a different context after restore or identity change.

Client methods include `capture`, `retrieval`, `working`, `handoff`, `repository`, `embedding`, `processing` and `experience`. Requests use versioned JSON schemas, not free-form option dictionaries. See the [API reference](reference.md#interfaces) and executable examples for full request bodies.

For capture, keep a stable key and stable source-event identity for the same event. Changed bytes with the same identity produce a conflict. Save the exact request before dispatch; do not create a new identity merely to make an uncertain retry succeed. After a history change, reconcile first. The client captures in a named namespace (`namespace="sdk"` by default) that is deterministic for the principal, so the same key from any process replays the same result or reports `idempotency_conflict`; pass a different `namespace` to keep two integrations' keys apart.

`SharedClient` in `memoryking.shared_sdk` provides the shared HTTP transport with tenant/project binding and verified TLS. It rejects redirects and does not automatically retry mutations.

## MCP integration

The local bridge connects an MCP-capable host to an already running local service. Use absolute paths in your host's configuration:

```json
{
  "mcpServers": {
    "memoryking": {
      "command": "/absolute/path/to/memoryking/.venv/bin/python",
      "args": [
        "-m", "memoryking.adapters.stdio",
        "--socket", "/absolute/path/to/store/service.sock",
        "--credential", "/absolute/path/to/private/agent.credential",
        "--project", "demo"
      ]
    }
  }
}
```

This illustrates the common `mcpServers` shape; each host has its own configuration location and support requirements. The bridge itself is exercised, but third-party hosts are not certified integrations.

Nine tools are exposed: `memory_status`, `memory_capture`, `memory_brief`, `memory_fetch`, `memory_checkpoint`, `memory_resume`, `memory_plan`, `memory_handoff` and `memory_feedback`. Their exact input schemas are in the [tool catalog](../src/memoryking/adapters/stdio.tools.json); each description says when to call the tool, what comes back and the one mistake to avoid. Feedback accepts outcome/feedback actions; it cannot activate a procedure. Administration, policy changes, source registration and model configuration are deliberately outside this tool surface.

When the service runs with a local embedding model and the project is enabled for it, every briefing also searches by meaning: the service embeds the query itself and fuses the dense and lexical candidates, so an agent gets semantic recall without computing vectors. A briefing's `coverage.dense` says whether that channel was used.

Briefings and resume packets can be delivered as text. Pass `format: "text"` to `brief` or `resume` and the reply is a compact envelope: the same `receipt`, `status`, `usage` and `warnings`, plus `text`, a Markdown briefing with constraints first, numbered guidance carrying `[g1]`-style citations, caveats, and a Sources footer, and `sources`, the citation-to-record map. The envelope is what replay returns, byte for byte, so receipts and budgets behave exactly as for JSON. Use text when a model will read the briefing directly; use JSON when a program will.

A checkpoint can be saved progressively. Through the bridge, `memory_checkpoint` needs only `objective` and `current_state` (plus `task`); every other section defaults to empty, the repository to "none recorded", and each defaulted section is listed in `capture.gaps` as `defaulted:<section>` so the next resume says what was never written. `expected_revision` may be omitted or `null` to accept the current revision without a compare-and-set check; pass the revision you last saw when two writers might race. A missing retry `key` on checkpoint, plan write, handoff and feedback calls is minted by the bridge; pass your own stable key when a retry must replay the first result.

The bridge fills what the session already knows, so a minimal call works: `version`, `incarnation`, `project`, `bindings` (`[{}]`), `max_output_tokens` (6,000) and `tokenizer` (the exact tokenizer when installed, otherwise the disclosed byte estimate) default when omitted, and `memory_capture` fills `request.version` and `request.project`. Explicit values always win, and the service validates the completed command exactly as before, so a stale explicit `incarnation` is still rejected as `history_changed`.

The shared profile also exposes authenticated Streamable HTTP MCP at `/v1/tenants/{tenant}/projects/{project}/mcp`. Configure the tenant/project URL and bearer credential according to your host's supported transport; see [deployment](deployment.md).

### Lean profile for any host

Add `--profile lean` to the bridge command and the host sees five flat tools instead of nine schema-heavy ones: `remember`, `recall`, `save_progress`, `resume` and `hand_off`. Their catalog is under 8 KB with no `oneOf`, every call has defaults, briefings and resumes come back as cited Markdown, and each tool maps onto the full profile through the same client, so authorization, receipts and idempotency are unchanged. The catalog is in [`contracts/stdio.tools.lean.json`](../contracts/stdio.tools.lean.json). Use the full profile when a program, not a model, drives the tools.

The bridge is a plain stdio MCP server, so any host that launches one works the same way. Where the configuration lives differs by host:

| Host | Where to configure | Shape |
| --- | --- | --- |
| Claude Code | `claude mcp add memoryking -- /absolute/path/.venv/bin/python -m memoryking.adapters.stdio --socket ... --credential ... --project demo --profile lean`, or the `mcpServers` block in its settings. | command and args |
| Codex CLI | `~/.codex/config.toml`: `[mcp_servers.memoryking]` with `command = "/absolute/path/.venv/bin/python"` and `args = ["-m", "memoryking.adapters.stdio", "--socket", "...", "--credential", "...", "--project", "demo", "--profile", "lean"]`. | command and args |
| Grok and other hosts | The host's stdio MCP server setting; supply the same command and args. | command and args |

Hosts differ in how they surface tool descriptions and how many tools they tolerate; the lean profile is the safer default for a model-driven host. No host is a certified integration; test yours with `tools/stdio_demo.py`.

## Memory as files: the Anthropic memory tool and the Markdown vault

Anthropic's [memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool) is a file interface: a Claude API agent views, creates, edits, deletes and renames files under `/memories`, and the application answers each command. `memoryking.memory_tool.MemoryTool` answers them from a MemoryKing project, so an agent built on the Claude API uses MemoryKing as its memory backend with no prompt changes, and every write is an ordinary receipted capture or proposal:

```python
from memoryking.memory_tool import DEFINITION, MemoryTool
from memoryking.sdk import LocalClient

memory = MemoryTool(LocalClient(socket_path, credential_path, "demo"))
# In the tool-use loop: send DEFINITION in `tools`; for each memory tool_use block
# append memory.tool_result(block.id, block.input) to the next user message.
```

`tool_result` returns the `tool_result` block, with `is_error` set when the command was refused; `execute` returns the text alone and raises `MemoryToolError` with the text to send back. With the Python SDK's `BetaAbstractMemoryTool` helper, each of the six methods can delegate to `execute(command)`, which reads a dict or an SDK command object alike.

The files are the project's records. Every directory under `/memories/<project>` is a source and every file is one observation: the newest version of a note whose name is its event id, opened by a provenance block (id, source, version, who captured it, when, its hash) followed by the text exactly as stored. `str_replace`, `insert` and `create` on an existing file add a version (`<name>__v2`, `__v3`, ...) and keep the earlier ones as evidence; re-creating a file with the same text records nothing. `view` on a file is a receipted fetch, so what an agent reads counts as a delivery; `view` on a directory lists sources, files and sizes, newest first and bounded, and `README.md` explains the model to the agent. `claims/` holds reviewed knowledge, one file per claim: creating or editing a file there proposes a claim that waits for review like any proposal, and it needs an `evidence: [notes/acme, obs_...]` line in a leading `---` block naming the files or observation ids that support it; a reader sees accepted claims, a reviewer every state. `delete` and `rename` need an administrator credential, because memory is evidence; an agent credential is told to replace the content instead. Names that are not identifiers are mapped deterministically (`Meeting Notes/Acme Corp.md` becomes `Meeting-Notes/Acme-Corp.md`), paths outside the root or shaped like traversal are refused, and a capture from an untrusted source is held exactly as it would be from any other route. `tools/memory_tool_demo.py` runs the six commands against a disposable service.

The same file model is a **Markdown vault** for hand editing and portability:

```sh
.venv/bin/python -m memoryking --profile home vault export --project demo --dir ~/memory-vault
.venv/bin/python -m memoryking --profile home vault import --project demo --dir ~/memory-vault --dry-run
```

Export writes every visible observation (newest version per file) and claim as `<dir>/demo/<source>/<name>.md` and `<dir>/demo/claims/<key>.md`, private files in private directories, plus a README with the export time and the import instructions. Edit the text below a file's provenance block, add files, add a claim file with its evidence line, then import: a changed file becomes a new version, a new file a new observation, a claim file a proposal; unchanged files record nothing, and a file whose name is not an identifier or whose evidence does not resolve is reported and skipped. Nothing is deleted or overwritten in the store, so the vault can be re-exported after an import, carried to another store, or opened in any Markdown editor. Only `knowledge` claims are exported; constraints stay in the knowledge command.

## Structured plans

A plan records **what the agents intend to do and what they have actually reported doing**. It has a title, objective, optional context, ordered phases and steps with stable ids. Each step has a description, status, notes, prerequisite step ids and evidence references. A plan complements the checkpoint an individual agent resumes from: the checkpoint says where one agent stopped, the plan says how far the whole piece of work has come. It does not dispatch agents or authorize builds, merges or external actions.

The normal flow is:

1. Create the plan with `plan_save`, `expected_revision: 0`, an explicit reader audience and a stable retry key.
2. Read `current_revision` with `plan_get`. Mark a ready step `doing` with `plan_step` and that revision.
3. Capture the actual result as an observation, then mark the step `done` with its evidence reference. Use `blocked` with a reason when work cannot proceed.
4. Revise the whole plan with `plan_save` to add or reorder phases and steps. Keep the ids. Resolve a revision conflict by reading and reconciling the current plan.
5. Close the plan as `done` or `abandoned` with a closure note and a captured lesson. `done` requires a non-empty plan whose steps are all done or explicitly skipped.

All four actions, `plan_save`, `plan_step`, `plan_get` and `plan_list`, are one tool, **`memory_plan`**, in MCP, and `client.working(command)` in both Python clients; the CLI sends the same command to the `working` operation with `memoryking request`. The [closed command schema](../contracts/working-v1.schema.json) is authoritative and [`examples/plan.json`](../examples/plan.json) is a complete input. The same tool works from Claude Code, Codex, Grok or any other MCP host; nothing in it is host-specific.

```sh
.venv/bin/python tools/plans_demo.py
```

This disposable example uses the real local service and the model-facing tool contract: it creates two steps, captures inspection evidence, records one completed step, reads the earlier revision and lists the current plan. The same exercise runs over the shared HTTP service with native PostgreSQL in the test suite.

Every plan command carries `version: 1`, the current `incarnation`, `action` and `args.project`. Writes also carry `key` and `args.expected_revision`. A plan lives on a task stream named by `args.task`; use a separate task id from an ordinary agent checkpoint, because a stream cannot change from one kind to the other. `plan_step` requires `step`, `status`, `notes`, `evidence` and a nullable `decision`, and changes only those fields; `plan_save` replaces the whole plan. A read returns `revision` (the snapshot you got) and `current_revision` (the compare-and-set revision to write with, which differs after an operator reassigns the writer). `plan_get` takes a nullable `revision`, null for the current state; `plan_list` takes `limit` (1 to 50) and a nullable `cursor`. Read responses add derived progress and `reported_status` per step; strip those projection fields before using a read as a write body.

Step statuses are `todo`, `doing`, `done`, `blocked`, `skipped` and `unknown`. `doing` and `done` need every prerequisite done or skipped. `blocked` needs a note; `skipped` needs a note and a scope decision, and does not count as completed work. Changing the objective, a completed step's definition or its prerequisites needs fresh evidence or reopening the step; reordering, dropping or duplicating old evidence links is not fresh proof, and reopening then re-citing the old proof does not launder a changed definition. If a prerequisite changes, revise the affected steps together in one `plan_save`.

Progress is counted in equal steps, rounded down to a whole percent: completed steps over the current total. An empty plan has no percentage. The separate resolved count includes justified skips. Adding scope lowers progress. The declared lifecycle (`draft`, `active`, `done`, `abandoned`) is separate from effective completion: evidence the reader can no longer open, or history the reader cannot verify, turns the affected steps `unknown` rather than letting them pass. Recorded evidence is an agent's report, not independent certification.

Before stopping, an agent should capture its findings, save its checkpoint, then update the plan with evidence-backed progress, and read the plan back: a successful write alone does not establish that the next agent can read it.

## Capturing automatically

MemoryKing never reads a conversation on its own, but it is easy to feed from the places where evidence appears. Every route below uses the ordinary authorized capture with a deterministic event id derived from the project, the observed day and the full text, so re-running a hook replays instead of duplicating while the same text on another day or in another project is a new observation, and all of them work whatever model drives the agent.

- **From any model, on any host:** the `remember` tool in the lean profile, or `memory_capture` in the full one. This is the one path that needs no shell access.
- **From a shell:** `memoryking --profile home capture --project demo --source git --event "commit-$(git rev-parse HEAD)" --text "$(git log -1 --format=%s)"`, or pipe text on stdin. `memoryking recall --project demo --query "retry deadline"` prints the cited Markdown briefing. Connection details also come from `MEMORYKING_PROFILE`, or `MEMORYKING_SOCKET` and `MEMORYKING_CREDENTIAL`, with `MEMORYKING_PROJECT`.
- **From pytest:** `pytest -p memoryking.pytest_plugin` with those environment variables set records each failing test's id and report tail under source `pytest`. Without the variables it stays silent, and a capture error never fails the run.
- **From git:** a `post-commit` hook that runs the capture command above records each commit as an observation.
- **From Claude Code:** a `PostToolUse` hook on `Bash` that pipes the tool's output into `memoryking capture --source claude-code` when the command was a test or build. Codex and other hosts with shell hooks use the same command; hosts without hooks rely on the model calling `remember`.
- **At the session's edges:** the three host hooks below load memory when a session starts and save it before compaction and at the end, with no model involvement.

Hook captures record the observation time to the day unless `--observed-ms` is given, so a rerun sends identical bytes and replays; the store's own event log keeps the exact time of each capture. Hook captures are evidence like any other: a briefing cites them with their source, a reviewer can promote or quarantine what they contain, and the owner can ask where any of them went with `memoryking influence OBSERVATION --project P` or the dashboard's **Where did this go** view.

## Drafts that never accept themselves

Every other memory system lets a model extract facts on write. MemoryKing keeps its review layer and adds a drafting pass instead: `memoryking draft-proposals --project demo` (or the `knowledge` action `draft`, or the **Draft proposals** button in the dashboard's Review view) looks at the newest 512 visible observations for a name that several independent observations share, an anchor first (a path, ticket, host, symbol or error code) and failing that a rare term, takes the most recent sentence that carries it, and files an ordinary proposal with one evidence span per supporting observation. Two captures of the same event are one occurrence, not repetition. The draft is a `knowledge` claim in state `proposed`, its provenance names the method, topic and support, it carries the drafting principal, and it waits in the review queue like any proposal: nothing it writes becomes a constraint, a guide or accepted knowledge until a reviewer accepts it. A repeated run with the same evidence files nothing; new evidence on a known topic files a new revision on the same family, which a reviewer sees as a superseding proposal. Drafting needs the capture capability and is replay-safe under its retry key. It is deterministic and uses no model; a provider-written draft can be admitted later only through the egress policy, the way summaries are.

## Anchors: the exact names memory hangs on

At capture, MemoryKing extracts the exact names a developer's memory hangs on, with no model: file paths, code symbols (`RetryDeadline.compute`, `retry_deadline_ms`), ticket ids (`PAY-1234`, `#4521`), URLs and their hosts, hostnames, and error codes (`ECONNRESET`, `TimeoutError`, `HTTP 503`). They are stored beside the observation as anchors, at most 32 per record. A briefing whose query names an anchor finds every record that carries it exactly, even when the surrounding words differ, and cites it as an `anchor_match` with a full score; naming a thing outranks sharing a word with it. The memory graph lists each observation's anchors and draws a `shares_anchor` link between records that share a rare one (held by two to eight of the loaded records), which is a light knowledge graph without a graph database. Stores created before anchors existed gain them on the next `reindex`.

## Salience: memories that strengthen with use

On request (`salience: "on"` on `brief`, `--salience` on `recall`, `salience: true` on the lean `recall` tool), ranking adds a small, bounded nudge from use, computed from what the store already keeps. It is off unless asked, because a briefing's order must not depend on what earlier briefings delivered unless that is what you want, and an operator's frozen ranking policy, when one is active, supersedes it. A record delivered to you in a briefing, fetch or rerun during the last ninety days gains strength that fades by half every thirty days; a record you marked `helpful` gains more; a record nobody has been delivered and that is older than 180 days drops to an **archival** tier. Strength is per principal, because receipts and feedback are private and another principal's opinion must never reorder your briefing; the archival tier looks only at whether anyone used the record at all that is still searchable and still cited when nothing else matches, but ranked after everything active. The nudge is at most a tenth of a full lexical score, every briefed item carries its `salience` (strength, tier, deliveries, helpful votes), the receipt's trace records the stage with its constants, and an as-of or historical briefing disables it, because later use must not reorder an earlier view. Nothing is deleted or rewritten; looking at the graph never counts as use.

## Measuring recall without a judge

`tools/evidence_recall.py --data longmemeval_oracle.json --output report.json` records LongMemEval's sessions at their own dates, asks each question at its date through a real briefing, and scores whether the cited observations come from the labelled evidence sessions and answer turns. No answer is generated or judged, so the figure measures the memory layer alone and is not comparable to answer-accuracy leaderboards. Download the data separately (it is not committed); the check runs the same harness on a small committed sample as a gate.

## Briefings as of a moment

Every claim carries the time it became valid and the position at which it was recorded, and every observation its recorded position, so a briefing can be asked for the project as it stood at an instant. Pass `as_of_ms` to `brief`: the service resolves it to the history frontier at that wall time and judges claim validity at that time, the packet reports `as_of_ms`, `recorded_position` and `valid_ms`, and the receipt replays exactly. Nothing recorded after that instant appears, and a claim retired since then is still in force. The CLI takes `memoryking recall --as-of 2026-10-20` (a date means the end of that day), the lean `recall` tool takes `as_of`, and the dashboard's briefing form has an **As of** field. Ranking adjustments learned since then are disabled for an as-of briefing, and procedures are withheld, because both would import later knowledge into an earlier view. Passing `as_of_ms` together with an explicit `recorded_position` or `valid_ms` is refused.

## Hooks that load and save memory by themselves

Claude Code, Codex and Grok all run command hooks at the same three moments, and MemoryKing ships one command for each. Install them once per project (or per user) and every session on that host starts briefed and ends saved, whatever model is driving:

```sh
.venv/bin/python -m memoryking --profile home hooks install --host claude --project demo --task retry-fix
```

| Moment | Host event | What `memoryking hook …` does |
| --- | --- | --- |
| Session start, resume, or right after compaction | `SessionStart` | `session-start` prints the task's resume packet (when `--task` is set and a checkpoint exists) followed by a cited Markdown briefing for `--query`, the task, or the project. Claude Code and Codex add the output to the model's context. |
| Before the transcript is compacted | `PreCompact` | `pre-compact` captures the tail of the transcript (assistant text from the JSONL, bounded to 16 KiB) plus any compaction instructions as an observation under source `<host>-compact`, and with `--task` saves a minimal checkpoint that references it. |
| Session end | `SessionEnd` | `session-end` captures the host's last assistant message when it is given, else the transcript tail, under source `<host>-session`. |

The installer merges into the host's own file and leaves every other hook alone: `.claude/settings.json` for Claude Code, `.codex/hooks.json` for Codex and `.grok/hooks/memoryking.json` for Grok, under the project directory or, with `--scope user`, under your home directory. `--print` shows the merged file without writing it and `--remove` takes MemoryKing's entries out again. Commands are written with absolute paths and the profile (or `--socket` and `--credential`) spelled out, because hosts run hooks without your shell's environment. Running the installer twice is a no-op.

Hook captures use a deterministic event id from the host, the session and the text, and the hook checkpoint uses a stable retry key, so a hook that fires twice replays rather than duplicating; a later compaction with new text is a new observation. A hook never fails the host: errors go to stderr and the exit code stays zero (`--strict` for debugging). The hook reads the host's event JSON from stdin in either spelling (`hook_event_name` for Claude Code and Codex, `hookEventName` and the `GROK_*` variables for Grok).

Two host differences to know. Grok ignores hook stdout, so on Grok the session-start briefing is not injected; the save hooks still work, and Grok also reads the Claude Code settings file, so one install serves both. Claude Code gives `SessionEnd` hooks about a second and a half by default; the installer sets a 10-second timeout for that hook, which Claude Code honours, and the capture is one local socket call.

What the hooks save is evidence, not a summary: a reviewer sees the transcript tail with its source, and the checkpoint lists its defaulted sections as gaps so a resume says what was never written. `tools/hooks_demo.py` runs all three against a disposable service and shows the installer's output for each host.

## Source trust: holding captures you do not yet trust

Everything captured is evidence, but not every source deserves to reach a briefing before a person has looked. An admin can mark a source untrusted for one project or for every project (`*`), by exact name or glob:

```sh
.venv/bin/python -m memoryking --profile home trust set --project demo --source "scraper-*" --level untrusted
```

A capture from a matching source is recorded exactly like any other (same event id, same replay, same backup) but is **held**: invisible to every read, briefing, graph page, resume and claim proposal, and absent from the influence of anything built afterwards, until an admin releases it. `memoryking trust held --project demo` lists what is waiting with a 256-byte preview; `memoryking trust release OBSERVATION --project demo` releases one; `memoryking trust list` shows the rules. The dashboard's **Review & conflicts** view shows the same queue with a Release button and a form for rules. Rules and releases are journaled policy decisions: a restore replays them, and the policy epoch retires any view built before the decision. The most specific rule wins: an exact source beats a pattern, and a project rule beats a `*` rule, so one untrusted host can be allowed back for one project without touching the rest. Purge works on a held capture exactly as on any other.

This is the quarantine lifecycle claims already had, applied one layer down: a poisoned capture from a feed, a scraper or a host hook on a machine you do not control never reaches a model unreviewed, and if it did reach one, the influence report says where.

## Where this left off: session summaries

A resume packet is exact but long. Every resume now begins with a three-sentence summary when one exists: where the task stands, what was learned or tried, and what comes next. Summaries are derived artifacts with their own receipt, stored beside the checkpoint they summarize; the checkpoint remains the record and the summary never adds facts to it.

Three things produce them. A hand-off derives the sender's summary and returns it in the `prepare` result, so the recipient reads three sentences before the packet. The daemon derives one for any task whose latest checkpoint has been idle for `--summary-idle` (30 minutes by default) when the owner may read that checkpoint, so the dashboard's Task activity shows where each task left off. And a host can ask for one with the working-state action `summarize` or `memoryking summarize --project P --task T`.

The default method is `extractive-v1`: a deterministic paragraph built from the checkpoint body plus counts of recent observations and open operations, quoting nothing but the checkpoint, so it is safe to show to every reader of the checkpoint. With an [egress policy](deployment.md#egress-policy) loaded, `memoryking summarize --provider NAME --model M --egress-policy FILE` has a model write it instead: the resume packet travels through `AuthorizedConversation` as audited memory, and the daemon admits the reply as a summary only if it is byte for byte the delivered reply recorded in the caller's egress audit for that permit. A model-written summary therefore cannot be edited on the way in, and its sources are the receipts the audit cites.

## What the host must do

1. Supply accurate project/task identity and applicability bindings.
2. Save useful observations and checkpoints; MemoryKing does not scrape the conversation automatically.
3. Inspect briefing/resume status. `blocked`, `budget_insufficient`, `incomplete` and unavailable results require a decision, not silent truncation or permission bypass.
4. Treat source text and proposed lessons as untrusted evidence.
5. Reconcile uncertain external operations before repeating them.
6. Recheck authorization before sending retained context to a model.

For the last step, use `AuthorizedConversation` from `memoryking.harness_context`. It binds retained content and derived messages to authorization, checks current status before dispatch and rebuilds or blocks when dependencies become unavailable. Retrieved memory enters the conversation as user-role content whose bytes the store verifies against the receipt, never as a system instruction. A conversation holds at most 128 messages, so a dispatch carries up to 127 and the reply still fits; the request stays within 96 KiB. Ordinary capture/retrieval calls cannot remove restricted text from an unrelated host's old chat transcript. Provider requests already delivered cannot be recalled.

The packaged reference agent and inference examples use a local recording provider, which never transmits anything. To dispatch through a real model, the operator writes an [egress policy](deployment.md#egress-policy) and starts the service with `serve --egress-policy`; the host then builds an adapter from that policy (`memoryking.providers.adapter(policy, "openai-chat", "grok-4")`) and passes it to `AuthorizedConversation` in place of the recorder. Three adapters ship: Anthropic, OpenAI-compatible chat completions (OpenAI, xAI Grok, and local servers such as Ollama or vLLM) and Google Cloud Vertex AI (Gemini, or Anthropic models through Vertex). The daemon refuses any provider, model, byte count or classification the policy does not allow before issuing a permit, and audits every dispatch; see `memoryking egress-audit`. The reference agent's `model-turn` takes `--egress-policy FILE --provider NAME --model M` for the same path. There is no autonomous scheduling service included.

## Recipes

These commands run disposable examples in the [contributor environment](../CONTRIBUTING.md#setup):

| Command | Demonstrates |
| --- | --- |
| `python tools/knowledge_demo.py` | Capture → propose → review → quarantine. |
| `python tools/briefing_demo.py` | Briefing, bounded evidence fetch, exact replay and explicit rerun. |
| `python tools/working_demo.py` | Checkpoint/resume and uncertain-operation reconciliation. |
| `python tools/stdio_demo.py` | Python client and actual MCP stdio subprocess. |
| `python tools/plans_demo.py` | A structured plan: create, prove a step, read an earlier revision, list. |
| `python tools/hooks_demo.py` | The three host hooks: save before compaction, save at the end, load at the next start; installer output per host. |
| `python tools/memory_tool_demo.py` | Anthropic's memory tool over a project: view, create, edit, propose a claim, refuse delete for an agent, export the vault. |
| `python tools/inference_demo.py` | Revoked content excluded from subsequent local provider requests. |
| `python tools/repository_demo.py` | Repository snapshots and guidance freshness. |
| `python tools/experience_demo.py` | Reviewed procedures, outcomes and bounded ranking. |

Use `.venv/bin/python` in place of `python` unless the environment is activated. The examples make no paid provider calls.

[Back to README](../README.md) · [Design](design.md) · [API reference](reference.md)
