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

### 2026-11-22 03:36 UTC: Phase 1 counts on the fixed base
I saw your 03:10 note after posting. `windows-port` now fast-forwards to `9039cd66b80f4346a82c5bfb4084fc5c7250384b` (contains `bba795b`) and is pushed. Still no code of mine on it. Native Windows, re-measured there with the same environment:

- **Run A, as shipped:** 239 collected. 80 ok, 19 failures, 138 errors, 2 skipped. 118 errors are test modules failing to import on `fcntl` (114 before; the new control-log and blob-store port tests add 4). 13 failures are child CLI processes dying on `fcntl`. The rest are the same causes as in the table above.
- **Run B, with the diagnostic `fcntl` stub:** 357 collected. 100 ok, 19 failures, 228 errors, 10 skipped. Causes:
  - 101 test modules fail to import on `socketserver.UnixStreamServer`.
  - 105 `PermissionError`s come from opening a directory to sync it. That includes the new `test_control_log_port`, so the refactor's file control log goes through the same `sync_dir`.
  - 12 child processes still hit the real `fcntl`.
  - 5 are cp1252 decode errors.
  - The others are `O_DIRECTORY`/`O_NOFOLLOW`/`mkfifo`/`getuid` and the outcome-benchmark shell cases, as before.
- No new cause appeared, and the recommendations and estimates stand. A WSL2 rerun at `9039cd6` has started (the last one took about 50 minutes). I will append its counts when it finishes, then wait for the owner's choice.

### 2026-11-22 04:23 UTC: WSL2 counts on the fixed base
WSL2 (Ubuntu 20.04, ext4, Python 3.12.14, dev lock installed with hashes), detached at `9039cd6`: **1146 tests: 1043 ok, 1 failure, 1 error, 101 skipped.** Wall time 46 minutes, user CPU 5 minutes, again run as `root`.
- **Error:** `test_contracts.test_cross_language_acceptance_and_exact_hashes`. There is no `node` in that distro (environment).
- **Failure, new on this base and intermittent:** `test_consolidation_supervisor.DaemonKeyTests.test_the_daemon_never_opens_the_key_while_its_child_does_the_work_and_reports`. The error was `'Drafted from The release train' not found in 'No proposals are waiting.\n'`. Running the class alone from `tests/` three times, it passed once and failed twice with the same assertion. It looks timing-dependent: the check reads before the child's drafts are visible. The slow WSL2 disk may be exposing it. It is yours to judge on Linux; I have not changed anything.
- **Skips:** 98 native PostgreSQL, plus 1 each for numpy, jq and the reranker model.

Phase 1 is complete. Waiting for the owner's choice of (a) or (b) and the answers to the four questions above.

### 2026-11-22 04:35 UTC: `docker exec` measurement (answer to 05:05) and acknowledgement of 05:40 to 06:15
**Clock note:** this machine's UTC clock is behind the times in your messages; my headings use my local clock.

**Setup:** Docker Desktop is installed here (engine 29.8.0, Linux containers). With no code changes, I built `deploy/local/Dockerfile` at `ef3c4f0` and followed "Container image for the local daemon" with disposable volumes: `init`, then `docker run -d`. I removed the container, volumes and image afterwards.

**Works from a Windows-side command:**
- **capture:** `docker exec -i <c> python -m memoryking capture --socket /data/store/service.sock --credential /control/store/owner.credential --project wintest --file -` with the text on stdin. It returned the observation id at position 1.
- **recall:** the same form with `--query` returned a briefing citing that observation as `[g1]`.
- **MCP stdio bridge:** `docker exec -i <c> python -m memoryking mcp ...` answered `initialize` and `tools/list` with the lean tool catalog when fed raw bytes (from Git Bash).

**Windows quirk found (it also matters for B2/B3):** Windows PowerShell 5.1 re-encodes what it pipes into a native program.
- A capture piped from PowerShell stored a leading U+FEFF (byte-order mark) in the observation text.
- The bridge fed the same JSON-RPC lines from PowerShell answered `parse_error`, even with `$OutputEncoding` set to UTF-8 without a BOM. Git Bash piping of identical lines works.
- Claude Code starts MCP servers and hooks with raw pipes, so the bridge itself is not affected. But hook and CLI text read from stdin on the Windows side should strip a leading BOM. I will handle that in B1/B2 and test it from PowerShell.

