# Running the task-outcome benchmark

This is the protocol for the person who runs real agents. The tool never calls a model; you start the agent. Commands are run from the repository root with the project's Python (`PY` below).

Ten tasks (`benchmarks/outcomes/tasks/<id>/`) each have two sessions. Each session is a separate agent instance that shares nothing with the other except the task's working directory and, in the memory arms, a MemoryKing store. After session 2 a hidden check decides success. There are five arms; the prompts of the first four are identical except for one closing sentence, and the fifth adds none:

| Arm | What the agent has | Closing sentence of both prompts |
| --- | --- | --- |
| `off` | Nothing. | none |
| `notes` | A short `NOTES.md` in the working directory. This is the plain-file baseline: does MemoryKing beat a notes file given the same nudge? | `You can keep notes for later sessions in NOTES.md in this directory.` |
| `on` (the CLI arm) | The `memoryking` command on `PATH`, configured through the environment, and a `MEMORY.md` explaining `capture`, `recall`, `save-progress` and `resume`. | `This project has a memory tool; MEMORY.md in this directory explains it, and using it is allowed even though it keeps its data elsewhere.` |
| `mcp` | MemoryKing as tools from the lean stdio bridge, the way an MCP-capable host connects it: `remember`, `recall`, `save_progress`, `resume` and `hand_off`. No `memoryking` command and no `MEMORY.md`. `prepare` writes a standard `{"mcpServers": {...}}` configuration to `<workdir>.bench/mcp.json`, outside the working directory. | `This project has memory tools available (remember, recall, save_progress, resume); using them is allowed.` |
| `setup` | MemoryKing as `memoryking setup claude-code` installs it for a user: the session hooks (a start briefing that names the tools, pre-compact, session end) and the lean bridge with `alwaysLoad`, under one agent credential. `prepare` writes the host settings with the hooks to `<workdir>.bench/settings.json` and the MCP configuration to `<workdir>.bench/mcp.json`, outside the working directory, for the runner to pass to the host explicitly. No `memoryking` command, no `MEMORY.md`. | none: the agent is not told about memory; the hooks and the server's instructions are the only mention. This arm asks whether memory is used without the agent being told how. |

The `on`, `mcp` and `setup` arms get a private store, a daemon and a credential limited to `capture`, `read`, `checkpoint` and `handoff`. Give every arm the same task, model and settings, and randomize the order of arms and tasks.

## Rules for the run

