# Getting started

This guide takes you from an installed package to an agent that remembers, with one command. No model account, Docker or Node installation is needed for this route.

## 1. Install

Use Python 3.12 (3.13 also works for the core install); the reference patch is recorded in [`.python-version`](../.python-version). The local service is exercised on macOS. Linux containers are exercised separately; Windows is not a supported local target yet.

```sh
python3.12 -m venv .venv
.venv/bin/python -I -m pip install memoryking
.venv/bin/python -I -m pip check
git clone https://github.com/jamesking-us/memoryking.git && cd memoryking
```

This installs the released package from PyPI (`0.1.0`). The checkout is for the examples and tools in this guide; the installed service itself does not need it. To pin the wheel by hash, use the `install` line from the release's `RELEASE.json` on the [release page](https://github.com/jamesking-us/memoryking/releases). To build from source instead, install the locks and the checkout as the [contributing guide](../CONTRIBUTING.md) describes, or use the [release wheel or container image](deployment.md#release-build).

## 2. Connect your agent

```sh
.venv/bin/memoryking setup claude-code --project my-app
```

Use `codex` instead of `claude-code` for Codex, or `generic` for any other host that launches MCP servers over stdio. In one run this:

1. creates a store under `~/.memoryking/default` (and a profile named `default`) if you have none, and starts the service in the background; from then on the bridge and the command line start it again whenever it is not running, so you never run `serve` yourself;
2. issues one agent credential, `agent`, with the capabilities the memory tools use and no expiry: `capture` (remember, correct, propose, forget, feedback), `read` (recall, resume, fetch, recent), `checkpoint` (save_progress) and `handoff` (hand_off). It has no `review` or `admin`, so the agent cannot approve its own proposals or change policy;
3. registers the small ("lean") MCP tool set with the host. For Codex that is a marked block in `~/.codex/config.toml`. For Claude Code the command to register it (`claude mcp add-json memoryking '{...}'`) is printed for you to run, because MemoryKing does not edit Claude Code's MCP file (add `--register` and, when the `claude` command is on your PATH, setup runs that command for you); it sets `alwaysLoad` so Claude Code keeps the memory tools in every prompt instead of listing them by name only and loading one when the model searches for it (a model that never searches would never save anything); for `generic` it prints the command and the `mcpServers` JSON;
4. installs the session hooks (Claude Code and Codex) so a session starts briefed and saves before compaction and at its end without the model having to decide anything. The start briefing opens with a short note that names the memory tools and says to save an instruction, a decision, a fact it was told or a failed approach as soon as it has one, followed by what memory holds: the user's own recent words, how the last session ended, pins and constraints. The hooks run as the same agent as the tools, in your home directory's host settings rather than in the project's shared file. Because that file is per user, the hooks apply to every project you open on that host and save into `--project`; add `--scope project` to put them in this directory's host file instead, or `--no-hooks` to skip them;
5. prints a five-line summary, including the command that undoes it.

Run it again and nothing changes. `--dry-run` prints every change, including the exact file contents, without making any. `memoryking setup claude-code --project my-app --remove` puts every file it edited back byte for byte (and deletes the ones it created); it keeps the store and the credential, and `memoryking --profile default stop` stops the service. Setup refuses, before creating anything, a location whose socket path would exceed the 100-byte limit (`--data-dir` chooses another).

Restart the host, ask it to "remember that the deploy script needs the VPN", and later ask what it remembers. To check the service:

```sh
.venv/bin/memoryking --profile default status
```

`status` includes whether the service is running, whether it starts on demand and where its log is (`daemon.log` in the store directory). To rotate the credential later, use `credential issue --rotate` (see the reference below).

## 3. Try the complete example

```sh
.venv/bin/python examples/remember_and_resume.py
```

