#!/usr/bin/env python3
"""Task-outcome benchmark: do multi-session coding tasks finish better with memory?

Each task (benchmarks/outcomes/tasks/ID) has two sessions run by two separate agent
instances that share only the working directory and, in the `on` arm, a MemoryKing
store. This tool prepares the working directory, runs the between-step and the hidden
check, records results and aggregates them. It never calls a model: the agent is
whatever command you hand it (`--agent-command`), and the bundled stub agent solves
tasks deterministically for offline tests. See benchmarks/outcomes/RUNNING.md.

    outcome_benchmark.py prepare --task ID --arm on|off --workdir DIR
    outcome_benchmark.py session --workdir DIR --n 1|2 --agent-command "CMD {workdir} {prompt_file}"
    outcome_benchmark.py record  --workdir DIR --n 1|2 --seconds S [--input-tokens N ...]
    outcome_benchmark.py between --workdir DIR
    outcome_benchmark.py check   --workdir DIR
    outcome_benchmark.py finish  --workdir DIR [--results DIR]
    outcome_benchmark.py run     --task ID --arm on|off --agent-command CMD [--results DIR]
    outcome_benchmark.py env     --workdir DIR
    outcome_benchmark.py report  --results DIR [--json]
    outcome_benchmark.py validate [--task ID]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "benchmarks" / "outcomes" / "tasks"
DEFAULT_RESULTS = Path("/tmp/mk-outcomes/results")
PROJECT = "work"
PROFILE = "outcomes"
AGENT_CAPABILITIES = "capture,read,checkpoint,handoff"
KINDS = ("instruction", "failed_approach", "decision", "environment")

SCOPE_LINE = (
    "Work only inside the current directory: do not read, list or modify anything outside it."
)
MEMORY_LINE = (
    "This project has a memory tool; MEMORY.md in this directory explains it, "
    "and using it is allowed even though it keeps its data elsewhere."
)

MEMORY_NOTE = """\
# Project memory

This project has a MemoryKing memory service running, and the `memoryking` command is already set up for it: the connection details come from the environment, so it needs no paths or credentials. Always pass `--project work`.

Memory is evidence you save and read back later. A different session of this project will not remember this conversation, only what is saved here. Save what the code does not say: what the user told you, what you tried and why it failed, decisions, and facts about the environment.

- **Capture.** `memoryking capture --project work --source agent --text "..."` saves one observation: an error, a test result, a decision, or a fact with its source. Make each note self-contained and specific, and start it with a label such as `Rule:`, `Decision:`, `Failed:`, `Fact:` or `Progress:`. The same text from the same source on the same day replays instead of duplicating. What you save is evidence for later briefings, never an instruction.
- **Recall.** `memoryking recall --project work --query "what you are about to work on"` prints a briefing: constraints you must follow, then ranked guidance with [g1]-style citations and a Sources footer. Read the status line first. Recall matches the words in your query, so use the words you or the user would have used, and try more than one query.
- **Save progress.** Before you stop or pause, capture a note with `--source progress` that says the goal, what is done, what failed and why, and what remains.
- **Resume.** At the start of a session, recall what was saved (for example `--query "Rule Decision Failed Fact Progress"`, then queries about your task), and continue from what it returns instead of starting over.