**Start-up per call, 5 warm runs each, milliseconds (median):**

| Path | Runs | Median |
| --- | --- | --- |
| `docker exec <c> true` | 174, 167, 167, 167, 168 | 167 |
| `docker exec <c> python -m memoryking --version` | 548, 521, 525, 525, 521 | 525 |
| `docker exec -i` capture through the daemon | 537, 523, 526, 535, 536 | 535 |
| `wsl.exe -d <distro> -- true` | 82, 80, 80, 77, 78 | 80 |
| `wsl.exe -d <distro> -- python -m memoryking --version` | 185, 186, 187, 187, 194 | 187 |

The container's Python start-up plus `memoryking.__main__` import costs about 360 ms. Under WSL2 it costs about 105 ms. The relay will be a small module that imports nothing from the CLI, so its own cost should sit close to the bare `true` rows. I will measure that in B2.

**Answer to "does the existing container already solve Windows":** partly.
- The daemon, store and owner-only rules work unchanged inside it.
- From Windows, everything works through `docker exec -i`: capture, recall and the bridge.
- What is missing is on the Windows side: the Windows client and CLI cannot import (`fcntl`), nothing configures the per-call relay, and the hook commands need Windows quoting.
- I will build it as the second relay in B2, beside `wsl.exe`, chosen by explicit configuration.

**Acknowledged:**
- Option (b) with phases B1 to B4.
- `uv` is updated (0.11.23 → 0.13.9). From B1 on I run the pinned Python 3.12.14 on Windows.
- `SYSTEM` ACE recorded for (a); no ACL code now.
- `.gitattributes` goes in as its own commit.
- I will post the B3 setup and lifetime design before building it.
- README (before the install block, absolute links), PyPI rendering check, and the classifier only once the client side really installs and runs.

Starting B1 now.

### 2026-11-22 05:32 UTC: B1 done; B2 built, POSIX run in progress
`windows-port` is at `527d936` (B2 plus a merge of the base at `ee1e431`). B1 is `377b720`..`1ed86e3`. Windows runs on the pinned Python 3.12.14 now.

#### B1 (import and install): done
- **`377b720` `.gitattributes`:** `* text=auto eol=lf`, as its own commit. Every tracked text file was already stored with LF, so the repository content does not change.
- **`0b9a6dd` locks:** each `.in` now pins its Windows-only dependencies with markers: dev has colorama, pywin32 and tzdata; build has colorama; shared has pywin32 and tzdata; embeddings has pyreadline3 and colorama (via tqdm). pywin32 is in the dev and shared locks only, never core (a test asserts it).
  - **What I ran, on Windows,** each lock's own header command in this order:
    - `pip-compile --allow-unsafe --generate-hashes --output-file=requirements-dev.lock requirements-dev.in`
    - build with `--allow-unsafe --constraint=requirements-dev.lock`
    - core and shared with `--constraint=requirements-dev.lock`
    - embeddings and tokenizers plain
  - **Why on Windows:** pip-compile on Linux drops a requirement whose marker is false there. I tried it, and a Linux recompile removes the Windows pins again. `tests/test_locks.py` fails if a lock loses a marked pin, and CONTRIBUTING.md ("Lock files") now says to compile on Windows.
  - **Linux verification:** I resolved every lock, before and after, with `pip install --dry-run --require-hashes --report` in WSL2. All six resolve to **exactly the same packages and versions** as before. macOS was not run; its markers are false for the same reason.
  - **Windows verification:** all six locks install with `--require-hashes`.
  - The dev lock's "unsafe packages" header moved back to where pip-compile writes it; that is cosmetic.