The walkthrough uses a disposable store and two client connections. It saves an observed failure, retrieves an attributed briefing, saves an unfinished task and resumes it. It verifies that the failed attempt and next action survive. The example uses a disclosed byte-based token estimate, so the core installation needs no optional tokenizer.

This is real storage and client behavior with synthetic task content. It does not run an AI model or establish that a model will make better decisions.

## 4. Check the service

In a second terminal, from the same checkout:

```sh
.venv/bin/python -m memoryking --profile default status
```

This prints the store's history id, position, frontier, job states and a capacity bar for every bounded resource. `--version` prints the installed package version. If anything looks wrong, `memoryking --profile default doctor` checks the installation and names the fix for each failed check.

## 5. Open the dashboard

The dashboard is opt-in. Add `--dashboard-port 8771` to the `setup` command (then `memoryking --profile default stop`; the service restarts on demand with it) and the quickest route signs you in directly:

```sh
.venv/bin/python -m memoryking --profile default dashboard open
```

When no browser can be opened from the terminal, request a code file instead:

```sh
.venv/bin/python -m memoryking dashboard-login \
  --socket "$HOME/.memoryking/default/store/service.sock" \
  --credential "$HOME/.memoryking/default/control/owner.credential" \
  --output "$HOME/.memoryking/default/control/browser-login-1.json"
```

Open the URL printed by the command. Read the private output file locally and enter its `code` in the page. Use the **one-use code**, never the owner credential. The code expires after 60 seconds; request a new file name for another attempt.

The dashboard starts empty. The overview shows capacity use for the tenant's bounded resources and warns at 80 percent. **Task activity** and **Retrieval history** list the projects your account can read, with their record counts; pick one and select **Load**. Each task card opens with "Where this left off", a three-sentence summary the service derives once a task has been idle for half an hour (or when an agent hands it off), followed by the saved state. The browser remembers your last choice. **Memory** shows the project's stored records; its **Ask for a briefing** view runs the same briefing an agent would receive, with its receipt, and can verify the replay, and its **Timeline** view lists every recorded event for the project, newest first. Its **Memory graph** colours each record by how much authority it has (an observation, an accepted claim, a proposal awaiting review, a quarantined claim) and draws the records your briefings used larger; select one and **Show receipts** lists those briefings and opens any of them in Retrieval history. On a phone the views sit in a bar across the top, the cards stack and the graph is shown as a list until you ask for the drawing. **Retrieval history** lists recorded briefings; open one and **Compare with previous briefing** shows which references were added, dropped or kept, and how status and token cost changed. **Team** shows, for one project, who did what in the last thirty days (captures, claims, checkpoints, hand-offs, summaries, provider dispatches), the grants that allow it, a **Revoke** button per principal, and **Export audit**, which downloads a JSON record of the project's events, dispatches, receipts, summaries and grants without any content. The overview lists your readable projects as cards; opening one follows it in every view, and the page address (`#project=NAME`) can be shared. Each view checks your current permissions. They do not discover conversations in other applications automatically.

