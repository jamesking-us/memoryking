# Reports from the Codex session

Newest entry at the bottom, each headed with a UTC time. See `TO_CODEX.md` for the job and the rules.

## 2026-11-27 15:51:13 UTC — Phase 0

- Debian GNU/Linux 13.7; Python 3.12.14; Codex CLI 0.172.0-alpha.2. This is a hosted Codex environment.
- The CLI offers `codex exec` and reports ChatGPT login, but launching another session without a person has not been verified. Its `--ephemeral` option says it avoids persisting session files; the CLI also warns that it could not create PATH aliases because the filesystem is read-only.
- `codex features list` reports `hooks stable true`, and `codex exec --help` describes running enabled hooks and bypassing persisted hook trust. The help inspected does not identify the hooks file, so its location and actual command-hook behavior remain unverified.
- The current Codex configuration exists, the user hooks file is absent, and the configuration directory is not writable in this sandbox. Direct Git network access failed. No setup or test session attempted yet; sessions used: 0.

Question: May the test run in an environment where the Codex configuration and hooks can be changed and restored, and where the repository and model service are reachable? Also, can a test session be run without modifying any other files under the real Codex configuration directory? I will investigate the available non-mutating paths.
