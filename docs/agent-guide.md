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

For capture, keep a stable key and stable source-event identity for the same event. Changed bytes with the same identity produce a conflict. Save the exact request before dispatch; do not create a new identity merely to make an uncertain retry succeed. After a history change, reconcile first.

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

Eight tools are exposed: `memory_status`, `memory_capture`, `memory_brief`, `memory_fetch`, `memory_checkpoint`, `memory_resume`, `memory_handoff` and `memory_feedback`. Their exact input schemas are in the [tool catalog](../src/memoryking/adapters/stdio.tools.json). Feedback accepts outcome/feedback actions; it cannot activate a procedure. Administration, policy changes, source registration and model configuration are deliberately outside this tool surface.

The shared profile also exposes authenticated Streamable HTTP MCP at `/v1/tenants/{tenant}/projects/{project}/mcp`. Configure the tenant/project URL and bearer credential according to your host's supported transport; see [deployment](deployment.md).

## What the host must do

1. Supply accurate project/task identity and applicability bindings.
2. Save useful observations and checkpoints; MemoryKing does not scrape the conversation automatically.
3. Inspect briefing/resume status. `blocked`, `budget_insufficient`, `incomplete` and unavailable results require a decision, not silent truncation or permission bypass.
4. Treat source text and proposed lessons as untrusted evidence.
5. Reconcile uncertain external operations before repeating them.
6. Recheck authorization before sending retained context to a model.

For the last step, use `AuthorizedConversation` from `memoryking.harness_context`. It binds retained content and derived messages to authorization, checks current status before dispatch and rebuilds or blocks when dependencies become unavailable. Ordinary capture/retrieval calls cannot remove restricted text from an unrelated host's old chat transcript. Provider requests already delivered cannot be recalled.

The packaged reference agent and inference examples use a local recording provider. There is no production commercial-model connector or autonomous scheduling service included.

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

## Preparing phone viewers

The owner can bind a numeric GitHub identity and grant read-only project access
through the viewer policy operation. An agent cannot invite viewers or share
owner-only evidence just by writing a project name. Phone login and the mobile
interface are not available yet. Existing capture/checkpoint integrations continue
unchanged. See the [disposable permission example](../examples/viewer_identity.py)
and [owner setup](deployment.md#viewer-access-foundation).

[Back to README](../README.md) · [Design](design.md) · [API reference](reference.md)

## Structured plans

A plan records **what the agents intend to do and what they have actually reported doing**. It has a title, objective, optional context, ordered phases and stable step IDs. Each step has a description, status, notes, prerequisite IDs and evidence references. This complements the detailed checkpoint used to resume an individual agent. It does not dispatch agents or authorize builds, merges or external actions.

The normal flow is:

1. Create a plan with `plan_save`, `expected_revision: 0`, an explicit reader audience and a stable retry key.
2. Read the current write revision (`current_revision`) with `plan_get`. Mark a ready step `doing` using `plan_step` and that revision.
3. Capture the actual result as an observation, then mark the step `done` with its evidence reference. Use `blocked` plus a reason when work cannot proceed.
4. Revise the full plan with `plan_save` to add or reorder phases/steps. Preserve IDs. Resolve revision conflicts by reading and reconciling the current plan.
5. Close the plan explicitly as `done` or `abandoned`, with a closure note and a captured lesson. `done` requires a nonempty plan whose steps are done or explicitly skipped.

All four actions—`plan_save`, `plan_step`, `plan_get`, `plan_list`—are available through **`memory_plan`** in MCP and through `client.working(command)` in both Python clients. CLI users send the same command to the `working` operation with `memoryking request`. The [closed command schema](../contracts/working-v1.schema.json) is authoritative; [example plan](../examples/plan.json) contains a complete input object.

```sh
.venv/bin/python tools/plans_demo.py
```

This disposable example uses the real local service and model-tool contract. It creates two steps, captures inspection evidence, records one completed step, reads the original revision and lists the current plan. It makes no model calls or network changes. The same exercise is tested over the shared HTTP service with native PostgreSQL.

Plan commands always include `version: 1`, the current `incarnation`, `action` and `args.project`. Writes additionally include `key` and `args.expected_revision`. A plan uses a task-level stream, identified by `args.task`; use a separate ID from an ordinary agent checkpoint. `plan_step` requires `step`, `status`, `notes`, `evidence` and nullable `decision`. It changes only those fields. A full `plan_save` replaces the plan stream’s complete plan document; use ordinary task checkpoints in a separate stream. The `revision` field identifies the returned content snapshot, while `current_revision` is the CAS revision to use after an operator reassigns the writer. `plan_get` takes nullable `revision`; `null` selects current state. `plan_list` takes `limit` (1–50) and nullable `cursor`. Read responses include derived progress and effective status: they are **not** valid full-plan write bodies without removing projection-only fields such as `reported_status`.

Step statuses are `todo`, `doing`, `done`, `blocked`, `skipped` and `unknown`. `doing`/`done` require resolved prerequisites. `blocked` needs a note; `skipped` needs both a note and a scope decision. A skipped step does not count as completed work. Changing the objective, a completed step’s definition or its prerequisites requires fresh evidence or reopening the affected work. If a prerequisite changes, revise affected steps together in one full-plan write. Reusing an old proof after reopening cannot rehabilitate a changed definition. Reordering, duplicating or removing old evidence links is not fresh proof.

Progress uses equal step counts, rounded down to a whole percent: completed steps / current total. Empty plans have no percentage. The separate resolved count includes justified skipped work. Adding scope can lower progress. The declared plan lifecycle is separate from effective completion; inaccessible or unverifiable history cannot silently become a pass. Recorded evidence is an agent report, not independent certification.

## Resuming with a replacement agent

Before stopping, capture findings and failed attempts, then save a checkpoint with the exact repository revision, dirty files, outstanding tests, next actions and operation IDs. Update the structured plan with evidence-backed progress. Read the saved checkpoint back; a successful write alone does not establish that the next agent can read it.

Give the replacement agent the project/task IDs, repository locations and applicable instructions. Stop the previous writer first. The new agent uses its own authorized identity to resume the current checkpoint, read the plan and inspect Git state, running processes and durable job results. It must reconcile any newer local work and uncertain external operation before retrying. Never treat a missing chat response as proof that a build, write or delivery did not happen.

A checkpoint preserves recorded working state, not a running process or unrestricted access. It does not grant permission to another principal. Plan progress does not prove test success. Keep approvals, current source hashes and verification evidence separate, and use compare-and-swap revisions when saving the next checkpoint.
