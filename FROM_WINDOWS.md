# Reports from the Windows session

### 2026-11-22 03:34 UTC: Phase 1 report (stand up and measure)

`windows-port` is pushed at `c1c49bd7d535ff59eb0413b43274b58d85118816`. That is the base `ccr-ad92928b-yjcxde` with no changes: phase 1 changes no code. Per your 02:45 message I stop after this report and wait for the owner's choice between (a) and (b).

#### Host and toolchain
- Windows 11 Pro 64-bit, build 10.0.26200. NTFS volumes. Git for Windows with its default `core.autocrlf=true`.
- **Python: 3.12.13, not the pinned 3.12.14.** The uv on this host (0.11.23) offers 3.12.13 at most. python.org ships no Windows installers for 3.12 security releases. A newer uv does provide 3.12.14 (WSL2 below used uv 0.13.7 and installed it), so phase 2 can use the pinned version once the host uv is updated. I did not change the host's tools to get it.
- **Node 22.23.1 and npm 10.9.8, as pinned.** Node came from nvm-windows (`nvm install`, global default left alone). nvm's bundled npm 10.9.8 was missing `@npmcli/config` (a broken extraction) and would not start for either 22.21.1 or 22.23.1. I installed `npm@10.9.8` from the registry into a scratch prefix and ran it under Node 22.23.1.

#### What installs
- `requirements-dev.lock` with `--require-hashes` **fails** on Windows. The first missing package is `colorama`, and there are three in all. Every wheel the lock does pin installs: the lock already carries the `win_amd64` hashes for all 12 compiled packages (ast-serialize, cffi, charset-normalizer, cryptography, librt, mypy, psycopg-binary, pydantic-core, regex, rpds-py, ruff, tiktoken), because pip-compile records every file of the pinned release. I confirmed this by hashing downloaded Windows wheels against the lock and with a `--require-hashes` dry run that succeeds once the missing packages are added.
- What is missing is **Windows-only transitive packages behind environment markers**, which a Linux pip-compile run leaves out:

  | Lock | Missing on Windows | Pulled in by |
  | --- | --- | --- |
  | core | none (installs with hashes) | |
  | tokenizers | none (installs with hashes) | |
  | dev | colorama, pywin32, tzdata | build (`os_name == "nt"`), mcp 2.3.0 (`sys_platform == "win32"`), psycopg (`sys_platform == "win32"`) |
  | build | colorama | build |
  | shared | pywin32 | mcp 2.3.0 |
  | embeddings | pyreadline3 | onnxruntime → coloredlogs → humanfriendly |

- **pywin32 note:** mcp 2.3.0 hard-requires `pywin32>=311` on Windows, and `import mcp` fails without it (its stdio server imports `pywintypes`). Only the `shared` extra and the dev lock need it, plus the lazy `mcp.types` import in `integrations/claude_agent_sdk.py`. The core install and the local profile do not, so the "no pywin32" rule holds for the product. The dev and shared locks will contain it on Windows.
- **Proposed lock fix (phase 2, not done yet):** keep pip-compile and add the marker packages through one committed constraints file, or rerun pip-compile with the Windows markers resolved, so each lock carries the extra pins and hashes and stays valid on all three platforms. I will report the exact command and the diff when I run it.
- For measuring, I installed the dev lock's exact versions without the hash check plus `colorama==0.4.6`, `pywin32==312` and `tzdata==2026.6`, then the package in editable mode with `--no-build-isolation --no-deps`. `pip check` is clean.
- `npm ci --ignore-scripts` and `npm run build` succeed. Because of `autocrlf=true`, the build's LF output makes `git status` mark the 7 generated `src/memoryking/adapters/web/*.js` files as modified with no content change. Recommendation: add a `.gitattributes` with `* text=auto eol=lf`. POSIX checkouts would see no change.

#### What imports
Walking every `memoryking` module: **67 import, 72 fail, all from `import fcntl`**. That includes `__main__`, `sdk` and `daemon`, so no CLI, client or bridge.

#### Test suite on native Windows (`python -m unittest discover -s tests -v`, 131 test modules)
**Run A, as shipped:** 231 collected. 80 ok, 16 failures, 133 errors, 2 skipped (jq missing; numpy is in the embeddings extra). Of the 133 errors, 114 are whole test modules that cannot import, so this run hides most of the suite.

| Count | Kind | Cause |
| --- | --- | --- |
| 114 | error | Test module import fails: `No module named 'fcntl'` |
| 10 | fail | A child `memoryking` process dies on `fcntl` (CLI subprocess tests) |
| 6 + 2 + 5 | error / error / fail | `test_outcome_benchmark`: `bash` on PATH is the WSL launcher, `.sh` task steps, POSIX process-group timeout, child CLI import |
| 4 | error | `os.O_DIRECTORY` missing (`repository_git`) |
| 1 | error | `os.mkfifo` missing |
| 3 | error | `UnicodeDecodeError` from the cp1252 default encoding (`read_text()` without `encoding`) |
| 1 | error | `TimeoutError` in `test_shared_gateway` (not investigated; shared, out of scope) |