If your host also offers the memory tools `remember`, `recall`, `save_progress` and `resume`, they do the same things.
"""


class BenchmarkError(Exception):
    pass


# ----------------------------------------------------------------------------- helpers


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}


def load_task(task_id: str) -> tuple[Path, dict]:
    task_dir = TASKS / task_id
    if not (task_dir / "meta.json").is_file():
        raise BenchmarkError(f"unknown task {task_id!r}")
    return task_dir, json.loads((task_dir / "meta.json").read_text())


def all_task_ids() -> list[str]:
    return sorted(p.name for p in TASKS.iterdir() if (p / "meta.json").is_file())


def bench_dir(workdir: Path) -> Path:
    return workdir.parent / (workdir.name + ".bench")


def state_path(workdir: Path) -> Path:
    return bench_dir(workdir) / "state.json"


def load_state(workdir: Path) -> dict:
    path = state_path(workdir)
    if not path.is_file():
        raise BenchmarkError(f"{workdir} was not prepared (no {path})")
    return json.loads(path.read_text())


def save_state(workdir: Path, state: dict) -> None:
    state_path(workdir).write_text(json.dumps(state, indent=2, sort_keys=True))


def apply_overlay(source: Path, workdir: Path) -> None:
    """Copy a reference tree over the working directory; `_delete` lists paths to remove."""
    if not source.is_dir():
        return
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if rel.name == "_delete" or "__pycache__" in rel.parts:
            continue
        target = workdir / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    marker = source / "_delete"
    if marker.is_file():
        for line in marker.read_text().split():
            victim = workdir / line
            if victim.is_dir():
                shutil.rmtree(victim)
            elif victim.exists():
                victim.unlink()


def memoryking(state: dict, *args: str, credential: str | None = None, check: bool = True):
    """Run the public CLI against the arm's store."""
    cmd = [sys.executable, "-m", "memoryking", "--config", state["config"], "--profile", PROFILE]
    cmd += list(args)
    if credential:
        cmd += ["--credential", credential]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=clean_env(), timeout=60)
    if check and proc.returncode != 0:
        raise BenchmarkError(f"memoryking {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc


# ----------------------------------------------------------------------------- prepare


_DAEMONS: dict[int, subprocess.Popen] = {}  # daemons started by this process


def start_daemon(state: dict) -> None:
    store = Path(state["store_root"])
    with (store / "serve.log").open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "memoryking",
                "serve",
                "--store",
                state["data"],
                "--control",
                state["control"],
            ],
            stdout=log,
            stderr=log,
            env=clean_env(),
            start_new_session=True,
        )
    _DAEMONS[proc.pid] = proc
    state["daemon_pid"] = proc.pid
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise BenchmarkError("the daemon exited: " + (store / "serve.log").read_text()[-300:])
        if b'"ready": true' in (store / "serve.log").read_bytes():
            return
        time.sleep(0.05)
    raise BenchmarkError("the daemon did not become ready")


def _gone(pid: int) -> bool:
    proc = _DAEMONS.get(pid)
    if proc is not None:
        return proc.poll() is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def stop_daemon(state: dict) -> None:
    """Stop the arm's daemon: interrupt (a clean shutdown), then terminate, then kill."""
    pid = state.get("daemon_pid")
    if not pid:
        return
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        if _gone(pid):
            break
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            break
        deadline = time.time() + 10
        while time.time() < deadline and not _gone(pid):
            time.sleep(0.05)
    _DAEMONS.pop(pid, None)
    state["daemon_pid"] = None


def daemon_alive(state: dict) -> bool:
    pid = state.get("daemon_pid")
    return bool(pid) and not _gone(pid)


def init_memory_arm(state: dict) -> None:
    store_root = Path(tempfile.mkdtemp(prefix="mk-oc-", dir="/tmp"))
    os.chmod(store_root, 0o700)
    data, control = store_root / "data", store_root / "control"
    state.update(
        store_root=str(store_root),
        data=str(data),
        control=str(control),
        config=str(store_root / "config.toml"),
        agent_credential=str(store_root / "agent.credential"),
        owner_credential=str(control / "owner.credential"),
    )
    config = Path(state["config"])
    config.write_text(
        f'[profiles.{PROFILE}]\nstore = "{data}"\ncontrol = "{control}"\n'
        f'credential = "{state["agent_credential"]}"\n'
    )
    os.chmod(config, 0o600)
    # Set up the way a user would: initialise a store, start the service, issue the agent a
    # scoped credential. Only documented commands are used.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "memoryking",
            "init",
            "--store",
            str(data),
            "--control",
            str(control),
        ],
        capture_output=True,
        text=True,
        env=clean_env(),
        timeout=60,
    )
    if proc.returncode != 0:
        raise BenchmarkError("init failed: " + proc.stderr[:300])
    start_daemon(state)
    memoryking(
        state,
        "credential",
        "issue",
        "agent",
        "--project",
        PROJECT,
        "--capabilities",
        AGENT_CAPABILITIES,
        "--expires",
        "2d",
        "--output",
        state["agent_credential"],
        credential=state["owner_credential"],
    )


