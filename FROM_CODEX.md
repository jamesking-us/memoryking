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

## 2026-11-27 16:12:46 UTC — Phase 1, isolated setup and login

- Followed the board's isolated-home instruction: copied the original config into a private temporary home, set both `HOME` and `CODEX_HOME`, and used scratch project and data directories. Codex's `exec --help` says `--ignore-user-config` skips `$CODEX_HOME/config.toml` and that authentication still uses `CODEX_HOME`. MemoryKing setup instead resolves its host configuration using the home directory; setting both made the paths agree.
- Real setup succeeded: it created the scratch store and control data, started the daemon, issued an agent credential, changed the isolated `~/.codex/config.toml`, created `~/.codex/hooks.json`, and downloaded and enabled the semantic model. The managed block and hook structure match the preceding dry-run report. Status reported semantic model `bge-small-en-v1-5-onnx`, with 0 vectors for scratch-a.
- `codex mcp list` listed `memoryking` as enabled. In two attempted `codex exec` sessions, using `--ephemeral --dangerously-bypass-hook-trust --skip-git-repo-check`, Codex reported `hook: SessionStart Completed` and `hook: UserPromptSubmit Completed`; it read the isolated `~/.codex/hooks.json` and warned: `clamping SessionEnd hook timeout to 3s`. This establishes dispatch, not delivery of briefing to a model or successful capture. SessionEnd dispatch, payload fields, model-visible tool names and configuration acceptance by a successful model turn remain unverified. The CLI also warned that it refused to create helper binaries under a temporary directory.
- First attempt without copied login failed with HTTP 401: `Missing bearer or basic authentication in header`. After copying the one permitted login file privately, a second attempt failed with HTTP 401: `Could not parse your authentication token. Please try signing in again.` and `Your access token could not be refreshed because you have since logged out or signed in to another account. Please sign in again.` Neither attempt reached a model response. Per the board instruction, stopping further login attempts. Sessions attempted: 2; successful model turns: 0.

Question: Can a working authenticated Codex environment be provided for the remaining real-session checks? No other login method has been attempted.

## 2026-11-27 16:13:41 UTC — Phase 2

Not attempted: fact capture, CLI recall counts and source labels, briefing delivery, autonomous recall, and explicit memory tool calls all depend on a working model session. Both permitted login attempts failed as reported in Phase 1. Sessions attempted: 2; successful model turns: 0.

## 2026-11-27 16:13:54 UTC — Phase 3

Not attempted: preference capture, scratch-b setup and briefing, user memory counts and labels, and project isolation. These checks depend on successful authenticated sessions; the board instructs stopping after both login approaches fail. Sessions attempted: 2; successful model turns: 0.

## 2026-11-27 16:14:27 UTC — Phase 4 and final summary

Manual compaction and pre-compaction capture were not attempted because no authenticated session could be started. Removal was run for scratch-a and scratch-b; scratch-a restored the isolated MCP configuration and deleted the isolated hooks file, while scratch-b reported them unchanged and absent. The daemon stop was issued; its process was subsequently observed as a defunct process, not a running daemon. All temporary home, copied login, scratch data and project directories, checkout, virtual environment, and private backup were deleted after comparison.

Before deletion, byte comparisons of the private baseline, restored isolated config, and existing config all matched. Each SHA-256 was `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` (the baseline config was empty). Both isolated and existing hooks files were absent, matching their initial state. Sessions attempted: 2; successful model turns: 0.