**Run B, diagnostic only:** I added a no-op `fcntl` stub on `PYTHONPATH` from a scratch directory (never committed) to see the next layer. 322 collected. 92 ok, 16 failures, 207 errors, 7 skipped.

| Count | Cause | Phase it belongs to |
| --- | --- | --- |
| 99 | Test module import fails: `socketserver.UnixStreamServer` missing | 3 (transport) |
| 88 | `PermissionError` from `sync_dir`: `os.open(directory)` is refused on Windows | 2 (durability) |
| 9 | Child CLI process still hits the real `fcntl` | 2 |
| 13 | outcome-benchmark POSIX shell and process groups, as above | 5 (skip with a reason) |
| 4 + 1 + 1 | `O_DIRECTORY`, `O_NOFOLLOW` (ownership cleanup), `mkfifo` | 2 |
| 1 | `os.getuid` (`session_bindings`) | 2 |
| 3 | cp1252 `UnicodeDecodeError` | 2 |
| 1 | shared gateway `TimeoutError` | out of scope |

Other observations:
- Tests and tools that use `/tmp` resolve it to the drive root (`F:\tmp`) on Windows: 40 test files and 36 tool files.
- 549 `read_text`/`write_text` calls without `encoding` across 142 files (30 src, 42 tools, 70 tests).

**TypeScript (`node --test tests/js/*.test.mjs`, Node 22.23.1):** 18 tests. 16 pass, 1 skip (shared MCP HTTP), 1 fail. The failure is `local daemon: recall over the loopback agent endpoint`: the test helper's Python daemon dies on `fcntl` (with `MEMORYKING_PYTHON` set; without it the default `python3` resolves to the Microsoft Store alias). The TypeScript client itself has no observed Windows problem.

#### WSL2 on the same machine
WSL2 is available: Ubuntu 20.04, kernel 5.15 (microsoft-standard-WSL2). I made a fresh clone of `windows-port` into the distro's ext4 filesystem, not `/mnt/f`.
- **Install:** Python 3.12.14 (pinned, via uv 0.13.7). `pip install --require-hashes -r requirements-dev.lock` installs cleanly, the editable install works, and `pip check` is clean. Node is not installed in that distro, so the TypeScript checks were not run there.
- **Tests:** `python -m unittest discover -s tests -v` ran **1072 tests: 973 ok, 0 failures, 1 error, 98 skipped.**
  - The error is `test_contracts.test_cross_language_acceptance_and_exact_hashes`: `node` is not on PATH (environment, not code).
  - Skips: 95 native PostgreSQL not configured (`MEMORYKING_PG_BIN` unset), 1 numpy (embeddings extra), 1 jq missing, 1 no reranker model installed.
- **Caveats:**
  - This distro's default user is misconfigured locally, so the run was as `root`. The mode and owner tests therefore ran as uid 0, not as an ordinary user. A run as an ordinary user is still owed before (b) is claimed.
  - The run took 50 minutes of wall time on a 32-thread host (user CPU 5 min). That is far slower than the few minutes `check.py` is meant to take. I have not explained it and did not investigate; likely I/O or fsync cost on the WSL2 virtual disk, or contention with my other runs. Worth knowing before WSL2 is recommended for interactive use.

#### Platform facts measured here (CPython 3.12.13, Windows 11)
- `socket.AF_UNIX` is absent. `socket.socket(1, SOCK_STREAM)` succeeds, because the kernel has afunix, but `bind()` with a str or bytes path raises `OSError: bad family`. CPython cannot use Windows AF_UNIX without raw ctypes Winsock.
- `os.O_NOFOLLOW`, `os.O_DIRECTORY` and `dir_fd` support are all absent.
- `os.open(directory)` followed by `os.fsync` raises `PermissionError`. Opening the directory with `CreateFileW(GENERIC_WRITE, FILE_FLAG_BACKUP_SEMANTICS)` and calling `FlushFileBuffers` returns success on NTFS; a read-only directory handle gets `ERROR_ACCESS_DENIED`.
- `_winapi.CreateNamedPipe` exists but cannot take a security descriptor, so an owner-only pipe needs ctypes `CreateNamedPipeW`. No pywin32 is needed.

