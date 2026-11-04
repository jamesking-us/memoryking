# Getting started

This guide takes you from a new checkout to a local service and dashboard. No model account, Docker or Node installation is needed for this route.

## 1. Install

Use Python 3.12; the reference patch is recorded in [`.python-version`](../.python-version). The local service is exercised on macOS. Linux containers are exercised separately; Windows is not a supported local target yet.

```sh
python3.12 -m venv .venv
.venv/bin/python -I -m pip install --pre memoryking
.venv/bin/python -I -m pip check
git clone https://github.com/jamesking-us/memoryking.git && cd memoryking
```

This installs the released package from PyPI; `--pre` is needed while the current release is a preview (`0.1.0a3`). The checkout is for the examples and tools in this guide; the installed service itself does not need it. To pin the wheel by hash, use the `install` line from the release's `RELEASE.json` on the [release page](https://github.com/jamesking-us/memoryking/releases). To build from source instead, install the locks and the checkout as the [contributing guide](../CONTRIBUTING.md) describes, or use the [release wheel or container image](deployment.md#release-build).

## 2. Try the complete example

```sh
.venv/bin/python examples/remember_and_resume.py
```

The walkthrough uses a disposable store and two client connections. It saves an observed failure, retrieves an attributed briefing, saves an unfinished task and resumes it. It verifies that the failed attempt and next action survive. The example uses a disclosed byte-based token estimate, so the core installation needs no optional tokenizer.

This is real storage and client behavior with synthetic task content. It does not run an AI model or establish that a model will make better decisions.

## 3. Create your own store

Choose **new** directories. MemoryKing refuses to initialize over an existing store.

```sh
.venv/bin/python -m memoryking init \
  --store "$HOME/.memoryking-data" \
  --control "$HOME/.memoryking-control"
```

The output gives the path to an owner credential file. Keep it private. The data directory holds content; the separate control directory holds identity and recovery information. Neither directory may be inside the other. For disk-loss protection, retain control information independently of the content disk as well.

Name the store once so later commands need no paths:

```sh
.venv/bin/python -m memoryking profile add home \
  --store "$HOME/.memoryking-data" \
  --control "$HOME/.memoryking-control" \
  --dashboard-port 8771
```

Start the service:

```sh
.venv/bin/python -m memoryking --profile home serve
```

Without a profile, pass `--store`, `--control` and `--dashboard-port 8771` explicitly; explicit flags always override the profile.

Leave this terminal running. Ctrl-C stops the service cleanly. Restarting the same command uses the saved store. The socket is `$HOME/.memoryking-data/service.sock`; its full encoded path must fit within 100 bytes. The service completes its own bookkeeping jobs between requests and prunes expired records once an hour, so no separate worker or scheduled cleanup is needed. Add `--log-level info` to see redacted operational events on stderr, or `--trace` to log how long each request took, including time spent waiting for the store.

## 4. Check the service

In a second terminal, from the same checkout:

```sh
.venv/bin/python -m memoryking --profile home status
```

This prints the store's history id, position, frontier, job states and a capacity bar for every bounded resource. `--version` prints the installed package version. If anything looks wrong, `memoryking --profile home doctor` checks the installation and names the fix for each failed check.

## 5. Open the dashboard

From the same terminal, the quickest route signs you in directly:

```sh
.venv/bin/python -m memoryking --profile home dashboard open
```

When no browser can be opened from the terminal, request a code file instead:

```sh
.venv/bin/python -m memoryking dashboard-login \
  --socket "$HOME/.memoryking-data/service.sock" \
  --credential "$HOME/.memoryking-control/owner.credential" \
  --output "$HOME/.memoryking-control/browser-login-1.json"
```

Open the URL printed by the command. Read the private output file locally and enter its `code` in the page. Use the **one-use code**, never the owner credential. The code expires after 60 seconds; request a new file name for another attempt.

