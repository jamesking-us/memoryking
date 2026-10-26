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

Start the service:

```sh
.venv/bin/python -m memoryking serve \
  --store "$HOME/.memoryking-data" \
  --control "$HOME/.memoryking-control" \
  --dashboard-port 8771
```

Leave this terminal running. Ctrl-C stops the service cleanly. Restarting the same command uses the saved store. The socket is `$HOME/.memoryking-data/service.sock`; its full encoded path must fit within 100 bytes.

## 4. Open the dashboard

In a second terminal, from the same checkout:

```sh
.venv/bin/python -m memoryking dashboard-login \
  --socket "$HOME/.memoryking-data/service.sock" \
  --credential "$HOME/.memoryking-control/owner.credential" \
  --output "$HOME/.memoryking-control/browser-login-1.json"
```

Open the URL printed by the command. Read the private output file locally and enter its `code` in the page. Use the **one-use code**, never the owner credential. The code expires after 60 seconds; request a new file name for another attempt.

The dashboard starts empty. Enter your project in **Task activity** and select **Load tasks** after your agent has saved something to it. **Memory** and **Retrieval history** show the project's stored records and briefings. Each view checks your current permissions. They do not discover conversations in other applications automatically.

To explore a populated synthetic dashboard, install the [contributor environment](../CONTRIBUTING.md#setup) and run:

```sh
.venv/bin/python tools/dashboard_demo.py
```

The demo prints its loopback URL and a path to its private bootstrap file. Enter that file's one-use code and select project `demo`. Ctrl-C stops the demo and removes its temporary data. Its sample results are illustrative, not real agent work.

## 5. Connect an agent

The [agent guide](agent-guide.md) covers the Python client and MCP stdio bridge. A typical integration saves observations during work, checkpoints before interruption, and requests a briefing and resume packet when work continues.

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

[Back to README](../README.md) · [Agent guide](agent-guide.md) · [Deployment](deployment.md)
