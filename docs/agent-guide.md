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

Besides the everyday methods in [Using the client libraries](#using-the-client-libraries), client methods include `capture`, `retrieval`, `working`, `handoff`, `repository`, `embedding`, `processing` and `experience`. Requests use versioned JSON schemas, not free-form option dictionaries. See the [API reference](reference.md#interfaces) and executable examples for full request bodies.

For capture, keep a stable key and stable source-event identity for the same event. Changed bytes with the same identity produce a conflict. Save the exact request before dispatch; do not create a new identity merely to make an uncertain retry succeed. After a history change, reconcile first. The client captures in a named namespace (`namespace="sdk"` by default) that is deterministic for the principal, so the same key from any process replays the same result or reports `idempotency_conflict`; pass a different `namespace` to keep two integrations' keys apart.

`SharedClient` in `memoryking.shared_sdk` provides the shared HTTP transport with tenant/project binding and verified TLS. It rejects redirects and does not automatically retry mutations.

## Using the client libraries

Three clients cover the everyday five operations, with the same names and meaning as the [lean MCP tools](#lean-profile-for-any-host): `remember`, `recall`, `save_progress` (`saveProgress` in TypeScript), `resume` and `hand_off` (`handOff`). Each call goes through the same authorization, receipts, idempotency and budgets as the tool of that name. In Python the method and the tool share one implementation; the TypeScript client follows the same steps over the shared service's MCP endpoint, and its tests run against a real service.

### Python

`LocalClient` (Unix socket) and `SharedClient` (HTTPS) both have the methods. Results are small frozen dataclasses (`Remembered`, `Briefing`, `Progress`, `Resumed`, `HandOff`); the service's whole reply is `.raw`. Bad arguments raise `LocalError` with code `invalid_tool_arguments` before any request, and every `LocalError` carries a `hint`.

```python
from memoryking.client import MemoryKing

with MemoryKing.connect() as memory:  # profile or MEMORYKING_* variables, like the CLI
    memory.remember("Raising every timeout exceeded the total retry deadline.", source="pytest")
    briefing = memory.recall("timeouts", budget=4000)  # as_of=..., rerank=True, format="json"
    print(briefing.status, briefing.text)
    memory.save_progress("retry-fix", "Fix the retry deadline", "Investigating",
                         next_actions=["Run the regression"], share_with=["reviewer"])
    print(memory.resume("retry-fix").text)
    memory.hand_off("retry-fix", "reviewer")
```

`connect()` reads `MEMORYKING_PROFILE`, or `MEMORYKING_SOCKET` and `MEMORYKING_CREDENTIAL`, with `MEMORYKING_PROJECT` and `MEMORYKING_BINDINGS`, exactly as the command line does. With `MEMORYKING_URL` (or `url=`), `MEMORYKING_TENANT` and `MEMORYKING_CREDENTIAL` it returns a `SharedClient`; `MEMORYKING_CA_FILE` names a private certificate authority. The context manager holds no connection; it exists so code can scope a client the same way for every transport.

The async clients, `AsyncLocalClient` and `AsyncSharedClient` (or `await MemoryKing.aconnect()`), have the same five methods as coroutines and use only the standard library. They run the synchronous client on worker threads with `asyncio.to_thread`: the client's current-context checks, defaults, idempotency keys and checkpoint merging are one synchronous code path shared by both transports, and Python's standard library has no async HTTPS client, so a native async copy would duplicate security-relevant logic. The event loop never blocks and calls from different tasks run concurrently. Cancelling a task does not cancel a request already sent, so a cancelled write may still have been applied; retry it with the same `event` (for `remember`) or read the task's `revision` first.

```python
async with await MemoryKing.aconnect() as memory:
    briefing = await memory.recall("timeouts")
```

Run [the executable example](../examples/client_python.py) with `.venv/bin/python examples/client_python.py`.

### TypeScript

`@memoryking/client` lives in `packages/client`, a second workspace package beside the contracts, because the npm workspace already builds every package together and the client needs the contracts' generated types. It is ready to publish (Apache-2.0, `files`, README) but is not published yet; the contracts package stays private. It needs Node 22, has no runtime dependencies and is an ES module.

```ts
import { MemoryKing, MemoryKingError } from "@memoryking/client";

const memory = MemoryKing.connect(); // MEMORYKING_URL, _TENANT, _PROJECT, _CREDENTIAL (a file path)
await memory.remember("Raising every timeout exceeded the total retry deadline.");
const briefing = await memory.recall("timeouts", { budget: 4000 });
await memory.saveProgress("retry-fix", "Fix the retry deadline", "Investigating", {
  nextActions: ["Run the regression"],
  shareWith: ["reviewer"],
});
const note = await memory.resume("retry-fix");
try {
  await memory.handOff("retry-fix", "reviewer");
} catch (error) {
  if (error instanceof MemoryKingError) console.error(error.code, error.hint);
}
```

`MemoryKing` talks to the shared service over HTTPS (plain `http` only to loopback with `loopbackHttp: true`). Failures throw `MemoryKingError` carrying the service's `code` and `hint`. A private certificate authority is trusted through Node's `NODE_EXTRA_CA_CERTS`.

The local daemon's loopback agent endpoint (`POST /api/v1/retrieval`, see [Dashboard agent endpoint](reference.md#dashboard-agent-endpoint)) serves retrieval commands only. `MemoryKingLocal` therefore has `recall` and nothing else, and needs the dashboard port and the store's history id (`History` in `memoryking status`). A local program that writes uses the Python client or the stdio MCP bridge, or the shared service. The [TypeScript example](../examples/client_typescript.mjs) recalls from a real local daemon; run it with `npm run build` then `.venv/bin/python examples/client_typescript.py`. The write operations are exercised by `tests/js/client.test.mjs` against a real shared service when PostgreSQL is available (`MEMORYKING_PG_BIN`); without it that test is skipped.

## Using MemoryKing from agent frameworks

Each adapter is a thin translation between a framework's interface and the [client methods](#using-the-client-libraries): the tools run `memoryking.lean.call`, the code the lean MCP tools run, so authorization, receipts, idempotency and budgets are unchanged and nothing is copied. A refusal comes back to the model as text (`{"error": code, "hint": ...}`), not as an exception that ends the run. The adapters live in `memoryking.integrations`; the framework is imported only when you call them, and MemoryKing does not install any of them. Install the framework in your own environment. MemoryKing declares no extras for them, because a hash-pinned lock of each framework's dependency tree is not maintained here.

| Framework | Tested version | The adapter provides | It deliberately does not |
| --- | --- | --- | --- |
| Any provider, your own loop (`memoryking.integrations.function_calling`) | none needed | `FunctionTools(client)`: the tool definitions in the Chat Completions or Responses shape, and `call(name, arguments_json)` returning the text a model should see. | Run the loop, pick a provider or retry. |
| Claude Agent SDK (`memoryking.integrations.claude_agent_sdk`) | `claude-agent-sdk` 0.2.187 | `mcp_server_config` (the lean bridge for `mcp_servers`), `sdk_server` and `sdk_tools` (the same tools in-process), `session_context` (a briefing for the system prompt), `pre_compact_hook` and `hooks` (save before a compaction), `session_start_hook`. | Drive a live Claude Code session in its tests. `session_start_hook` is registered under `SessionStart`, which the SDK's typed hook events do not list; it has been called directly, not by a live session, so use `session_context` in the system prompt for a dependable load. Hooks need the local socket, so a `SharedClient` is refused. |
| OpenAI Agents SDK (`memoryking.integrations.openai_agents`) | `openai-agents` 0.25.0 | `tools(client)`: `FunctionTool` objects for `Agent(tools=...)`. | A `Session`. The session protocol stores a conversation and requires `pop_item` and `clear_session`; MemoryKing is not a transcript store, keeps observations as evidence and never deletes on request. Keep the SDK's session for the conversation and use `resume` and `recall` for what should outlive it. |
| LangChain and LangGraph (`memoryking.integrations.langchain`) | `langchain-core` 1.8.4, `langgraph` 1.2.24 | `tools(client)`: `StructuredTool` objects. `MemoryKingStore`: a `BaseStore` where `put` is a capture (the key and value saved as one observation, so a changed value is a new version), `search((project,), query=...)` is a briefing returned as one `SearchItem`, `delete(namespace, memory_id)` is a retirement request. | `get`, `list_namespaces`, TTLs, filters, offsets, a query-less search and a search narrower than the project raise `NotImplementedError`. Namespaces are `(project, *labels)`; the labels become the observation's source and may use only letters, digits, `_` and `-`. `delete` takes the id of a memory from a briefing's sources, not the key you put, and an admin decides. |
| LlamaIndex (`memoryking.integrations.llama_index`) | `llama-index-core` 0.14.26 | `tools(client)`: `FunctionTool` objects. `MemoryKingBlock`: a memory block that briefs the latest user message into the prompt. | Write. The block's `put` does nothing, because ejected messages are a transcript; an agent saves what matters with `remember` or `save_progress`. It briefs from what was saved; it is not a retriever over documents. |

Every adapter was exercised against the real framework classes and a real temporary store and daemon, with no model and no provider key: the Claude Agent SDK tools and hooks by calling them as the SDK does, the lean bridge command over MCP stdio, the OpenAI Agents tools inside the SDK's own `Runner` loop with its scripted test model, the LangChain tools inside a LangGraph `ToolNode`, and the LlamaIndex block inside `Memory`. No live model session was run, so how a particular model uses the tools is not tested here. The tests are in `tests/integrations/` and skip when a framework is missing; run them in a scratch environment that has the frameworks installed with `python -m unittest discover -s tests/integrations`. The function-calling helper's tests are part of the main suite. Each framework has an example in [`examples/integrations`](../examples/integrations) that needs no model; `with_function_calling.py` needs only MemoryKing, and each example's comments show where a real model plugs in.

```python
from agents import Agent

from memoryking.client import MemoryKing
from memoryking.integrations import openai_agents

memory = MemoryKing.connect()
agent = Agent(name="engineer", tools=openai_agents.tools(memory))
```

## MCP integration

The quickest route is `memoryking setup <claude-code|codex|generic> --project NAME` ([getting started](getting-started.md#2-connect-your-agent)): it creates the store if needed, starts the service, issues one agent credential, registers the lean bridge and installs the hooks under that same credential. What follows is the manual configuration it writes for you.

The local bridge connects an MCP-capable host to a running local service, and with a store profile that allows it (`profile add --autostart`) it starts the service itself when the socket is gone. Spell the paths out, or name a store profile with `--store-profile` (the bridge's `--profile` chooses the tool set, not the store), and add `--profile lean` unless a program, not a model, drives the tools:

```json
{
  "mcpServers": {
    "memoryking": {
      "command": "/absolute/path/to/memoryking/.venv/bin/python",
      "args": [
        "-m", "memoryking.adapters.stdio",
        "--socket", "/absolute/path/to/store/service.sock",
        "--credential", "/absolute/path/to/private/agent.credential",
        "--project", "demo",
        "--profile", "lean"
      ]
    }
  }
}
```

This illustrates the common `mcpServers` shape; each host has its own configuration location and support requirements. The bridge itself is exercised, but third-party hosts are not certified integrations.

The bridge serves the small lean tool set by default ([below](#lean-profile-for-any-host)). `--profile full` serves nine schema-heavy tools meant for programs: `memory_status`, `memory_capture`, `memory_brief`, `memory_fetch`, `memory_checkpoint`, `memory_resume`, `memory_plan`, `memory_handoff` and `memory_feedback`. Their exact input schemas are in the [tool catalog](../src/memoryking/adapters/stdio.tools.json), generated from the contracts with every definition a tool does not use left out and a description on every field a caller fills (about 44 KB, down from about 85 KB); each tool description says when to call the tool, what comes back and the one mistake to avoid. Feedback accepts outcome/feedback actions; it cannot activate a procedure. Administration, policy changes, source registration and model configuration are deliberately outside this tool surface.

When the service has a local embedding model loaded and the project is enabled for it (`memoryking semantic enable`, or `setup` with the embeddings extra installed), every briefing also searches by meaning: the service embeds the query itself and combines the semantic and lexical candidates, so an agent gets semantic recall without computing vectors. Observations are embedded passage by passage and accepted claims as well, so reviewed knowledge is found by meaning too. A briefing's `coverage.dense` says whether that channel was used, and when it was not, the receipt's trace says why (`no_model_loaded`, `project_not_enabled`); search is then lexical, exactly as without the extra.

Briefings and resume packets can be delivered as text. Pass `format: "text"` to `brief` or `resume` and the reply is a compact envelope: the same `receipt`, `status`, `usage` and `warnings`, plus `text`, a Markdown briefing with constraints first (`## Must follow`), then `## Accepted knowledge` and `## Unreviewed observations` as separate sections whose numbered items carry `[g1]`-style citations and, for an observation, the time it was recorded to the minute, caveats, and a Sources footer, and `sources`, the citation-to-record map. The envelope is what replay returns, byte for byte, so receipts and budgets behave exactly as for JSON. Use text when a model will read the briefing directly; use JSON when a program will.

A checkpoint can be saved progressively. Through the bridge, `memory_checkpoint` needs only `objective` and `current_state` (plus `task`); every other section defaults to empty, the repository to "none recorded", and each defaulted section is listed in `capture.gaps` as `defaulted:<section>` so the next resume says what was never written. `expected_revision` may be omitted or `null` to accept the current revision without a compare-and-set check; pass the revision you last saw when two writers might race. A missing retry `key` on checkpoint, plan write, handoff and feedback calls is minted by the bridge; pass your own stable key when a retry must replay the first result.

The bridge fills what the session already knows, so a minimal call works: `version`, `incarnation`, `project`, `bindings` (`[{}]`), `max_output_tokens` (6,000) and `tokenizer` (the exact tokenizer when installed, otherwise the disclosed `chars-estimate-v1` estimate of about three characters per token) default when omitted, and `memory_capture` fills `request.version` and `request.project`. Explicit values always win, and the service validates the completed command exactly as before, so a stale explicit `incarnation` is still rejected as `history_changed`.

The shared profile also exposes authenticated Streamable HTTP MCP at `/v1/tenants/{tenant}/projects/{project}/mcp`. It serves the lean tool set by default and the full catalog with `?profile=full` (any other value is refused as `unknown_profile`); `confirm` is not served over HTTP, since chat review is a local operator switch. The endpoint accepts the protocol revisions of the pinned MCP library: `2024-11-05`, `2025-03-26`, `2025-06-18` and `2025-11-25` through the `initialize` handshake, and the stateless `2026-07-28` revision, where each request carries its own protocol envelope and no session. The stdio bridge speaks the four handshake revisions: it answers with the client's revision when it is one of them and with `2025-11-25` otherwise, and it does not implement the stateless revision. Configure the tenant/project URL and bearer credential according to your host's supported transport; see [deployment](deployment.md).

### Lean profile for any host

The bridge serves the lean profile unless you pass `--profile full`. The host sees flat tools instead of the full profile's schema-heavy ones: `remember`, `recall`, `save_progress`, `resume` and `hand_off`; `fetch` (read the exact text behind an id in a briefing's Sources, optionally a byte range, receipted like any fetch), `feedback` (say whether a recalled item was `helpful`, `not_helpful` or `harmful`, by receipt id and the item's id or tag such as `g1`; what salience and the health report read) and `recent` (the newest memories you may read, with ids for `correct`, `forget` and `fetch`); and `propose`, `correct` and `forget` for [reviewing and correcting memory](#reviewing-and-correcting-memory). `confirm` is added only when the bridge runs with `--allow-chat-review`, since without it the tool can only refuse. The default catalog (eleven tools) is about 12 KB with no `oneOf`, every call has defaults, and each tool maps onto the full profile through the same client, so authorization, receipts and idempotency are unchanged. The catalog file, [`contracts/stdio.tools.lean.json`](../contracts/stdio.tools.lean.json), lists every lean tool including `confirm`. Use the full profile when a program, not a model, drives the tools.

**What a reply looks like.** A lean reply is its text block alone: no `structuredContent`, and no tool declares an `outputSchema`. Hosts differ in what they show a model when a result has both parts (Claude Code 2.1.330 shows only `structuredContent`), so the lean tools put everything the model needs in one part, and it reads it once in every host. A `recall` or `resume` is the Markdown briefing followed by one line, `[receipt rcpt_...; status partial; warnings ...; ...]`: the receipt id (what `feedback` takes), the status, any warning the briefing does not already say, and, when the briefing cites sources, a reminder that `fetch` with an id from the Sources list reads one in full. `remember`, `correct` and `save_progress` answer in one line with the id (`Saved memory as obs_...`, or `Already saved as obs_...` when the same text from the same source on the same day was saved before, which is how a replay shows); `propose`, `confirm`, `forget`, `hand_off` and `feedback` answer in one line with the id of what they filed; `fetch` returns the span; `recent` a list of ids with the first words of each. A refusal is unchanged: a result marked as an error with a short `error`, `hint` and the detail that names the field, in both parts. The full profile's text block is still the canonical JSON envelope (byte-exact for replay), as before. A program that needs the receipt, the `sources` map, usage or other envelope fields should use `--profile full`; the lean tools no longer carry them as `structuredContent`.

**When a call is wrong.** A failed argument check is a tool result marked as an error, not a protocol error: it names the first failing JSON path (`$.query`), what was wrong, what was expected (types, allowed values, a pattern in words, lengths and ranges, the properties that exist) and, for a missing field, its name. It never repeats what you sent, only its type and size. A denied call names the capability the operation needs (`required_capability`), so a credential that lacks `checkpoint` says so. The same detail appears in the CLI's JSON errors, and `credential issue` with an unknown capability lists the ones that exist.

Lean `save_progress` merges: a section you leave out (`next_actions`, `hard_constraints`, `findings`, `failed_attempts`, `blockers`) keeps what the task stream's latest save recorded, a section you send replaces it, and an empty list clears it. If the stream exists but cannot be read back, the save is refused rather than replacing it blind. Each save passes the revision it merged from, so two racing writers conflict instead of overwriting. Findings are saved as `hypothesis` findings with no evidence; attach evidence through the full profile. The full profile's `memory_checkpoint` keeps explicit replace semantics.

A resume is `incomplete` with `reason: recovery_reconciliation_required` only for real recovery gaps: an operation whose outcome is unknown or that needs reconciliation, a required or stored artifact that is unavailable, dirty repository state without its artifacts, or any non-`defaulted:` gap the writer recorded in `capture.gaps`. Sections nobody wrote (`defaulted:<section>`) do not block; the packet lists them as `unrecorded_sections` and the Markdown shows them under "Never written down".

### Session bindings: telling the tools where you work

A constraint can apply only to an environment, role, task stage, repository, path or commit. To know whether it applies, the service needs those facts; when a field the constraint depends on is missing, its applicability is unknown, and an unknown essential constraint blocks a briefing rather than being dropped. The lean tools take no bindings, so you state them once per session instead of on every call:

| Where | Form |
| --- | --- |
| Flag | `--binding environment=prod`, repeatable, on the stdio bridge and on `memoryking recall`, `memoryking summarize`, `memoryking hook` and `memoryking hooks install` (which writes it into the installed hook commands). |
| Environment | `MEMORYKING_BINDINGS="environment=prod,role=implement"`: comma-separated `key=value` pairs, or a JSON object (`{"path": "src/a,b.py"}`) when a value contains a comma. |
| Profile | `memoryking profile add work --store ... --control ... --binding environment=prod`, or `bindings = { environment = "prod" }` in `~/.memoryking/config.toml`; `--profile work` (or `MEMORYKING_PROFILE`, and `--store-profile` on the bridge) applies it. |

Sources merge key by key: the profile first, then `MEMORYKING_BINDINGS`, then flags. Keys are the selector fields and nothing else: `repository_id`, `component_id`, `symbol_id`, `environment`, `task_stage`, `role`, `path`, `commit` and `snapshot_id`; anything else is refused as `invalid_binding`. Dependency versions are a map and are supplied per call through the full profile. The lean tool schemas are unchanged, because bindings are session configuration, not something the model should guess. In an MCP host set them on the bridge command, for example `claude mcp add memoryking -- /absolute/path/.venv/bin/python -m memoryking.adapters.stdio --socket ... --credential ... --project demo --profile lean --binding environment=prod`; for Codex add `"--binding", "environment=prod"` to `args`; for any other stdio host add the same two arguments, or put `MEMORYKING_BINDINGS` in the server's environment block. The host runs the bridge, so state the environment it really works in.

When applicability is still unknown, the reply says so and names the field. A blocked briefing says "Blocked: applicability_unknown. Decide, do not truncate. Missing binding fields: environment.", and the packet carries the same names in `unbound_fields`. Only field names are given, never a constraint's text or values, and a constraint you are not allowed to read contributes nothing (that case is `essential_policy_unavailable`). Bind the field, or answer with a binding that rules the constraint out (`environment=dev`), and ask again.

Your own working state does not depend on that. A resume whose only problem is unresolved constraint applicability still returns the checkpoint you are authorized to read, with `status: "blocked"`, `reason: "applicability_unknown"`, `unbound_fields`, and no constraints; the Markdown shows your saved state under a note that mandatory constraints are unresolved, so do not assume none apply. Lean `save_progress` therefore keeps working and merges as usual. The packet is still blocked, so a harness or `AuthorizedConversation` refuses to feed it to a model, and hand-offs, summaries and adapter resumes get no checkpoint from it. Every other blocked reason (an unresolved conflict, a purged or unreadable essential policy, required reconciliation, changed history, a missing grant) still withholds the checkpoint: those mean the constraints cannot be trusted to be complete, not just that a field is missing.

The bridge is a plain stdio MCP server, so any host that launches one works the same way. Where the configuration lives differs by host:

| Host | Where to configure | Shape |
| --- | --- | --- |
| Claude Code | `claude mcp add-json memoryking '{"type":"stdio","command":"/absolute/path/.venv/bin/python","args":["-m","memoryking.adapters.stdio","--socket","...","--credential","...","--project","demo","--profile","lean"],"alwaysLoad":true}'`, or the same entry in the `mcpServers` block of its settings or of a file passed with `--mcp-config`. | command and args; `alwaysLoad` (below) |
| Codex CLI | `~/.codex/config.toml`: `[mcp_servers.memoryking]` with `command = "/absolute/path/.venv/bin/python"` and `args = ["-m", "memoryking.adapters.stdio", "--socket", "...", "--credential", "...", "--project", "demo", "--profile", "lean"]`. | command and args |
| Grok and other hosts | The host's stdio MCP server setting; supply the same command and args. | command and args |

Hosts differ in how they surface tool descriptions and how many tools they tolerate; the lean profile is the safer default for a model-driven host. No host is a certified integration; test yours with `tools/stdio_demo.py`.

**Hosts that load tools lazily.** Claude Code (checked against version 2.1.330) lists an MCP server's tools by name only and loads a tool's schema when the model fetches it with its tool-search tool; a model that never does so never calls the tool, and in the first task-outcome run no agent ever saved anything through the bridge for that reason ([reference](reference.md#what-the-first-run-shows)). Three things make the tools survive this. The server's instructions reach the model in any case, and say to save as soon as the user gives an instruction or a decision or an approach fails. The bridge marks `remember`, `recall`, `save_progress` and `resume` with `_meta` `anthropic/alwaysLoad`, which that host reads from a tool listing, and gives the other tools an `anthropic/searchHint`; other hosts ignore both keys. And `memoryking setup claude-code` prints a registration with `"alwaysLoad": true` on the server, an option of Claude Code's own MCP configuration that keeps every tool of the server in the prompt (the always-loaded four cost about 4 KB of prompt in every request). Each tool's name and first sentence say when to call it, which is all a deferred listing shows. None of this is needed for hosts that load every tool schema up front.

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
- **From a shell:** `memoryking --profile home capture --project demo --source git --event "commit-$(git rev-parse HEAD)" --text "$(git log -1 --format=%s)"`, or pipe text on stdin. `memoryking recall --project demo --query "retry deadline"` prints the cited Markdown briefing. Connection details also come from `MEMORYKING_PROFILE`, or `MEMORYKING_SOCKET` and `MEMORYKING_CREDENTIAL`, with `MEMORYKING_PROJECT` as the default for `--project` in `capture`, `recall`, `summarize`, `save-progress` and `resume`.
- **Checkpoints from a shell:** a shell-only agent can save and read task progress with the same code as the lean `save_progress` and `resume` tools: `memoryking save-progress --task fix-retry --objective "Fix the retry deadline" --state "Backoff exceeds the deadline" --next "Run the regression" --next "Update the changelog" --finding "Raising every timeout hides it" --blocker "Need a full regression run" --failed "Raise every timeout => still failed"`, then `memoryking resume --task fix-retry`. Only `--task`, `--objective` and `--state` are required; `--next`, `--finding`, `--blocker`, `--constraint`, `--failed "action => outcome"` and `--share-with` repeat. A section left out keeps what the last save recorded, as in the tool; `--json` prints the structured result.
- **From pytest:** `pytest -p memoryking.pytest_plugin` with those environment variables set records each failing test's id and report tail under source `pytest`. Without the variables it stays silent, and a capture error never fails the run.
- **From git:** a `post-commit` hook that runs the capture command above records each commit as an observation.
- **From Claude Code:** a `PostToolUse` hook on `Bash` that pipes the tool's output into `memoryking capture --source claude-code` when the command was a test or build. Codex and other hosts with shell hooks use the same command; hosts without hooks rely on the model calling `remember`.
- **At the session's edges:** the three host hooks below load memory when a session starts and save it before compaction and at the end, with no model involvement.

Hook captures record the observation time to the day unless `--observed-ms` is given, so a rerun sends identical bytes and replays; the store's own event log keeps the exact time of each capture. Hook captures are evidence like any other: a briefing cites them with their source, a reviewer can promote or quarantine what they contain, and the owner can ask where any of them went with `memoryking influence OBSERVATION --project P` or the dashboard's **Where did this go** view.

## Drafts that never accept themselves

Every other memory system lets a model extract facts on write. MemoryKing keeps its review layer and adds a drafting pass instead: `memoryking draft-proposals --project demo` (or the `knowledge` action `draft`, or the **Draft proposals** button in the dashboard's Review view) looks at the newest 512 visible observations for a name that several independent observations share, an anchor first (a path, ticket, host, symbol or error code) and failing that a rare term, takes the most recent sentence that carries it, and files an ordinary proposal with one evidence span per supporting observation. Two captures of the same event are one occurrence, not repetition. The draft is a `knowledge` claim in state `proposed`, its provenance names the method, topic and support, it carries the drafting principal, and it waits in the review queue like any proposal: nothing it writes becomes a constraint, a guide or accepted knowledge until a reviewer accepts it. A repeated run with the same evidence files nothing; new evidence on a known topic files a new revision on the same family, which a reviewer sees as a superseding proposal. Drafting needs the capture capability and is replay-safe under its retry key. It is deterministic and uses no model; for drafts a model writes, see [Consolidation](#consolidation-a-model-drafts-you-review) below. The same command also looks for procedures: a failed attempt or failure that recurs in at least two separate tasks (same normalised text or same anchors), or a rework step that recurs in successful outcomes, becomes a `repeated_outcome_v1` procedure proposal that cites the checkpoints and outcome records and goes through the ordinary procedure review and, separately, activation; `--no-procedures` skips it. An avoid procedure is filed only when a successful outcome in the same tasks supplies the success evidence a procedure must carry.

## Consolidation: a model drafts, you review

Consolidation lets a model read recent observations and propose claims, under rules that keep it from ever deciding anything. It is off until an operator sets it up, because it sends observation text to a provider.

**Enabling a provider.** Write an [egress policy](deployment.md#egress-policy) naming the provider, its models, its host and the highest classification it may receive, and start the daemon with `serve --egress-policy FILE`. The credential used must hold `capture` and `inference`.

**Running it.** `memoryking consolidate --project demo --egress-policy FILE --provider openai-chat --model grok-4 [--since 7d] [--limit 24]` makes one pass: it prints how many claims were filed, which were dropped and why (`quote_not_verbatim`, `unknown_observation`, `invalid_kind`, `duplicate`, `previously_rejected`, ...), and the proposal ids. Nothing is accepted; review them with `memoryking review list` and read the quotes under each. Without an egress policy the command refuses, and `--provider local-recording` runs the flow without a model and files nothing. A pass remembers where it stopped (the consolidation frontier), so the next one starts with what is new; a pass whose reply cannot be used leaves the frontier where it was. Observations above the provider's classification ceiling are skipped, observations from an untrusted source never enter, and each pass sends one request, which `memoryking egress-audit` lists.

**On a schedule.** `memoryking consolidate --watch 6h --idle 15m ...` keeps running in your own process, and `serve --consolidate-every 6h --consolidate-project demo --consolidate-provider NAME --consolidate-model M --egress-policy FILE` has the daemon launch and supervise a separate `consolidate` process for each project (restarted with a growing delay if it fails, stopped when the daemon stops, listed in `memoryking status`), so the daemon itself never reads the provider's key file. Both run a pass only when the project has recorded nothing for the idle time (default 15 minutes). Neither is on by default.

**Showing drafts to agents (optional).** `memoryking unreviewed on --project demo` (an admin decision, off by default) lets query briefings include up to three drafted claims (from consolidation, `propose`, or repeated evidence) in a section called "Unreviewed, drafted from evidence", after the accepted guidance, each marked `drafted_unreviewed` with its method, age and the sentences it cites. An agent should read them as leads, not rules: they are never under "Must follow", never counted as accepted, never shown for `freshness: attested`, and they disappear from the next briefing as soon as a reviewer rejects them, their evidence is purged or they are accepted (then they are ordinary guidance). `memoryking unreviewed show` and `off` read and revert the setting.

**Is it worth it.** `memoryking health --project demo` counts drafts by method (`repeated_evidence_v1`, `model_consolidation_v1`, `repeated_outcome_v1`): drafted, accepted, rejected, waiting, and the share accepted. A method whose drafts are mostly rejected is costing review time.

## Remembering things about the user

Some things are true of the person, not of the project: how they like work done, tools they want used or avoided, the language to reply in. Save those to the **user scope**, and every project their agents work in will hear about them. Use `remember` with `scope: "user"` (CLI: `memoryking capture --scope user`). Never put project facts, code or secrets there, because the scope is shared with all of that user's agents.

- Example: the user says "I prefer small commits, one concern each". Call `remember` with `text` "The user prefers small commits, one concern each", `source` `chat` and `scope` `user`. Later, in another repository, `recall` or the session start shows it, labelled "from your own user memory".
- Example: the build in this repository fails on Node 18. That is a project fact: call `remember` without a scope. The user scope is for what stays true when the repository changes.

Writing takes the `capture` capability in the user's scope, which an agent has when it acts for the user (`credential issue --for-user`). What you save is a note, not an order: it becomes a standing claim or constraint only when the user accepts a proposal made from it. Users see, export and erase their own scope with `memoryking user show`, `user export --dir DIR` and `user purge --yes`. If a user rule and a project rule disagree, the briefing is blocked with an unresolved conflict and both are shown; decide with the user rather than picking one.

## Reviewing and correcting memory

**Review from the terminal.** `memoryking review list --project demo` shows the proposals waiting for a decision, oldest first, ten to a page (`--limit`, `--after`; `--kind constraint` lists proposed constraints), each with its id, text, how it was filed (`repeated_evidence_v1` for a draft, `agent_proposal_v1` for one an agent filed), how many observations support it and the sentences it cites, then the retirement requests waiting (below). `review show ID` prints one in full with the scope it would apply to, `review accept ID --reason "why"` accepts it, `review reject ID --reason "why"` rejects it, and `review accept ID --as constraint [--effect prohibit]` files and accepts a mandatory constraint with the same words and evidence and closes the draft (a constraint accepted this way has no machine rule beyond its text). Accepting confirms that you read the scope `show` prints. The reason is free text kept with the decision (the `note` field); the identifier `reason` stays a machine code. These commands send the existing `review` action with your own credential, so the same capabilities apply: `review` for knowledge, `policy` for a constraint. The dashboard's Review view shows the same queue.

**Review from chat.** The lean `propose` tool lets an agent file a claim with the observations it rests on; it is a proposal, never accepted knowledge. The lean `confirm` tool records the user's accept or reject of a proposal, with the user's own words. It is off unless the operator starts the bridge with `--allow-chat-review`, and it works only when the bridge's credential also holds the `review` capability. Turning it on means: the agent can record a decision the user gave in conversation, and the daemon cannot tell a truthful relay from a made-up one. So the decision is recorded as *confirmed in chat*, naming the principal that relayed it and the principal that proposed the claim, with the quoted answer, and a reviewer sees that in `review show` and can undo it with `deprecate`. Only knowledge can be confirmed this way; a constraint, a deprecation or a quarantine cannot. Leave it off when you do not trust the agent to report your answers.

**Replace and retire.** `remember` and the CLI's `capture --supersedes ID` (and the lean `correct`, which saves new text that replaces a memory) link the new record to an earlier one the caller can read, in the same project. The old record stays as evidence, untouched, but a briefing prefers the replacement: the old one no longer appears beside it, and appears alone only when the replacement is not visible to the reader (unreadable to them, held, or purged). The lean `forget` asks for a memory to be retired and cannot delete anything. Anyone who may capture and can read the record may ask. Until it is decided the record is marked "retirement requested" in other readers' briefings and hidden from the requester's own. A reviewer or admin sees the request in `review list` and the dashboard; a reviewer or admin may dismiss it (`review retire-dismiss ID --reason "why"`), and only an admin may approve it (`review retire-approve ID`), which purges the record with the usual dependency follow-through. A reviewer without the admin grant sees requests and dismisses them but cannot approve.

**Pins and the session start.** `memoryking pin add|remove|list --project demo --claim REVISION` (or `--observation ID`) keeps an accepted claim or an observation in front of every session; an admin or a reviewer decides, and the pin is a journaled policy decision. `memoryking recall --project demo --start [--task T] [--days N]` prints a briefing with no query: the mandatory constraints for the session's bindings, the pins, the task's latest checkpoint (objective, next actions, what not to repeat), and the most recent decisions (claims accepted in the last 14 days and the newest five observations whose source is `decision`, `instruction` or `user`, optionally followed by `-` or `_` and a suffix, such as `user-instruction`; the newest two show up to 2,400 bytes, the rest 800), how the newest hook-captured session ended (the last 1,600 bytes of an observation whose source is `<host>-session` or `<host>-compact`), and the newest three observations of the user's own scope when the agent acts for a user, all inside the token budget, with the same receipt, permissions and holds as any briefing, and a plain statement when nothing is recorded yet. It is also what the session-start hook prints when no `--query` is given. The lean `recall` tool still needs a query.

## Anchors: the exact names memory hangs on

At capture, MemoryKing extracts the exact names a developer's memory hangs on, with no model: file paths, code symbols (`RetryDeadline.compute`, `retry_deadline_ms`), ticket ids (`PAY-1234`, `#4521`), URLs and their hosts, hostnames, and error codes (`ECONNRESET`, `TimeoutError`, `HTTP 503`). They are stored beside the observation as anchors, at most 32 per record. A briefing whose query names an anchor finds every record that carries it exactly, even when the surrounding words differ, and cites it as an `anchor_match` with a full score; naming a thing outranks sharing a word with it. The memory graph lists each observation's anchors and draws a `shares_anchor` link between records that share a rare one (held by two to eight of the loaded records), which is a light knowledge graph without a graph database. Stores created before anchors existed gain them on the next `reindex`.

Other words are matched by form and weighed by rarity: "retry" finds "retries" and "retried", a word that appears in few records counts for more than one that appears in most, and a short note beats a long one that mentions the word in passing. A long observation is read in passages of about a thousand characters: the briefing quotes the passage that holds your words (not the start of the text) with its exact byte range, which `fetch` returns, and a long capture (more than about six passages) that holds the answer is ranked by that passage when it beats the whole. Write queries as the distinctive words you would expect in the note, not a sentence: common function words ("the", "of", "which") are ignored, a rare word is worth several common ones, and an exact name (a path, a ticket, a symbol) beats all of them. Words with digits or underscores (`retry_charge`, `PAY-142`) are matched exactly. When two records score equally the newer one comes first, so a correction is shown before the fact it replaced. A store upgraded from an earlier release keeps the previous ranking (`lexical-v1`) until `reindex` has rebuilt its lexical index; `status` and `doctor` say so.

A briefing shows each fact once. Notes that say the same thing (the same sentence captured twice, or five notes that differ only by a run number, with the same ticket ids, paths and symbols) come back as one item, the newest record's text, with `also_recorded_in` naming the others; several versions of one memory file come back as the newest with `supersedes` naming the older ones; and an accepted claim is not repeated by the observations it cites unless they add text (`evidence_not_repeated`). The receipt still depends on every record, so purging any of them retires the briefing. Notes about different tickets, files or symbols, and notes that differ in a negation or in any word that is not a number, are never merged.

## Salience: memories that strengthen with use

On request (`salience: "on"` on `brief`, `--salience` on `recall`, `salience: true` on the lean `recall` tool), ranking adds a small, bounded nudge from use, computed from what the store already keeps. It is off unless asked, because a briefing's order must not depend on what earlier briefings delivered unless that is what you want, and an operator's frozen ranking policy, when one is active, supersedes it. A record delivered to you in a briefing, fetch or rerun during the last ninety days gains strength that fades by half every thirty days; a record you marked `helpful` gains more; a record nobody has been delivered and that is older than 180 days drops to an **archival** tier. Strength is per principal, because receipts and feedback are private and another principal's opinion must never reorder your briefing; the archival tier looks only at whether anyone used the record at all that is still searchable and still cited when nothing else matches, but ranked after everything active. The nudge is at most a tenth of a typical full lexical match, every briefed item carries its `salience` (strength, tier, deliveries, helpful votes), the receipt's trace records the stage with its constants, and an as-of or historical briefing disables it, because later use must not reorder an earlier view. Nothing is deleted or rewritten; looking at the graph never counts as use.

## Reranking the shortlist with a local cross-encoder

Every stage above is deterministic and needs no model. The one place a model may touch the order is a reranker you install yourself: a small cross-encoder that reads the query against each of the top 16 candidates and reorders that window by its relevance score. It is off unless a briefing asks (`rerank: "on"` on `brief`, `--rerank` on `recall`, `rerank: true` on the lean `recall` tool), it runs on the daemon's CPU with no egress, and it only moves records that lexical search, anchors, dense fusion and salience already admitted: it cannot add one, drop one, or change what a constraint or a conflict says. Each moved item reports its `rerank_score_ppm`, the receipt's trace says whether the stage was applied and why not otherwise, and the model's fingerprint is recorded in the diagnostics so a replay names what ordered it. Install it with `python -m memoryking.model_install --model reranker --destination DIR` and start the daemon with `serve --reranker-model DIR`; without a model the request is answered honestly (`unavailable_no_model`) and the briefing stays as it was. On the pooled LongMemEval oracle set the reranker's effect on session recall, answer-turn recall and MRR, and its cost in latency, are recorded in the [reference](reference.md#local-reranker) and in `tools/evidence_recall.py --reranker-model DIR`, so you can decide whether it earns its place on your own corpus.

## Measuring recall without a judge

`tools/evidence_recall.py --data longmemeval_oracle.json --output report.json` records LongMemEval's sessions at their own dates, asks each question at its date through a real briefing, and scores whether the cited observations come from the labelled evidence sessions and answer turns. No answer is generated or judged, so the figure measures the memory layer alone and is not comparable to answer-accuracy leaderboards. Download the data separately (it is not committed); the check runs the same harness on a committed 72-question sample as a gate. The tool also reports recall at 5 and 10, with `--no-pool` for the per-question setting other systems publish, and compares a change with an earlier report (`--compare`); the [reference](reference.md#retrieval-quality-gate) gives the definitions and the measured figures, and also covers the stale-versus-current and scale benchmarks.

## Briefings as of a moment

Every claim carries the time it became valid and the position at which it was recorded, and every observation its recorded position, so a briefing can be asked for the project as it stood at an instant. Pass `as_of_ms` to `brief`: the service resolves it to the history frontier at that wall time and judges claim validity at that time, the packet reports `as_of_ms`, `recorded_position` and `valid_ms`, and the receipt replays exactly. Nothing recorded after that instant appears, and a claim retired since then is still in force. The CLI takes `memoryking recall --as-of 2026-10-20` (a date means the end of that day), the lean `recall` tool takes `as_of`, and the dashboard's briefing form has an **As of** field. Ranking adjustments learned since then are disabled for an as-of briefing, and procedures are withheld, because both would import later knowledge into an earlier view. Passing `as_of_ms` together with an explicit `recorded_position` or `valid_ms` is refused.

Questions that carry time work without any argument: "what did we decide last week", "the failure yesterday", "before the March release" or "in 2025" are understood (English only, UTC; see the reference), the date words are not searched as words, and records from that time rank higher. To restrict a search to a period instead, use `memoryking recall --from 2026-03-01 --to 2026-03-31` (or `time_range` on `brief`, or `from` and `to` on the lean `recall` tool); a bare date means the start of the day for `--from` and the end of it for `--to`.

## Agents that only run shell commands

An agent with a shell but no MCP tools can use the `memoryking` command (`capture`, `recall`, `save-progress`, `resume`, with the connection details in the environment). Tell it in a file it reads (a `MEMORY.md` or a section of its instruction file; `benchmarks/outcomes` has a worked one) three things that matter more than the syntax. Save as soon as you have it: an instruction or a decision from the user, a fact you were told, an approach that failed and why, using their exact words, before carrying on, because a session can end at any point and what was not saved is lost. Do not recall from an empty store: when the briefing says nothing is recorded, that is the answer. Recall when a session continues earlier work, with the words the user would have used. In the first task-outcome run the only memory-arm failure of this kind came from an agent that looked at an empty store, did the work and never saved what it had been told. Tell it also how to save text that a shell mangles: a host's command check can refuse a quoted `--text "..."` containing parentheses, and a heredoc containing braces and quotes (seen with Claude Code 2.1.330 in the second run, where six saves were refused and one run lost its only save). `capture --file FILE` after writing the words to FILE with the file tool was accepted by that host; `capture` with the text on stdin (or `--file -`) is for hosts whose check allows a pipe.

## Hooks that load and save memory by themselves

Claude Code, Codex and Grok all run command hooks at the same three moments, and MemoryKing ships one command for each. Install them once and every session on that host starts briefed and ends saved, whatever model is driving. `memoryking setup` does this under the same agent credential as the MCP bridge and writes your user-level host file, not the project's shared one. By hand:

```sh
.venv/bin/python -m memoryking hooks install --host claude --scope user --project demo --socket "$HOME/.memoryking-data/service.sock" --credential "$HOME/.memoryking-control/agent.credential"
```

| Moment | Host event | What `memoryking hook …` does |
| --- | --- | --- |
| Session start, resume, or right after compaction | `SessionStart` | `session-start` prints, first, a short note that names the memory tools in the host's vocabulary (`mcp__memoryking__remember` in Claude Code; added when the command has `--tools mcp`, which `setup` writes) and says to save as soon as there is something to save and that an empty briefing needs no recall, then the hooks' checkpoint for `--task` (when one exists) and a cited Markdown briefing: with `--query` the agent's own latest checkpoint first, then a briefing for that query; without one, the query-free session start (constraints, pins, the task's last checkpoint, recent decisions). Claude Code and Codex add the output to the model's context. |
| Before the transcript is compacted | `PreCompact` | `pre-compact` captures the tail of the transcript (user turns, assistant text and tool errors from the JSONL, bounded to 16 KiB) plus any compaction instructions as an observation under source `<host>-compact`, the user's own turns from that tail (verbatim, secret shapes replaced, at most 8,000 characters, newest kept) as a second observation under source `user-<host>` so the next session's start briefing shows an instruction or a decision whole, and with `--task` saves a minimal checkpoint that references it. The checkpoint holds the newest 2,000 characters of the tail and goes to its own stream, `<task>--mkhook`, so it never replaces a checkpoint the agent saved on `<task>`; each hook has its own retry key, so pre-compact followed by session-end both succeed. |
| Session end | `SessionEnd` | `session-end` captures the host's last assistant message when it is given, else the transcript tail, under source `<host>-session`, and the user's turns under `user-<host>` as above. System reminders a host marks as its own (`isMeta`) are not user turns. |

The installer merges into the host's own file and leaves every other hook alone: `.claude/settings.json` for Claude Code, `.codex/hooks.json` for Codex and `.grok/hooks/memoryking.json` for Grok, under the project directory or, with `--scope user`, under your home directory. `--print` shows the merged file without writing it and `--remove` takes MemoryKing's entries out again. Commands are written with absolute paths and the profile (or `--socket` and `--credential`) spelled out, because hosts run hooks without your shell's environment; a profile's `bindings` therefore reach the hooks, while `MEMORYKING_BINDINGS` does only if the host passes its environment through (otherwise install with `--binding environment=prod`, repeatable, which writes the binding into each installed command so the hooks resolve constraints as the MCP bridge does; flags win over both). A hook acts as the credential it is given. With only `--profile NAME` that is the profile's credential, which is the owner's unless the profile names another, and the owner is a different principal from the agent the MCP bridge runs as: the hooks' saves are then refused (`checkpoint_writer_denied`, on stderr only) and the session start cannot see the agent's checkpoint. Give the hooks the bridge's credential, as `setup` does. Running the installer twice with the same arguments is a no-op, and changing the bindings rewrites MemoryKing's entries in place.

What the hooks read and scrub. Tool-call inputs (a file body passed to a write tool, a command) and successful tool results are never read; failed tool results are. Before anything is stored, the hooks replace common secret shapes with `[REDACTED]` and add a line with the count to the capture: private-key blocks, `sk-` and `rk-` keys, Stripe-style `sk_live_` keys, AWS access key ids, GitHub, Slack and Google API tokens, JWTs, bearer tokens, and the value in `password=`, `secret:`, `api_key=`, `access_key=` or `*_token=` assignments. This is a safety net, not a guarantee: a secret in another shape, split across lines, encoded, or written in prose is stored as written, and a harmless value can be replaced. Keep secrets out of transcripts, and use `trust` rules to hold sources you do not trust. Everything is deterministic; no model is called.

Hook captures use a deterministic event id from the host, the session and the text, and the hook checkpoint uses a stable retry key, so a hook that fires twice replays rather than duplicating; a later compaction with new text is a new observation. A hook never fails the host: errors go to stderr and the exit code stays zero (`--strict` for debugging). The hook reads the host's event JSON from stdin in either spelling (`hook_event_name` for Claude Code and Codex, `hookEventName` and the `GROK_*` variables for Grok).

**Headless sessions** (`claude -p`). Checked against Claude Code 2.1.330's own configuration schema and bundled code, not by running a model: settings passed with `--settings FILE` are a source that stays on when `--setting-sources ""` turns user, project and local settings off, so the installed hooks run in a headless session whose own settings, memory and project files are otherwise out of the way; a `SessionStart` hook's plain output is added to the model's context; `SessionEnd` hooks run when the session shuts down, within the longest timeout configured for them (the installer's 10 seconds; the host's default is 1.5 seconds). `--include-hook-events` with `--output-format stream-json` records each hook's start and result in the stream, which is how to see that they ran. `--bare` skips hooks altogether. Hooks are shell commands run by the host, so tool permission rules do not apply to them. What a hook captured is what the transcript file holds: a session cut off by a kill or a timeout may leave no `SessionEnd` capture, and `PreCompact` is the only save before a long session loses its start.

Two host differences to know. Grok ignores hook stdout, so on Grok the session-start briefing is not injected; the save hooks still work, and Grok also reads the Claude Code settings file, so one install serves both. Claude Code gives `SessionEnd` hooks about a second and a half by default; the installer sets a 10-second timeout for that hook, which Claude Code honours, and the capture is one local socket call.

What the hooks save is evidence, not a summary: a reviewer sees the transcript tail with its source, and the checkpoint lists its defaulted sections as gaps so a resume says what was never written. `tools/hooks_demo.py` runs all three against a disposable service and shows the installer's output for each host.

## Source trust: holding captures you do not yet trust

Everything captured is evidence, but not every source deserves to reach a briefing before a person has looked. An admin can mark a source untrusted for one project or for every project (`*`), by exact name or glob:

```sh
.venv/bin/python -m memoryking --profile home trust set --project demo --source "scraper-*" --level untrusted
```

A capture from a matching source is recorded exactly like any other (same event id, same replay, same backup) but is **held**: invisible to every read, briefing, graph page, resume and claim proposal, and absent from the influence of anything built afterwards, until an admin releases it. `memoryking trust held --project demo` lists what is waiting with a 256-byte preview; `memoryking trust release OBSERVATION --project demo` releases one; `memoryking trust list` shows the rules. The dashboard's **Review & conflicts** view shows the same queue with a Release button and a form for rules. Rules and releases are journaled policy decisions: a restore replays them, and the policy epoch retires any view built before the decision. The most specific rule wins: an exact source beats a pattern, and a project rule beats a `*` rule, so one untrusted host can be allowed back for one project without touching the rest. Purge works on a held capture exactly as on any other.

This is the quarantine lifecycle claims already had, applied one layer down: a poisoned capture from a feed, a scraper or a host hook on a machine you do not control never reaches a model unreviewed, and if it did reach one, the influence report says where.

## Is memory paying for itself: the health report

Request and latency metrics say the service is up; they do not say the memory is any good. `memoryking health --project demo` (or the **Memory health** section of the dashboard's Jobs & health view) answers from records the store already keeps, with counts only: how much of the feedback agents gave was helpful, how many briefings found no relevant memory, how much of what was captured ever reached a delivery, how many claims lost their evidence to a purge or were replaced, and how long the oldest proposal has waited for review. Five signals say what the numbers suggest; each `watch` or `attention` line names the next move, such as reviewing the backlog, capturing what agents keep asking about, or re-proposing a claim from evidence that remains. Ratios stay `unknown` until there are enough votes or briefings to mean anything, and the thresholds are fixed and documented in the [reference](reference.md#memory-health), so a change in the report is a change in the project, not in the judge. It needs an admin credential because it aggregates every principal's feedback and deliveries, but it names none of them.

## Where this left off: session summaries

A resume packet is exact but long. Every resume now begins with a three-sentence summary when one exists: where the task stands, what was learned or tried, and what comes next. Summaries are derived artifacts with their own receipt, stored beside the checkpoint they summarize; the checkpoint remains the record and the summary never adds facts to it.

Three things produce them. A hand-off derives the sender's summary and returns it in the `prepare` result, so the recipient reads three sentences before the packet. The daemon derives one for any task whose latest checkpoint has been idle for `--summary-idle` (30 minutes by default) when the owner may read that checkpoint, so the dashboard's Task activity shows where each task left off. And a host can ask for one with the working-state action `summarize` or `memoryking summarize --project P --task T`.

The default method is `extractive-v1`: a deterministic paragraph built from the checkpoint body plus counts of recent observations and open operations, quoting nothing but the checkpoint, so it is safe to show to every reader of the checkpoint. With an [egress policy](deployment.md#egress-policy) loaded, `memoryking summarize --provider NAME --model M --egress-policy FILE` has a model write it instead: the resume packet travels through `AuthorizedConversation` as audited memory, and the daemon admits the reply as a summary only if it is byte for byte the delivered reply recorded in the caller's egress audit for that permit. A model-written summary therefore cannot be edited on the way in, and its sources are the receipts the audit cites.

## What the host must do

1. Supply accurate project/task identity and applicability bindings (for the lean tools and hooks, once per session; see session bindings).
2. Save useful observations and checkpoints; MemoryKing does not scrape the conversation automatically.
3. Inspect briefing/resume status. `blocked`, `budget_insufficient`, `incomplete` and unavailable results require a decision, not silent truncation or permission bypass.
4. Treat source text and proposed lessons as untrusted evidence.
5. Reconcile uncertain external operations before repeating them. A resume packet lists every unresolved operation and the newest resolved ones; `operations_truncated: true` means older resolved operations were left out, which never hides an unresolved one.
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
