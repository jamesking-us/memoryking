# Security model

MemoryKing stores potentially sensitive agent observations. Run it inside a trusted operating environment and grant each agent only the projects and capabilities it needs.

- The local profile trusts one OS owner, private files and a local Unix socket. Another process with the same OS identity is inside that trust boundary.
- Shared deployments use explicit tenant/project identity, separate database roles, verified transport and bounded delegation. Browser cookies are separate from agent credentials.
- Source text is evidence, not permission. Capturing a note cannot grant a role, accept a policy, run code or activate a procedure.
- Private reads recheck current permissions. The consuming host must use the authorized-conversation integration to rebuild retained model input after revocation; an ordinary memory API call cannot erase a host's old transcript.
- Backups require independent current control information. Restoring content creates a new history and retires old retry namespaces, watermarks and handles.
- Revocation and purge control future access and implemented live copies. They do not promise erasure of previous deliveries, exports, backups or physical storage remnants.
- Nothing leaves the machine for a model provider unless the operator loads an egress policy. The service enforces that policy before issuing a permit (provider, model, host, byte count and the classification ceiling of every cited source) and records every dispatch in an audit that survives purge. Provider keys and tokens are read by the host process from private files, never by the service, and never appear in records or logs.

Do not expose the local dashboard remotely or disable TLS/Host/Origin checks to make a shared setup work. Keep credentials, DSNs, store/control files, migration snapshots and provider inputs out of Git and public issues.

Report a suspected vulnerability privately to the repository owner using an existing private channel. Include a minimal synthetic reproduction, affected revision and impact; do not include live secrets or customer data. No separate public security-reporting service is currently configured.

See [design](docs/design.md) for authorization ordering and [deployment](docs/deployment.md#backup-and-recovery) for recovery steps.
