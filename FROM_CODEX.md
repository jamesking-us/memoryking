# Reports from the Codex session

Newest entry at the bottom, each headed with a UTC time. See `TO_CODEX.md` for the job and the rules.

## 2026-11-27 15:51:13 UTC — Phase 0

- Debian GNU/Linux 13.7; Python 3.12.14; Codex CLI 0.172.0-alpha.2. This is a hosted Codex environment.
- The CLI offers `codex exec` and reports ChatGPT login, but launching another session without a person has not been verified. Its `--ephemeral` option says it avoids persisting session files; the CLI also warns that it could not create PATH aliases because the filesystem is read-only.
- `codex features list` reports `hooks stable true`, and `codex exec --help` describes running enabled hooks and bypassing persisted hook trust. The help inspected does not identify the hooks file, so its location and actual command-hook behavior remain unverified.
- The current Codex configuration exists, the user hooks file is absent, and the configuration directory is not writable in this sandbox. Direct Git network access failed. No setup or test session attempted yet; sessions used: 0.

Question: May the test run in an environment where the Codex configuration and hooks can be changed and restored, and where the repository and model service are reachable? Also, can a test session be run without modifying any other files under the real Codex configuration directory? I will investigate the available non-mutating paths.

## 2026-11-27 15:52:57 UTC — Phase 1, partial

- The test branch head at start was `0eef9f45acb0bd44374fcd9b70e0f37042812116`. Network access was possible with an explicitly network-enabled command; a detached checkout and a fresh Python 3.12 virtual environment were created outside the repository, and editable installation of MemoryKing 0.2.0 succeeded.
- Before setup, the existing Codex config was privately copied outside the repository; the hooks file was absent. A byte comparison currently confirms the config still matches and the hooks file remains absent.
- Dry-run passed. It reported that it would create the scratch store and control data, start a daemon, issue an agent credential, change `~/.codex/config.toml`, create `~/.codex/hooks.json`, and download and enable the pinned semantic model. The proposed managed block was:

```toml
# >>> memoryking setup codex: managed block; undo with `memoryking setup codex --remove`
[mcp_servers.memoryking]
command = "<scratch-venv>/bin/python3.12"
args = ["-m", "memoryking", "mcp", "--store-profile", "default-agent", "--project", "scratch-a", "--profile", "lean"]
# <<< memoryking setup codex
```

- Proposed hooks structure: top-level `hooks` object containing `SessionStart` (matcher `startup|resume|compact`), `PreCompact`, `SessionEnd`, and `UserPromptSubmit`; each contains command hooks, with timeouts of 30, 30, 30, and 5 seconds respectively. The commands invoke `memoryking.hook_cli` with the corresponding event. No file was written by the dry run.
- Real setup and Codex acceptance were not attempted: the sandbox does not allow writing the existing Codex directory, and setup also writes `~/.memoryking/config.toml` and setup bookkeeping. The default profiles file is absent. Source inspection shows setup rejects `MEMORYKING_CONFIG`, so that file cannot simply be redirected with that variable. Semantic status is not yet available. Sessions used: 0.

Question: Can this test be moved to a suitable writable Codex host, or should it use an isolated temporary home and Codex configuration instead? The latter would test an isolated configuration rather than the existing one. Before running a session against the existing configuration, please clarify whether any necessary additional changes under `~/.codex` are authorized; the CLI's ephemeral option promises no session files but does not establish that no other files change.