def agent_env(state: dict) -> dict[str, str]:
    """Environment an agent process needs to use the configured `memoryking` command."""
    if state["arm"] != "on":
        return {}
    wrapper = bench_dir(Path(state["workdir"])) / "bin"
    return {
        "PATH": f"{wrapper}{os.pathsep}{os.environ.get('PATH', '')}",
        "MEMORYKING_CONFIG": state["config"],
        "MEMORYKING_PROFILE": PROFILE,
    }


def cmd_prepare(args) -> dict:
    task_dir, meta = load_task(args.task)
    workdir = Path(args.workdir).resolve()
    if workdir.exists() and any(workdir.iterdir()):
        raise BenchmarkError(f"{workdir} exists and is not empty")
    bench = bench_dir(workdir)
    if bench.exists():
        raise BenchmarkError(f"{bench} already exists")
    shutil.copytree(
        task_dir / "repo", workdir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    bench.mkdir(parents=True)
    state = {
        "task": args.task,
        "arm": args.arm,
        "kind": meta["kind"],
        "workdir": str(workdir),
        "created": time.time(),
        "sessions": {},
        "rep": args.rep,
    }
    for n in (1, 2):
        text = (task_dir / f"session{n}.md").read_text().strip()
        parts = [SCOPE_LINE, text]
        if args.arm == "on":
            parts.append(MEMORY_LINE)
        (bench / f"session{n}.prompt.md").write_text("\n\n".join(parts) + "\n")
    if args.arm == "on":
        (workdir / "MEMORY.md").write_text(MEMORY_NOTE)
        wrapper = bench / "bin"
        wrapper.mkdir()
        script = wrapper / "memoryking"
        script.write_text(
            f'#!/bin/sh\nunset PYTHONPATH PYTHONHOME\nexec "{sys.executable}" -m memoryking "$@"\n'
        )
        script.chmod(0o755)
        try:
            init_memory_arm(state)
        except BaseException:
            stop_daemon(state)
            raise
    save_state(workdir, state)
    env = agent_env(state)
    (bench / "env.sh").write_text("".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items()))
    return {
        "workdir": str(workdir),
        "arm": args.arm,
        "session1_prompt": str(bench / "session1.prompt.md"),
        "session2_prompt": str(bench / "session2.prompt.md"),
        "env_file": str(bench / "env.sh"),
    }


# ----------------------------------------------------------------------------- sessions


def record_session(state: dict, n: int, **fields) -> None:
    entry = state["sessions"].setdefault(str(n), {})
    entry.update({k: v for k, v in fields.items() if v is not None})