The dashboard starts empty. The overview shows capacity use for the tenant's bounded resources and warns at 80 percent. **Task activity** and **Retrieval history** list the projects your account can read, with their record counts; pick one and select **Load**. Each task card opens with "Where this left off", a three-sentence summary the service derives once a task has been idle for half an hour (or when an agent hands it off), followed by the saved state. The browser remembers your last choice. **Memory** shows the project's stored records; its **Ask for a briefing** view runs the same briefing an agent would receive, with its receipt, and can verify the replay, and its **Timeline** view lists every recorded event for the project, newest first. Its **Memory graph** colours each record by how much authority it has (an observation, an accepted claim, a proposal awaiting review, a quarantined claim) and draws the records your briefings used larger; select one and **Show receipts** lists those briefings and opens any of them in Retrieval history. On a phone the views sit in a bar across the top, the cards stack and the graph is shown as a list until you ask for the drawing. **Retrieval history** lists recorded briefings; open one and **Compare with previous briefing** shows which references were added, dropped or kept, and how status and token cost changed. **Team** shows, for one project, who did what in the last thirty days (captures, claims, checkpoints, hand-offs, summaries, provider dispatches), the grants that allow it, a **Revoke** button per principal, and **Export audit**, which downloads a JSON record of the project's events, dispatches, receipts, summaries and grants without any content. The overview lists your readable projects as cards; opening one follows it in every view, and the page address (`#project=NAME`) can be shared. Each view checks your current permissions. They do not discover conversations in other applications automatically.