- One fresh agent instance per session. Do not resume a conversation, and do not let the agent host carry its own memory between the two sessions (project memory files, saved sessions, auto-memory): give each session a fresh host profile or config directory (for example a new `HOME`), identical in every arm.
- The same model, settings, tool permissions and time limit in every arm and in both sessions. Run each task and arm at least three times (`--rep 1`, `--rep 2`, ...) if you want any estimate of run-to-run variation.
- Tell the agent only what the prompt file says. Do not mention a benchmark, the other session, the other arm or the check. The agent must not be able to read `check.py`, `meta.json`, `reference/` or the other session's prompt: `prepare` copies only the fixture into the working directory, and everything else stays outside it. Start the agent with the working directory as its current directory and no access to this repository checkout (a sandboxed directory, or a host setting that limits file access to the working directory). The prompt tells it to work only inside the working directory; enforce that where the host allows.
- The agent may write notes into the working directory (a `NOTES.md`, comments in code). That is a legitimate way to carry knowledge in any arm and the between-step does not remove it. `between` records which runs left notes (see [Notes left on disk](#notes-left-on-disk)) and the report counts them for `off` and `notes`; say so in your write-up.
- Never put credentials or provider keys in the working directory.

## One task and arm, step by step

```sh
PY=$PWD/.venv/bin/python           # the project's Python, as an absolute path
TASK=legacy-rounding               # any directory name under benchmarks/outcomes/tasks/
ARM=on                             # off, notes, on, mcp or setup
W=/tmp/mk-outcomes/work/$TASK-$ARM # a path that does not exist yet
RESULTS=/tmp/mk-outcomes/results

# 1. Prepare. Copies the fixture to $W and writes the prompts beside it ($W.bench/).
#    Arm notes writes NOTES.md into $W. Arm on starts a store and daemon in a short /tmp
#    path and writes MEMORY.md into $W. Arm mcp starts the same store and daemon and
#    writes $W.bench/mcp.json.
$PY tools/outcome_benchmark.py prepare --task $TASK --arm $ARM --workdir $W
```

It prints the two prompt files, `$W.bench/session1.prompt.md` and `$W.bench/session2.prompt.md`, and for arm `mcp` the configuration path.

**Session 1.** Start a fresh agent with `$W` as its working directory and give it, as its first and only instruction, the literal contents of `$W.bench/session1.prompt.md`. For the `off` arm that is the scope sentence and the task text:

```
Work only inside the current directory: do not read, list or modify anything outside it.

<the task's session1.md, as the user wrote it>
```

For the other arms the same text ends with the one sentence from the table above and nothing else changes. For arm `on`:

```
This project has a memory tool; MEMORY.md in this directory explains it, and using it is allowed even though it keeps its data elsewhere.
```

**Arm `on`.** The agent's process needs the environment that makes `memoryking` work for this project (a profile in a private config file, a scoped credential, and a `memoryking` command on `PATH`). Get it from the tool instead of building it by hand:

```sh
( eval "$($PY tools/outcome_benchmark.py env --workdir $W)"; cd $W && exec <your agent command> )
```

`env` prints `export` lines for `PATH`, `MEMORYKING_CONFIG`, `MEMORYKING_PROFILE` and `MEMORYKING_PROJECT` (so the commands need no `--project`), and nothing in the `off` and `notes` arms. The credential has the `capture`, `read`, `checkpoint` and `handoff` capabilities and nothing administrative. Do not also register the MCP bridge in this arm: the arms differ in how the agent reaches memory.

**Arms `mcp` and `setup`.** Register the server in `$W.bench/mcp.json` with the host, and nothing else, and make sure no `memoryking` command is on the agent's `PATH`. `env` prints a `PATH` without any directory that holds a `memoryking` command and `unset` lines for any `MEMORYKING_*` variables in your shell. The file has this shape (absolute paths; the credential is a scoped private file):

```json
{"mcpServers": {"memoryking": {"command": "/abs/path/python", "args": [
  "-m", "memoryking.adapters.stdio",
  "--socket", "/tmp/mk-oc-XXXX/data/service.sock",
  "--credential", "/tmp/mk-oc-XXXX/agent.credential",
  "--project", "work", "--profile", "lean"]}}}
```

In arm `setup` the file's entry is the one `memoryking setup claude-code` prints (`type`, `command`, `args`, `alwaysLoad: true`), and `settings.json` holds the hook entries `memoryking setup claude-code` would write to the user's settings file, built by the same code (`host_hooks.install`, `setup_host.mcp_entry`). Real setup names the store by profile; here the commands carry the socket and credential paths, because each session gets a fresh home directory. The hooks and the bridge use the same agent credential.

Set every `on`, `mcp` and `setup` run up the same way.

When session 1 ends, record what it cost (any of these may be left out and the report then shows `n/a`):

```sh
$PY tools/outcome_benchmark.py record --workdir $W --n 1 --seconds 312 --input-tokens 184000 --output-tokens 9100 --cost-usd 1.42
```

**Between the sessions.** Run the task's between-step (it may delete scratch files, a slow script or sample data so the knowledge is truly not on disk; in the memory arms the daemon and store stay up). It also records which notes session 1 left and, for the memory arms, the store's statistics after session 1:

```sh
$PY tools/outcome_benchmark.py between --workdir $W
```

**Session 2.** Exactly as session 1, with a new agent instance and `$W.bench/session2.prompt.md`. Then `record --n 2`.

```sh
# 2. Check, then finish. check runs the hidden check and prints JSON; finish stops the
#    daemon, collects store statistics for the memory arms and writes the result file.
$PY tools/outcome_benchmark.py check  --workdir $W
$PY tools/outcome_benchmark.py finish --workdir $W --results $RESULTS
# -> $RESULTS/$TASK-$ARM.json   (a repeat N > 1 writes $RESULTS/$TASK-$ARM-rN.json)
```

`check` and `finish` print JSON; `finish` names the result file. The result holds `passed`, the named checks with their details, per-session seconds, tokens and cost, whether the between-step ran, which notes were left on disk, and, for the `on` and `mcp` arms, the store's own counts (observations captured, briefings served, deliveries, checkpoints saved) as they stood at `between` and at `finish`; the report uses the two to tell whether memory was written in session 1 and read in session 2. `finish` removes the arm's store and stops its daemon. If a run is abandoned, run `finish` after a `check`, or stop the daemon whose pid is in `$W.bench/state.json`.

Pass `--rep N` to `prepare` for a repeat so the paired comparison keeps its pairs.

## When the agent is one command

If your agent runs as a command, the tool drives everything: one `run` per task and arm does prepare, session 1, between, session 2, check and finish.

```sh
$PY tools/outcome_benchmark.py run --task $TASK --arm $ARM --results $RESULTS --timeout 1800 \
  --agent-command "my-agent --cwd {workdir} --prompt-file {prompt_file}"
```

The command is split like a shell would and run without a shell. Placeholders:

| Placeholder | Value |
| --- | --- |
| `{workdir}` | The task's working directory, which is also the command's current directory. |
| `{prompt_file}` | `session1.prompt.md` or `session2.prompt.md` in `<workdir>.bench/`. |
| `{session}` | 1 or 2. |
| `{mcp_config}` | Arms `mcp` and `setup`: the path of `mcp.json`. The tool refuses a run of those arms whose command lacks it, and any other arm whose command has it. |
| `{settings}` | Arm `setup` only: the path of `settings.json`, the host settings with the hooks. Required in that arm and refused in the others. |
| `{usage_file}` | A file the command may write usage figures to (below). |

`--prompt-stdin` also feeds the prompt file on the command's standard input, for agent commands that read the prompt there. Other braces (for example in a JSON argument) are left alone.

For every session the tool sets the working directory and the arm's environment (arm `on`: the configured `memoryking`; arm `mcp`: no `memoryking` on `PATH` and no `MEMORYKING_*` variables); enforces `--timeout` seconds of wall-clock time (default 3600), killing the command's whole process group on expiry and recording `timed_out`; writes the command's standard output to `<workdir>.bench/agent<N>.stdout` and its standard error to `agent<N>.stderr`; and records the measured wall time and exit code.

**Token and cost figures** are recorded automatically from the first of these that yields any:

1. A JSON file at `{usage_file}`.
2. The command's standard output read through `--usage-jq FILTER` (needs the `jq` command): a jq filter that turns whatever the agent CLI prints into an object with the keys below.
3. With no filter, the last JSON object on the standard output, read for the same keys at its top level.

| Key | Meaning |
| --- | --- |
| `input_tokens` | All input tokens the session used. If your agent reports cache reads and writes separately, add them in the filter, and do the same in every arm. |
| `output_tokens` | Output tokens. |
| `cost_usd` | Cost in US dollars as the agent CLI reports it. |
| `duration_seconds` (or `duration_ms`) | The agent's own duration; kept beside the measured wall time, which is what the report uses. |

A missing key is left out and the report shows `n/a`. Use `session --workdir $W --n 1 --agent-command ...` to run one session at a time and `record` to enter figures by hand.

### Worked example: Claude Code in headless mode

This is the shape used for the first real run (Claude Code CLI 2.1.330, `claude -p`), extended for the `setup` arm. Flags and their behaviour were checked against `claude --help` and the CLI's own configuration schema; take any change from your own version's help. The isolation requirements are those under "Rules for the run".

```sh
cat > /tmp/mk-outcomes/run-agent.sh <<'SH'
#!/bin/sh
# usage: run-agent.sh WORKDIR PROMPT_FILE [MCP_CONFIG [SETTINGS]]
set -eu
workdir=$1; prompt=$2; mcp=${3:-}; settings=${4:-}
# A fresh host profile for this session only: no saved sessions, no auto-memory, no user-level
# memory file survives from the other session. The hooks carry their own paths, so they do not
# need the old HOME.
H=$(mktemp -d /tmp/mk-host-XXXXXX)
trap 'rm -rf "$H"' EXIT
cd "$workdir"
# Scrubbed environment: PATH, proxy and certificate settings, the arm's MEMORYKING_* variables
# (none in arms mcp and setup) and the new HOME.
envargs="HOME=$H PATH=$PATH TMPDIR=/tmp LANG=C.UTF-8 CLAUDE_CODE_DISABLE_AUTO_MEMORY=1"
for v in $(env | sed -n -E 's/^((MEMORYKING_[A-Z_]*|[Hh][Tt][Tt][Pp][Ss]?_[Pp][Rr][Oo][Xx][Yy]|[Nn][Oo]_[Pp][Rr][Oo][Xx][Yy]|SSL_CERT_FILE|NODE_EXTRA_CA_CERTS|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE|ANTHROPIC_BASE_URL))=.*/\1/p'); do
  eval "val=\${$v}"; envargs="$envargs $v=$val"
done
tools="Bash(python -m unittest:*),Bash(python3 -m unittest:*),Bash(python:*),Bash(python3:*)"
# --setting-sources "" turns user, project and local settings off; settings passed with
# --settings stay on (the CLI always adds its flag and policy sources). --strict-mcp-config
# loads only the servers of --mcp-config.
set -- -p --model sonnet --output-format stream-json --verbose --include-hook-events \
  --max-turns 60 --permission-mode acceptEdits --permission-prompts none \
  --setting-sources "" --disable-slash-commands --strict-mcp-config
if [ -n "$mcp" ]; then
  tools="$tools,mcp__memoryking__remember,mcp__memoryking__recall,mcp__memoryking__save_progress,mcp__memoryking__resume,mcp__memoryking__hand_off,mcp__memoryking__fetch"
  set -- "$@" --mcp-config "$mcp"
elif [ -n "${MEMORYKING_CONFIG:-}" ]; then
  tools="$tools,Bash(memoryking:*)"
fi
[ -n "$settings" ] && set -- "$@" --settings "$settings"
# shellcheck disable=SC2086
env -i $envargs claude "$@" --allowed-tools "$tools" -- "$(cat "$prompt")"
SH
chmod +x /tmp/mk-outcomes/run-agent.sh

JQ='{input_tokens: (.usage.input_tokens + (.usage.cache_read_input_tokens // 0)
                    + (.usage.cache_creation_input_tokens // 0)),
     output_tokens: .usage.output_tokens, cost_usd: .total_cost_usd,
     duration_seconds: (.duration_ms / 1000)}'

# Arm setup: both files, three repetitions (repeat for each task and rep, in random order).
$PY tools/outcome_benchmark.py run --task $TASK --arm setup --rep 1 --results $RESULTS \
  --timeout 900 --usage-jq "$JQ" \
  --agent-command "/tmp/mk-outcomes/run-agent.sh {workdir} {prompt_file} {mcp_config} {settings}"
```

For arm `mcp` drop `{settings}`; for the others drop both placeholders. In arm `on`, the wrapper allows `Bash(memoryking:*)` when the environment has `MEMORYKING_CONFIG`. The hook commands are run by the host itself, so no tool permission is needed for them.

What this relies on, and how each part is known:

- **MCP tools are loaded lazily by default.** The first run's transcripts show the memory tools listed by name only, and the model fetching a schema with its tool-search tool before the first call. The server option `alwaysLoad: true` (in an `--mcp-config` entry) keeps all of a server's tools in the prompt, and a tool's `_meta` `anthropic/alwaysLoad` does the same for that tool; both are in the CLI's own configuration schema and tool loader. The `setup` arm's configuration sets the first, and the bridge sets the second.
- **`--settings` still applies with `--setting-sources ""`.** The CLI's settings loader always adds the flag and policy sources to whatever `--setting-sources` allows. Confirm in the first stream of a `setup` run: with `--include-hook-events` the stream records each hook's start and result, and `memoryking` hook commands should appear for `SessionStart` at the beginning and `SessionEnd` at the end. A `setup` run whose stream shows no hook events is a driver fault, not an agent result: stop and look before running more.
- **A `SessionStart` hook's output reaches the model.** Documented for the CLI and consistent with its code; the transcript of a `setup` run should contain the briefing text beginning "MemoryKing is this project's memory". Verify once.
- **`SessionEnd` hooks run when a headless session shuts down**, with the timeout of the hook entry (10 seconds as installed; the CLI's own default is 1.5 seconds). Verify by the store's counts after session 1: `between` records observations captured, and arm `setup` should show at least the two hook captures (`claude-session` and `user-claude`). A session killed by `--timeout` may leave none; that is real behaviour, count it as such.
- **Fresh instance per session, the host's own memory off, least-permissive tools, no access to this repository**: as for every arm. Use a throwaway two-session probe once (a made-up codeword in session 1, asked for in session 2) to confirm that nothing but the arm's own mechanism carries it; in arm `setup` the codeword is expected to carry through the hooks, so run the probe in arm `off` to confirm the isolation.
- **Usage keys.** Confirm the `--usage-jq` filter on one real result; a filter that matches nothing records `n/a`, not an error.

Observed in the second run (CLI 2.1.330): with `--include-hook-events` the stream shows the start hook and its output but not the session-end hook, which runs after the result line; add `--debug-file` to read its completion (`SessionEnd ... completed with status 0`) and confirm by the captures in the store. The `setup` and `mcp` allowed-tools list includes `mcp__memoryking__fetch`, because the briefing tells the agent to use it; the second run left it out, so its `fetch` calls were refused.

Run each task and arm at least three times (`--rep 1`, `--rep 2`, `--rep 3`) and read the result with `report --rep`.

## Reporting

```sh
$PY tools/outcome_benchmark.py report --results $RESULTS          # Markdown tables
$PY tools/outcome_benchmark.py report --results $RESULTS --json
```

The report takes any number of arms and shows:

- success rate with a 95% interval per arm, per task and per knowledge kind;
- tokens, cost and wall time per arm as mean and median per run, both sessions summed, and `n/a` where the runner did not record them (a run missing a figure is left out of that figure);
- for each pair of arms, an exact paired comparison (a two-sided McNemar test, which is a binomial sign test on the tasks where exactly one of the two arms passed) with its p-value and a reading in plain words;
- for the `on`, `mcp` and `setup` arms, how many runs actually used memory: something written in session 1 (observations or checkpoints in the store's own statistics at `between`) and something read in session 2 (briefings or deliveries that grew between `between` and `finish`). A run where the agent never touched memory is an `off` run in practice, so look at these counts before reading the success rates;
- for `off` and `notes`, how many runs left notes on disk during session 1;
- with `--rep`, the results over repetitions: the pass rate per arm with an interval that resamples tasks (repetitions of one task are not independent runs; the Wilson interval beside it treats every run as independent and is too narrow), and paired tests over task-by-repetition pairs (too small a p-value for the same reason) and over tasks (each task counts once; ties dropped).

Ten tasks can only show a large effect: between two arms, six wins for one and none for the other is the smallest result that reaches p < 0.05. Repeating tasks adds pairs but not independent tasks; say so. With five arms there are ten pairs and nothing corrects for looking at all of them.

### Notes left on disk

`between` compares the working directory as session 1 left it, before the between-step removes anything, with a copy taken at `prepare`. A run left notes when any of these hold:

- a new file whose extension is not code, configuration or data (`.py .pyi .sh .js .ts .json .toml .yaml .yml .cfg .ini .csv .sql .html .css .lock .conf`), such as `NOTES.md`, `scratch.txt` or a file with no extension;
- an existing non-code file whose text changed, which includes the `NOTES.md` that arm `notes` starts with;
- a Python file whose only change is added or edited comments, blank lines or docstrings (compared as syntax trees with docstrings removed), or a new Python file that holds nothing but comments or a docstring.

Caches (`__pycache__`, `.pytest_cache`) and deletions are ignored. The heuristic does not see comments added in a file that also has code changes, and it counts an edit to a fixture's own Markdown file as a note, so the result file lists the files found; read them before quoting the count.

## What the result means

A pass means the hidden check passed for that agent, that model and that run. It is not production quality evidence, and the tasks are small fixtures built so that the carried knowledge decides the outcome. Report the model, settings, time limits, how many runs per task and arm, any run where the agent left notes in the working directory, and any run that failed for reasons unrelated to the task (an agent crash, a timeout). `validate` shows, without a model, that every task's check fails on the untouched repository and after a faithful session 1, passes the reference solution and fails the plausible wrong one:

```sh
$PY tools/outcome_benchmark.py validate            # all tasks, or --task ID
```