def cmd_session(args) -> dict:
    workdir = Path(args.workdir).resolve()
    state = load_state(workdir)
    bench = bench_dir(workdir)
    prompt = bench / f"session{args.n}.prompt.md"
    usage = bench / f"usage{args.n}.json"
    usage.unlink(missing_ok=True)
    values = {
        "workdir": str(workdir),
        "prompt_file": str(prompt),
        "usage_file": str(usage),
        "session": str(args.n),
    }
    argv = [part.format(**values) for part in shlex.split(args.agent_command)]
    env = {**os.environ, **agent_env(state)}
    started = time.time()
    try:
        proc = subprocess.run(
            argv,
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=args.timeout,
        )
        code, tail = proc.returncode, (proc.stdout + proc.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        code, tail = -9, "timed out"
    except OSError as exc:
        code, tail = 127, f"cannot start the agent command: {exc}"
    seconds = round(time.time() - started, 3)
    (bench / f"agent{args.n}.log").write_text(tail)
    figures = {}
    if usage.is_file():
        try:
            figures = json.loads(usage.read_text())
        except ValueError:
            figures = {}
    record_session(
        state,
        args.n,
        seconds=seconds,
        exit_code=code,
        input_tokens=figures.get("input_tokens"),
        output_tokens=figures.get("output_tokens"),
        cost_usd=figures.get("cost_usd"),
    )
    save_state(workdir, state)
    return {"session": args.n, "seconds": seconds, "exit_code": code}


def cmd_record(args) -> dict:
    workdir = Path(args.workdir).resolve()
    state = load_state(workdir)
    record_session(
        state,
        args.n,
        seconds=args.seconds,
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        cost_usd=args.cost_usd,
    )
    save_state(workdir, state)
    return state["sessions"][str(args.n)]


def cmd_between(args) -> dict:
    workdir = Path(args.workdir).resolve()
    state = load_state(workdir)
    task_dir, _ = load_task(state["task"])
    script = task_dir / "between.sh"
    ran = False
    if script.is_file():
        proc = subprocess.run(
            ["bash", str(script), str(workdir)],
            cwd=workdir,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=120,
        )
        if proc.returncode != 0:
            raise BenchmarkError("between.sh failed: " + proc.stderr[:300])
        ran = True
    if state["arm"] == "on" and not daemon_alive(state):
        raise BenchmarkError("the memory daemon is not running between the sessions")
    state["between"] = {"ran": ran}
    save_state(workdir, state)
    return {"between_script": ran, "daemon_running": daemon_alive(state)}


# ----------------------------------------------------------------------------- check


def cmd_check(args) -> dict:
    workdir = Path(args.workdir).resolve()
    state = load_state(workdir)
    task_dir, _ = load_task(state["task"])
    proc = subprocess.run(
        [sys.executable, str(task_dir / "check.py"), str(workdir)],
        capture_output=True,
        text=True,
        env=clean_env(),
        timeout=300,
    )
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        result = {
            "passed": False,
            "checks": [{"name": "check ran", "ok": False, "detail": proc.stderr[-500:]}],
        }
    result["passed"] = bool(result.get("passed")) and proc.returncode == 0
    state["check"] = result
    save_state(workdir, state)
    return {"task": state["task"], "arm": state["arm"], **result}


# ----------------------------------------------------------------------------- finish


def store_statistics(state: dict) -> dict:
    stats: dict = {}
    owner = state["owner_credential"]
    health = memoryking(state, "health", "--project", PROJECT, "--json", credential=owner)
    data = json.loads(health.stdout)
    stats["observations_captured"] = data["observations"]["total"]
    stats["briefings_served"] = data["briefings"]["total"]
    stats["deliveries"] = data["briefings"]["deliveries"]
    stats["briefings_without_relevant_memory"] = data["briefings"]["no_relevant_memory"]
    status = json.loads(
        memoryking(state, "status", "--json", "--project", PROJECT, credential=owner).stdout
    )
    capacity = status.get("capacity", {})
    stats["checkpoints_saved"] = _find_used(capacity, "checkpoints")
    stats["position"] = status.get("position")
    return stats


def _find_used(capacity, name):
    items = capacity.items() if isinstance(capacity, dict) else []
    for key, value in items:
        if key == name and isinstance(value, dict):
            return value.get("used", value.get("current"))
        if key == name and isinstance(value, (int, float)):
            return value
    if isinstance(capacity, list):
        for entry in capacity:
            if isinstance(entry, dict) and entry.get("name") == name:
                return entry.get("used", entry.get("current"))
    return None


def cmd_finish(args) -> dict:
    workdir = Path(args.workdir).resolve()
    state = load_state(workdir)
    if "check" not in state:
        raise BenchmarkError("run `check` before `finish`")
    stats = None
    if state["arm"] == "on":
        try:
            stats = store_statistics(state)
        finally:
            stop_daemon(state)
            shutil.rmtree(state["store_root"], ignore_errors=True)
    result = {
        "task": state["task"],
        "arm": state["arm"],
        "kind": state["kind"],
        "rep": state.get("rep", 1),
        "passed": state["check"]["passed"],
        "checks": state["check"]["checks"],
        "sessions": state["sessions"],
        "between": state.get("between", {"ran": False}),
        "store": stats,
        "finished_at": time.time(),
    }
    state["finished"] = True
    save_state(workdir, state)
    results = Path(args.results or DEFAULT_RESULTS)
    results.mkdir(parents=True, exist_ok=True)
    suffix = "" if result["rep"] == 1 else f"-r{result['rep']}"
    out = results / f"{state['task']}-{state['arm']}{suffix}.json"
    out.write_text(json.dumps(result, indent=2, sort_keys=True))
    return {"result_file": str(out), **{k: result[k] for k in ("task", "arm", "passed", "store")}}


def cmd_env(args) -> str:
    state = load_state(Path(args.workdir).resolve())
    return "".join(f"export {k}={shlex.quote(v)}\n" for k, v in agent_env(state).items())


def cmd_run(args) -> dict:
    """prepare, session 1, between, session 2, check, finish in one go."""
    workdir = Path(args.workdir) if args.workdir else None
    cleanup = None
    if workdir is None:
        cleanup = tempfile.TemporaryDirectory(prefix="mk-run-")
        workdir = Path(cleanup.name) / "work"
    try:
        prepare_args = argparse.Namespace(
            task=args.task, arm=args.arm, workdir=str(workdir), rep=args.rep
        )
        cmd_prepare(prepare_args)
        try:
            for n in (1, 2):
                ns = argparse.Namespace(
                    workdir=str(workdir),
                    n=n,
                    agent_command=args.agent_command,
                    timeout=args.timeout,
                )
                cmd_session(ns)
                if n == 1:
                    cmd_between(argparse.Namespace(workdir=str(workdir)))
            cmd_check(argparse.Namespace(workdir=str(workdir)))
        except BaseException:
            state = load_state(Path(workdir).resolve())
            stop_daemon(state)
            if state.get("store_root"):
                shutil.rmtree(state["store_root"], ignore_errors=True)
            raise
        return cmd_finish(argparse.Namespace(workdir=str(workdir), results=args.results))
    finally:
        if cleanup:
            cleanup.cleanup()


# ----------------------------------------------------------------------------- validate


def validate_task(task_id: str) -> dict:
    """Four outcomes of the hidden check without a model: untouched, after a faithful
    session 1, with the reference second session, and with the plausible wrong one."""
    task_dir, _ = load_task(task_id)
    outcomes, visible = {}, {}
    for label in ("untouched", "after_session1", "reference", "wrong"):
        with tempfile.TemporaryDirectory(prefix="mk-val-") as tmp:
            workdir = Path(tmp) / "work"
            cmd_prepare(argparse.Namespace(task=task_id, arm="off", workdir=str(workdir), rep=1))
            if label != "untouched":
                apply_overlay(task_dir / "reference" / "s1", workdir)
                cmd_between(argparse.Namespace(workdir=str(workdir)))
            if label == "reference":
                apply_overlay(task_dir / "reference" / "s2", workdir)
            if label == "wrong":
                apply_overlay(task_dir / "reference_wrong" / "s2", workdir)
            outcomes[label] = cmd_check(argparse.Namespace(workdir=str(workdir)))["passed"]
            if label in ("untouched", "after_session1", "reference"):
                visible[label] = visible_tests_pass(workdir)
    expected = {"untouched": False, "after_session1": False, "reference": True, "wrong": False}
    return {
        "task": task_id,
        "outcomes": outcomes,
        "visible_tests_pass": visible,
        "ok": outcomes == expected and all(visible.values()),
    }


def visible_tests_pass(workdir: Path) -> bool:
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
        cwd=workdir,
        capture_output=True,
        text=True,
        env={**clean_env(), "PYTHONPATH": str(workdir)},
        timeout=120,
    )
    return proc.returncode == 0