To explore a populated synthetic dashboard, install the [contributor environment](../CONTRIBUTING.md#setup) and run:

```sh
.venv/bin/python tools/dashboard_demo.py
```

The demo prints its loopback URL and a path to its private bootstrap file. Enter that file's one-use code and select project `demo`. Ctrl-C stops the demo and removes its temporary data. Its sample results are illustrative, not real agent work.

## 6. Optional: semantic search

Lexical search works out of the box. For meaning-based recall, install the embeddings lock and the pinned model once, then tell the service where the model is:

```sh
.venv/bin/python -I -m pip install --require-hashes -r requirements-embeddings.lock
.venv/bin/python -m memoryking.model_install --destination "$HOME/.memoryking-model"
.venv/bin/python -m memoryking profile add home --store "$HOME/.memoryking-data" --control "$HOME/.memoryking-control" --dashboard-port 8771 --embedding-model "$HOME/.memoryking-model"
.venv/bin/python -m memoryking --profile home embeddings enable --project demo
```

Restart `serve` after adding the model to the profile. The same profile can schedule backups: add `--backup-dir "$HOME/.memoryking-backups" --backup-every 1d --backup-keep 14` to `profile add`, and `doctor` will tell you when the newest backup is stale. The service then indexes the project in the background and every briefing searches by meaning as well as by words; `doctor` reports both the runtime and the model. The model runs on your CPU and nothing leaves the machine.

## 7. Connect an agent

The [agent guide](agent-guide.md) covers the Python client and MCP stdio bridge. A typical integration saves observations during work, checkpoints before interruption, and requests a briefing and resume packet when work continues.

Agents should not use the owner credential. Issue each one a scoped credential:

```sh
.venv/bin/python -m memoryking --profile home credential issue agent-1 --project demo --capabilities capture,read --expires 30d --output "$HOME/.memoryking-control/agent-1.credential"
```

`credential list` shows what is issued and `credential revoke` withdraws one.

To follow a multi-step piece of work rather than one agent's checkpoint, have the agents keep a structured plan with the `memory_plan` tool described in the [agent guide](agent-guide.md#structured-plans); `.venv/bin/python tools/plans_demo.py` runs a disposable example. In the dashboard, choose the project under **Task activity** and open **Plans**: a phase shows its steps, evidence and progress.

For multiple machines or tenants, use the [shared deployment](deployment.md#shared-postgresql). Do not expose the local loopback dashboard through a network proxy.

## If something goes wrong

| Symptom | First check |
| --- | --- |
| Python version rejected | Use Python 3.12 and create a fresh virtual environment. |
| An unrelated Python package is imported | Run commands with `env -u PYTHONPATH -u PYTHONHOME` before `.venv/bin/python`. |
| Service refuses a directory | Check that data/control are disjoint, owner-private paths and no other daemon owns them. |
| Login code rejected | Obtain a fresh code in a new output file; use the printed origin exactly. |
| Dashboard has no tasks | Connect the correct project and ensure the agent has saved checkpoints. |
| Exact tokenizer unavailable | Install the optional tokenizer lock or explicitly use `utf8-byte-estimate-v1`. |

## Try viewer permissions safely

Run `.venv/bin/python examples/viewer_identity.py` to exercise explicit viewer
grants, private-note isolation and revocation in a temporary store. It uses local
owner IPC and an internally simulated authenticated subject. It does not perform
GitHub sign-in or start the phone application.

## Use the phone viewer

On an installation configured by your operator, open its private HTTPS address
with `/viewer/` at the end. Choose **Continue with GitHub**, finish the sign-in and
select a shared project. The operator must invite your numeric GitHub account and
share the project's underlying sources; owning a repository is not an invitation.
See [GitHub setup](deployment.md#github-sign-in-setup) for that operator procedure.

1. **Overview** shows saved tasks and blockers. Counts describe the tasks shown,
   not the whole organization's work or proof that an agent is currently running.
2. **Tasks** opens a checkpoint's criteria and permitted evidence. Passed, failed,
   not evaluated, waived and unknown remain different states. Knowledge graph
   offers another route to the task and its recorded evidence.
3. **Memory** searches shared findings and shows their source and authority.
   Captured text is inert; external links open only when you choose one.
   Choose **Knowledge graph** to explore memories, accepted claims, tasks and
   criteria. Tap a node to see its recorded connections, then **Open memory** or
   **Open task**. Choose **Back to graph** to return to the same selected node,
   zoom, layout and node-list search. This also works after opening a task's
   evidence. Both views keep checking access; switching tabs or losing access
   clears this navigation state. Drag the background to pan, drag a node to rearrange it, and
   pinch to zoom. **Zoom in**, **Zoom out**, **Reset view** and **Labels** provide
   simple controls. **Node list** offers keyboard navigation and searches the
   nodes in this view; it does not search the whole store. Use Memory search for
   that. The graph marks a bounded sample, and distance is not confidence.
4. **Settings** changes the color theme or signs out this/all sessions. Only the
   appearance preference is saved locally.

Open **Reports** from Overview or a task. Choose a published revision, use Contents
to move between loaded headings, and read the complete narrative, ADRs, code and
tables. Tables become labeled records on narrow screens; code scrolls within its
own block. Large documents load in bounded batches; the screen says when contents
are still incomplete. Figures load when selected. The app does not download the
PDF or EPUB. A report's technical result, human review, app availability, Gmail
submission and unverified Kindle arrival are separate facts.

Private content clears when you leave the app, lose the connection, switch
projects or sign out. Returning starts a fresh access check and may reset the
selected project/detail. There is no private offline mode. If a report's supporting
source is revoked, the full report becomes unavailable. During a rate limit, the
app waits for the server's retry interval; repeated refresh clicks do not shorten it.

In Android Chrome, the browser menu may offer **Add to Home screen** or **Install**
on a trusted HTTPS installation. The manifest uses standalone display and local
icons, with no service worker. This UI has desktop-browser emulation coverage;
physical Android installation, TalkBack and real GitHub/private-network operation
still need qualification. iPhone is not qualified. Keep the private network and
MemoryKing host running; a cold offline launch may show Chrome's network error.

## Experimental reporting

A report turns an agent's work into a document you can review later. The current
SDK can store a complete phase report, build PDF/EPUB copies and publish a reviewed
record for authorized readers. An optional worker runs queued builds and fixed-destination
Kindle submissions. The read-only phone viewer can read currently permitted,
published reports without renderer dependencies on the phone.

On native macOS, install the optional locked renderer dependencies in the pinned
project environment and run this disposable example:

```sh
.venv/bin/python -m pip install -r requirements-reports.lock
.venv/bin/python examples/report_draft.py
```

It creates a temporary store, grants a separate worker only read/build authority,
starts the actual local service with a private configuration file, and submits a
17-section synthetic document. The worker builds its artifacts in the background. The output says `built`, `qa: null`
and `email_sent: false`. It does not attest visual review, publish a report or
contact an external service. Linux x86-64 rendering requires Landlock ABI 6 or later
and seccomp; unsupported hosts fail closed. See the [deployment guide](deployment.md#report-worker-configuration).

For real work, the operator first sets a project/program policy and binds external
source permissions. An authorized author submits the narrative, README snapshot,
evidence manifest and dependencies. A worker builds the copies; a reviewer inspects
every PDF page and the EPUB and records the exact artifact hashes. Publication
then checks every intended reader's current access. A frozen report becomes
unavailable if any supporting source is later revoked or purged.

[Back to README](../README.md) · [Agent guide](agent-guide.md) · [Deployment](deployment.md)

## Follow a plan

Run `.venv/bin/python tools/plans_demo.py` in the contributor environment for a disposable example. For a lasting plan, connect an agent to your running installation and use the `memory_plan` tool described in the [agent guide](agent-guide.md#structured-plans). In the desktop dashboard, enter the project in **Task activity**, then open **Plans**. In the phone viewer, use **Overview → Plans** or **Tasks → Plans**. Select **View phase steps** to bring the selected phase’s steps into view and move keyboard focus to their heading. Background refresh preserves your reading position. Opening evidence provides a direct return to the selected phase; **All steps** restores the complete plan. A report document alone does not create an execution plan.
