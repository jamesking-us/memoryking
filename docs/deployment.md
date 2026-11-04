# Deployment and configuration

Start with the local profile unless you need several machines or tenants. These are evaluation deployments; production performance, failover and recovery service levels are not qualified.

## Installation profiles

All profiles use Python 3.12. Dependencies are hash-locked; package source is installed separately with `--no-deps`.

| Lock | Purpose |
| --- | --- |
| `requirements-core.lock` | Local service, Python client, stdio bridge and dashboard. |
| `requirements-build.lock` | Build a wheel from the source checkout. |
| `requirements-shared.lock` | PostgreSQL driver/pool and shared HTTP/MCP runtime. |
| `requirements-tokenizers.lock` | Optional exact `cl100k_base` token counting. |
| `requirements-embeddings.lock` | Optional local CPU embedding runtime; model files installed separately. |
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

The shared-profile image under `deploy/shared/` is published the same way when needed. After the first upload, the getting-started guide's install becomes `pip install --pre memoryking` followed by `init`, `profile add`, `serve` and `dashboard open`.

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
| `--profile NAME`, `--config FILE` | Fill omitted `--store`, `--control`, `--socket`, `--credential` and `--dashboard-port` from a named profile in `~/.memoryking/config.toml` (`MEMORYKING_PROFILE`, `MEMORYKING_CONFIG`). The file holds paths only. `profile add NAME --store ... --control ...`, `profile list` and `profile remove NAME` maintain it. Explicit flags always win. |
| `--store`, `--control` | Required paths for initialization, serving and offline maintenance. Neither may contain the other. |
| `init --tenant`, `--principal` | `main` and `owner`; IDs are 1–96 ASCII letters, digits, `_` or `-`. |
| `serve --dashboard-port` | Dashboard disabled unless set. `0` chooses a free port; otherwise 0–65535. Always loopback. |
| `serve` housekeeping | The daemon expires replay results, artifacts, usage rows and access receipts past their 30-day retention once an hour, and completes audit jobs between requests; `gc` remains available for offline use. |
| `serve --dashboard-port` metrics | With the dashboard enabled, `GET /metrics` with a bearer admin credential returns Prometheus text (requests, errors, latency, capacity, jobs, frontier) for scraping. |
| `serve --backup-dir DIR [--backup-every 1d] [--backup-keep 14]` | The daemon backs up into `DIR/backup-<timestamp>` when the newest complete backup is older than the interval, checked between requests, and keeps the newest N. Only complete backups it recognises are pruned. A failed backup leaves no partial copy and is retried after the interval, at most hourly, rather than every minute. A profile may set `backup_dir`, `backup_every` and `backup_keep`; `doctor --backup-dir DIR --backup-every 1d` warns when none exist and fails past twice the interval. The control tree is still retained separately. |
| `serve --embedding-model DIR` | Loads the installed local model; the daemon indexes every enabled project and embeds briefing queries itself. A profile may set `embedding_model`. `embeddings enable|status --project P` manage a project's partition. |
| `serve --reranker-model DIR` | Loads the installed local cross-encoder; a briefing that asks for `rerank` has its top candidates reordered by it. A profile may set `reranker_model`. Install with `python -m memoryking.model_install --model reranker --destination DIR`; `doctor --reranker-model DIR` verifies it. |
| `serve --log-level` | `warning` by default. Redacted operator log lines on stderr: component, event, request id, error type and code location, never content, SQL or paths. An `internal_error` reply carries the `request_id` of its line. |
| `serve --trace` | One `event=trace` line per request at info level (the level is lowered to info if it was higher): for a socket request the operation, the command action, the outcome code, elapsed milliseconds, time spent waiting for the store lock, time inside transactions, the number of transactions and the response size; for a dashboard request the method, the route family (such as `/api/receipts`, never the project or receipt id), the status and the same timings. Nothing in a trace line names content, a project or a principal. The shared service accepts the same flag. |
| `serve --egress-policy FILE` | Loads the private egress policy below; without it the only model provider is the local recorder and nothing leaves the machine. A policy change needs a restart and retires every open conversation. The shared service accepts the same flag. |
| `serve --summary-idle 30m` | Between requests the daemon derives a "where this left off" summary, as the owner, for every task whose latest checkpoint has been idle this long and has none; `0` disables. Summaries follow the 30-day retention. |
| `summarize --project P --task T [--run R] [--provider NAME --model M --egress-policy FILE] [--json]` | Derives a summary with its own receipt: extractive by default, or written by a policy adapter through the audited inference path. Needs a credential with `checkpoint` (and `inference` for `--provider`). |
| `egress-audit --project P [--before MS] [--json]` | Lists what left the store for a provider in the last 30 days: when, status (`delivered`, `failed` or `pending`), provider, model, host, classification, bytes, cited receipts and permit. Needs a credential with `inference`. |
| `status --socket`, `--credential` | Human summary of store health through the running daemon: history, position, frontier, jobs and capacity bars; `--json` prints the raw result. Needs an admin credential. |
| `health --project P`, `--days N`, `--json` | Whether memory is paying for itself in one project: helpful feedback, empty briefings, memory never read, lost evidence and the review backlog, as counts with fixed thresholds (see the [reference](reference.md#memory-health)). Needs an admin credential. |
| `--version` | Prints the installed package version. |
| `doctor --store`, `--control` | Checks Python, directory ownership and layout, socket path, schema version, journal integrity and tokenizer; with `--socket` and `--credential` it also reports capacity and frontier from the daemon. Exit 1 when a check fails. |
| `credential issue NAME --project P --capabilities a,b [--expires 30d] --output FILE` | Issues a scoped credential into a new private file. `credential list` shows every grant with validity; `credential revoke NAME --project P` removes one. All take `--socket` and an admin `--credential`. |
| `capture --project P [--text T] [--source S] [--event E]` | Saves one observation from `--text` or stdin with a stable event id; `recall --project P --query Q` prints a cited Markdown briefing (`--json` for the packet). Both accept `--profile` or `MEMORYKING_SOCKET`, `MEMORYKING_CREDENTIAL` and `MEMORYKING_PROJECT`. |
| `dashboard open --socket`, `--credential` | Issues a one-use code and opens the dashboard signed in; `--print-only` prints the link instead. The code travels in the URL fragment and expires in 60 seconds. |
| `request --socket`, `--credential` | Required socket and private credential file paths. |
| `request --credential-output` | New private output file required for a grant; secret values are not printed. |
| `dashboard-login --output` | New private file containing a one-use code; expires after 60 seconds. |
| `--backup` | New external directory for backup; verified existing directory for restore. |

Paths/runtime changes require restart. Permission changes apply transactionally. Keep owner credentials out of agent prompts and browser pages; issue scoped credentials for normal agents. Files use owner-only modes (directories 0700, sensitive files 0600).

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

## Shared PostgreSQL

The shared profile is a Python service backed by PostgreSQL 18.6. It exposes:

- JSON operations: `POST /v1/tenants/{tenant}/operations`.
- MCP: `/v1/tenants/{tenant}/projects/{project}/mcp`.
- Optional browser dashboard for one configured tenant, with separate cookie sessions.

Use separate `mk_migration`, `mk_runtime`, `mk_worker` and `mk_backup` roles. Provision them with `memoryking.shared provision-roles`, migrate with the migration DSN, then initialize each tenant. Credentials and DSNs are private files. Remote PostgreSQL DSNs must use `sslmode=verify-full`; local private Unix sockets are also supported.

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

`--host` must match the actual request authority exactly and may be repeated. `--origin` may also be repeated. The illustrative hostname needs your own trusted certificate and routing; these commands do not provision DNS or publish a service. `--pool-size` defaults to eight runtime connections. `--loopback-http` is a development-only alternative restricted to loopback bind and peer; proxy headers are not a substitute for authenticated transport.

For the dashboard, add `--dashboard-tenant team`. Bootstrap requires the configured tenant's wildcard administrator. The shared dashboard has the same Team view as the local one: per-principal activity, grant revocation (which advances the policy epoch and signs every browser session out) and the audit export; see the [reference](reference.md#dashboard-team-view).

Shared `request` accepts `--url`, `--tenant`, `--project`, `--credential` and optional `--ca-file`. Its stdin envelope is `{"version":1,"operation":"NAME","args":{...}}`. Grant/delegate output requires a new `--credential-output`; dashboard bootstrap requires `--bootstrap-output`. The [shared demo](../tools/shared_demo.py) is a complete disposable operator/client example.

## Docker evaluation

The general shared rehearsal below is separate from the private home preview.

## Private home preview

The opt-in [home Compose profile](../deploy/home/compose.yaml) runs the restricted
phone viewer and optional report worker in one application container, alongside
an unenrolled Tailscale sidecar. It publishes no host ports. SQLite is the default;
an optional [PostgreSQL overlay](../deploy/home/postgres.yaml) connects through a
private Unix socket. The home runner opens no administrative HTTP/MCP gateway.
Your existing local installation is separate and is not imported automatically.

This is an evaluation preview. Local Mac Docker rehearsals cover both stores;
real Android, GitHub, Tailscale account/ACL/HTTPS setup and final qualification are
still pending. Do not forward a router port or enable public Funnel. Host sleep,
reboot, internet failure and Docker downtime make the service unavailable. Starting
Docker after a reboot also requires the normal Mac/Docker Desktop login lifecycle;
this profile does not promise unattended availability.

For a **new disposable installation**, use a clean checkout of a reviewed commit
and keep its full revision as the image tag. Docker Desktop and Compose must be
running. These commands create new named volumes; do not use bootstrap on an
existing installation:

```sh
export MK_HOME_TAG="$(git rev-parse HEAD)"
docker compose -f deploy/home/compose.yaml build app gateway
docker compose -f deploy/home/compose.yaml run --rm --no-deps bootstrap
docker compose -f deploy/home/compose.yaml up -d --no-build --wait gateway app
docker compose -f deploy/home/compose.yaml logs app
```

The application emits `"ready": true` after its configured components start.
Compose's running state alone is not application readiness. Fresh bootstrap creates
private disabled viewer/report configuration, a fresh store and an owner credential;
it never prints credential values. Tailscale remains in `NeedsLogin` with no tailnet
identity. Nothing is yet reachable from a phone.

For a fresh PostgreSQL installation, use a separate Compose project name and
include both files **on every command**. Build the database image too, prepare the
private role secrets, start the database, then initialize MemoryKing before the app:

```sh
docker compose -p memoryking-home-pg -f deploy/home/compose.yaml -f deploy/home/postgres.yaml build app gateway postgres
docker compose -p memoryking-home-pg -f deploy/home/compose.yaml -f deploy/home/postgres.yaml run --rm --no-deps bootstrap
docker compose -p memoryking-home-pg -f deploy/home/compose.yaml -f deploy/home/postgres.yaml up -d --no-build --wait postgres
docker compose -p memoryking-home-pg -f deploy/home/compose.yaml -f deploy/home/postgres.yaml run --rm --no-deps bootstrap postgres-init
docker compose -p memoryking-home-pg -f deploy/home/compose.yaml -f deploy/home/postgres.yaml up -d --no-build --wait gateway app
```

The database has no TCP listener or Docker network. Its health check authenticates
and reads the expected database. A partially initialized database needs explicit
operator diagnosis; bootstrap refuses to silently reuse existing roles or stores.
The runtime DSN cannot use TCP, multiple hosts, service indirection or
`PGHOSTADDR`/`PGSERVICE`/`PGSERVICEFILE` environment fallback.

### Home boundaries and configuration

| Component | Configuration and bound |
| --- | --- |
| App | UID/GID 10001, read-only root, no extra capabilities, no-new-privileges, 1 GiB memory, 2 CPUs, 96 processes, 512 MiB private temporary filesystem. |
| Gateway | UID/GID 10002, userspace networking, no TUN device or added capabilities; 256 MiB memory, 1 CPU, 64 processes. Only its own persistent state is mounted. Log uploads are disabled. |
| PostgreSQL | Optional, 512 MiB memory, 1 CPU, 96 processes, 32 connections; runtime pool defaults to eight, accepted range 2-16. Socket owner-only permissions and role authentication both apply. |
| Viewer | `/secrets/viewer.json`, disabled initially; loopback 8772 with a fixed HTTPS public origin. Use the existing closed viewer configuration and GitHub setup below. |
| Reports | `/secrets/reports.json`, disabled initially; one renderer at a time, ten pending builds, 120-second render deadline and 512 MiB child RSS limit. App-only reporting needs no SMTP credential. |
| Secrets | Private named volume; app sees it read-only. Gateway sees none. PostgreSQL administration/backup credentials are on a separate operator/database-only volume. |
| Data | Separate named data, content-addressed artifact, independent control-history and archive volumes. Rendered report files share the canonical artifact store; the browser never receives a raw filesystem URL. |
| Stop | Up to 240 seconds for bounded work to drain. Wait for actual process exit before copying, restoring or changing storage. |

The image pins Python, Tailscale and optional PostgreSQL base digests; Python
dependencies are hash locked. Preserve the built image ID and wheel hash as well
as the source revision. Supported renderer confinement on this profile is Linux
x86-64 with Landlock ABI 6 or newer; unsupported kernels/architectures fail closed.
Container behavior on an Apple Silicon host has not been qualified by an Intel Mac
result. Root, the Docker administrator and the service owner are trusted.

The native entry point is `python -m memoryking.home --data PATH --control PATH`.
Its backend defaults to `local`; `--backend shared` requires `--dsn-file PATH` and
accepts `--pool-size 2..16`. Optional `--viewer-config PATH` and `--report-config PATH`
use the same private closed configuration contracts as the existing service.
The CLI opens no HTTP listener when viewer configuration is absent or disabled; local
Unix IPC remains available to authorized same-host clients. Startup checks the
current shared schema and control history even when the viewer is disabled.

To reproduce the isolated container checks, use a new evidence directory:

```sh
.venv/bin/python tools/home_compose_rehearsal.py --output tmp/home-rehearsal
```

The driver builds both profiles, creates only synthetic data, records actual
container/image identities, and removes its disposable containers/volumes afterward.
Its separate test launcher controls the GitHub callback; production code contains
no test login bypass. No account is enrolled and no fixture email is sent. `--keep`
retains stopped synthetic volumes for diagnosis. This is a local integration
rehearsal, not proof of real tailnet access or physical Android behavior.

### Home maintenance and recovery

Keep the same Compose project name, files and immutable image tag when stopping
or restarting. `docker compose ... stop app` prevents automatic restart until you
explicitly start it again; check the container's actual stopped state and exit code.
An exit caused by Docker's kill deadline needs failure diagnosis before maintenance.
Do not remove named volumes as an upgrade or recovery shortcut.
When recreating the gateway, recreate its app as well so both join the same new
network namespace. A complete `docker compose ... down` followed by `up -d
--no-build --wait gateway app` preserves named volumes; **omit `--volumes`** on
real installations. Reconfirm application readiness afterward. Merely restarting
the gateway is not a complete application recovery procedure.

Back up through the existing signed logical backup adapters while the application
is stopped. Retain canonical artifacts, private configuration and the **current**
independent control/security/delivery journal separately. The named archive volume
is on the same Docker disk, so copy protected backups to independently retained,
encrypted storage. Signatures detect tampering; they do not encrypt secrets.
Protect the gateway's persistent identity state too. Do not start two copies of
the same restored gateway identity or report sender. Keep the current journal
head in a separately retained location so losing a Docker disk does not leave only
an older revocation/delivery history.

For the default SQLite project, stop and inspect the app, then create a new archive
(replace `backup-001` with a new name each time):

```sh
docker compose -f deploy/home/compose.yaml stop app
docker inspect --format '{{json .State}}' "$(docker compose -f deploy/home/compose.yaml ps -aq app)"
docker compose -f deploy/home/compose.yaml run --rm --no-deps operator -m memoryking backup --store /data/store --control /control/security --backup /archives/backup-001
```

For the optional PostgreSQL project, keep PostgreSQL running and stop only the app:

```sh
docker compose -p memoryking-home-pg -f deploy/home/compose.yaml -f deploy/home/postgres.yaml stop app
docker inspect --format '{{json .State}}' "$(docker compose -p memoryking-home-pg -f deploy/home/compose.yaml -f deploy/home/postgres.yaml ps -aq app)"
docker compose -p memoryking-home-pg -f deploy/home/compose.yaml -f deploy/home/postgres.yaml run --rm --no-deps operator -m memoryking.shared backup --dsn-file /secrets/runtime.dsn --backup-dsn-file /pgadmin/backup.dsn --content /data/store --control /control/security --tenant main --credential /control/security/main/owner.credential --archive /archives/backup-001
```

Both backup commands were exercised against synthetic home containers. The logical
archive includes canonical blobs; it does **not** replace independently retained
current control history, secret configuration or gateway identity. Copy those private
volumes only while their owning service is stopped. Do not export secret contents
to terminal output or put them in Git. Retain the current journal head separately
using the existing [control-history procedure](#backup-and-recovery), and verify the
encrypted off-device copy before treating it as disaster recovery.

Before restoring, disable the report worker and sender. Restore old content with
the latest independently retained control history; never replace that history with
an older snapshot to get the service to start. Interrupted restores remain fenced
until the explicit restore procedure finishes. Missing history blocks startup.
After restore, reconcile lost operations, refresh incarnation-bound credentials and
review report/delivery state before enabling any sender. An accepted or ambiguous
submission must not be retried automatically.

With the app stopped, report configuration disabled and current control history
intact, these are the exercised restore entry points for the two profiles:

```sh
docker compose -f deploy/home/compose.yaml run --rm --no-deps operator -m memoryking restore --store /data/store --control /control/security --backup /archives/backup-001
docker compose -p memoryking-home-pg -f deploy/home/compose.yaml -f deploy/home/postgres.yaml run --rm --no-deps operator -m memoryking.shared restore --dsn-file /pgadmin/migration.dsn --content /data/store --control /control/security --tenant main --archive /archives/backup-001
```

Run only the command for the installation being restored. Each creates a replacement
history and new owner credential; do not reuse old agent/viewer/report credentials.
Keep the sender off while reconciling the lost requests and existing delivery
receipts. These commands deliberately do not include a lost-control override.

For upgrades, keep the last image/configuration and current backups. A prior image
is usable only if it accepts the current schema and configuration. Do not bypass a
schema rejection or blindly downgrade files. Compatible prior/current packaged-core
reopening and unsupported-schema rejection have local rehearsal coverage. Real
account/device setup and final private-pilot qualification remain pending.

| Failure | First action |
| --- | --- |
| `unsafe_permissions` | Check UID/GID and 0700-directory/0600-secret modes; do not make private files world-readable. |
| `owner_busy_or_unsafe` | Locate and stop the existing SQLite owner; do not delete its lock or copy a running database. |
| `home_database_socket_required` | Check the explicit socket DSN and reject TCP/service/environment fallbacks. |
| `schema_unsupported` | Preserve data, control history and the rejected package identity; select a verified compatible image or repair/roll forward. |
| `recovery_required` | Preserve the current journal/anchor and complete the explicit restore/reconciliation procedure. |
| Neutral startup failure | Privately inspect missing secret/control files, TLS certificate/key match and database readiness; never paste secret contents into logs or support messages. |
| Gateway `NeedsLogin` | Expected after fresh bootstrap. No private network registration has occurred yet. |
| Phone offline / host asleep | Restore Mac/Docker/network availability and sign in again; do not bypass TLS or expand network access to troubleshoot. |

The rehearsal accepts `--compatible-revision FULL_GIT_SHA` to compare the actual
prior/current packaged core against the same stopped synthetic store. This does
not invent a prior home entry point or approve an arbitrary schema downgrade.
For an immutable contributor job, use `tools/local_ci.py --revision FULL_GIT_SHA`
with its usual local runtime arguments plus `--home-containers` and, when needed,
`--home-baseline PRIOR_FULL_GIT_SHA`.

### Measure a synthetic viewer workload

Contributors can run `tools/mobile_load_trial.py` against fresh disposable native
SQLite and PostgreSQL stores. Supply `--output` with a new directory and
`--postgres-bin` with an absolute PostgreSQL binary directory. The default
`--duration 1800` runs each store sequentially for 30 minutes after seeding one
project with 100 saved tasks and 2,000 observations. Five distinct viewer identities
make staggered TLS reads at up to one read per second each, including bounded
task, memory/search, pagination and graph reads. A mid-run grant reduction must
deny an old session before access is explicitly restored.

Before measurement, the seeder drains projection jobs in bounded batches through
the normal claim/finish protocol, then verifies the corpus counts and an empty
pending queue. It does not increase queue limits or write synthetic job states
directly. Preparation time is reported separately from the timed read workload.

The output contains sanitized request timings, status/pressure counts, response
body byte totals, session churn, resource samples and database admission timings.
No cookies, OAuth tokens, response contents or real mail are retained. SQLite
timings measure the canonical lock; PostgreSQL gate/clock timings include query
overhead. RSS includes the harness/server and disposable PostgreSQL process tree.
An explicit `--smoke --duration 10` uses a smaller corpus to check the runner; it
does not qualify the full workload. The provider is controlled and the network is
local direct TLS: neither run measures physical-phone refresh or a real tailnet.
A finite run cannot establish unlimited-duration memory stability.

Add `--mobile-load-seconds 1800` to the immutable `tools/local_ci.py` command to
retain the full both-store run with the clean-checkout evidence. This requires
`--postgres-bin`, runs locally, and adds at least an hour. It fails on unexpected
read errors, pressure responses or a successful-read p95 of one second or more;
the output retains failed measurements for investigation. Inspect all-status and
per-route distributions as well as the aggregate before choosing a deployment.

### Restrict devices at an isolated Serve endpoint

If you keep an existing broad tailnet policy, the viewer can additionally reject
HTTP requests from devices outside an explicit list. Add this optional field to
the private viewer configuration, replacing the example addresses with the
approved devices' actual Tailscale IPv4/IPv6 addresses:

```json
"trusted_proxy": {
  "kind": "tailscale_serve_v1",
  "source_ips": ["100.64.0.1", "fd7a:115c:a1e0::1"]
}
```

This option requires `isolated_proxy` transport and the dedicated home namespace
behind the pinned Tailscale Serve proxy. Keep the application loopback-only, with
no published ports or other proxy in front. Serve supplies the actual source
address; the viewer requires a single allowed address, exact forwarded authority
and HTTPS, and rejects Funnel and alternate proxy chains. One to sixteen exact
canonical Tailscale addresses are supported; ranges and wildcard identities are
rejected. Missing or disallowed sources cannot load even the sign-in page.

This is HTTP request denial at this endpoint, not a change to your tailnet ACL
or a block on TLS handshakes. Same-namespace processes and the Docker owner are
trusted; headers are forgeable if the listener is exposed elsewhere. The option
never signs a user in: GitHub authentication and explicit MemoryKing grants still
apply. Address changes require an operator update/restart. Requalify the proxy
path after a gateway upgrade. Disable Serve/the viewer before removing the gate
unless another verified network restriction is already in place.

## Shared Docker rehearsal

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

Database roles are least privilege: the worker role can insert receipts but not change or delete them, and cannot write tenant counters directly; the commit serial advances only through the owner-run `next_commit_serial` function. Row-level security is keyed on a session setting the client sets itself, so treat it as defense in depth behind the role grants, not as the sole boundary.

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

Backup uses SQLite's backup API, referenced blobs and a signed integrity manifest. It excludes credentials and the independent control tree. Retain the **current** control identity/journal/anchor separately; do not restore old control files alongside old content. Retain the emitted current journal checkpoint independently if lost-anchor recovery is required.

Restore verifies the archive, replays current revocations/deletions and creates a new history and owner credential. Old credentials, request namespaces, watermarks and handles are retired. Reconcile acknowledged work that is absent from the backup before issuing new retries. Optional indexes, adapter bases, procedure activation and ranking need their documented fresh-history rebuild/reconciliation.

If the anchor alone is missing, `restore --lost-anchor-journal-head CURRENT_INDEPENDENT_64_HEX_HEAD` requires a genuinely independently retained current checkpoint, intact identity and matching journal. Missing or rolled-back evidence fails closed. Do not derive that argument from a potentially rolled-back local journal.

Shared backup uses the runtime and separate backup DSNs plus current tenant owner credential. Restore uses the migration DSN:

```sh
.venv/bin/python -m memoryking.shared backup --help
.venv/bin/python -m memoryking.shared restore --help
```

The signed per-tenant archive is not a PostgreSQL physical backup/PITR stream. It bounds rows at 1 MiB, the manifest at 16 MiB and payload at 16 GiB. Backup briefly fences the tenant. Interrupted restore remains fenced until explicit recovery succeeds. Other tenants retain their own state.

## Upgrades, retention and removal

Current storage schemas are SQLite 14 and PostgreSQL 9 (archive schema 7). Keep the installed revision, wheel/image hashes, locks, content archive and current control evidence before an upgrade. Test on a disposable copy first. Local upgrades run under exclusive store ownership; shared schema migration runs before API/workers start, using the migration role.

Private viewer/report builds used SQLite 9/10 and PostgreSQL 4/5 for different tables than the released core. The combined migration checks the actual table inventory, preserves viewer/report records, and joins both lineages. SQLite joins run atomically under exclusive ownership. Shared joins remain offline migration-role operations. New archives identify the combined schema; signed historical shared archives are distinguished by their exact table inventories. An older binary cannot open the joined schema. After a migration, use the verified new binary or an explicit history-aware recovery procedure with retained control evidence; do not restart an old image against migrated data.

Do not run an older binary against a newer schema or manually lower a schema version. Prefer a compatible roll-forward; use verified fresh-history restore when recovery is needed. There is no automatic reverse migration or multi-host failover.

Offline local `gc` expires retained result bodies and removes unreferenced blobs; consumed request keys remain consumed. Purge and retention previews are privileged operations with explicit dependency effects. They do not erase copies already exported or delivered. There is no installed backup/retention scheduler.

### Enabling plan tracking

Structured plans use the existing core store and checkpoint permissions; there is no additional service, port, provider or configuration flag. Upgrade the service and its bundled dashboard together, then reconnect MCP clients so their tool catalog includes `memory_plan`; updating a client alone is not enough, because the installed service must accept the plan commands. Existing checkpoints stay compatible, and plan revisions use the same archive, restore and purge paths. An older binary rejects plan-bearing checkpoints, so keep the matching service version when restoring an archive that contains plans.

Stop services before removing runtime environments or images. Removing software does not remove data, control, archives or migration bundles. Keep or deliberately dispose of those separately under your retention policy.

## Viewer access foundation

Viewer identity policy is available to the owner; the separate viewer listener and
GitHub authentication require explicit configuration. No new OAuth app or network
configuration is created automatically. The existing dashboard retains its owner-only
boundary.

Run `.venv/bin/python examples/viewer_identity.py` for an exercised disposable
SQLite/owner-IPC walkthrough. It does not connect to GitHub, change an existing
store or expose a viewer credential.

The owner CLI is `python -m memoryking viewer-admin --socket SOCKET --credential
OWNER_CREDENTIAL_FILE`, reading one closed viewer command from stdin. Commands
contain version `1`, the current history incarnation, a stable retry `key`, `action`
and typed `args`. Use the authoritative viewer schema and the exercised example,
not a copied history identifier. Shared owner requests use the same `viewer_admin`
operation through the existing authenticated shared request channel.

| Action | Arguments and effect |
| --- | --- |
| `bind` | Principal, fixed issuer `https://github.com`, decimal numeric `user_id`; requires a fresh dedicated principal without existing agent/owner grants. |
| `grant` | Exact project, expected binding version, nullable start/expiry in UTC milliseconds; read-only and does not change content readers. |
| `revoke` | Exact project and expected binding version; removes that viewer grant. |
| `disable` | Principal and expected version; removes grants and disables sign-in identity resolution. |
| `rebind` | New numeric subject and expected version; invalidates sessions and clears old project grants instead of transferring them to another account. It does not re-enable a disabled binding. |
| `logout_all` | Principal and expected version; advances the independently durable generation. |
| `list` | Empty args; bounded owner-only identity/grant inventory with truncation reported. |

Identity uses the numeric subject, not login name/email. Every mutation advances
binding version and session generation and the canonical tenant policy epoch. Old
verified contexts are rejected. At most 256 bindings and 1,024 viewer project
grants exist per tenant. General agent-grant and revoke commands reject registered
viewer principals with `viewer_policy_operation_required`; use the versioned
`viewer-admin` operations so both permission projections and the binding version
change together. A grant prepared before a viewer revocation is then rejected as
`revision_conflict`. Ordinary agent principals retain the general grant/revoke API.
Operator retries are journaled; a mismatched canonical payload
under the same key is rejected. After restore, reconcile and use the new history
namespace explicitly; do not blindly repeat an old request.

SQLite upgrades from schema 8 to 9 under exclusive ownership; PostgreSQL migrates
from shared schema 3 to 4 with the separate migration role. Both add tenant-bound
viewer bindings, grants and retry projections; PostgreSQL forces row security and
workers cannot mutate those tables. Current independent security history is still
required on restore. Preserve the old package and a verified archive, and rehearse
roll-forward/fresh-history recovery before adoption. There is no reverse migration.
No running store has to be adopted to try the disposable example.

Viewer/report JSON schemas and generated types are available now. They define
viewer listener/session boundaries and implemented report operations. The phone
PWA is available as a local evaluation preview; physical Android and real private-network
qualification remain pending. Reporting is described below. Viewer configuration is rejected
for non-HTTPS public origins, non-loopback listeners, invalid TLS-mode combinations,
invalid limits or unknown fields. Pass it explicitly to the serving command; the
listener opens only when enabled.

## Experimental reporting storage

Reports add SQLite schema 10 and shared schema 5. Upgrades are transactional; the
runtime and ordinary projection worker cannot run migration DDL. PostgreSQL report
tables use forced tenant RLS; projection workers cannot mutate them. Content
backups include uploaded inputs, reader fragments and rendered artifacts. The
independent control journal retains source readership and report policy changes
across content rollback.

Restore changes the history incarnation. Existing report rows and their immutable
bytes can remain in an archive, but old SDK handles and reader cursors cannot be
used as current reports. Unfinished uploads and worker tokens are retired. Current
source permissions are replayed before activation. There is no automatic report
republication or sender activation during restore. Delivery intent, submitting,
accepted, unknown and reconciliation records live in independent control history.
Restoring an older queued job cannot resend a later accepted/unknown submission;
missing current control evidence blocks startup/sending. Purge conservatively erases
all report content for the tenant, including derived blobs once garbage collected.

Install the optional reports lock only for an explicitly configured rendering
host. The native macOS worker needs the pinned Python runtime and system fonts;
keep secrets and store data outside runtime/package paths. Rendering is denied
if those paths overlap. It has no network or mail configuration. The supervisor
limits RSS to 512 MiB and wall time to 120 seconds; a worker alarm also bounds an
orphaned render before its durable 130-second lease expires. Outputs are private,
hashed and fsynced before manifest commit. Linux x86-64 rendering requires Landlock
ABI 6 or later and seccomp. It denies network access, child processes, executable
launches, inherited descriptors, permission changes and unknown syscalls. Unsupported
kernels and architectures fail closed. The shared Dockerfile includes locked report
libraries and pinned DejaVu fonts. Container checks exercised real PDF/EPUB output
on Linux 7.0.14 with Docker Engine 29.9.2, as a non-root user with a read-only root,
no network, no capabilities and no-new-privileges. That result does not qualify a
mobile deployment or other architectures; there is no unrestricted fallback.


## Report worker configuration

Both `memoryking serve` and `memoryking.shared serve` accept `--report-config`.
Omit it to leave scheduling and SMTP disabled. This private, owner-only JSON file
uses the generated `ReportWorkerConfiguration` schema; every field is required:

```json
{
  "version": 1,
  "enabled": true,
  "tenant": "main",
  "project": "example",
  "credential_file": "/private/report-worker.credential",
  "mail_secret_file": null,
  "sender_enabled": false,
  "poll_seconds": 5
}
```

Paths above are setup placeholders. Use absolute regular-file paths and mode 0600;
symlinks, extra fields, missing credentials and unsafe renderer-readable locations
are rejected. Grant this worker only `read,report_author` in its project; include
its principal in report/source readership. Add `report_delivery` only when sending
is explicitly configured. A read-only phone credential cannot run the worker.
The project and tenant are checked at startup, and every operation rechecks current
authority. Shared scheduling needs a pool size of at least two so rendering does
not occupy the only request connection. Default pool size remains eight.

To configure mail, set `mail_secret_file` to a protected JSON file containing
`version: 1`, an identifier `destination`, and the three string fields `GMAIL_FROM`,
`GMAIL_APP_KEY`, `KINDLE_TO`. This destination is restricted to the configured tenant
and project. The project/program policy must also explicitly select Kindle/both
and bind a currently permitted recipient principal. Keep `sender_enabled: false`
until ready; setting it true enables queued submissions. The product never reads
a shell startup file. No mail secret is passed to the renderer or exposed in status.

The poll interval is 1–60 seconds. There is one bounded build and at most one mail
attempt per tick; inaccessible jobs do not prevent other queued work. Interrupted
builds fail for explicit correction. Recovered mail claims become unknown and need
evidenced reconciliation. Stop waits for an in-flight bounded operation before
closing the store; restart loads changed configuration. Logs contain only worker
state transitions. File configuration cannot grant a project permission, attest
visual QA, publish a report or imply delivery to a device.

## Replacing an existing report sender

Keep the new product sender disabled while an operator verifies the old sender's
complete inventory, immutable report/QA files, unique accepted receipts and exact
recipient fingerprint. Preserve its independent delivery history. Use
`sender_status` to verify the new fixed destination and disabled state, then
`import_delivery` to reserve each verified accepted identity. Uncertain outcomes
must be reconciled from evidence; do not guess acceptance or retry them.

Hold the old sender's exclusive coordination lock throughout import and fencing.
After verifying every returned import receipt, durably disable the old sender
before enabling the product worker. A failed transfer must never automatically
fall back to the old sender. The product cannot fence an unrelated external
process for you; use a trusted installation-specific collector and retain its
transition receipt. Imported receipts do not publish old reports or send mail.
Fresh reports still require source authority, exact artifact QA and publication.

## Restricted viewer listener

This optional listener serves a public shell, GitHub authentication endpoints and a
bounded read API. Its authentication implementation is tested with a controlled
provider. The four-tab phone UI has local browser coverage; real GitHub/Android
and home deployment remain unqualified.
Without a current session, private requests return 401. The listener is disabled
when `--viewer-config` is omitted or `enabled` is false.
The existing local owner dashboard and shared agent interface remain separate.

Run `.venv/bin/python tools/viewer_demo.py` from the source checkout for a complete
temporary SQLite rehearsal. It checks omitted/disabled startup, public navigation,
denied private access and shutdown. It creates no external account, report or email.
Native PostgreSQL startup is exercised separately by the viewer startup suite.

Start from [examples/viewer-config.json](../examples/viewer-config.json). All fields
are required; the sample is not implicitly loaded. Copy it outside the checkout,
set permissions to 0600, and choose unused loopback ports. Supply it as
`memoryking serve --store DATA --control CONTROL --viewer-config VIEWER_JSON`, or
add `--viewer-config VIEWER_JSON` to the existing `memoryking-shared serve` command.
Never start a second SQLite owner against a running data directory.

| Configuration | Sample and effect |
| --- | --- |
| `version`, `enabled`, `tenant` | `1`, `false`, `main`; explicit opt-in and one fixed tenant. |
| `public_origin` | Exact HTTPS origin, without a path, wildcard or user information; supplied Host and Origin must match. |
| `listen_host`, `listen_port` | `127.0.0.1`, `8772`; only IPv4/IPv6 loopback is permitted. |
| `transport` | `isolated_proxy` for a separately isolated HTTPS gateway, or `direct_tls`. Proxy mode does not trust forwarded identity headers. |
| `tls_cert_file`, `tls_key_file` | Both null for proxy mode; both required for direct TLS. The key must be a private regular file; TLS 1.2 is the minimum. |
| `github_client_id`, `github_secret_file` | Dedicated GitHub OAuth app client ID and path to its protected secret file. Enabled startup reads the secret; disabled startup does not. |
| `session_idle_seconds`, `session_absolute_seconds`, `login_seconds` | `1800`, `28800`, `300`; active idle, absolute and login-transaction lifetimes. Idle cannot exceed absolute lifetime. |
| `poll_seconds`, `display_lifetime_seconds` | `10`, `15`; visible client polling accepts 5–60 seconds, display leases 1–15. The session response supplies the polling interval. Keep polling shorter than the lease for a continuous view; a longer interval clears the display while waiting. Neither setting extends access expiry. |
| `limits.active_sessions`, `limits.pending_logins` | `5`, `64`; sessions per binding and combined pending contexts/logins, maxima 20/256. At the session ceiling the oldest session is evicted. |
| `limits.list_items`, `limits.detail_bytes` | `50`, `16384`; smaller configured ceilings are honored. Detail bytes range 1024–16384. |
| `limits.graph_nodes`, `limits.graph_edges` | `100`, `150`; maxima, with explicit partial coverage. |
| `limits.response_bytes` | `262144`; allowed 16384–262144. An oversized response fails closed. |
| `limits.concurrency`, `limits.request_seconds` | `4`, `5`; ranges 1–16 and 1–10. This budget also bounds database admission/query work. |

Configuration is closed and limited to 32 KiB. Invalid files fail startup. Listener
binding happens only when the service starts, after configuration validation.
Changing configuration requires a restart; there is no unprotected reload route.
Back up configuration and TLS keys privately alongside the separately retained
security control history. A content restore changes history and invalidates prior
viewer contexts and cursors; it cannot restore revoked access. The viewer adds no
new database schema in this release.

Loopback isolation requires a trusted local host. Proxy mode requires a dedicated
container/network boundary before deployment, because another local process could
otherwise access its plaintext loopback socket. It does not authorize router port
forwarding or public exposure. The planned private Tailscale home profile and real
Android installation are not yet qualified. Desktop Chromium tests use disposable stores, a controlled GitHub page/exchange,
actual browser sessions and a certificate pinned only in the test browser process.

For the route list, freshness rules and stable errors, see
[restricted API reference](reference.md#restricted-viewer-api-foundation).

## GitHub sign-in setup

This is a setup worksheet for a future private installation. It does not create an
OAuth application or change your network. The authentication endpoints are locally
verified with a controlled provider; real GitHub callback and physical Android
qualification are still required before treating the phone installation as ready.

1. Choose the one private HTTPS origin your phone will use. For an origin such as
   `https://memoryking.example.invalid`, the exact callback is
   `https://memoryking.example.invalid/viewer/auth/github/callback`. The `.invalid`
   name is a placeholder. Do not copy it into a real registration.
2. Register a **dedicated login-only GitHub OAuth App** for that installation. Use
   the exact origin for the homepage and the exact callback above; disable wildcard
   callback matching if the registration offers it. Do not reuse a broad repository
   integration. MemoryKing requests no repository, organization, email or offline
   scopes and rejects a nonempty granted scope set.
3. Put only the app client secret in a service-owner-owned regular file, mode 0600,
   outside the checkout and public assets. A single trailing newline is accepted;
   the value is limited to 16–256 ASCII letters, digits, `_` and `-`. Symlinks,
   FIFOs, public permissions and extra trailing data are rejected. Mount this file
   read-only when containerizing. Set `github_client_id` and `github_secret_file`
   in the private viewer configuration. Never put the secret in an image,
   JavaScript, URL, command argument or environment example.
4. Through the owner channel, bind the intended account's verified numeric GitHub
   ID to a dedicated viewer principal, then grant exact projects. A login name or
   repository membership is insufficient. Share source content explicitly when
   appropriate: a project grant cannot override an owner-only note's readers.
5. Select the TLS mode and start the existing service with the viewer configuration.
   Configure outbound verified HTTPS to GitHub; the provider hosts are fixed in
   code. There is no test-provider URL, insecure TLS or proxy override in production
   configuration. The phone dashboard interface is still under development.

The server issues `__Host-memoryking-login` and `__Host-memoryking-viewer` cookies
with Secure, HttpOnly, SameSite=Lax and Path=/, without Domain. Browser scripts
receive neither the provider token nor the session cookie. Sessions are process
local: restart forces sign-in. Default limits are 30 minutes idle and 8 hours total;
re-authentication rotates the token. Policy changes and restore invalidate old
contexts. Run one viewer-serving process per installation; independent workers do
not share sessions.

Logout and logout-all are CSRF-protected POST operations. Logout-all durably
advances the account's MemoryKing generation. Revoking an OAuth grant at GitHub
alone does **not** instantly end an existing MemoryKing session; use MemoryKing
revoke/disable/logout-all when access must end, and retain the configured expiry
limits. No provider refresh token or access token is stored after identity lookup.

A failed, denied, expired or uncertain callback returns to the clean `/viewer/`
path and requires a fresh login. The consumed code/state is never automatically
retried. If login repeatedly fails, check the exact origin/callback, private secret
file, clock, empty provider scopes, numeric binding and current project grant.
Provider bodies, callbacks and cookie values are deliberately absent from logs.
The callback validates GitHub's exact authorization-server issuer,
`https://github.com/login/oauth`, before exchanging a code. A missing issuer or
even a different spelling fails closed. This is separate from the stored
identity binding's `https://github.com` issuer; existing bindings do not change.
GitHub denial fields (`error_description`, `error_uri`) are bounded and discarded,
never rendered or fetched. See GitHub's [issuer documentation](https://docs.github.com/en/apps/github-authentication-discovery-endpoints).
Transport counters and generic status codes cannot identify a hidden project.
Pending-context pressure returns 503; retry after capacity recovers, using a new
login context. Backward clock movement clears authentication state and blocks
until the clock catches up to its prior high-water time.

GitHub documents [state, PKCE and scope inheritance](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)
and [OAuth scopes](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps).
The dedicated app and returned-scope check enforce this installation's login-only
boundary; they do not synchronize GitHub repository permissions into MemoryKing.

[Back to README](../README.md) · [Reference and troubleshooting](reference.md) · [Security](../SECURITY.md)

### Enabling plan tracking

Structured plans use the existing core store, checkpoint permissions and viewer listener; there is no additional service, port, paid provider or configuration flag. Upgrade the service and its bundled dashboard/viewer assets together, then reconnect MCP clients so their tool catalog includes `memory_plan`. The installed server must support the new working commands; updating a client alone is insufficient. Back up using the normal procedure before an upgrade. Existing ordinary checkpoints remain compatible, and historical plan revisions use the same archive/restore and purge paths. An older binary can reject plan-bearing checkpoints; retain the matching service version when restoring an archive that contains plans. Upgrade staging and verify a read/write/read cycle before changing live agent routing.
