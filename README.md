<p align="center"><img src="src/memoryking/adapters/web/elephant.png" width="180" alt="MemoryKing" /></p>

# Give your AI agents a memory they can pick up again

MemoryKing keeps the useful context behind an agent's work: what it learned, what it tried, what must not be forgotten, and what should happen next.

Think of it as a **shared project notebook with a save point**. When a conversation ends, an agent restarts, or another agent takes over, the work can continue from a recorded state instead of starting with a blank page.

It is a service that you connect to your agents. It does not automatically read your chats or run agents for you.

## Why it is useful

Imagine an agent investigating a checkout bug. It discovers that a retry can charge a customer twice, tries a fix that fails, and runs out of context before finishing.

With MemoryKing, it can leave the next agent:

- **The goal:** fix the retry without creating duplicate charges.
- **The evidence:** the relevant error and test output, with their sources.
- **The failed approach:** what was tried and why not to repeat it.
- **The unfinished work:** the patch state, missing tests and next action.
- **The boundaries:** use the sandbox and keep production changes subject to approval.

The next agent receives a focused briefing and the saved task state. A reviewer can see the evidence and what is still unverified. This is an illustrative workflow; your integration supplies the observations and checks.

```mermaid
flowchart LR
    A[Agent does work] --> B[Save findings and task state]
    B --> C[MemoryKing checks access and relevance]
    C --> D[Next session gets a focused briefing]
    D --> E[Agent continues or hands off]
    E --> B
```

## What it does for agents

| Need | What MemoryKing provides |
| --- | --- |
| Remember useful findings | Searchable observations with source attribution; reviewed knowledge stays distinct from unverified notes. |
| Follow a plan | Named plans with phases, dependencies, completed steps and blockers; the same progress is available on desktop and phone. |
| Resume unfinished work | Checkpoints containing the objective, constraints, failed attempts, verification gaps and next actions. |
| Work together | Handoffs bound to the recipient's current permissions. |
| Keep context manageable | Focused briefings with explicit output budgets and links back to evidence. |
| Avoid stale advice | Repository identity, applicability rules and freshness checks for guidance tied to code. |
| Recover safely | Records of uncertain operations and history-aware backup/restore, so a restart does not silently authorize repeating an action. |
| Understand what happened | A dashboard with task progress, evidence, history, a force-directed memory graph and retrieval receipts. |

The core runs locally **without a model account, GPU or paid API**. A shared PostgreSQL profile supports multiple agents and tenants. Optional local embeddings add semantic search.

## See it work

You need Python **3.12** and Git. Repository access is currently private. From a terminal:

```sh
git clone https://github.com/jamesking-us/memoryking.git
cd memoryking
python3.12 -m venv .venv
.venv/bin/python -I -m pip install --require-hashes -r requirements-build.lock -r requirements-core.lock
.venv/bin/python -I -m pip install --no-build-isolation --no-deps .
.venv/bin/python examples/remember_and_resume.py
```

The example creates a temporary store, saves a finding and an unfinished task, closes the first connection, and resumes through a new client. It prints a readable walkthrough, makes no model requests, and removes its temporary data when finished.

For a lasting installation and the browser dashboard, follow the [getting started guide](docs/getting-started.md). If you are evaluating the idea rather than installing software, start with the [agent workflows](docs/agent-guide.md).

## See what your agents are doing

![MemoryKing dashboard showing a plan at 50 percent, its expanded design phase, and a task with pending verification](docs/images/dashboard.png)

*Dashboard with a synthetic plan and task. Progress reflects saved agent reports and evidence.*

The dashboard helps you inspect recorded work without reading a long chat transcript:

- **Plans:** see how much work is complete, which phase needs attention and what remains. Open a phase for steps, notes, dependencies and evidence. Skipped work is shown separately.
- **Task activity:** saved goals, completed and pending criteria, blockers and observed heartbeats.
- **Memory:** an interactive graph and list of accessible memories and evidence. The active view refreshes automatically while you read, preserving access checks and your graph selection.
- **History and review:** inspect changes, review proposals and preview affected content.
- **Retrieval history:** see the exact briefing returned, its sources and recorded usage.
- **Appearance:** textured dark and light themes, a crowned cybernetic elephant, and an optional depth-tilt animation that respects reduced motion.

Agents must publish their state through the supported interfaces. A displayed heartbeat is an observation, not proof that an agent is still working; missing cost data stays unknown.

### Check a project from your phone

<img src="docs/images/mobile-plans.png" width="360" alt="MemoryKing phone plan showing one of two steps completed and phase progress" />
<img src="docs/images/mobile-dashboard.png" width="360" alt="MemoryKing phone viewer showing a synthetic blocked task and distinct criterion states" />
<img src="docs/images/mobile-knowledge-graph.png" width="360" alt="MemoryKing phone knowledge graph showing memories, an accepted claim and recorded links to task criteria" />

