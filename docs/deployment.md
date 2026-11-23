# Deployment and configuration

Start with the local profile unless you need several machines or tenants. These are evaluation deployments; production performance, failover and recovery service levels are not qualified.

## Installation profiles

All profiles use Python 3.12; the container images pin it, and 3.13 is supported for the core install (see [Runtimes](reference.md#runtimes-python-versions-and-windows)). Dependencies are hash-locked; package source is installed separately with `--no-deps`.

| Lock | Purpose |
| --- | --- |
| `requirements-core.lock` | Local service, Python client, stdio bridge and dashboard, with the semantic-search libraries (onnxruntime, numpy, tokenizers). The model is not in it: `memoryking setup` or `memoryking semantic enable` downloads and verifies it. |
| `requirements-build.lock` | Build a wheel from the source checkout. |
| `requirements-shared.lock` | The core lock plus the PostgreSQL driver/pool and shared HTTP/MCP runtime. |
| `requirements-tokenizers.lock` | Optional exact `cl100k_base` token counting. |
| `requirements-dev.lock` | Contributor checks, builds and complete examples. |

The [getting started commands](getting-started.md#1-install) install the smallest source-based setup. For deployment without the checkout, build a wheel and install it into a separate runtime:

```sh
.venv/bin/python -m build --wheel --no-isolation
python3.12 -m venv .venv-runtime
.venv-runtime/bin/python -I -m pip install --require-hashes -r requirements-core.lock
.venv-runtime/bin/python -I -m pip install --no-deps dist/memoryking-0.1.0a1-py3-none-any.whl
.venv-runtime/bin/python -I -m pip check
.venv-runtime/bin/memoryking --help
```

Run the build step in the contributor environment. Verify the wheel revision/hash before moving it to another host. For a shared runtime, use the shared lock instead of the core lock. Node is needed to rebuild browser assets and TypeScript packages, not to run the installed Python service.

### Release build

`tools/build_release.py` builds the wheel and sdist reproducibly (the archive timestamps come from the commit, so one commit always yields the same bytes), writes `dist/SHA256SUMS` and `dist/RELEASE.json` (version, revision, tool versions, hashes), installs the wheel into a fresh virtual environment with the core lock, and checks `memoryking --version`. The manifest's `install` line pins the wheel by hash:

```sh
pip install --require-hashes --no-deps memoryking-0.1.0a1-py3-none-any.whl --hash=sha256:<from RELEASE.json>
```

The tool uploads nothing; publishing is the owner's sequence [below](#publishing-a-release). `tools/local_ci.py` runs the release build on a clean checkout.

### Container image for the local daemon

[`deploy/local/Dockerfile`](../deploy/local/Dockerfile) builds the release wheel on a digest-pinned Python image and installs it with the core lock only, as an unprivileged user, with `/data` and `/control` as volumes:

```sh
docker build -f deploy/local/Dockerfile -t memoryking:local .
docker volume create mk-data && docker volume create mk-control
docker run --rm -v mk-data:/data -v mk-control:/control memoryking:local init --store /data/store --control /control/store
docker run -d --name memoryking -v mk-data:/data -v mk-control:/control memoryking:local
```

The published image is `ghcr.io/jamesking-us/memoryking:<version>`; pull it by the digest recorded on the release page (for `0.1.0a1`, `ghcr.io/jamesking-us/memoryking@sha256:681adce7eaec14f747cfb60accd78118f6f21680e5d207999abe33bb08620c61`) and use that reference in place of `memoryking:local` above. Each published digest is signed keyless with Sigstore by the owner; verify it before running it, and refuse an image that fails:

```sh
cosign verify --certificate-identity jamesking.us@gmail.com --certificate-oidc-issuer https://github.com/login/oauth ghcr.io/jamesking-us/memoryking@sha256:<digest from the release page>
```

Named volumes take the image's ownership of `/data` and `/control`; the store and control trees are created inside them because `init` insists on new directories. The socket is `/data/store/service.sock` on the data volume, so an agent in another container reaches it by mounting the same volume, and the owner credential is in the control volume. The dashboard binds loopback inside the container; run with `--network host` on Linux to open it from the host, or use the shared profile for anything beyond one machine. `tools/image_rehearsal.py --output NEW.json` builds the image, initialises a disposable store, starts the daemon, queries its status and records the image digest; that digest is what the owner signs and publishes. The tool pushes nothing.

### Publishing a release

MemoryKing is licensed under the Apache License 2.0. Publishing runs locally with the owner's credentials; nothing in the repository or a hosted workflow holds them. The version lives in `pyproject.toml` and `src/memoryking/__init__.py`; a pre-release such as `0.1.0a1` installs only with `pip install --pre`.

1. Move the **Unreleased** section of `CHANGELOG.md` under a heading for the new version and date, and bump the version in `pyproject.toml` and `src/memoryking/__init__.py`. Merge the release commit to `main`, tag it (`git tag -a v0.1.0a1 -m "MemoryKing 0.1.0a1"`) and push the tag.
2. In the contributor environment at that tag, run `.venv/bin/python tools/build_release.py`. It writes `dist/SHA256SUMS` and `dist/RELEASE.json` and proves a fresh install; a second run of the same commit must reproduce the same hashes.
3. Upload the wheel and sdist from a throwaway environment that has `twine` (it is not in the hash locks), with a PyPI API token scoped to the project (the first upload of a new project needs an account-wide token; replace it with a project token afterwards): `twine upload dist/memoryking-0.1.0a1*`. Confirm the hashes PyPI shows (`https://pypi.org/pypi/memoryking/<version>/json`) match `SHA256SUMS`.
4. Build and rehearse the image (`tools/image_rehearsal.py --output tmp/image-0.1.0a1.json`), then tag and push it to a registry you control, for example `docker tag memoryking:local ghcr.io/jamesking-us/memoryking:0.1.0a1 && docker push ghcr.io/jamesking-us/memoryking:0.1.0a1`, and sign the pushed digest (the index digest `docker push` prints) with your cosign key: `cosign sign --key cosign.key ghcr.io/jamesking-us/memoryking@sha256:<digest>`, or keyless with `cosign sign` alone (the route used so far), which signs in through a browser or a device code, binds the signature to the signing identity, and records it in the public transparency log; the release notes then carry the `cosign verify` command with that identity and issuer. With a key pair, publish `cosign.pub` instead. A new package on the GitHub registry starts private; make it public in the package's settings on GitHub (the API cannot), or nobody can pull it.
5. Create the GitHub release for the tag, marked as a pre-release while the version is one, with the version's changelog section as its notes, followed by the install command, the image digest and the `cosign verify` line, and with `SHA256SUMS`, `RELEASE.json`, the wheel and the sdist attached, so the wheel and image of one version are tied together.

The shared-profile image under `deploy/shared/` is published the same way when needed. After the first upload, the getting-started guide's install becomes `pip install memoryking` followed by `init`, `profile add`, `serve` and `dashboard open`.

## Local service

```sh
.venv/bin/python -m memoryking init --store "$HOME/.memoryking-data" --control "$HOME/.memoryking-control"
.venv/bin/python -m memoryking serve --store "$HOME/.memoryking-data" --control "$HOME/.memoryking-control" --dashboard-port 8771
```

One process owns each store. Use private local filesystems, disjoint data/control trees and short Unix-socket paths. Separate tenants use separate local stores. Network-shared SQLite files and multiple writers opening the same store are unsupported.

The foreground process exits cleanly on Ctrl-C or SIGTERM. A process supervisor may restart it using the same absolute paths and OS owner. `tools/supervision_rehearsal.py --output NEW_DIRECTORY` tests a temporary macOS launchd service and removes it afterward; it is not a persistent service installer. Native systemd and Windows supervision are not qualified.

### Local configuration

Configuration comes from CLI flags and authorized API operations. There is no general TOML/YAML configuration file or automatic environment-variable override system.

| Option | Default / meaning |
| --- | --- |
| `--profile NAME`, `--config FILE` | Fill omitted `--store`, `--control`, `--socket`, `--credential` and `--dashboard-port` from a named profile in `~/.memoryking/config.toml` (`MEMORYKING_PROFILE`, `MEMORYKING_CONFIG`). The file holds paths and, optionally, session `bindings` (see the agent guide), never secrets. `profile add NAME --store ... --control ...`, `profile list` and `profile remove NAME` maintain it. Explicit flags always win. |
| `setup HOST --project P [--principal NAME] [--data-dir DIR] [--scope user\|project] [--task T] [--expires D] [--dashboard-port N] [--no-hooks] [--no-prompt-hook] [--dry-run] [--remove]` | Connects a host in one idempotent run; HOST is `claude-code`, `codex` or `generic`. Creates a store and the profiles `default` (owner) and `default-agent` when none exist, starts the daemon, issues one agent credential (`capture`, `read`, `checkpoint`, `handoff`; no expiry unless `--expires`), registers the lean MCP bridge (a marked block in `~/.codex/config.toml` for Codex; for Claude Code and any other host the command is printed, because their MCP file format is not documented here) and installs the session hooks as the same principal, plus, for Claude Code and Codex, a prompt-time recall hook (`UserPromptSubmit`; see [the agent guide](agent-guide.md#the-prompt-time-recall-hook)). `--no-prompt-hook` leaves the prompt hook out and, on a rerun, removes it; `MEMORYKING_PROMPT_HOOK=off` in the host's environment silences it without touching any file. `--profile NAME` (before the command) names the store profile instead of `default`. Files it edits are recorded so `--remove` restores them byte for byte. The default location is `~/.memoryking/NAME`, refused when the socket path would exceed 100 bytes. |
| `hook prompt [--budget 2000] [--limit 32] [--floor 300000] [--deadline 1.5] [--third-party]` | What the installed `UserPromptSubmit` hook runs: reads the event on stdin and prints a short cited briefing for the prompt, or nothing. `--limit` is how many of the closest records the lookup returns (at most 32), `--budget` the tokens of them the hook shows (as many records as fit, each cut to its particular sentences when they do not all fit whole; the text says how many were left out), `--floor` the lowest lexical score (millionths) a record may have, `--deadline` the seconds before the lookup is abandoned (`deadline_exceeded` on stderr, nothing printed); the host's own timeout for the hook is 5 seconds. `--third-party` (or `MEMORYKING_HOOK_THIRD_PARTY=1`) also shows records a third party wrote (tool results and errors, fetched pages), fenced and labelled; by default only what the user and the assistant said is shown. Records are shown newest first with their observed day, and up to three matching records of the user's own memory join them inside the same budget. It stores no memory (the briefing has a receipt, like any, naming every record the lookup returned) and never fails the host. |
| `profile add --autostart`, `stop` | A profile with `autostart = true` lets the CLI and the stdio bridge start the daemon in the background when its socket is not listening (one starter at a time, a lock file in the store directory; log in `daemon.log` there; `daemon.pid` while it runs). `memoryking --profile NAME stop` asks it to shut down cleanly. `status` shows whether it runs and starts on demand. Without autostart a missing daemon is `transport_unavailable` with the command to start it. |
| `--store`, `--control` | Required paths for initialization, serving and offline maintenance. Neither may contain the other. |
| `init --tenant`, `--principal` | `main` and `owner`; IDs are 1–96 ASCII letters, digits, `_` or `-`. |
| `serve --dashboard-port` | Dashboard disabled unless set. `0` chooses a free port; otherwise 0–65535. Always loopback. |
| `serve` housekeeping | The daemon expires replay results, artifacts, usage rows and access receipts past their 30-day retention once an hour, deletes finished jobs and old artifact tombstones in bounded batches, and completes audit jobs between requests; `gc` remains available for offline use. |
| `serve --dashboard-port` metrics | With the dashboard enabled, `GET /metrics` with a bearer admin credential returns Prometheus text (requests, errors, latency, capacity, jobs, frontier) for scraping. |
| `serve --backup-dir DIR [--backup-every 1d] [--backup-keep 14]` | The daemon backs up into `DIR/backup-<timestamp>` when the newest complete backup is older than the interval, checked by a maintenance thread (never the thread that accepts connections), and keeps the newest N. Each backup hard-links the blob files the previous one holds (after hashing them) and copies only new ones, so it is complete on its own and pruning an older backup never breaks a newer one. Only complete backups it recognises are pruned. A failed backup leaves no partial copy and is retried after the interval, at most hourly, rather than every minute. A profile may set `backup_dir`, `backup_every` and `backup_keep`; `doctor --backup-dir DIR --backup-every 1d` warns when none exist and fails past twice the interval. The control tree is still retained separately. |
| `serve --instance-id ID` | Names this deployment for the [instance lease](#the-instance-lease); also accepted by `backup`, `gc` and `credential rotate-owner`, and by the shared `serve`, `worker-once`, `backup`, `rotate-owner` and `compact-operations`. Required when processes of one tenant run on more than one host. |
| `semantic enable [--project P]`, `semantic disable --project P`, `semantic status` | One-command semantic search for the local profile: installs and verifies the pinned model into `~/.memoryking/models` (stating what it downloads first), restarts the daemon to load it, enables the project. `status` shows a `Semantic` line with progress. Needs the embedding libraries, which the core install has; `setup` runs it by default (`--no-semantic` skips). If the download fails it says why and this command retries. |
| `serve --embedding-model DIR` | Loads a model from DIR instead of the models directory; without the flag an installed, verified model is loaded on its own (`--no-semantic` prevents that). The daemon indexes every enabled project, enables each new project that has memory, and embeds briefing queries itself. A profile may set `embedding_model`. `embeddings enable\|status --project P` manage one project's partition. |
| `serve --reranker-model DIR` | Loads the installed local cross-encoder; a briefing that asks for `rerank` has its top candidates reordered by it. A profile may set `reranker_model`. Install with `python -m memoryking.model_install --model reranker --destination DIR`; `doctor --reranker-model DIR` verifies it. |
| `serve --log-level` | `warning` by default. Redacted operator log lines on stderr: component, event, request id, error type and code location, never content, SQL or paths. An `internal_error` reply carries the `request_id` of its line. |
| `serve --trace` | One `event=trace` line per request at info level (the level is lowered to info if it was higher): for a socket request the operation, the command action, the outcome code, elapsed milliseconds, time spent waiting for the store lock, time inside transactions, the number of transactions and the response size; for a dashboard request the method, the route family (such as `/api/receipts`, never the project or receipt id), the status and the same timings. Nothing in a trace line names content, a project or a principal. The shared service accepts the same flag. |
| `serve --egress-policy FILE` | Loads the private egress policy below; without it the only model provider is the local recorder and nothing leaves the machine. A policy change needs a restart and retires every open conversation. The shared service accepts the same flag. |
| `serve --summary-idle 30m` | Between requests the daemon derives a "where this left off" summary, as the owner, for every task whose latest checkpoint has been idle this long and has none; `0` disables. Summaries follow the 30-day retention. |
| `summarize --project P --task T [--run R] [--provider NAME --model M --egress-policy FILE] [--json]` | Derives a summary with its own receipt: extractive by default, or written by a policy adapter through the audited inference path. Needs a credential with `checkpoint` (and `inference` for `--provider`). |
| `consolidate --project P --egress-policy FILE --provider NAME --model M [--since 7d] [--limit N] [--watch EVERY --idle 15m] [--json]` | One consolidation pass (or, with `--watch`, a loop that runs a pass whenever the project has been quiet for `--idle`): a model drafts claims from the observations after the project's consolidation frontier, through the egress policy and the audited conversation; admitted claims are ordinary proposals awaiting review. Refuses without an egress policy; `--provider local-recording` dry-runs and files nothing. Needs `capture` and `inference`. |
| `serve --consolidate-every 6h --consolidate-project P [--consolidate-provider NAME --consolidate-model M --consolidate-idle 15m]` | Off by default. Every interval the daemon launches a separate `consolidate` process per named project (as the owner, with the same egress policy), which runs a pass only when the project has been idle; needs `--egress-policy`. The daemon supervises these processes (a failing one is retried after a growing delay, all are stopped when the daemon stops, and `status` lists each with its PID) and never reads the provider's key file; the child does. |
| `egress-audit --project P [--before MS] [--json]` | Lists what left the store for a provider in the last 30 days: when, status (`delivered`, `failed` or `pending`), provider, model, host, classification, bytes, cited receipts and permit. Needs a credential with `inference`. |
| `status --socket`, `--credential` | Human summary of store health through the running daemon: history, position, frontier, jobs and capacity bars; `--json` prints the raw result. Needs an admin credential. |
| `health --project P`, `--days N`, `--json` | Whether memory is paying for itself in one project: helpful feedback, empty briefings, memory never read, lost evidence and the review backlog, as counts with fixed thresholds (see the [reference](reference.md#memory-health)). Needs an admin credential. |
| `--version` | Prints the installed package version. |
| `doctor --store`, `--control` | Checks Python, directory ownership and layout, socket path, schema version, journal integrity and tokenizer; with `--socket` and `--credential` it also reports capacity and frontier from the daemon. Exit 1 when a check fails. |
| `credential issue NAME --project P --capabilities a,b [--expires 30d] [--rotate] --output FILE` | Issues a scoped credential into a new private file; `--rotate` replaces the principal's credential so every earlier one stops working. `credential list` shows every grant with validity; `credential revoke NAME --project P` removes one. All take `--socket` and an admin `--credential`. |
| `credential rotate-owner --store`, `--control` | Offline: replaces `owner.credential` with a new credential and retires the old one. The daemon must be stopped. See [rotating credentials](#rotating-credentials). |
| `capture --project P [--text T \| --file PATH] [--source S] [--event E]` | Saves one observation from `--text`, from `--file` (`-` is stdin) or from stdin with a stable event id (text with quotes, brackets or parentheses needs no shell quoting from a file or stdin); `recall --project P --query Q` prints a cited Markdown briefing (`--json` for the packet). Both accept `--profile` or `MEMORYKING_SOCKET`, `MEMORYKING_CREDENTIAL` and `MEMORYKING_PROJECT` (the default for `--project`, here and in `summarize`). |
| `save-progress --task T --objective O --state S [--next A] [--finding F] [--blocker B] [--failed "action => outcome"]`, `resume --task T` | The lean `save_progress` and `resume` tools as commands, for an agent that has a shell and no MCP. Repeatable flags repeat; `--json` prints the structured result. Same connection options as `capture`. |
| `dashboard open --socket`, `--credential` | Issues a one-use code and opens the dashboard signed in; `--print-only` prints the link instead. The code travels in the URL fragment and expires in 60 seconds. |
| `request --socket`, `--credential` | Required socket and private credential file paths. |
| `request --credential-output` | New private output file required for a grant; secret values are not printed. |
| `dashboard-login --output` | New private file containing a one-use code; expires after 60 seconds. |
| `--backup` | New external directory for backup; verified existing directory for restore. |

Paths/runtime changes require restart. Permission changes apply transactionally; for retiring a credential see [rotating credentials](#rotating-credentials). Keep owner credentials out of agent prompts and browser pages; issue scoped credentials for normal agents. Files use owner-only modes (directories 0700, sensitive files 0600).

### Egress policy

By default MemoryKing never sends retained context to a model: `AuthorizedConversation` accepts only the local recorder. To let a host dispatch through a real provider, write a private policy file (mode 0600) and start the service with `serve --egress-policy FILE`. The daemon enforces the policy at authorization time, so a host cannot exceed it, and records every departure in the egress audit.

```toml
revision = "team-2026-11"              # any change retires every open conversation

[[providers]]                          # Anthropic
name = "anthropic-messages"
host = "api.anthropic.com"
models = ["claude-sonnet-5-5"]
key_file = "/private/anthropic.key"    # read by the host process, never by the daemon
max_classification = "internal"        # restricted sources never leave through this provider

[[providers]]                          # OpenAI, xAI Grok, or any chat-completions server
name = "openai-chat"
host = "api.x.ai"
models = ["grok-4"]
key_file = "/private/xai.key"

[[providers]]                          # a model running on this machine (Ollama, vLLM, LM Studio)
name = "openai-chat"
host = "127.0.0.1:11434"
insecure_http = true                   # plain HTTP is accepted for loopback hosts only
models = ["llama3.1:8b"]
max_classification = "restricted"      # nothing leaves the machine, so the ceiling can be higher

[[providers]]                          # Google Cloud Vertex AI, Gemini
name = "vertex-ai"
location = "us-central1"               # host defaults to LOCATION-aiplatform.googleapis.com
project = "my-gcp-project"
models = ["gemini-2.5-pro"]
token_command = ["/usr/bin/gcloud", "auth", "print-access-token"]

[[providers]]                          # Vertex AI, Anthropic models through rawPredict
name = "vertex-ai"
location = "us-east5"
project = "my-gcp-project"
publisher = "anthropic"
models = ["claude-sonnet-5-5"]
token_file = "/private/vertex.token"
```

Fields: `name` is one of `anthropic-messages`, `openai-chat` or `vertex-ai`; `host` (required except for Vertex, where it defaults from `location`), `models` (the only models the daemon will authorize), `max_classification` (`public`, `internal` or `restricted`; default `internal`), `max_request_bytes` (default and maximum 98,304), `timeout_s` (default 60, maximum 300), `max_output_tokens` (default 1,024), `path` (overrides the provider's endpoint path, for proxies), `headers` (up to eight extra headers; authorization headers are refused), and credentials: `key_file` for API keys, `token_file` for a bearer token, or `token_command` (an absolute program and arguments whose standard output is the token, run by the host process with a minimal environment). Vertex also takes `location`, `project` and `publisher` (`google` for `generateContent`, `anthropic` for `rawPredict`). A provider name may appear more than once only with different hosts in separate policy files; within one file names are unique, so run two local servers as two policies or one server with several models.

Adapters make one HTTPS request per dispatch with certificate verification, no retry, no streaming and no tool use, and keep no state: the full conversation is resent each time. Every consumed permit writes an audit record (provider, model, host, policy revision, request and manifest hashes, the content-free manifest, highest classification, byte count) that the host completes with the outcome, wire hash, response hash and token usage. `memoryking egress-audit` and the dashboard's **Jobs & health** view list them for 30 days; purging a source does not remove the record of a delivery that already happened.

## Windows

Windows runs the agent-side tools (the command line, the MCP bridge and the Claude Code hooks) natively, and keeps the memory store and its daemon in one of two places on the same machine:

- **WSL2**: a Linux distro under the Windows Subsystem for Linux. Setup installs MemoryKing in a virtual environment in the distro's own home (`~/.local/share/memoryking/venv`), with the same version as Windows and its dependencies hash-checked from the lock files the package ships, and keeps the store in `~/.memoryking`. The distro needs Python 3.12 or 3.13 with `venv`.
- **Docker Desktop** with Linux containers: a container named `memoryking` from the MemoryKing image, with the store in three named volumes (`memoryking-data`, `memoryking-control`, `memoryking-home`). Setup needs the image named with `--image`: one you built (`docker build -f deploy/local/Dockerfile -t memoryking:local .`) or the published one by digest. The image has the core install, which includes the semantic-search libraries, but not the model: setup downloads it into the `/data` volume (`MEMORYKING_MODELS=/data/models`), so it survives a new container. The image has no `tokenizers` extra, so a Windows side with it is refused with `store_extras_mismatch`; use WSL2 for that, where setup installs the extra on both sides.

Setup never installs WSL2 or Docker Desktop. When neither is there it stops and names what to install (`wsl --install`, or Docker Desktop), which needs administrator rights and may need a restart. When both are there it does not guess:

```powershell
memoryking setup claude-code --project my-app --backend wsl:Ubuntu
memoryking setup claude-code --project my-app --backend docker --image memoryking:local
```

One run installs the store side, creates the store under the usual owner-only rules, starts the daemon, issues the agent's limited credential, downloads the search model on the store side (after printing the same consent text as on other systems: model, size, licence, host, pinned revision; `--no-semantic` skips it, `--dry-run` downloads nothing, and if the download fails setup still finishes on keyword search and names running the same command again as the retry), writes the Claude Code hooks, prints the MCP registration command, then saves and recalls a marked note through the relay and prints `MemoryKing works: ...` or exactly what failed. `--dry-run` lists every change and makes none; a second run changes nothing it does not need to. The hooks are written in the form the installed Claude Code runs without a shell (`command` plus `args`, Claude Code 2.1.330 and later), or otherwise as one command string that Git Bash and PowerShell read the same; the summary says which. Neither form contains a path on the store side or a secret.

**What the Windows side needs.** Only the relay, the bridge and the hooks run on Windows, and they never load the search libraries (the store side does the searching). `pip install memoryking` installs the libraries on Windows too, about 170 MB that stay unused. To keep the Windows side small, install in two steps, `pip install jsonschema==4.26.0` and then `pip install --no-deps memoryking`; everything on the Windows side then works, and `pip check` is the only thing that complains. The store side always gets the libraries from the hash-checked locks setup ships.

**How Windows reaches the store.** Each hook, the MCP bridge and any command typed by hand start `memoryking relay` on the store side (`wsl.exe -d DISTRO` or `docker exec -i memoryking`) and keep it open; it passes request lines to the daemon's Unix socket and replies back without reading them. The agent's credential stays on the store side; the relay hands it to the Windows process in its first line, which keeps it only in memory. A relay of another version, or a store side without the `tokenizers` extra when Windows has it, is refused at once with the command that fixes it. For commands typed by hand, `--user-environment` also sets `MEMORYKING_RELAY` and `MEMORYKING_RELAY_PROFILE` for your Windows user (nothing secret; `--remove` puts back what was there), after which `memoryking status` and `memoryking recall --query ...` work in a new terminal.

**Keeping the daemon running.** Nothing has to be started by hand. The relay starts the daemon on the store side when it is not running; the first hook after that takes about 1.5 s instead of half a second. In WSL2 the running daemon keeps the distro up; when the distro stops (`wsl --terminate Ubuntu`, `wsl --shutdown` or a Windows restart), the next session starts both again. The container restarts with Docker Desktop (`--restart unless-stopped`), and the daemon inside it starts on demand. Each hook takes about 0.45 s in the shell-free form, 0.55 s through Git Bash and 1.2 s through PowerShell.

**Checking it.** `memoryking doctor` on Windows says which back end the hooks use, whether the store side answers, the versions on both sides, the store side's own checks, and the one next step when something is wrong. `memoryking doctor --replay-hooks` runs the installed hooks the way Claude Code runs them, with real event payloads, through two sessions, and checks that the second session is briefed with what the first one saved. Neither calls a model.

**Upgrading.** Upgrade both sides together: install the new version on Windows (`pip install --upgrade memoryking`), then run the same `memoryking setup claude-code ...` again. With WSL2 it installs the matching version in the distro and restarts the daemon on it. With Docker, build or pull the new image, remove the old container (`docker rm -f memoryking`; the volumes and the store stay) and run setup with the new `--image`. Setup compares the image itself, not only its tag, and refuses a container from an older build with `store_version_mismatch`, naming both.

**Removing it.** `memoryking setup claude-code --project my-app --remove` puts Claude Code's settings files back byte for byte from setup's record in `%LOCALAPPDATA%\MemoryKing\setup` (or, for a file changed since, takes out only setup's entries) and puts back the user variables. It keeps the store, and prints how to remove it: in WSL2, stop the daemon there (`~/.local/share/memoryking/venv/bin/memoryking --profile default stop`) and delete `~/.memoryking` and `~/.local/share/memoryking`; with Docker, `docker rm -f memoryking` and `docker volume rm memoryking-data memoryking-control memoryking-home`. Removing the store deletes the memory.

**Not supported.** A store on Windows itself; a store on a Windows drive seen from WSL2 (`/mnt/c/...`) or a Windows folder mounted into the container (setup refuses with `store_filesystem_unsupported`, because those file systems do not keep the owner-only modes and durable renames the store relies on); WSL1; a machine where both WSL2 and Docker Desktop are unavailable or disabled; Windows setup for hosts other than Claude Code.

**If Docker Desktop will not start** after an unclean shutdown, with errors about files in its `run` folder that cannot be opened: quit Docker Desktop, rename `%LOCALAPPDATA%\Docker\run` and `%LOCALAPPDATA%\docker-secrets-engine` (both, together), and start it again; it recreates them.

## Shared PostgreSQL

The shared profile is a Python service backed by PostgreSQL 18.6. It exposes:

- JSON operations: `POST /v1/tenants/{tenant}/operations`.
- MCP: `/v1/tenants/{tenant}/projects/{project}/mcp` (lean tools; add `?profile=full` for the full catalog).
- Optional browser dashboard for one configured tenant, with separate cookie sessions.
- `GET /healthz` (liveness) and `GET /readyz` (readiness), described under [Operating the shared service](#operating-the-shared-service).
- `GET /v1/tenants/{tenant}/metrics`, Prometheus text behind that tenant's admin credential.

Use separate `mk_migration`, `mk_runtime`, `mk_worker` and `mk_backup` roles. Provision them with `memoryking.shared provision-roles`, migrate with the migration DSN, then initialize each tenant. Credentials and DSNs are private files. Remote PostgreSQL DSNs must use `sslmode=verify-full`; local private Unix sockets are also supported.

**Semantic search in the shared profile.** `requirements-shared.lock`, and so the shared image, carries the same libraries (onnxruntime, numpy, tokenizers) as the core install, but no model: an image that held a 130 MB model would make every image pull larger for a tenant that never indexes. Indexing runs in a worker on a machine you choose. Install the pinned model there once with `python -m memoryking.model_install --destination DIR` (it prints nothing but the verified result and contacts only the model host) and start `python -m memoryking.embedding_worker --model DIR ...`; without a worker the shared service searches by keywords.

Operator sequence (the paths below are placeholders for your reviewed configuration):

```sh
.venv/bin/python -m memoryking.shared provision-roles --dsn-file /private/bootstrap.dsn --role-secrets /private/roles
.venv/bin/python -m memoryking.shared migrate --dsn-file /private/migration.dsn
.venv/bin/python -m memoryking.shared init-tenant --dsn-file /private/migration.dsn --content /private/content --control /private/control --tenant team
.venv/bin/python -m memoryking.shared serve \
  --dsn-file /private/runtime.dsn --content /private/content --control /private/control \
  --bind 127.0.0.1 --port 8765 --host memory.example:8765 \
  --origin https://memory.example:8765 \
  --tls-cert /private/server.pem --tls-key /private/server.key
```

`--host` must match the actual request authority exactly and may be repeated. `--origin` may also be repeated. The illustrative hostname needs your own trusted certificate and routing; these commands do not provision DNS or publish a service. `--pool-size` defaults to eight runtime connections and `--max-requests` to 32 requests in flight (a request beyond that is refused at once as `service_busy`, HTTP 503). `--loopback-http` is a development-only alternative restricted to loopback bind and peer; proxy headers are not a substitute for authenticated transport.

For the dashboard, add `--dashboard-tenant team`. Bootstrap requires the configured tenant's wildcard administrator. The shared dashboard has the same Team view as the local one: per-principal activity, grant revocation (which advances the policy epoch and signs every browser session out) and the audit export; see the [reference](reference.md#dashboard-team-view).

To move a tenant to the current lexical ranking after an upgrade, run `.venv/bin/python -m memoryking.shared reindex --dsn-file /private/runtime.dsn --content /private/content --control /private/control --tenant team --credential /private/admin.credential --wait` (admin credential). The starting call is short at any size; with `--wait` the command does the batched rebuild itself and prints percent complete and an estimate on stderr until the tenant is `current`, and without it the serving process advances the rebuild between requests. `status` shows the same progress. The local equivalent is `memoryking reindex [--project P] [--wait]`.

Shared `request` accepts `--url`, `--tenant`, `--project`, `--credential` and optional `--ca-file`. Its stdin envelope is `{"version":1,"operation":"NAME","args":{...}}`. Grant/delegate output requires a new `--credential-output`; dashboard bootstrap requires `--bootstrap-output`. The [shared demo](../tools/shared_demo.py) is a complete disposable operator/client example.

### Tying agents to a user

Memory about a user lives in its own scope (`_user_<principal>`) and only the user and the agents that act for them can read it; see the design guide. An agent acts for a user when it holds a grant on that scope, which is a journaled grant like any other.

- **Local store.** The owner is the default user. Every credential the owner issues with `credential issue` acts for the owner when it holds `capture` or `read`. Pass `--for-user NAME` to make an agent act for someone else, or `--no-user` to keep a credential away from user memory. `memoryking user show` lists who acts for you.
- **Shared service.** Add `"for_user": "NAME"` to the `grant` request (or grant the principal on `_user_NAME` with `capture`, `read` and `export`). The user's own principal holds the scope with `review` and `policy` and can link and unlink their agents with their own credential. An agent acts for one user; a second link is refused (`user_link_conflict`).
- **Administrators.** A `*` grant never reaches a user scope. Administrators can link agents and can erase a scope with `memoryking user purge --user NAME --yes`, without being able to read it. To end an agent's access: `memoryking credential revoke AGENT --project _user_NAME`.

### Rotating credentials

A credential is a signed document naming the store, history, tenant and principal (and, in the shared profile, the service audience). Each principal also has a **generation**, stored with its grants and carried in the credential once it is above zero. A credential is accepted only when its generation is the principal's current one; otherwise it is refused with `credential_replaced`. The generation advances in two cases, both journaled in the signed control journal like every other policy change, so a restore replays them and they retire retained briefings the way a revoke does:

- **A principal loses its last grant** (`credential revoke` of the only grant it held; revoking one of several grants leaves the rest, and the credential, in place). Granting the principal again afterwards issues a different credential, and the old one stays refused.
- **An explicit rotation.** `credential issue NAME --project P --capabilities a,b --rotate --output NEW_FILE` restates the grant, advances the generation and writes the new credential; the old one is refused from that moment. Pass the principal's existing project and capabilities to leave its access unchanged. In the shared profile, send the `grant` operation through `memoryking.shared request` with `"rotate": true` in `args` and a new `--credential-output`. Without `--rotate`, issuing for an unchanged grant returns the principal's current credential, as before, so existing automation keeps working.

Distribute the new credential to the agent before or right after rotating; the old one stops at the commit, so there is a short interval in which the agent must be restarted with the new file.

**The owner credential** is rotated offline with `memoryking credential rotate-owner --store STORE --control CONTROL`, with the daemon stopped (it refuses while the store is open). The command writes the new credential durably as `owner.credential.next`, commits the new generation through the journal, then renames the file over `owner.credential`, so the working credential is on disk at every moment after the old one stops. If the command dies after the commit, the new credential is in `owner.credential.next`; run the command again and it completes the rename without rotating a second time. Copy the new file to wherever agents or profiles read the owner credential. **The shared profile's operator credential** is the file `CONTROL/TENANT/owner.credential`, written by `init-tenant` and `restore`. Rotate it with `.venv/bin/python -m memoryking.shared rotate-owner --dsn-file secrets/runtime.dsn --content CONTENT --control CONTROL --tenant TENANT [--audience NAME]` (the runtime role's DSN, as for `serve`). It needs no service stop: it is one ordinary tenant transaction that restates the credential holder's admin grant with `rotate`, which the `grant` operation cannot do because it never grants `admin`. It follows the same sequence as the local command (`owner.credential.next` first, commit, rename; run it again after an interruption) and refuses with `administrative_operation_required` unless the file holds a credential for a principal with a current tenant-wide admin grant. Copy the new file to wherever the operator keeps it; a copy of the old credential stops working at the commit, including for `end-maintenance` and the dashboard sign-in. The command prints the file path and never the credential.

**Upgrades.** Credentials issued before this release carry no generation and are treated as generation 0, so they keep working until their principal is revoked or rotated; nothing needs re-issuing. A principal at generation 0 gets exactly the credential it always did. SQLite stores migrate to schema 15 and shared stores to schema 10 when opened or migrated.

**Restore.** The generations are part of the backup (SQLite schema 10, shared archive schema 8) and the journal replays any later rotation. A restore still creates a new history and retires every credential. The credential it issues carries the principal's current generation as the restored database records it, in both profiles, so an admin that was rotated, or revoked and granted again, is not locked out of the tenant it restored; a credential minted for the new history at an older generation is refused with `credential_replaced`. Archives written before generations existed (shared archive schemas 6 and 7) still restore, with every generation at 0.

**Other entry points.** The same generation rule applies to `end-maintenance` (a rotated-away credential is refused with `credential_replaced`, and only after the grant check, so a principal without a current admin grant still gets `denied`) and to dashboard sign-in and sessions in both profiles: a rotated credential cannot issue a bootstrap code, exchange an existing one or keep a session.

**Delegation.** `delegate` (shared profile) lets a principal hand out a narrower copy of its own access, for example to a sub-agent, for at most an hour. The holder of the token acts as the issuer, never as the principal named in the request: reader lists, checkpoint ownership, hand-off recipients, receipts and the event actor all see the issuer. The token is limited to the one project and the capabilities named, without `admin`, and cannot delegate again. The named principal is the party the work is done for: it must hold every delegated capability when the token is issued, and it is the only identity the token never becomes. The issuer's grant is checked on every use, so narrowing or revoking it narrows or retires the token at once. The token also records the generations of the issuer and the named principal when it is issued, so rotating or revoking either retires it. The named principal is carried in the signed token, not in event or receipt rows, which have no field for it; the delegation id in the token's chain is what a receipt binding records.

**Delegated tokens issued before this release are refused.** They acted as the named principal, which let any holder of `read` on a project read, and write checkpoints as, an agent it was never granted. They now fail with `delegation_superseded`; the issuer delegates again. Nothing else needs re-issuing. The local profile has no delegation.

**Expiry.** `--expires` sets the grant's expiry. It is enforced by the store against its own clock on every authorization, not by the credential file, so a credential cannot outlive its grant and extending the grant revives the same credential; rotate to retire it instead.

## Docker evaluation

The [Compose stack](../deploy/shared/compose.yaml) builds the application from source and pins its base images. It publishes only loopback HTTPS. PostgreSQL has no TCP listener; API/worker/operator communicate through a private Unix-socket volume. Content, current control state, database files, archives and role secrets are separate mounts.

From the contributor environment with a working Docker engine and Compose:

```sh
.venv/bin/python tools/shared_compose_prepare.py --directory tmp/shared-evaluation
docker build --force-rm -f deploy/shared/Dockerfile -t memoryking-shared:local .
docker compose --env-file tmp/shared-evaluation/compose.env -f deploy/shared/compose.yaml up -d postgres
docker compose --env-file tmp/shared-evaluation/compose.env -f deploy/shared/compose.yaml run --rm operator provision-roles --dsn-file /secrets/bootstrap.dsn --role-secrets /secrets/roles
docker compose --env-file tmp/shared-evaluation/compose.env -f deploy/shared/compose.yaml run --rm operator migrate --dsn-file /secrets/migration.dsn
docker compose --env-file tmp/shared-evaluation/compose.env -f deploy/shared/compose.yaml run --rm operator init-tenant --dsn-file /secrets/migration.dsn --content /content-volume/data --control /control-volume/data --tenant demo
docker compose --env-file tmp/shared-evaluation/compose.env -f deploy/shared/compose.yaml up -d gateway
```

Preparation requires a new private directory. Wait for PostgreSQL health before provisioning roles. Keep the explicit `--env-file` on every command. `MK_STATE`, `MK_UID`, `MK_GID` and `MK_PORT` are generated in that file; the default port is 8765. A private one-day test certificate is generated. Clients explicitly trust `secrets/api/test-cert.pem` through `--ca-file`; do not bypass browser certificate warnings. This profile is a local rehearsal, not an internet-ready deployment template.

The owner credential is in `tmp/shared-evaluation/control/data/demo/owner.credential`. A worker needs its own project-scoped `read,jobs` credential at `secrets/worker/worker.credential`. The one-shot worker handles only `audit_probe`; repository and embedding workers are separate entry points. The local daemon drains `audit_probe` jobs between requests and the shared service drains up to 64 after each capture, so the scheduled worker is a safety net for jobs created by other paths and for a tenant that only reads; it reports `idle` when there is nothing to claim.

Database roles are least privilege: the worker role can insert receipts but not change or delete them, and cannot write tenant counters directly; the commit serial advances only through the owner-run `next_commit_serial` function. Row-level security is keyed on a session setting the client sets itself, so treat it as defense in depth behind the role grants, not as the sole boundary. Restore refuses a migration role that is a superuser or has `BYPASSRLS` (the runtime pool already refuses such roles), and every statement in restore and in the shared result expiry names its tenant instead of relying on row-level security alone. The shared service answers a request for an unknown tenant exactly as it answers a bad credential for a known one, so responses do not reveal which tenants exist, and a lock or statement timeout (or deadlock) during a request returns `service_busy` with 429 rather than an internal error; retry after a pause.

If a backup process dies after fencing its tenant, the anchor stays in `maintenance` and every request answers `recovery_required`. Clear it with the operation id the backup recorded (`maintenance_operation` in the anchor), presenting a current admin credential:

```sh
.venv/bin/python -m memoryking.shared end-maintenance --dsn-file secrets/runtime.dsn --control CONTROL --tenant demo --credential CONTROL/demo/owner.credential --operation OPERATION_ID
```

Any other operation id, an expired or non-admin credential, or a tenant that is not fenced is refused.

Stop the stack while preserving its data:

```sh
docker compose --env-file tmp/shared-evaluation/compose.env -f deploy/shared/compose.yaml down
```

The reusable test helper is `tools/shared_compose_rehearsal.py --compose /absolute/path/to/docker-compose --directory NEW_STATE --output NEW_LOGS`. It runs the role/API/worker/crash/archive/restore/migration sequence, records results and stops its containers. API, gateway and database have bounded `on-failure:3` restart policies; exhausted retries need diagnosis. The shared-profile image is not published; there is no Kubernetes chart or managed cloud installer.

## Backup and recovery

The daemon can schedule its own backups (`serve --backup-dir`, above). For a manual backup or a restore, stop the local daemon first:

```sh
.venv/bin/python -m memoryking backup --store "$HOME/.memoryking-data" --control "$HOME/.memoryking-control" --backup "$HOME/.memoryking-backup-001"
.venv/bin/python -m memoryking control-checkpoint --store "$HOME/.memoryking-data" --control "$HOME/.memoryking-control"
.venv/bin/python -m memoryking restore --store "$HOME/.memoryking-data" --control "$HOME/.memoryking-control" --backup "$HOME/.memoryking-backup-001"
```

Backup uses SQLite's online backup API, referenced blobs, a `blob-index` file listing them and a signed integrity manifest that commits to the index by SHA-256 and count (the signing profile bounds a document at 1 MiB, so the manifest never embeds the list, and a store of any size can be backed up). Restore verifies the manifest signature, the index against its committed hash and every blob against the index. Backups written by earlier releases, which embed the list or record blobs without the hash-prefix directory, still restore.

The store lock is held only to snapshot the database and list the live blobs; blob files are immutable and named by their hash, so they are copied afterwards while requests run, each hashed as it streams and fsynced once. `memoryking backup --incremental-from PREVIOUS` (and every scheduled backup) hard-links a blob the previous backup holds only after reading that copy and checking its hash, never because of its name; a previous backup whose manifest does not verify is ignored and everything is copied. A blob that a purge deletes during the copy is left out of the backup (the restore replays the purge); a missing blob that an observation still names fails the backup as `blob_corrupt`. Hard links need the backups on one filesystem; otherwise files are copied. Blob files live under `blobs/<tenant>/<first two hex digits>/<hash>`; a store written by an earlier release keeps working with its flat directory, and the daemon moves files into their shard directories a few at a time (each move is one atomic rename, so stopping at any point loses nothing). Purge deletes the purged observation's own file unless another observation names the same content; `memoryking gc` is the explicit full sweep for files a crash left behind, and a start sweeps only when a purge was interrupted. A scheduled backup that fails for any reason is logged, removes its partial directory and waits before retrying. It excludes credentials and the independent control tree. Retain the **current** control identity/journal/anchor separately; do not restore old control files alongside old content. Retain the emitted current journal checkpoint independently if lost-anchor recovery is required.

Restore verifies the archive, replays current revocations/deletions and creates a new history and owner credential. Old credentials, request namespaces, watermarks and handles are retired. Reconcile acknowledged work that is absent from the backup before issuing new retries. Optional indexes, adapter bases, procedure activation and ranking need their documented fresh-history rebuild/reconciliation.

If the anchor alone is missing, `restore --lost-anchor-journal-head CURRENT_INDEPENDENT_64_HEX_HEAD` requires a genuinely independently retained current checkpoint, intact identity and matching journal. Missing or rolled-back evidence fails closed. Do not derive that argument from a potentially rolled-back local journal.

The store also refuses to open (`journal_rollback`) when the database has applied more journal records than the control journal holds, for example after a power loss that kept the database commit but lost the journal's tail, or when a copied control tree from an earlier moment is paired with a newer database. Control files and backup files are flushed with `F_FULLFSYNC` on platforms that provide it (macOS), the same strength as SQLite's `fullfsync` setting, and with `fsync` elsewhere; the macOS path has not been exercised on real hardware in this repository's tests.

Shared backup uses the runtime and separate backup DSNs plus current tenant owner credential. Restore uses the migration DSN:

```sh
.venv/bin/python -m memoryking.shared backup --help
.venv/bin/python -m memoryking.shared restore --help
```

The signed per-tenant archive is not a PostgreSQL physical backup/PITR stream. It bounds rows at 1 MiB, the signed manifest at 1 MiB (the blob list lives in a hash-committed `blob-index` file, which is not bounded) and payload at 16 GiB. Backup fences the tenant only while the repeatable-read snapshot is established (the time it takes to take the exclusive gate and run one query, requests in that window answer `recovery_required` or `service_busy`); rows are then dumped and blob files copied from that snapshot while requests run, and `--previous ARCHIVE` hard-links the blobs an earlier archive of the tenant already holds after verifying their hashes. The archive keeps blobs under `blobs/<first two hex digits>/<hash>`; flat archives from earlier releases still restore. Whatever PostgreSQL consistency needs beyond that (a physical backup or point-in-time recovery) is not part of this archive. Interrupted restore remains fenced until explicit recovery succeeds. Other tenants retain their own state.

### Journal cost and records that fail to apply

Every policy decision is a signed, hash-chained line in the control journal. Tool-operation records (intents and their transitions) used to share it, at about three lines of roughly 1.3 KB per operation, and nothing compacted it. They now have their own log (see [The operations log](#the-operations-log)), so the journal grows only with policy decisions: 1,000 tool operations added 3.9 MB to the journal before and add nothing to it now. Verifying a journal from nothing costs about 0.03 s at 1,000 records, 0.3 s at 10,000, 0.9 s at 30,000 and 3 s at 100,000 (measured on a development host; yours will differ).

A running process does not repeat that work. It keeps, per control tree, the position the verified journal ends at and the hash of every verified line, and a request or an append reads and verifies only what was added after that position (the shared service keeps one per tenant, so a store built for each request still starts from it). The cache is used only when the file is the same one, is not shorter, still ends in the line whose hash is the cached chain head and, if its size is unchanged, has not been written since; anything else is verified again from the first record. Every call still compares the anchor's sequence and head with the journal, so a truncated or rolled-back journal is refused with `journal_rollback` exactly as before, and torn-tail and damage rules are unchanged. A cold process still verifies everything once: the shared service does that for each tenant in the background right after it starts (`journal-warm`), and a request that arrives first does it itself. What the cache does not do is notice damage inside a prefix it already verified: that is found at the next start, by `doctor`, and by every restore, which verify the whole journal. Measured per call at 1,000 / 10,000 / 30,000 / 100,000 records: a shared request went from 28 / 291 / 870 / 3,322 ms to under 1 ms, and a local append from 4 / 27 / 101 / 322 ms to 1 / 2 / 2 / 3 ms (the append's two fsyncs remain).

A journal record is applied to the database in this order: written to the journal, applied, then the anchor advances over it. When a record is found unapplied (a crash, or a request that gave up), it is applied before the next write without that request's five-second budget, one record per transaction, so a long purge can finish and the records that did commit stay applied if a later one fails. Reads cannot apply records and answer `journal_replay_required` until a write has. Locally, a policy record (a purge, for example) that was journalled but ran out of its budget is applied immediately and the daemon keeps serving; any other failure after a journal write, and any operation record, still stops the daemon until it is restarted, and the restart applies the record (an operation whose acknowledgment was lost must be reconciled after that restart).

A record that fails to apply is retried by every later write and never skipped: a policy record may be a revocation or a purge, and skipping one is a security decision. To see what is stuck, stop the local daemon and run `memoryking journal pending --store STORE --control CONTROL`, or run `.venv/bin/python -m memoryking.shared journal-pending --dsn-file secrets/runtime.dsn --control CONTROL --tenant TENANT` (no service stop needed). Both print one JSON document: how many records the journal holds, how many the database has applied, and for each pending record its sequence, kind, tenant and the error code from the last attempt (`null` before any attempt), never the record's content; an `operations` member lists the same for the operations log. They apply, change and skip nothing. A record that keeps failing for a reason a restart cannot cure (`journal_tenant_missing`: the database is not the one the journal was written for) is repaired by the recovery path above, a restore from a backup with the current independently retained control tree, not by editing control files. There is no command that skips a record.

### The operations log

Operation intents and transitions are signed, hash-chained records in `CONTROL/operations/NNNNNNNN.log`, rolled by size (4 MiB). Each record names the hash of the one before it, across segments. A segment that is full ends with a signed trailer naming the head before it, its record count and its lowest and highest sequence. The control anchor carries `operations_sequence` and `operations_head`, and every use of the log compares them with it exactly as the journal head is compared, so a truncated, replaced or rolled-back log fails closed with `journal_rollback`, and a broken chain or trailer with `journal_invalid`. The database remembers how far it has applied the log (`meta` key `operations`); records are applied the way journal records are, outside the request budget, and a restore replays the log with the same incarnation and revision rules as before, so a restored store never silently authorizes repeating an external action.

Stores that already have operation records keep them in the journal. Those records stay valid and are still replayed from there; only new operation records go to the log, so no signed history is rewritten and an old backup restores as it always did.

**Compaction.** The log is compacted by dropping whole sealed segments, never the one being written. A segment goes only when (1) it lies at or below the operations position of the OLDEST retained backup, so restoring any retained backup still finds its intents, and (2) every operation in it is resolved and reconciled in the current history (a succeeded or failed operation, not one that is intended, dispatching, unknown or waiting for reconciliation). An unresolved or ambiguous operation is never dropped. With no backup, nothing is compacted. The local daemon compacts after each scheduled backup (`serve --backup-dir`), so the "oldest retained backup" is the oldest complete one in that directory. The shared profile has no scheduler: run `.venv/bin/python -m memoryking.shared compact-operations --dsn-file secrets/runtime.dsn --content CONTENT --control CONTROL --tenant TENANT --credential owner.credential --archive-dir DIR`, where DIR holds the retained archives; only archives whose signed manifest verifies for this store count, a backup older than the position compaction reached is refused at restore with `backup_invalid`, and a backup written before this release counts as position 0 and so keeps everything.

**Consumed identities outlive compaction.** Before a segment is dropped, the operation identities it consumed are added to `operations/consumed-<sha256>`, an index that a signed `operations/base` file names by hash and count (the base also records the sequence and head the retained log continues from). Every intent checks it, so an identity is never reused, whatever backup is restored later. The index grows by about 40 bytes per operation and is never pruned.

Lost-anchor recovery (`restore --lost-anchor-journal-head`) has an independently retained checkpoint for the journal only; the operations log is accepted as its own chain, as found.

### The instance lease

Two processes serving one tenant from copies of the same control tree would each have a valid signing key and a valid-looking history, and could fork it. A lease recorded in the database guards against that mistake. It is a row `{holder, renewed_ms}` per tenant (per store, locally). The holder is a hash of the operator's `--instance-id` when one is given, otherwise of the control directory's device and inode.

- Processes that share one control tree on one volume compute the same holder and **co-hold** the lease; the shared profile legitimately runs several API processes per tenant.
- A process with a different holder is refused with `control_lease_held` until the recorded renewal is older than **60 seconds**. A restart after a crash has the same holder, so it is never blocked.
- The holder renews at most every **15 seconds**, inside transactions that already write. A tenant with no row claims the lease on its first writing transaction. Read-only requests and the shared worker, whose database role cannot write the lease, check it but never write it; the next runtime write claims or renews it, and a holder that was idle for more than 60 seconds renews it on its next write. The check runs before any journal record is applied.
- A restore resets the lease, so a restored store starts again.

This protects against accidents, such as a copied control tree started by mistake. It does not protect against a malicious holder of the copy, who has the signing key and can write anything, including a lease row. **A deployment on more than one host or container must set the same `--instance-id` on every process** (`serve`, `worker-once`, `backup`, `compact-operations`, `rotate-owner`), because device and inode are not comparable across hosts: without it, processes on different hosts compute different holders and refuse each other. Offline commands that open the store, such as `rotate-owner`, need the same id as the running instance. A copied store started on purpose, for example for disaster recovery, waits up to 60 seconds after the original's last renewal, or is restored from a backup.

### Clock recovery

The store never accepts a time earlier than the latest time it recorded: that record lives in the control anchor, outside the database, so a rolled-back clock cannot replay expired grants or reopen revoked access. The consequence is that a wall clock which jumped forward (a bad NTP source, a manual change, a virtual machine resumed with a wrong time) and was then corrected leaves the recorded time ahead of the real one. Until real time reaches it, every request answers `clock_untrusted`.

Diagnose it with `memoryking doctor --store ... --control ...`. The `clock` check fails and says how far ahead the recorded time is (for example `3 h 5 min`); an `ok` result means the clock is not the cause. Then:

1. Make sure the system clock is correct now (`timedatectl status`, `chronyc tracking`) and stays so. Do not move it forward to match the record.
2. Wait. The store recovers by itself the moment real time passes the recorded time; no restart is needed. Grants and credentials are not extended or shortened by the wait. If the offset is small, this is the whole procedure.
3. If the offset is too large to wait out, there is no lighter override and none should be added, because one would also remove the rollback protection. The only existing path is a full restore under [Backup and recovery](#backup-and-recovery): restore creates a new history incarnation (old credentials, namespaces, watermarks and handles are retired) and sets the recorded time to the current clock. A local restore does this directly; a shared restore refuses while the clock is behind the record, so it needs the lost-anchor form (`--lost-anchor-journal-head`, which requires an independently retained current journal checkpoint and an absent anchor). Use it only when the clock is known to be right.

The shared profile has the same rule per tenant (`doctor` covers the local profile only).

### Operating the shared service

**Health.** `GET /healthz` answers `{"status":"ok"}` while the process runs. `GET /readyz` answers 200 `{"ready":true}` or 503 `{"ready":false}` after checking that the database answers, the control tree is readable and the database's schema version is one this build accepts; the answer is cached for two seconds, and neither endpoint takes a credential, checks the `Host` header (a load balancer probes by address; TLS is still required) or says which check failed. The local daemon serves the same two paths on its optional loopback ports: `serve --health-port PORT` (a port of its own, nothing else on it) and the dashboard port.

**Metrics.** `GET /v1/tenants/{tenant}/metrics` with `Authorization: Bearer <that tenant's admin credential>` (no cookie) returns Prometheus text. The process-wide counters carry no tenant, principal or content: requests and errors by operation and code, a latency histogram per operation (including `gate_wait`, the time requests spend waiting for the tenant's transaction gate, and `housekeeping`), and HTTP responses by status. The gauges are that tenant's own: every capacity bound with its use and limit, jobs by state, position and frontier, `memoryking_journal_bytes`, `memoryking_backup_age_seconds` (from the `last-backup` file a completed backup writes in the tenant's control directory; -1 when there is none), `memoryking_housekeeping_age_seconds`, readiness per check, the database pool (open, idle, waiting, maximum) and `memoryking_concurrency` (limit and use of `http_requests` and `database_pool`). The local `/metrics` carries the same families with `local_workers` and `local_waiters`. Counters reset on restart.

**Briefings and the gate.** Briefings and captures share a tenant's transaction gate; a policy change, purge or restore waits for those in flight and is served before any that arrive after it. `gate_wait` therefore stays near zero for briefings and rises only for a request that needs the gate alone. A briefing that needs a model (the query embedding or the reranker) runs it with no gate or connection held, so a slow model costs its own caller only. Connection role and durability checks run when a pooled connection is first used and every five seconds after, not on every request.

**Anchor interval (opt-in, off by default).** Every request that commits anything, a receipt included, writes the control anchor (two fsyncs) before its response is released. `serve --anchor-interval-ms N` (local and shared; 0, the default, is this strict behaviour; at most 60,000) lets the commit serial and the clock's high-water mark of a request that moved no tenant position be written up to N milliseconds late; positions, journal records and operations are still anchored first. What you give up for the throughput: (1) a database rolled back to a copy taken less than N ms before a crash, losing only receipts delivered in that window, is not detected, and (2) after a restart, a clock that went back by less than N ms is not refused. A rollback behind any position, journal record or operation is refused in both modes. `status` shows `anchor_interval_ms`; a non-zero value means the store runs relaxed.

**Concurrency.** Shared: `serve --max-requests` (default 32) and `--pool-size` (default 8). Local: `serve --workers` (default 8, 1 to 256) and `--waiters` (default 16, 0 to 1,024; each waits at most five seconds). A request beyond the limits is refused immediately as `service_busy`.

**Housekeeping.** The service runs retention on a thread of its own, at start and then hourly, for every tenant under the control directory: it expires results, receipts, usage, audit rows and summaries, deletes finished jobs and old artifact tombstones in bounded batches, finishes an unfinished lexical rebuild, moves flat blob files into their shard directories and, once a day, removes orphaned blob files. It acts as a reserved principal with no grants, waits at most 30 seconds for a pool connection, logs each failure (error type and code, never content) and carries on with the next tenant, so a tenant with no traffic is still maintained and a busy pool delays a pass rather than ending it.

**Rolling upgrade.** A service accepts a database at exactly its own schema version. It also accepts a database one version ahead when that migration is listed as additive (`ADDITIVE_MIGRATIONS` in `postgres_admin.py`): one that only adds tables, columns or indexes this build ignores, and after which nothing this build writes is read differently by the newer one. For such a migration the order is: take a backup, run `migrate` with the new release, then restart services one at a time (old services keep serving while the database is one version ahead). Migration 13 to 14 is not listed: the newer service finds the receipts to delete on a purge through a table the older one does not fill, so a mixed fleet could leave a receipt behind. For it, and for any migration not listed, stop every service, run `migrate`, start the new release. A service that meets any other version refuses the request as `schema_unsupported` (HTTP 503), and the reply's hint names both versions: the database's and the one the service needs.

## Upgrades, retention and removal

Current storage schemas are SQLite 19 and PostgreSQL 14 (archive schema 11). Keep the installed revision, wheel/image hashes, locks, content archive and current control evidence before an upgrade. Test on a disposable copy first. Local upgrades run under exclusive store ownership; shared schema migration runs before API/workers start, using the migration role.

Do not run an older binary against a newer schema or manually lower a schema version. Prefer a compatible roll-forward; use verified fresh-history restore when recovery is needed. There is no automatic reverse migration or multi-host failover.

`gc` (offline local, and once a day on the shared service's own housekeeping thread) expires retained result bodies and removes unreferenced blobs; consumed request keys remain consumed. Purge and retention previews are privileged operations with explicit dependency effects. They do not erase copies already exported or delivered. There is no installed backup/retention scheduler.

### Enabling plan tracking

Structured plans use the existing core store and checkpoint permissions; there is no additional service, port, provider or configuration flag. Upgrade the service and its bundled dashboard together, then reconnect MCP clients so their tool catalog includes `memory_plan`; updating a client alone is not enough, because the installed service must accept the plan commands. Existing checkpoints stay compatible, and plan revisions use the same archive, restore and purge paths. An older binary rejects plan-bearing checkpoints, so keep the matching service version when restoring an archive that contains plans.

Stop services before removing runtime environments or images. Removing software does not remove data, control, archives or migration bundles. Keep or deliberately dispose of those separately under your retention policy.

[Back to README](../README.md) · [Reference and troubleshooting](reference.md) · [Security](../SECURITY.md)