To explore a populated synthetic dashboard, install the [contributor environment](../CONTRIBUTING.md#setup) and run:

```sh
.venv/bin/python tools/dashboard_demo.py
```

The demo prints its loopback URL and a path to its private bootstrap file. Enter that file's one-use code and select project `demo`. Ctrl-C stops the demo and removes its temporary data. Its sample results are illustrative, not real agent work.

## 6. Semantic search

Lexical search works out of the box and makes no network request. Meaning-based recall is a two-step install, because the core install is deliberately free of models:

```sh
# step 1: the optional runtime (onnxruntime, numpy, tokenizers), hash-pinned
.venv/bin/python -I -m pip install --require-hashes -r requirements-embeddings.lock
# step 2: setup (or this command alone) installs the model and turns search on
.venv/bin/python -m memoryking --profile default semantic enable
```

`semantic enable` states what it is about to download (the model's name, size, licence, host and pinned revision), fetches it into `~/.memoryking/models`, verifies every file against its SHA-256, restarts the daemon so it loads the model, and switches the project on (`--project P`, or every project that has memory). Indexing then runs in the background and `memoryking status` shows its progress on the `Semantic` line. `memoryking setup <host> --project P` does the same when the runtime is installed (skip it with `--no-semantic`); without the runtime it downloads nothing and tells you how to get there. From then on a daemon that finds the installed model loads it without any flag, a briefing uses the semantic channel whenever the project is ready, and falls back to lexical search, saying why in the receipt's trace, when it is not. `memoryking semantic disable --project P` turns a project off. The model runs on your CPU and nothing leaves the machine.

## Reference: doing it by hand

The steps `setup` performs, for when you want other paths or a service run by your own supervisor. Skip creating a store if `setup` already made one.

### Create your own store

Choose **new** directories: MemoryKing refuses to initialize over an existing store.

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

Leave this terminal running, or add `--autostart` to `profile add` so the command line and the MCP bridge start the service in the background when it is down. Ctrl-C stops the service cleanly. Restarting the same command uses the saved store. The socket is `$HOME/.memoryking-data/service.sock`; its full encoded path must fit within 100 bytes. The service completes its own bookkeeping jobs between requests and prunes expired records once an hour, so no separate worker or scheduled cleanup is needed. Add `--log-level info` to see redacted operational events on stderr, or `--trace` to log how long each request took, including time spent waiting for the store.

### Connecting an agent by hand

The [agent guide](agent-guide.md) covers the Python client and MCP stdio bridge. A typical integration saves observations during work, checkpoints before interruption, and requests a briefing and resume packet when work continues.

Agents should not use the owner credential. Issue each one a scoped credential. `capture,read` alone is not enough for an agent that saves progress: `save_progress` needs `checkpoint` and `hand_off` needs `handoff`. Leave out `--expires` unless you want the agent to stop working on a date (a credential issued with `--expires 30d` silently stops after thirty days); `--rotate` replaces a credential and invalidates every earlier one of that principal:

```sh
.venv/bin/python -m memoryking --profile home credential issue agent-1 --project demo --capabilities capture,read,checkpoint,handoff --output "$HOME/.memoryking-control/agent-1.credential"
```

`credential list` shows what is issued and `credential revoke` withdraws one. Register the bridge with `--profile lean` (on the bridge `--profile` chooses the tool set; `--store-profile` names the store) and install the hooks with the same credential, so the hooks and the tools act as one principal: `memoryking hooks install --host claude --scope user --project demo --credential PATH --socket PATH`. The [agent guide](agent-guide.md) has the configuration for each host.

To follow a multi-step piece of work rather than one agent's checkpoint, have the agents keep a structured plan with the `memory_plan` tool described in the [agent guide](agent-guide.md#structured-plans); `.venv/bin/python tools/plans_demo.py` runs a disposable example. In the dashboard, choose the project under **Task activity** and open **Plans**: a phase shows its steps, evidence and progress.

For multiple machines or tenants, use the [shared deployment](deployment.md#shared-postgresql). Do not expose the local loopback dashboard through a network proxy.

## If something goes wrong

| Symptom | First check |
| --- | --- |
| Python version rejected | Use Python 3.12 or 3.13 and create a fresh virtual environment. |
| An unrelated Python package is imported | Run commands with `env -u PYTHONPATH -u PYTHONHOME` before `.venv/bin/python`. |
| Service refuses a directory | Check that data/control are disjoint, owner-private paths and no other daemon owns them. |
| Login code rejected | Obtain a fresh code in a new output file; use the printed origin exactly. |
| Dashboard has no tasks | Connect the correct project and ensure the agent has saved checkpoints. |
| Exact tokenizer unavailable | Install the optional tokenizer lock or use the default `chars-estimate-v1` estimate. |

[Back to README](../README.md) · [Agent guide](agent-guide.md) · [Deployment](deployment.md)