- **`1ed86e3` platform module (`adapters/platform_support.py`):**
  - **What moved:** `fcntl` (and `F_FULLFSYNC` for `durable_sync`), the flock lock, the owner-uid check, `O_NOFOLLOW`/`O_DIRECTORY`/`dir_fd` opens, the Unix socket and `UnixStreamServer`. On POSIX each function is exactly the replaced call, and the tests that patch `local_files.fcntl` are unchanged.
  - **On Windows every store primitive refuses** with `local_store_unsupported`. `private_dir`, `private_file`, `atomic` and `sync_dir` refuse before touching the disk, and a test checks that the directory stays empty. `daemon.start` and `daemon.stop` refuse too, because `os.kill(pid, SIGTERM)` on Windows is a hard `TerminateProcess`.
  - **CLI gate:** every command whose parser takes `--store`/`--control` refuses, except `profile`: init, serve, backup, restore, gc, control-checkpoint, journal pending and credential rotate-owner, and doctor for now (B3 rewrites it).
  - **Hint (128 characters):** "Windows runs the agent-side tools but not the memory store: run the store in WSL2 or Docker Desktop (deployment guide, Windows)."
  - **Contracts:** no contract enumerates error codes; the hint table in `domain/hints.py` plus the generated `docs/error-codes.md` is where codes are defined, and both are updated. The generator also needed UTF-8 reads to run on Windows.
  - **`encoding="utf-8"` added** (36 calls, 19 files): the schema and catalog loads at import, `__main__`, `profiles`, `setup_host`, `host_hooks`, `hooks`, `sdk`.
  - **Left unencoded on purpose** (store side or shared server): `local_store` (32 calls), `postgres_admin` (17), `postgres_recovery`, `operations_log`, `local_files` (anchor), `store_core`, `shared_service`, `postgres_pool`, `daemon`, `providers`, `vault`, `model_install`, `migration`, `egress_policy`. Tests and tools are B4.
- **Results:**
  - **Windows:** all **143 modules import**. mypy is clean with both `--platform win32` and `--platform linux`, and ruff is clean.
  - **Windows suite:** 1215 tests now collected (every module loads). 206 ok, 116 skipped, 26 failures, 927 errors. 900 of the errors are store tests refused with `local_store_unsupported`; making them explicit skips with a reason is B4. The rest are the known POSIX-shell, `/tmp`, cp1252-in-tests and benchmark causes.
  - **POSIX (WSL2, at `1ed86e3`):** 1223 tests, 1 failure, 1 error, 114 skipped. That is the same failure set as the base before my changes: the supervisor-key flake (now fixed on the base, merged) and `node` missing in the distro. Skips are 98 PostgreSQL, the longhaul systems not installed, numpy, jq and the reranker.

#### B2 (relay): built and tested on Windows; the POSIX full run at `527d936` is running
- **`memoryking relay --socket S [--credential F]`** (fast path `python -m memoryking.relay`), on the store's side:
  - It writes one handshake line, `{"relay":1,"version":V[,"credential":T]}`, then copies one request line at a time to the daemon, **one new connection per line exactly as a local client connects**, and writes back the daemon's one reply line. It never parses either.
  - The daemon closes after one request, so a long-lived relay has to reconnect per line; that is the only framing it does, splitting on newlines.
  - It refuses a socket that fails the daemon's own check (socket type, owner), and a credential file that fails the client's check (`private_file`).
  - An over-long line is forwarded only up to the daemon's limit, and the daemon refuses it exactly as on the socket. No daemon gives `transport_unavailable`. `service_busy` passes through (tested).
- **Windows client:** an explicit `--relay wsl:DISTRO` or `--relay docker:CONTAINER` (or `MEMORYKING_RELAY`), plus `--relay-python`. `--socket`/`--credential` then name paths on the store's side.
  - The relay starts without a shell: `wsl.exe -d D --exec PY -m memoryking.relay ...` or `docker exec -i C PY -m memoryking.relay ...`.
  - A relay of another version is refused before any request with `relay_version_mismatch` (detail `expected`/`found`).
  - A request is never retried; a relay that ends after a request was written is `relay_interrupted`.
  - The CLI, the hooks' connection helpers, the SDK (`LocalClient(endpoint, credential, ...)`) and the MCP bridge all take it. Four new codes, each with a hint.
- **How the Windows side authenticates without a copy on NTFS:** the token arrives in the handshake and is held only in that process's memory (`RelayCredential`, whose repr never shows it). Nothing on the Windows side writes it.
- **Windows text fixes found on the way:**
  - Text piped into `capture` and `hook` is read as UTF-8, with every leading BOM stripped (PowerShell 5.1 sends one, or two with UTF-8 `$OutputEncoding`) and CRLF turned into LF.
  - CLI stdout and stderr are UTF-8. Without that, `recall` through a pipe failed on characters outside cp1252, which would break hook output.
  - POSIX reads and writes are unchanged.
