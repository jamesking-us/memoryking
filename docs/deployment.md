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

1. Merge the release commit to `main`, tag it (`git tag -a v0.1.0a1 -m "MemoryKing 0.1.0a1"`) and push the tag.
2. In the contributor environment at that tag, run `.venv/bin/python tools/build_release.py`. It writes `dist/SHA256SUMS` and `dist/RELEASE.json` and proves a fresh install; a second run of the same commit must reproduce the same hashes.
3. Upload the wheel and sdist from a throwaway environment that has `twine` (it is not in the hash locks), with a PyPI API token scoped to the project (the first upload of a new project needs an account-wide token; replace it with a project token afterwards): `twine upload dist/memoryking-0.1.0a1*`. Confirm the hashes PyPI shows (`https://pypi.org/pypi/memoryking/<version>/json`) match `SHA256SUMS`.
4. Build and rehearse the image (`tools/image_rehearsal.py --output tmp/image-0.1.0a1.json`), then tag and push it to a registry you control, for example `docker tag memoryking:local ghcr.io/jamesking-us/memoryking:0.1.0a1 && docker push ghcr.io/jamesking-us/memoryking:0.1.0a1`, and sign the pushed digest (the index digest `docker push` prints) with your cosign key: `cosign sign --key cosign.key ghcr.io/jamesking-us/memoryking@sha256:<digest>`, or keyless with `cosign sign` alone (the route used so far), which signs in through a browser or a device code, binds the signature to the signing identity, and records it in the public transparency log; the release notes then carry the `cosign verify` command with that identity and issuer. With a key pair, publish `cosign.pub` instead. A new package on the GitHub registry starts private; make it public in the package's settings on GitHub (the API cannot), or nobody can pull it.
5. Create the GitHub release for the tag, marked as a pre-release while the version is one, with `SHA256SUMS`, `RELEASE.json`, the wheel and the sdist attached and the image digest in its notes, so the wheel and image of one version are tied together.

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
| `serve --log-level` | `warning` by default. Redacted operator log lines on stderr: component, event, request id, error type and code location, never content, SQL or paths. An `internal_error` reply carries the `request_id` of its line. |
| `serve --trace` | One `event=trace` line per request at info level (the level is lowered to info if it was higher): for a socket request the operation, the command action, the outcome code, elapsed milliseconds, time spent waiting for the store lock, time inside transactions, the number of transactions and the response size; for a dashboard request the method, the route family (such as `/api/receipts`, never the project or receipt id), the status and the same timings. Nothing in a trace line names content, a project or a principal. The shared service accepts the same flag. |
| `serve --egress-policy FILE` | Loads the private egress policy below; without it the only model provider is the local recorder and nothing leaves the machine. A policy change needs a restart and retires every open conversation. The shared service accepts the same flag. |
| `serve --summary-idle 30m` | Between requests the daemon derives a "where this left off" summary, as the owner, for every task whose latest checkpoint has been idle this long and has none; `0` disables. Summaries follow the 30-day retention. |
| `summarize --project P --task T [--run R] [--provider NAME --model M --egress-policy FILE] [--json]` | Derives a summary with its own receipt: extractive by default, or written by a policy adapter through the audited inference path. Needs a credential with `checkpoint` (and `inference` for `--provider`). |
| `egress-audit --project P [--before MS] [--json]` | Lists what left the store for a provider in the last 30 days: when, status (`delivered`, `failed` or `pending`), provider, model, host, classification, bytes, cited receipts and permit. Needs a credential with `inference`. |
| `status --socket`, `--credential` | Human summary of store health through the running daemon: history, position, frontier, jobs and capacity bars; `--json` prints the raw result. Needs an admin credential. |
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

Current storage schemas are SQLite 11 and PostgreSQL 6. Keep the installed revision, wheel/image hashes, locks, content archive and current control evidence before an upgrade. Test on a disposable copy first. Local upgrades run under exclusive store ownership; shared schema migration runs before API/workers start, using the migration role.

Do not run an older binary against a newer schema or manually lower a schema version. Prefer a compatible roll-forward; use verified fresh-history restore when recovery is needed. There is no automatic reverse migration or multi-host failover.

Offline local `gc` expires retained result bodies and removes unreferenced blobs; consumed request keys remain consumed. Purge and retention previews are privileged operations with explicit dependency effects. They do not erase copies already exported or delivered. There is no installed backup/retention scheduler.

### Enabling plan tracking

Structured plans use the existing core store and checkpoint permissions; there is no additional service, port, provider or configuration flag. Upgrade the service and its bundled dashboard together, then reconnect MCP clients so their tool catalog includes `memory_plan`; updating a client alone is not enough, because the installed service must accept the plan commands. Existing checkpoints stay compatible, and plan revisions use the same archive, restore and purge paths. An older binary rejects plan-bearing checkpoints, so keep the matching service version when restoring an archive that contains plans.

Stop services before removing runtime environments or images. Removing software does not remove data, control, archives or migration bundles. Keep or deliberately dispose of those separately under your retention policy.

[Back to README](../README.md) · [Reference and troubleshooting](reference.md) · [Security](../SECURITY.md)