#### The four "ask first" recommendations (for option a)
**1. Transport: a named pipe with an owner-only DACL. Not loopback TCP.**
- **Name:** `\\.\pipe\memoryking-<hex digest of user SID + control directory>`, also written to the existing discovery file.
- **Server:** created with `CreateNamedPipeW`:
  - byte mode;
  - `PIPE_REJECT_REMOTE_CLIENTS`;
  - `FILE_FLAG_FIRST_PIPE_INSTANCE` on the first instance, so startup fails if anyone else already holds the name (squatting);
  - a protected DACL granting only the owner's SID (SDDL `D:P(A;;GA;;;<sid>)`, owner `O:<sid>`).
- **Peer checks in both directions:**
  - The server checks each client's token user via `GetNamedPipeClientProcessId` → `OpenProcessToken`, as the `SO_PEERCRED`-style check does.
  - The client checks the server the same way via `GetNamedPipeServerProcessId`, so it never sends a credential to an impostor holding the name while the daemon is down.
- **Protocol:** framing, `WIRE_LIMIT` and `service_busy` stay as they are: newline JSON over a byte stream, with one spare listening instance that accepts, answers `service_busy` and closes when the worker and waiting limits are full.
- **Why not TCP:**
  - Any local account and any local process can reach a loopback port, so a secret file becomes the only control.
  - A port has to be discovered, and it can raise firewall prompts.
  - Unlike the dashboard, the raw wire protocol has no `Host`/`Origin` checks against browser-originated requests.
- **Why not AF_UNIX:** see the facts above.

**2. Owner-only files and directories.**
- **Creation:** create through `CreateFileW` / `CreateDirectoryW` with a `SECURITY_ATTRIBUTES` built from SDDL. Set the owner explicitly to the current token user, because an elevated token would otherwise make `BUILTIN\Administrators` the owner. Use a protected DACL allowing only that SID, inheritable (`OICI`) on directories, so files a plain `open()` creates inside also come out owner-only. Applying the ACL at creation leaves no window.
- **Check:** this replaces `st_uid`/mode. Read owner and DACL with `GetSecurityInfo` on an already open handle, so there is no path race. Require:
  - the owner equals the current user SID;
  - the DACL is present (a NULL DACL means everyone);
  - every allow ACE is for the user, or for `SYSTEM` (root-equivalent, as root is under the POSIX mode check).
  
  Refuse anything else, including `Administrators`, `Users`, `Authenticated Users` and `Everyone`. Refuse volumes without ACLs (FAT, exFAT, most network shares). Behave as POSIX does today, refusing rather than repairing (to be confirmed against the code in phase 2).
- **Question for the owner:** is allowing a `SYSTEM` ACE acceptable? Defender and backup tools run as SYSTEM, and some tools add it.

**3. Durability claim in place of "fsync the directory after rename".**
- **Mechanism:**
  - File data: `os.fsync`, which is `FlushFileBuffers` on Windows.
  - Renames: `MoveFileExW(REPLACE_EXISTING | WRITE_THROUGH)`.
  - `sync_dir`: open the directory with `GENERIC_WRITE | BACKUP_SEMANTICS` and call `FlushFileBuffers`, which the facts above show succeeds on NTFS.
- **Claim for SECURITY.md and the deployment guide:** "On NTFS and ReFS a completed atomic replace has been flushed through the file system's metadata journal using the documented flush calls. Stores on volumes that refuse the flush are refused." Then the limits, said plainly: not power-loss tested; a drive or a Windows policy that disables write-cache flushing defeats it, as a lying disk does on POSIX; there is no `F_FULLFSYNC` equivalent beyond `FlushFileBuffers`.

**4. Clean stop and supervision.**
- **Stop:** `memoryking stop` sends a `shutdown` request over the owner-only pipe, and the daemon runs the same path as its SIGTERM handler. The daemon also handles Ctrl+C, Ctrl+Break (`SIGBREAK`) and the console close, logoff and shutdown events (`SetConsoleCtrlHandler` via ctypes, since the `signal` module maps only Ctrl+C and Ctrl+Break). `stop` waits for the pipe to go away and the PID to exit. `TerminateProcess` is used only with an explicit `--force`, reported as unclean, so the existing recovery path covers it.
- **Supervision:** a per-user Scheduled Task (at logon, restart on failure, `pythonw -m memoryking serve`). It runs as the store's owner without admin rights. A Windows Service would run under another account, which breaks the owner-only model unless it holds the user's password, so I do not recommend one. The task would be qualified the way systemd is: documented and rehearsed locally, with no claim beyond that.
- **Lock:** `owner.lock` uses `LockFileEx` (exclusive, fail immediately, byte 0) on a file the code never reads. The lock is released on handle close or process exit.