| Check | Result | Evidence or limit |
| --- | --- | --- |
| Environment and CLI inventory | Passed | Hosted Debian 13.7, Python 3.12.14, Codex 0.172.0-alpha.2. |
| Branch and installation | Passed | Tested setup at `0eef9f45acb0bd44374fcd9b70e0f37042812116` in Python 3.12 venv. |
| Setup dry run and actual setup | Passed | Isolated home; managed block and four hook registrations created. |
| Semantic status | Passed | Model enabled; 0 vectors for scratch-a. |
| MCP registration listing | Passed | Codex listed memoryking enabled; actual model-visible tools unverified. |
| Authenticated noninteractive model turn | Failed | Both permitted login approaches produced HTTP 401. |
| Hook file and exec dispatch | Passed | Isolated `~/.codex/hooks.json` read; SessionStart and UserPromptSubmit reported completed. Hook output delivery and payload field names unverified. |
| SessionEnd dispatch | Not attempted | No successful normal session end observed. |
| Fact capture, recall and briefing delivery | Not attempted | No authenticated model turn. |
| Explicit memory tool save and recall | Not attempted | No authenticated model turn. |
| User preference and cross-project isolation | Not attempted | No authenticated model turn. |
| Manual compaction and pre-compaction capture | Not attempted | No authenticated session. |
| Removal and original config comparison | Passed | Byte comparisons and absence checks above; temporary files deleted. |

This was an isolated-configuration check. MemoryKing setup uses `HOME` rather than `CODEX_HOME`; Codex documents `CODEX_HOME` for user config and authentication. A working authenticated Codex environment is needed to finish the real-session checks.

## 2026-11-27 16:29:47 UTC — Local Phase 0

- Ubuntu 26.04.1 LTS; default Python 3.14.4; Codex CLI 0.166.0. This is a local-machine session running under a restricted filesystem sandbox. `CODEX_HOME` is unset or agrees with the default `~/.codex` location.
- One noninteractive `codex exec` model session completed successfully, with `--dangerously-bypass-hook-trust --skip-git-repo-check --ephemeral`; no person typed into it. Sessions used in this local run: 1.
- `codex features list` says `hooks stable true`. `codex exec --help` documents `--dangerously-bypass-hook-trust` for enabled hooks and `$CODEX_HOME/config.toml` for user config. Current [official hook documentation](https://learn.chatgpt.com/docs/hooks) states user hooks are read from `$CODEX_HOME/hooks.json`, defaulting to `~/.codex/hooks.json`, and also supports inline config; actual behavior of this installed version remains to be tested.
- Test branch head: `0eef9f45acb0bd44374fcd9b70e0f37042812116`. Before setup, the existing config was privately backed up and matched its copy; hooks file was absent. The sandbox prevents writing the real configuration without elevated execution. Python 3.12 is not on the current PATH; installation prerequisites remain to be checked.

## 2026-11-27 16:49:38 UTC — Local Phase 1

- Installed test head `0eef9f45acb0bd44374fcd9b70e0f37042812116` as MemoryKing 0.2.0 in a fresh Python 3.12.14 virtual environment. Dry run reported creation of a scratch store and credential, creation of `~/.codex/hooks.json`, and optional semantic model download. It did **not** propose a managed MCP block: `not written: [mcp_servers.memoryking] already exists in ~/.codex/config.toml`; it printed a command and arguments for manual registration.
- Real setup with `--no-semantic` created the scratch store and control data, started its daemon, issued an agent credential, and created the hooks file; it again left the existing MCP registration unchanged. Status says semantic off, keyword search only. The hooks structure contains `SessionStart` with matcher `startup|resume|compact`, `PreCompact`, `SessionEnd`, and `UserPromptSubmit`, each with a command hook; configured timeouts respectively 30, 30, 30, 5 seconds. The commands use `memoryking.hook_cli` with corresponding event arguments. No managed block was written.
- To avoid connecting test sessions to the pre-existing registration, the acceptance session used a per-invocation `-c` override for `mcp_servers.memoryking` pointing at the scratch bridge. With `--json --dangerously-bypass-hook-trust --skip-git-repo-check --ephemeral`, an authenticated session reported the available tools as `correct`, `feedback`, `fetch`, `forget`, `hand_off`, `propose`, `recall`, `recent`, `remember`, `resume`, and `save_progress`. Codex accepted the hooks file but reported `clamping SessionEnd hook timeout to 3s`; the explicit hook-trust bypass also produced its documented warning. There were unrelated plugin icon and rollout database warnings. No normal managed-block registration was achieved because of the pre-existing entry. Sessions used: 2.
