# Running the task-outcome benchmark

This is the protocol for the person who runs real agents. The tool never calls a model; you start the agent. Commands are run from the repository root with the project's Python (`PY` below).

Ten tasks (`benchmarks/outcomes/tasks/<id>/`) each have two sessions. Each session is a separate agent instance that shares nothing with the other except the task's working directory and, in the `on` arm, a MemoryKing store. The `off` arm gets the same prompts without any memory tooling. After session 2 a hidden check decides success.

## Rules for the run

- One fresh agent instance per session. Do not resume a conversation, and do not let the agent host carry its own memory between the two sessions (project memory files, saved sessions, auto-memory): give each session a fresh host profile or config directory (for example a new `HOME`), identical in both arms.
- The same model, settings, tool permissions and time limit in both arms and in both sessions. Run each task and arm at least three times (`--rep 1`, `--rep 2`, ...) if you want any estimate of run-to-run variation; randomize the order of arms and tasks.
- Tell the agent only what the prompt file says. Do not mention a benchmark, the other session, the other arm or the check. The agent must not be able to read `check.py`, `meta.json`, `reference/` or the other session's prompt: `prepare` copies only the fixture into the working directory, and everything else stays outside it. Start the agent with the working directory as its current directory and no access to this repository checkout (a sandboxed directory, or a host setting that limits file access to the working directory). The prompt tells it to work only inside the working directory; enforce that where the host allows.
- The agent may write notes into the working directory (a `NOTES.md`, comments in code). That is a legitimate way to carry knowledge in either arm and the between-step does not remove it; look at `ls` after session 1 and say in your write-up which runs left such notes.
- Never put credentials or provider keys in the working directory.

## One task and arm, step by step

```sh
PY=$PWD/.venv/bin/python           # the project's Python, as an absolute path
TASK=legacy-rounding               # any directory name under benchmarks/outcomes/tasks/
ARM=on                             # on or off
W=/tmp/mk-outcomes/work/$TASK-$ARM # a path that does not exist yet
RESULTS=/tmp/mk-outcomes/results

# 1. Prepare. Copies the fixture to $W, writes the prompts beside it ($W.bench/), and for
#    arm on starts a store and daemon in a short /tmp path and writes MEMORY.md into $W.
$PY tools/outcome_benchmark.py prepare --task $TASK --arm $ARM --workdir $W
```

It prints the two prompt files, `$W.bench/session1.prompt.md` and `$W.bench/session2.prompt.md`.

**Session 1.** Start a fresh agent with `$W` as its working directory and give it, as its first and only instruction, the literal contents of `$W.bench/session1.prompt.md`. For the `off` arm that is the scope sentence and the task text:

```
Work only inside the current directory: do not read, list or modify anything outside it.

<the task's session1.md, as the user wrote it>
```

For the `on` arm the same text ends with one more sentence, and nothing else changes:

```
This project has a memory tool; MEMORY.md in this directory explains it, and using it is allowed even though it keeps its data elsewhere.
```

In the `on` arm the agent's process needs the environment that makes `memoryking` work for this project (a profile in a private config file, a scoped credential, and a `memoryking` command on `PATH`). Get it from the tool instead of building it by hand:

```sh
( eval "$($PY tools/outcome_benchmark.py env --workdir $W)"; cd $W && exec <your agent command> )
```

`env` prints `export` lines for `PATH`, `MEMORYKING_CONFIG` and `MEMORYKING_PROFILE` and prints nothing in the `off` arm. The credential has the `capture`, `read`, `checkpoint` and `handoff` capabilities and nothing administrative. If your host speaks MCP you may also register the lean bridge for the agent: `$PY -m memoryking.adapters.stdio --socket <data>/service.sock --credential <agent.credential> --project work --profile lean`, using the `data` and `agent_credential` paths in `$W.bench/state.json`; `MEMORY.md` already says the lean tools do the same as the commands. Do the same in every `on` run.

When session 1 ends, record what it cost (any of these may be left out and the report then shows `n/a`):

```sh
$PY tools/outcome_benchmark.py record --workdir $W --n 1 --seconds 312 --input-tokens 184000 --output-tokens 9100 --cost-usd 1.42
```

**Between the sessions.** Run the task's between-step (it may delete scratch files, a slow script or sample data so the knowledge is truly not on disk; in the `on` arm the daemon and store stay up):

```sh
$PY tools/outcome_benchmark.py between --workdir $W
```

**Session 2.** Exactly as session 1, with a new agent instance and `$W.bench/session2.prompt.md`. Then `record --n 2`.

```sh
# 2. Check, then finish. check runs the hidden check and prints JSON; finish stops the
#    daemon, collects store statistics for arm on and writes the result file.
$PY tools/outcome_benchmark.py check  --workdir $W
$PY tools/outcome_benchmark.py finish --workdir $W --results $RESULTS
# -> $RESULTS/$TASK-$ARM.json   (a repeat N > 1 writes $RESULTS/$TASK-$ARM-rN.json)
```

`check` and `finish` print JSON; `finish` names the result file. The result holds `passed`, the named checks with their details, per-session seconds, tokens and cost, whether the between-step ran, and, for the `on` arm, the store's own counts: observations captured, briefings served, deliveries, checkpoints saved. `finish` removes the arm's store and stops its daemon. If a run is abandoned, run `finish` after a `check`, or stop the daemon whose pid is in `$W.bench/state.json`.

Pass `--rep N` to `prepare` for a repeat so the paired comparison keeps its pairs.

## When the agent is one command

If your agent runs as a command that takes a directory and a prompt file, the tool can drive everything, with the working directory as the agent's current directory and the environment set for the `on` arm:

```sh
$PY tools/outcome_benchmark.py run --task $TASK --arm $ARM --results $RESULTS \
  --agent-command "my-agent --cwd {workdir} --prompt-file {prompt_file}"
```

Placeholders are `{workdir}`, `{prompt_file}`, `{session}` (1 or 2) and `{usage_file}`. If the agent writes `{"input_tokens": N, "output_tokens": N, "cost_usd": X}` to `{usage_file}` the result carries it. Use `session --workdir $W --n 1 --agent-command ...` to run one session at a time. The command is split like a shell would and run without a shell. The bundled stub (`benchmarks/outcomes/stub_agent.py`) is such a command and uses no model; it is for testing the pipeline, never for results.

## Reporting

```sh
$PY tools/outcome_benchmark.py report --results $RESULTS          # Markdown tables
$PY tools/outcome_benchmark.py report --results $RESULTS --json
```

The report gives success rate with a 95% interval per arm, per task and per knowledge kind, mean tokens, seconds and cost where the runner supplied them, and an exact paired comparison (a two-sided McNemar test, which is a binomial sign test on the discordant pairs) with its p-value. Ten tasks can only show a large effect: with ten task pairs, six wins for one arm and none for the other is the smallest result that reaches p < 0.05. Repeating tasks adds pairs but not independent tasks; say so.

## What the result means

A pass means the hidden check passed for that agent, that model and that run. It is not production quality evidence, and the tasks are small fixtures built so that the carried knowledge decides the outcome. Report the model, settings, time limits, how many runs per task and arm, any run where the agent left notes in the working directory, and any run that failed for reasons unrelated to the task (an agent crash, a timeout). `validate` shows, without a model, that every task's check fails on the untouched repository and after a faithful session 1, passes the reference solution and fails the plausible wrong one:

```sh
$PY tools/outcome_benchmark.py validate            # all tasks, or --task ID
```