*Phone-sized browser views with synthetic data; full physical Android qualification, including plan navigation and graph controls, is pending.*

A plan can say “inspect the retry logic, then verify the fix.” When an agent records the inspection and its evidence, the plan shows **1 of 2 steps complete**. The remaining verification stays visible for the next agent—and for you. Run [the plan example](tools/plans_demo.py) or read the [agent guide](docs/agent-guide.md#structured-plans).

The optional phone viewer has four tabs: **Overview, Tasks, Memory and Settings**.
Open **Overview → Plans** or **Tasks → Plans** to browse the project’s plans and phase progress. Select a phase to see its steps; **Back to plan** returns directly from evidence.
For example, open a blocked task, inspect the evidence behind a completed check,
then read its latest report. Reports include the full narrative, decisions, code,
tables and figures. In **Memory → Knowledge graph**, explore shared memories,
accepted claims and the evidence connecting them to tasks. Tap a node to inspect
its connections or open the source; drag to pan, pinch to zoom, or use the buttons
and searchable node list. **Back to graph** returns from details to your selected
node, zoom and layout. The graph shows a bounded sample of recorded links.

Sign in with GitHub on an operator-configured installation. Your operator chooses
which projects and sources you can read; repository membership alone does not
share notes. The viewer is read-only. It clears private content when backgrounded,
disconnected or signed out and checks access again before showing it.

The interface has local browser coverage at phone-sized layouts, and the home
containers have been exercised locally with both databases. Real GitHub,
physical Android installation and the private home hosting setup are still being
qualified. The existing local dashboard remains the trusted owner's interface.

To try the permission model safely, run `.venv/bin/python examples/viewer_identity.py`.
Run `.venv/bin/python tools/viewer_demo.py` to check the optional listener's public
shell, disabled mode and protected reads. Both examples use temporary stores and
no external account. See [listener configuration](docs/deployment.md#restricted-viewer-listener)
, [GitHub setup](docs/deployment.md#github-sign-in-setup), and
[viewer access](docs/deployment.md#viewer-access-foundation) for the owner operation
and upgrade/recovery requirements. Experimental reporting can now store a complete
report, build PDF and EPUB copies locally, record visual-review evidence and make
the reviewed version readable through the SDK. Each reader must still have access
to every supporting source. An optional worker builds queued reports and can submit reviewed EPUBs through
a configured Gmail destination. Operators can preserve verified receipts when
replacing an older sender, without resending historical reports. Read published
reports from Overview or a task's Reports button. App availability, Gmail submission
and Kindle device arrival are shown separately. See [phone usage](docs/getting-started.md#use-the-phone-viewer)
and [reporting](docs/getting-started.md#experimental-reporting).

## Choose your setup

| Setup | Suitable for | What runs |
| --- | --- | --- |
| Local | One person or agents on one computer | Python service, SQLite, private local files; optional browser dashboard. |
| Shared | Multiple agents using a central service | PostgreSQL, authenticated HTTP/MCP, separate database roles and recovery storage. |
| Docker evaluation | Trying the shared profile on one machine | A source-built Compose stack with loopback HTTPS. |
| Private home preview | Preparing a phone viewer on your own Mac | One app container and a private-network sidecar; SQLite by default, with no published host ports. Account/device qualification is still pending. |

See [deployment and configuration](docs/deployment.md) for commands, prerequisites, backup and recovery.
The [home preview](docs/deployment.md#private-home-preview) starts with sign-in and
report delivery disabled, so installation alone does not share your memory or send email.

## Read the documentation

| Guide | What you will learn |
| --- | --- |
| [Getting started](docs/getting-started.md) | Install, run the demo, create a store and open the dashboard. |
| [Using MemoryKing with agents](docs/agent-guide.md) | Capture → brief → checkpoint → resume → handoff, Python and MCP integration. |
| [Deployment and configuration](docs/deployment.md) | Local/shared/Docker setups, credentials, configuration, upgrades and recovery. |
| [How it is designed](docs/design.md) | Storage, knowledge review, retrieval, permissions and recovery guarantees. |
| [Feature and API reference](docs/reference.md) | Interfaces, optional capabilities, limits, migration and troubleshooting. |
| [Contributing](CONTRIBUTING.md) | Build, test and maintain the project locally. |

## Current status

**Early preview for local evaluation; not yet production-qualified.** Core behavior has automated coverage, but measured workload targets have not been met. Improved real-task success or reduced token cost has not yet been demonstrated. Windows, complete browser/device coverage, automatic failover and production recovery targets are not qualified. Third-party agent hosts need integration testing.

MemoryKing helps preserve and inspect context; it does not make an agent's conclusions automatically correct. Use reviewed evidence and appropriate human approval for consequential work. See the [security model](SECURITY.md) and [current limits](docs/reference.md#current-limits).

No public package or container image is published. A project distribution license has not yet been selected; see [distribution status](CONTRIBUTING.md#distribution).