# ----------------------------------------------------------------------------- report


def exact_sign_test(only_on: int, only_off: int) -> float:
    """Two-sided exact McNemar p-value: a binomial test on the discordant pairs."""
    n = only_on + only_off
    if n == 0:
        return 1.0
    k = min(only_on, only_off)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def load_results(results: Path) -> list[dict]:
    rows = []
    for path in sorted(results.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except ValueError:
            continue
        if isinstance(data, dict) and "task" in data and "arm" in data and "passed" in data:
            rows.append(data)
    return rows


def _session_sum(row: dict, field: str):
    values = [s.get(field) for s in row.get("sessions", {}).values()]
    if any(v is None for v in values) or not values:
        return None
    return sum(values)


def _tokens(row: dict):
    a, b = _session_sum(row, "input_tokens"), _session_sum(row, "output_tokens")
    return None if a is None or b is None else a + b


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def aggregate(rows: list[dict]) -> dict:
    arms: dict[str, dict] = {}
    for arm in ("on", "off"):
        subset = [r for r in rows if r["arm"] == arm]
        wins = sum(1 for r in subset if r["passed"])
        low, high = wilson(wins, len(subset))
        arms[arm] = {
            "runs": len(subset),
            "successes": wins,
            "rate": wins / len(subset) if subset else None,
            "ci95": [low, high],
            "mean_tokens": _mean(_tokens(r) for r in subset),
            "mean_seconds": _mean(_session_sum(r, "seconds") for r in subset),
            "mean_cost_usd": _mean(_session_sum(r, "cost_usd") for r in subset),
        }
    per_task: dict[str, dict] = {}
    per_kind: dict[str, dict] = {}
    for row in rows:
        entry = per_task.setdefault(row["task"], {"kind": row.get("kind"), "on": [], "off": []})
        entry[row["arm"]].append(row)
        kind = per_kind.setdefault(row.get("kind") or "unknown", {"on": [0, 0], "off": [0, 0]})
        kind[row["arm"]][1] += 1
        kind[row["arm"]][0] += 1 if row["passed"] else 0
    only_on = only_off = both = neither = 0
    for entry in per_task.values():
        by_rep = {"on": {}, "off": {}}
        for arm in ("on", "off"):
            for row in entry[arm]:
                by_rep[arm][row.get("rep", 1)] = row["passed"]
        for rep in set(by_rep["on"]) & set(by_rep["off"]):
            a, b = by_rep["on"][rep], by_rep["off"][rep]
            if a and b:
                both += 1
            elif a:
                only_on += 1
            elif b:
                only_off += 1
            else:
                neither += 1
    pairs = both + neither + only_on + only_off
    return {
        "arms": arms,
        "per_task": {
            t: {
                "kind": e["kind"],
                **{
                    arm: {
                        "passed": [r["passed"] for r in e[arm]],
                        "tokens": _mean(_tokens(r) for r in e[arm]),
                        "seconds": _mean(_session_sum(r, "seconds") for r in e[arm]),
                    }
                    for arm in ("on", "off")
                },
            }
            for t, e in sorted(per_task.items())
        },
        "per_kind": {
            k: {arm: {"successes": v[arm][0], "runs": v[arm][1]} for arm in ("on", "off")}
            for k, v in sorted(per_kind.items())
        },
        "paired": {
            "pairs": pairs,
            "both_pass": both,
            "both_fail": neither,
            "only_memory_on": only_on,
            "only_memory_off": only_off,
            "p_value": exact_sign_test(only_on, only_off),
            "test": "exact McNemar (two-sided binomial sign test on discordant pairs)",
        },
    }


def _pct(value):
    return "n/a" if value is None else f"{100 * value:.0f}%"


def _num(value, digits=0):
    return "n/a" if value is None else f"{value:,.{digits}f}"


def render_markdown(agg: dict) -> str:
    out = ["### Task outcomes with memory on and off", ""]
    out += ["| Arm | Runs | Successes | Success rate | 95% interval | Mean tokens | Mean seconds |"]
    out += ["| --- | ---: | ---: | ---: | --- | ---: | ---: |"]
    for arm, label in (("on", "Memory on"), ("off", "Memory off")):
        a = agg["arms"][arm]
        out.append(
            f"| {label} | {a['runs']} | {a['successes']} | {_pct(a['rate'])} | "
            f"{_pct(a['ci95'][0])} to {_pct(a['ci95'][1])} | {_num(a['mean_tokens'])} | "
            f"{_num(a['mean_seconds'], 1)} |"
        )
    out += ["", "| Task | Knowledge kind | Memory on | Memory off |", "| --- | --- | :-: | :-: |"]

    def cell(entry):
        if not entry["passed"]:
            return "not run"
        return " ".join("pass" if p else "fail" for p in entry["passed"])

    for task, entry in agg["per_task"].items():
        out.append(f"| {task} | {entry['kind']} | {cell(entry['on'])} | {cell(entry['off'])} |")
    out += ["", "| Knowledge kind | Memory on | Memory off |", "| --- | ---: | ---: |"]
    for kind, v in agg["per_kind"].items():
        out.append(
            f"| {kind} | {v['on']['successes']} of {v['on']['runs']} | "
            f"{v['off']['successes']} of {v['off']['runs']} |"
        )
    p = agg["paired"]
    out += [
        "",
        f"Paired comparison over {p['pairs']} task pairs: memory on only passed {p['only_memory_on']}, "
        f"memory off only passed {p['only_memory_off']}, both passed {p['both_pass']}, "
        f"neither passed {p['both_fail']}. Exact McNemar test on the discordant pairs: "
        f"p = {p['p_value']:.4f}.",
        "",
        "Ten tasks can only show a large effect: with ten pairs, six or more tasks won by one "
        "arm and none by the other is the smallest result that reaches p < 0.05, and a small "
        "difference is indistinguishable from chance. Success here means the hidden check "
        "passed for one agent, one model and one run per task.",
    ]
    return "\n".join(out) + "\n"


def cmd_report(args):
    rows = load_results(Path(args.results))
    if not rows:
        raise BenchmarkError(f"no result files in {args.results}")
    agg = aggregate(rows)
    return json.dumps(agg, indent=2, sort_keys=True) if args.json else render_markdown(agg)


# ----------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, **kw):
        p = sub.add_parser(name, **kw)
        return p

    p = add("prepare")
    p.add_argument("--task", required=True)
    p.add_argument("--arm", choices=("on", "off"), required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument(
        "--rep", type=int, default=1, help="repetition number when a task is run more than once"
    )
    p = add("session")
    p.add_argument("--workdir", required=True)
    p.add_argument("--n", type=int, choices=(1, 2), required=True)
    p.add_argument(
        "--agent-command",
        required=True,
        help="placeholders: {workdir} {prompt_file} {usage_file} {session}",
    )
    p.add_argument("--timeout", type=int, default=3600)
    p = add("record")
    p.add_argument("--workdir", required=True)
    p.add_argument("--n", type=int, choices=(1, 2), required=True)
    p.add_argument("--seconds", type=float)
    p.add_argument("--input-tokens", type=int)
    p.add_argument("--output-tokens", type=int)
    p.add_argument("--cost-usd", type=float)
    for name in ("between", "check", "env"):
        add(name).add_argument("--workdir", required=True)
    p = add("finish")
    p.add_argument("--workdir", required=True)
    p.add_argument("--results", help=f"default {DEFAULT_RESULTS}")
    p = add("run")
    p.add_argument("--task", required=True)
    p.add_argument("--arm", choices=("on", "off"), required=True)
    p.add_argument("--agent-command", required=True)
    p.add_argument("--workdir")
    p.add_argument("--results")
    p.add_argument("--rep", type=int, default=1)
    p.add_argument("--timeout", type=int, default=3600)
    p = add("report")
    p.add_argument("--results", required=True)
    p.add_argument("--json", action="store_true")
    p = add("validate")
    p.add_argument("--task")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            tasks = [args.task] if args.task else all_task_ids()
            rows = [validate_task(t) for t in tasks]
            print(json.dumps(rows, indent=2))
            return 0 if all(r["ok"] for r in rows) else 1
        handler = globals()["cmd_" + args.command]
        result = handler(args)
    except BenchmarkError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    if isinstance(result, str):
        sys.stdout.write(result)
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
