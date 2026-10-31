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

Eight tools are exposed: `memory_status`, `memory_capture`, `memory_brief`, `memory_fetch`, `memory_checkpoint`, `memory_resume`, `memory_handoff` and `memory_feedback`. Their exact input schemas are in the [tool catalog](../src/memoryking/adapters/stdio.tools.json); each description says when to call the tool, what comes back and the one mistake to avoid. Feedback accepts outcome/feedback actions; it cannot activate a procedure. Administration, policy changes, source registration and model configuration are deliberately outside this tool surface.

The bridge fills what the session already knows, so a minimal call works: `version`, `incarnation`, `project`, `bindings` (`[{}]`), `max_output_tokens` (6,000) and `tokenizer` (the exact tokenizer when installed, otherwise the disclosed byte estimate) default when omitted, and `memory_capture` fills `request.version` and `request.project`. Explicit values always win, and the service validates the completed command exactly as before, so a stale explicit `incarnation` is still rejected as `history_changed`.

The shared profile also exposes authenticated Streamable HTTP MCP at `/v1/tenants/{tenant}/projects/{project}/mcp`. Configure the tenant/project URL and bearer credential according to your host's supported transport; see [deployment](deployment.md).

## What the host must do

1. Supply accurate project/task identity and applicability bindings.
2. Save useful observations and checkpoints; MemoryKing does not scrape the conversation automatically.
3. Inspect briefing/resume status. `blocked`, `budget_insufficient`, `incomplete` and unavailable results require a decision, not silent truncation or permission bypass.
4. Treat source text and proposed lessons as untrusted evidence.
5. Reconcile uncertain external operations before repeating them.
6. Recheck authorization before sending retained context to a model.

For the last step, use `AuthorizedConversation` from `memoryking.harness_context`. It binds retained content and derived messages to authorization, checks current status before dispatch and rebuilds or blocks when dependencies become unavailable. Retrieved memory enters the conversation as user-role content whose bytes the store verifies against the receipt, never as a system instruction. A dispatch carries up to 128 messages within a 96 KiB request. Ordinary capture/retrieval calls cannot remove restricted text from an unrelated host's old chat transcript. Provider requests already delivered cannot be recalled.

The packaged reference agent and inference examples use a local recording provider, and `AuthorizedConversation` accepts only that provider today: forwarding to a commercial model needs a reviewed adapter with an explicit egress policy, which is not included. There is no production commercial-model connector or autonomous scheduling service included.

## Recipes

These commands run disposable examples in the [contributor environment](../CONTRIBUTING.md#setup):

| Command | Demonstrates |
| --- | --- |
| `python tools/knowledge_demo.py` | Capture → propose → review → quarantine. |
| `python tools/briefing_demo.py` | Briefing, bounded evidence fetch, exact replay and explicit rerun. |
| `python tools/working_demo.py` | Checkpoint/resume and uncertain-operation reconciliation. |
| `python tools/stdio_demo.py` | Python client and actual MCP stdio subprocess. |
| `python tools/inference_demo.py` | Revoked content excluded from subsequent local provider requests. |
| `python tools/repository_demo.py` | Repository snapshots and guidance freshness. |
| `python tools/experience_demo.py` | Reviewed procedures, outcomes and bounded ranking. |

Use `.venv/bin/python` in place of `python` unless the environment is activated. The examples make no paid provider calls.

[Back to README](../README.md) · [Design](design.md) · [API reference](reference.md)
