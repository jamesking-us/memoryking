# Getting started

This guide takes you from a new checkout to a local service and dashboard. No model account, Docker or Node installation is needed for this route.

## 1. Install

Use Python 3.12; the reference patch is recorded in [`.python-version`](../.python-version). The local service is exercised on macOS. Linux containers are exercised separately; Windows is not a supported local target yet.

```sh
git clone https://github.com/jamesking-us/memoryking.git
cd memoryking
python3.12 -m venv .venv
.venv/bin/python -I -m pip install --require-hashes -r requirements-build.lock -r requirements-core.lock
.venv/bin/python -I -m pip install --no-build-isolation --no-deps .
.venv/bin/python -I -m pip check
```

These commands install locked dependencies and build the package from this checkout. There is no public registry release to install by name. Keep the checkout to run the examples; the installed service itself does not need it.

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

Leave this terminal running. Ctrl-C stops the service cleanly. Restarting the same command uses the saved store. The socket is `$HOME/.memoryking-data/service.sock`; its full encoded path must fit within 100 bytes. The service completes its own bookkeeping jobs between requests and prunes expired records once an hour, so no separate worker or scheduled cleanup is needed. Add `--log-level info` to see redacted operational events on stderr.

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

The dashboard starts empty. The overview shows capacity use for the tenant's bounded resources and warns at 80 percent. **Task activity** and **Retrieval history** list the projects your account can read, with their record counts; pick one and select **Load**. The browser remembers your last choice. **Memory** shows the project's stored records; its **Ask for a briefing** view runs the same briefing an agent would receive, with its receipt, and can verify the replay, and its **Timeline** view lists every recorded event for the project, newest first. Its **Memory graph** colours each record by how much authority it has (an observation, an accepted claim, a proposal awaiting review, a quarantined claim) and draws the records your briefings used larger; select one and **Show receipts** lists those briefings and opens any of them in Retrieval history. On a phone the views sit in a bar across the top, the cards stack and the graph is shown as a list until you ask for the drawing. **Retrieval history** lists recorded briefings; open one and **Compare with previous briefing** shows which references were added, dropped or kept, and how status and token cost changed. Each view checks your current permissions. They do not discover conversations in other applications automatically.

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

[Back to README](../README.md) · [Agent guide](agent-guide.md) · [Deployment](deployment.md)