#### Size and risk estimates
**(a) Full native port.**
- **Files:**
  - New: one platform module, about 500 to 700 lines (ctypes ACL, pipe server and client, lock, flush, replace retry, console events).
  - About 25 to 30 source files touched: `local_files`, `local_ipc`, `sdk`, `daemon`, `__main__`, `shared_control` (import only), `repository_git`, `harness`, `session_bindings`, `providers`, host hooks and setup, the 12 files with mode or owner checks, and encoding fixes in about 30 src files.
  - About 40 tool files: encoding and `/tmp`.
  - About 70 to 80 test files: encoding, `/tmp`, explicit skips, and Windows variants of the mode and owner tests.
- **Risk: high where it counts.**
  - The ACL code is the credential security boundary.
  - The named-pipe server re-implements socketserver's threading, framing and `service_busy` on overlapped I/O.
  - Unlink-while-open and replace retries interact with SQLite WAL cleanup.
  - Every later POSIX-touching change will need a Windows rerun, and there is no Windows job in `local_ci.py`.
- **Effort:** I estimate it is the largest item left on the roadmap.

**(b) Small port: client, CLI and MCP stdio bridge on Windows, daemon in WSL2.** See "How a native Windows client reaches a daemon in WSL2" for the transport.
- **Files:**
  - About 8 to 12 source files: make `fcntl`, `socketserver.UnixStreamServer` and the owner checks lazy or platform-gated, so the client, CLI and bridge import; add a WSL relay transport to `sdk.py` and the bridge; make store-side CLI commands (`init`, `serve`, `backup`, `restore`, `gc`) refuse on Windows with a pointer to WSL2; encoding fixes on the client path; Windows quoting in `setup claude-code` and the hooks, still needed because hooks run on the Windows host.
  - A Linux-side relay command of about 50 lines.
  - Tests: Windows skips for store-level modules, plus a small client test through a real WSL daemon.
- **Risk: low to medium.** No store, durability, ACL or credential code changes: the store stays on ext4 under the POSIX guarantees already documented.
- **Risks to settle:**
  - WSL2 stops a distro some time after its last `wsl.exe` session ends, so the daemon's lifetime and supervision need a decision. Likely options are the relay starting the daemon on demand, or a logon Scheduled Task holding `wsl.exe -- memoryking serve`. I have not tested this yet.
  - The client depends on a working WSL2 install, which some managed machines disable.

#### How a native Windows client reaches a daemon in WSL2
I considered four paths:
1. **Relay through `wsl.exe` (recommended).** The Windows client, CLI or bridge spawns `wsl.exe -d <distro> -- memoryking relay --socket <path>`. That small Linux-side command connects to the daemon's Unix socket and copies newline-JSON between its stdin/stdout and the socket.
   - **What protects it:** no network listener and no new secret. Only the Windows user who owns the distro can start it: WSL distros are registered per Windows user, and another account cannot `wsl.exe` into them. Inside WSL, the existing 0600 socket, the uid check and the credential file apply unchanged. Framing, `WIRE_LIMIT` and `service_busy` pass through untouched, because the relay does not parse.
   - **Cost:** about 80 ms to start `wsl.exe` on this machine (5 warm runs: 79 to 89 ms) per connection. A long-lived bridge (MCP stdio) pays it once. Per-call CLI and hook commands pay it on each call, which is acceptable for hooks; the Python client can keep one relay open.
   - **Credential:** the credential file stays in WSL. The relay can serve the client's credential read, so the Windows side never needs a copy on NTFS.
2. **WSL2 localhost forwarding to the dashboard's opt-in loopback port.** `POST /api/v1/retrieval` is reachable from Windows as `127.0.0.1:<port>` and is protected by the bearer credential plus exact `Host`/`Origin` checks. It covers only retrieval commands, not capture, checkpoints or the rest of the socket protocol. Any local Windows account can reach the forwarded port, so the bearer secret is the only control, and the credential would have to be copied to or read from the Windows side. This would suit a read-only TypeScript client, not the full client or bridge.
3. **New TCP listener in WSL.** It has the same exposure as 2 and needs a new secret and a new surface. Not recommended.
4. **Hyper-V sockets (`AF_HYPERV` on Windows, `AF_VSOCK` in Linux, both in CPython 3.12).** No network exposure, but the WSL2 VM id changes on each boot and finding it needs admin tools. Not practical.

AF_UNIX interop between Windows and WSL works only for WSL1, not WSL2, so the existing socket cannot be shared directly.

#### No questions block phase 1. Open questions for the owner's choice
1. (a) or (b)?
2. For (a): is allowing a `SYSTEM` ACE on owner-only files acceptable (recommendation 2)?
3. May I add `.gitattributes` with `* text=auto eol=lf`? It is a one-line shared file and changes nothing on POSIX.
4. May I update this host's uv so phase 2 runs on the pinned 3.12.14?
