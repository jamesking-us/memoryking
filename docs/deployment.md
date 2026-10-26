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
| `--store`, `--control` | Required paths for initialization, serving and offline maintenance. Neither may contain the other. |
| `init --tenant`, `--principal` | `main` and `owner`; IDs are 1–96 ASCII letters, digits, `_` or `-`. |
| `serve --dashboard-port` | Dashboard disabled unless set. `0` chooses a free port; otherwise 0–65535. Always loopback. |
| `serve --plan-root` | Optional compatibility attachment for an explicitly trusted development-record checkout. Normal product use needs no report files; default is the current directory. |
| `request --socket`, `--credential` | Required socket and private credential file paths. |
| `request --credential-output` | New private output file required for a grant; secret values are not printed. |
| `dashboard-login --output` | New private file containing a one-use code; expires after 60 seconds. |
| `--backup` | New external directory for backup; verified existing directory for restore. |

Paths/runtime changes require restart. Permission changes apply transactionally. Keep owner credentials out of agent prompts and browser pages; issue scoped credentials for normal agents. Files use owner-only modes (directories 0700, sensitive files 0600).

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

For the dashboard, add `--plan-root /trusted/product-checkout --dashboard-tenant team`; both are required together in the shared CLI. A checkout with no development record opens the product workspace normally. Do not attach another tenant's private files. Bootstrap requires the configured tenant's wildcard administrator.

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

The owner credential is in `tmp/shared-evaluation/control/data/demo/owner.credential`. A worker needs its own project-scoped `read,jobs` credential at `secrets/worker/worker.credential`. The one-shot worker handles only `audit_probe`; repository and embedding workers are separate entry points.

Stop the stack while preserving its data:

```sh
docker compose --env-file tmp/shared-evaluation/compose.env -f deploy/shared/compose.yaml down
```

The reusable test helper is `tools/shared_compose_rehearsal.py --compose /absolute/path/to/docker-compose --directory NEW_STATE --output NEW_LOGS`. It runs the role/API/worker/crash/archive/restore/migration sequence, records results and stops its containers. API, gateway and database have bounded `on-failure:3` restart policies; exhausted retries need diagnosis. There is no published image, Kubernetes chart or managed cloud installer.

## Backup and recovery

Stop the local daemon before offline maintenance:

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

Current storage schemas are SQLite 10 and PostgreSQL 5. Keep the installed revision, wheel/image hashes, locks, content archive and current control evidence before an upgrade. Test on a disposable copy first. Local upgrades run under exclusive store ownership; shared schema migration runs before API/workers start, using the migration role.

Do not run an older binary against a newer schema or manually lower a schema version. Prefer a compatible roll-forward; use verified fresh-history restore when recovery is needed. There is no automatic reverse migration or multi-host failover.

Offline local `gc` expires retained result bodies and removes unreferenced blobs; consumed request keys remain consumed. Purge and retention previews are privileged operations with explicit dependency effects. They do not erase copies already exported or delivered. There is no installed backup/retention scheduler.

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
PWA is still being built; reporting is described below. Viewer configuration is rejected
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
Transport counters and generic status codes cannot identify a hidden project.
Pending-context pressure returns 503; retry after capacity recovers, using a new
login context. Backward clock movement clears authentication state and blocks
until the clock catches up to its prior high-water time.

GitHub documents [state, PKCE and scope inheritance](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)
and [OAuth scopes](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps).
The dedicated app and returned-scope check enforce this installation's login-only
boundary; they do not synchronize GitHub repository permissions into MemoryKing.

[Back to README](../README.md) · [Reference and troubleshooting](reference.md) · [Security](../SECURITY.md)