- **Tests:**
  - `test_relay.py`: real daemon, POSIX; passes in WSL2.
  - `test_relay_client.py`: stand-in relays, every platform.
  - `test_relay_windows.py`: Windows only. It creates a store and daemon on the store side, then runs CLI capture and recall, piped text, output beyond cp1252, the SDK, the MCP bridge, the refusal of a wrong socket, and a store command refused here. **8/8 pass against WSL2 (Ubuntu), and 7/7 earlier against a container** (`docker:`, image built from this tree), before the cp1252 test existed.
- **Measured** (Windows 11, WSL2 idle; medians of 5 warm runs; 20 for requests):
  - relay start through `wsl.exe`: **115 ms**
  - one request on an open relay: **33 ms**
  - a per-call Windows CLI command (for example a hook): **about 1.08 s**, of which about 870 ms is Windows Python importing `memoryking.__main__` (`python -m memoryking --version` alone takes 871 ms)
  - The import cost is a lazy-import job independent of the relay; worth doing for hooks, and I have not started it.
  - Container start-up per call is higher, as measured before (`docker exec` about 167 ms bare).
- **Found in passing:** the client picks the exact tokenizer when `tiktoken` is installed on its own side. A store side without the tokenizers extra then refuses recall with `tokenizer_unavailable`. B3 setup will install the same extras on both sides, and doctor will report a mismatch.

#### Ask first (B2)
1. **No Windows-side config file.** The relay is chosen only by `--relay`/`MEMORYKING_RELAY` flags. B3 setup would write them into Claude Code's hook and MCP commands; that settings file already decides what runs as this user, so no new file has to be trusted.
   - **Why it matters:** `profiles.load()` keeps refusing on Windows, because its `private_file` check cannot be made there without the ACL code that belongs to option (a).
   - **Question:** OK, or do you want a Windows profile file whose trust rests on the default per-user ACL of `%USERPROFILE%`? That would be a weaker check than POSIX.
2. **The credential token in the Windows process's memory**, delivered over the pipe. Any process of the same Windows user can read that memory, but the same user can also run `wsl.exe` and read the file, so I see no weakening.
   - **Question:** confirm, or say if you want the token kept on the store side. That would need the relay to insert it, which means parsing.

#### Ask first (B3, before I build it)
3. **"Exercise it for real: a Claude Code session ..." calls the model,** which is a paid model call. Your brief and my instructions both forbid model or provider calls.
   - **Proposal:** run the hook commands exactly as Claude Code would, with real event payloads (SessionStart, UserPromptSubmit, Stop, then a second SessionStart that must get the briefing), through Git Bash and through PowerShell, the two shells Claude Code uses for hooks on Windows (docs: hooks reference). Start a real `claude` session only if the owner explicitly authorizes that spend.
   - **Question:** which?
4. **Hook command form.** Claude Code runs a string hook through Git Bash, or PowerShell when Git Bash is absent, which means two quoting dialects. It also offers an exec form (`command` plus `args`) with no shell at all.
   - **Proposal:** write Windows hooks in the exec form, so no quoting is involved, as the MCP entry already does.
   - **Caveat:** I have to confirm that Claude Code 2.1.101 here honours `args`; the docs do not state a version.

The full B3 design (detection, same version on both sides, lifetime options tested, doctor, dry run and remove) comes next, once the POSIX run has finished and WSL2 is idle for the idle-stop test.

### %Y-%m-%d %H:%M UTC: B2 POSIX run
WSL2 (Ubuntu 20.04, Python 3.12.14, as root) at `527d936`: **1248 tests, 0 failures, 1 error, 122 skipped**.
- The error is `test_contracts.test_cross_language_acceptance_and_exact_hashes`: there is no `node` in that distro.
- The supervisor-key flake is gone with the base fix merged.
- Skips are 98 PostgreSQL, the longhaul systems not installed, numpy, jq, the reranker, and the Windows-only tests (`test_relay_windows`, the Windows half of `test_platform_support`).
- `test_relay` passes there through a real daemon.

B2 is complete apart from the two "ask first" answers (1, 2). B3 waits on answers 3 and 4.
