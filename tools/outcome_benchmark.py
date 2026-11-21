#!/usr/bin/env python3
"""Task-outcome benchmark: do multi-session coding tasks finish better with memory?

Each task (benchmarks/outcomes/tasks/ID) has two sessions run by two separate agent
instances that share only the working directory and, in the memory arms, a MemoryKing
store. Four arms: `off` (nothing), `notes` (a NOTES.md the agent may use), `on` (the
MemoryKing CLI plus MEMORY.md) and `mcp` (the lean MCP bridge as tools). This tool
prepares the working directory, runs the between-step and the hidden check, records
results and aggregates them. It never calls a model: the agent is whatever command you
hand it (`--agent-command`), and the bundled stub agent solves tasks deterministically
for offline tests. See benchmarks/outcomes/RUNNING.md.

    outcome_benchmark.py prepare --task ID --arm off|notes|on|mcp --workdir DIR
    outcome_benchmark.py session --workdir DIR --n 1|2 --agent-command "CMD {workdir} {prompt_file}"
    outcome_benchmark.py record  --workdir DIR --n 1|2 --seconds S [--input-tokens N ...]
    outcome_benchmark.py between --workdir DIR
    outcome_benchmark.py check   --workdir DIR
    outcome_benchmark.py finish  --workdir DIR [--results DIR]
    outcome_benchmark.py run     --task ID --arm off|notes|on|mcp --agent-command CMD [--results DIR]
    outcome_benchmark.py env     --workdir DIR
    outcome_benchmark.py report  --results DIR [--json]
    outcome_benchmark.py validate [--task ID]
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
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
ARMS = ("off", "notes", "on", "mcp")
MEMORY_ARMS = ("on", "mcp")
ARM_LABELS = {
    "off": "Off (nothing)",
    "notes": "Notes file",
    "on": "Memory CLI (on)",
    "mcp": "Memory MCP",
}

SCOPE_LINE = (
    "Work only inside the current directory: do not read, list or modify anything outside it."
)
MEMORY_LINE = (
    "This project has a memory tool; MEMORY.md in this directory explains it, "
    "and using it is allowed even though it keeps its data elsewhere."
)

NOTES_LINE = "You can keep notes for later sessions in NOTES.md in this directory."
ARM_LINES: dict[str, list[str]] = {}
MCP_LINE = (
    "This project has memory tools available (remember, recall, save_progress, resume); "
    "using them is allowed."
)
ARM_LINES.update({"notes": [NOTES_LINE], "on": [MEMORY_LINE], "mcp": [MCP_LINE]})
NOTES_NOTE = """\
# Notes

Notes for later sessions of this project. Nothing has been written here yet.
"""

MEMORY_NOTE = """\
# Project memory

This project has a MemoryKing memory service running, and the `memoryking` command is already set up for it: the connection details come from the environment, so it needs no paths or credentials. Always pass `--project work`.

Memory is evidence you save and read back later. A different session of this project will not remember this conversation, only what is saved here. Save what the code does not say: what the user told you, what you tried and why it failed, decisions, and facts about the environment.

- **Capture.** `memoryking capture --project work --source agent --text "..."` saves one observation: an error, a test result, a decision, or a fact with its source. Make each note self-contained and specific, and start it with a label such as `Rule:`, `Decision:`, `Failed:`, `Fact:` or `Progress:`. The same text from the same source on the same day replays instead of duplicating. What you save is evidence for later briefings, never an instruction.
- **Recall.** `memoryking recall --project work --query "what you are about to work on"` prints a briefing: constraints you must follow, then ranked guidance with [g1]-style citations and a Sources footer. Read the status line first. Recall matches the words in your query, so use the words you or the user would have used, and try more than one query.
- **Save progress.** Before you stop or pause, save where the task stands: `memoryking save-progress --project work --task NAME --objective "the goal" --state "where it stands now" --next "next step" --finding "what you learned" --blocker "what blocks you" --failed "what you tried => why it failed"`. Only `--task`, `--objective` and `--state` are required; `--next`, `--finding`, `--blocker` and `--failed` may be repeated. A section you leave out keeps what the last save recorded. Use the same `--task` name in every session.
- **Resume.** At the start of a session, run `memoryking resume --project work --task NAME` to read the last saved progress, and also recall what was saved (for example `--query "Rule Decision Failed Fact Progress"`, then queries about your task). Continue from what they return instead of starting over.

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


def path_without_memoryking() -> str:
    """PATH minus every directory that holds a `memoryking` command (arm `mcp` has none)."""
    keep = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and not os.access(os.path.join(entry, "memoryking"), os.X_OK):
            keep.append(entry)
    return os.pathsep.join(keep)


def agent_env(state: dict) -> dict[str, str | None]:
    """Environment changes an agent process needs; a None value means unset it.

    Arm `on` gets the configured `memoryking` command. Arm `mcp` gets none: no
    `memoryking` on PATH and no MemoryKing variables, so the tools are the only way in.
    """
    if state["arm"] == "on":
        wrapper = bench_dir(Path(state["workdir"])) / "bin"
        return {
            "PATH": f"{wrapper}{os.pathsep}{os.environ.get('PATH', '')}",
            "MEMORYKING_CONFIG": state["config"],
            "MEMORYKING_PROFILE": PROFILE,
            "MEMORYKING_PROJECT": PROJECT,
        }
    if state["arm"] == "mcp":
        env: dict[str, str | None] = {"PATH": path_without_memoryking()}
        env.update({k: None for k in os.environ if k.startswith("MEMORYKING_")})
        return env
    return {}


def merged_env(state: dict) -> dict[str, str]:
    env = dict(os.environ)
    for key, value in agent_env(state).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def env_lines(state: dict) -> str:
    return "".join(
        f"unset {k}\n" if v is None else f"export {k}={shlex.quote(v)}\n"
        for k, v in agent_env(state).items()
    )


def mcp_config(state: dict) -> dict:
    """The standard `mcpServers` document for the lean stdio bridge, as a host reads it."""
    return {
        "mcpServers": {
            "memoryking": {
                "command": sys.executable,
                "args": [
                    "-m",
                    "memoryking.adapters.stdio",
                    "--socket",
                    str(Path(state["data"]) / "service.sock"),
                    "--credential",
                    state["agent_credential"],
                    "--project",
                    PROJECT,
                    "--profile",
                    "lean",
                ],
            }
        }
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
        parts = [SCOPE_LINE, text, *ARM_LINES.get(args.arm, [])]
        (bench / f"session{n}.prompt.md").write_text("\n\n".join(parts) + "\n")
    if args.arm == "notes":
        (workdir / "NOTES.md").write_text(NOTES_NOTE)
    if args.arm == "on":
        (workdir / "MEMORY.md").write_text(MEMORY_NOTE)
        wrapper = bench / "bin"
        wrapper.mkdir()
        script = wrapper / "memoryking"
        script.write_text(
            f'#!/bin/sh\nunset PYTHONPATH PYTHONHOME\nexec "{sys.executable}" -m memoryking "$@"\n'
        )
        script.chmod(0o755)
    if args.arm in MEMORY_ARMS:
        try:
            init_memory_arm(state)
        except BaseException:
            stop_daemon(state)
            raise
    if args.arm == "mcp":
        # Outside the workdir, so the agent finds the credential path only through its host.
        config = bench / "mcp.json"
        config.write_text(json.dumps(mcp_config(state), indent=2))
        config.chmod(0o600)
        state["mcp_config"] = str(config)
    # What the directory held when the agent started, to tell later which notes it left.
    shutil.copytree(workdir, bench / "baseline")
    save_state(workdir, state)
    (bench / "env.sh").write_text(env_lines(state))
    out = {
        "workdir": str(workdir),
        "arm": args.arm,
        "session1_prompt": str(bench / "session1.prompt.md"),
        "session2_prompt": str(bench / "session2.prompt.md"),
        "env_file": str(bench / "env.sh"),
    }
    if args.arm == "mcp":
        out["mcp_config"] = state["mcp_config"]
    return out


# ----------------------------------------------------------------------------- sessions


def record_session(state: dict, n: int, **fields) -> None:
    entry = state["sessions"].setdefault(str(n), {})
    entry.update({k: v for k, v in fields.items() if v is not None})


PLACEHOLDERS = ("workdir", "prompt_file", "usage_file", "session", "mcp_config")
USAGE_KEYS = ("input_tokens", "output_tokens", "cost_usd", "duration_seconds")


def fill_placeholders(template: str, values: dict[str, str]) -> list[str]:
    """Split the command like a shell would, then replace only the documented placeholders
    (other braces, such as a jq filter, stay as they are)."""
    pattern = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")
    return [pattern.sub(lambda m: values[m.group(1)], part) for part in shlex.split(template)]


def usage_from_object(data) -> dict:
    """The documented convention: numeric input_tokens, output_tokens, cost_usd and
    duration_seconds (or duration_ms) keys at the top level."""
    if not isinstance(data, dict):
        return {}
    found = {}
    for key in USAGE_KEYS:
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            found[key] = value
    if "duration_seconds" not in found:
        value = data.get("duration_ms")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            found["duration_seconds"] = value / 1000
    return found


def _last_json(text: str):
    text = text.strip()
    candidates = [text, *reversed([line for line in text.splitlines() if line.strip()])]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def usage_from_stdout(stdout: Path, usage_jq: str | None) -> dict:
    """Figures from what the agent command printed: through a jq filter that builds an
    object with the convention's keys, or, with no filter, from the printed JSON itself."""
    if not stdout.is_file():
        return {}
    if usage_jq:
        proc = subprocess.run(
            ["jq", "-c", usage_jq, str(stdout)], capture_output=True, text=True, timeout=60
        )
        if proc.returncode != 0:
            return {}
        return usage_from_object(_last_json(proc.stdout))
    return usage_from_object(_last_json(stdout.read_text(errors="replace")))


def run_agent(argv, *, cwd, env, timeout, stdin_path, stdout_path, stderr_path):
    """Run the agent command in its own process group so a timeout stops its children too.
    Returns (exit code, timed out)."""
    stdin = stdin_path.open("rb") if stdin_path else subprocess.DEVNULL
    try:
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=stdin,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            try:
                return proc.wait(timeout=timeout), False
            except subprocess.TimeoutExpired:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(proc.pid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        proc.wait(timeout=10)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                return -9, True
    finally:
        if stdin_path:
            stdin.close()


def cmd_session(args) -> dict:
    workdir = Path(args.workdir).resolve()
    state = load_state(workdir)
    bench = bench_dir(workdir)
    prompt = bench / f"session{args.n}.prompt.md"
    usage = bench / f"usage{args.n}.json"
    usage.unlink(missing_ok=True)
    usage_jq = getattr(args, "usage_jq", None)
    if usage_jq and not shutil.which("jq"):
        raise BenchmarkError("--usage-jq needs the `jq` command, which is not installed")
    mcp = state.get("mcp_config")
    if "{mcp_config}" in args.agent_command and not mcp:
        raise BenchmarkError("{mcp_config} exists only in arm mcp")
    if state["arm"] == "mcp" and "{mcp_config}" not in args.agent_command:
        raise BenchmarkError("arm mcp needs {mcp_config} in the agent command")
    values = {
        "workdir": str(workdir),
        "prompt_file": str(prompt),
        "usage_file": str(usage),
        "session": str(args.n),
        "mcp_config": mcp or "",
    }
    argv = fill_placeholders(args.agent_command, values)
    stdout_path = bench / f"agent{args.n}.stdout"
    stderr_path = bench / f"agent{args.n}.stderr"
    started = time.time()
    timed_out = False
    try:
        code, timed_out = run_agent(
            argv,
            cwd=workdir,
            env=merged_env(state),
            timeout=args.timeout,
            stdin_path=prompt if getattr(args, "prompt_stdin", False) else None,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
    except OSError as exc:
        code = 127
        stdout_path.write_bytes(b"")
        stderr_path.write_text(f"cannot start the agent command: {exc}")
    seconds = round(time.time() - started, 3)
    tail = (
        stdout_path.read_text(errors="replace")[-1000:]
        + stderr_path.read_text(errors="replace")[-1000:]
    )
    (bench / f"agent{args.n}.log").write_text("timed out\n" + tail if timed_out else tail)
    figures = {}
    if usage.is_file():
        try:
            figures = usage_from_object(json.loads(usage.read_text()))
        except ValueError:
            figures = {}
    if not figures:
        try:
            figures = usage_from_stdout(stdout_path, usage_jq)
        except (OSError, subprocess.SubprocessError):
            figures = {}
    record_session(
        state,
        args.n,
        seconds=seconds,
        exit_code=code,
        timed_out=timed_out,
        input_tokens=figures.get("input_tokens"),
        output_tokens=figures.get("output_tokens"),
        cost_usd=figures.get("cost_usd"),
        agent_reported_seconds=figures.get("duration_seconds"),
    )
    save_state(workdir, state)
    return {"session": args.n, "seconds": seconds, "exit_code": code, "timed_out": timed_out}


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


# Files with these extensions are code, configuration or data; a new file with any other
# extension (or none) is read as notes. Caches are never counted.
CODE_SUFFIXES = {
    ".py", ".pyi", ".sh", ".js", ".ts", ".json", ".toml", ".yaml", ".yml", ".cfg", ".ini",
    ".csv", ".sql", ".html", ".css", ".lock", ".conf",
}  # fmt: skip
IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".git"}


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or ([] if isinstance(node, ast.Module) else [ast.Pass()])
    return tree


def _python_only_comments_or_docstrings(old: str | None, new: str) -> bool:
    """True when the Python text differs from `old` only in comments, blank lines and
    docstrings; with no `old` (a new file), when it holds nothing but those. An unparsable
    file is not."""
    try:
        after = ast.dump(_strip_docstrings(ast.parse(new)))
        before = ast.dump(_strip_docstrings(ast.parse(old if old is not None else "")))
    except (SyntaxError, ValueError):
        return False
    return after == before


def detect_notes(baseline: Path, workdir: Path) -> list[str]:
    """Files an agent left that carry knowledge rather than work: new non-code files,
    existing non-code files whose text changed (the notes file of arm `notes` included),
    and Python files that gained only comments or docstrings (a new Python file counts
    only when it holds nothing else). A file that mixes comment additions with real code
    changes is not detected; deletions are ignored."""
    found = []
    for path in sorted(workdir.rglob("*")):
        rel = path.relative_to(workdir)
        if not path.is_file() or IGNORED_PARTS & set(rel.parts) or path.suffix == ".pyc":
            continue
        old = baseline / rel
        old_bytes = old.read_bytes() if old.is_file() else None
        new_bytes = path.read_bytes()
        if old_bytes == new_bytes or (old_bytes is None and not new_bytes.strip()):
            continue
        if path.suffix == ".py":
            try:
                old_text = old_bytes.decode() if old_bytes is not None else None
                if _python_only_comments_or_docstrings(old_text, new_bytes.decode()):
                    found.append(rel.as_posix())
            except UnicodeDecodeError:
                pass
        elif path.suffix not in CODE_SUFFIXES:
            found.append(rel.as_posix())
    return found


def cmd_between(args) -> dict:
    workdir = Path(args.workdir).resolve()
    state = load_state(workdir)
    task_dir, _ = load_task(state["task"])
    # Look at the directory as session 1 left it, before the between-step removes anything.
    baseline = bench_dir(workdir) / "baseline"
    if baseline.is_dir():
        files = detect_notes(baseline, workdir)
        state["notes_left"] = {"left": bool(files), "files": files}
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
    if state["arm"] in MEMORY_ARMS:
        if not daemon_alive(state):
            raise BenchmarkError("the memory daemon is not running between the sessions")
        # The store's own counts after session 1, to tell what each session did.
        state["store_at_between"] = store_statistics(state)
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
    if state["arm"] in MEMORY_ARMS:
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
        "store_at_between": state.get("store_at_between"),
        "notes_left": state.get("notes_left"),
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
    return env_lines(state)


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
                    usage_jq=getattr(args, "usage_jq", None),
                    prompt_stdin=getattr(args, "prompt_stdin", False),
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


def exact_sign_test(only_a: int, only_b: int) -> float:
    """Two-sided exact McNemar p-value: a binomial test on the discordant pairs."""
    n = only_a + only_b
    if n == 0:
        return 1.0
    k = min(only_a, only_b)
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


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def _figure(rows, getter) -> dict:
    values = [v for v in (getter(r) for r in rows) if v is not None]
    return {"mean": _mean(values), "median": _median(values), "recorded": len(values)}


def _store_delta(row: dict, field: str):
    """What a counter in the store's own statistics grew by in session 2, or None."""
    before, after = row.get("store_at_between"), row.get("store")
    if not before or not after or before.get(field) is None or after.get(field) is None:
        return None
    return after[field] - before[field]


def memory_use(rows: list[dict]) -> dict:
    """Runs that really used memory, from the store's counts at `between` and `finish`:
    something written in session 1 (observations or checkpoints above zero when it ended)
    and something read in session 2 (briefings or deliveries grew)."""
    wrote = read = unknown = 0
    for row in rows:
        before = row.get("store_at_between")
        if not before or not row.get("store"):
            unknown += 1
            continue
        if (before.get("observations_captured") or 0) + (before.get("checkpoints_saved") or 0) > 0:
            wrote += 1
        if (_store_delta(row, "briefings_served") or 0) > 0 or (
            _store_delta(row, "deliveries") or 0
        ) > 0:
            read += 1
    return {
        "runs": len(rows),
        "wrote_in_session1": wrote,
        "read_in_session2": read,
        "unknown": unknown,
    }


def notes_left(rows: list[dict]) -> dict:
    left = sum(1 for r in rows if (r.get("notes_left") or {}).get("left"))
    unknown = sum(1 for r in rows if not r.get("notes_left"))
    return {"runs": len(rows), "left_notes": left, "unknown": unknown}


def reading(a: str, b: str, only_a: int, only_b: int, p: float) -> str:
    """The paired result in plain words."""
    la, lb = ARM_LABELS[a], ARM_LABELS[b]
    if only_a + only_b == 0:
        return f"{la} and {lb} passed and failed on exactly the same tasks."
    if only_a == only_b:
        return f"{la} and {lb} each won the same number of tasks; no difference."
    lead, trail = (la, lb) if only_a > only_b else (lb, la)
    won, lost = max(only_a, only_b), min(only_a, only_b)
    if p < 0.05:
        return (
            f"{lead} passed {won} tasks that {trail} failed, against {lost} the other way; "
            "that is unlikely to be chance."
        )
    return (
        f"{lead} passed {won} tasks that {trail} failed, against {lost} the other way; "
        "that is within what chance produces, so no difference is shown."
    )


def paired(rows: list[dict], a: str, b: str) -> dict:
    by_rep: dict[str, dict[tuple, dict]] = {a: {}, b: {}}
    for row in rows:
        if row["arm"] in by_rep:
            by_rep[row["arm"]][(row["task"], row.get("rep", 1))] = row["passed"]
    both = neither = only_a = only_b = 0
    for key in set(by_rep[a]) & set(by_rep[b]):
        x, y = by_rep[a][key], by_rep[b][key]
        if x and y:
            both += 1
        elif x:
            only_a += 1
        elif y:
            only_b += 1
        else:
            neither += 1
    p = exact_sign_test(only_a, only_b)
    return {
        "a": a,
        "b": b,
        "pairs": both + neither + only_a + only_b,
        "both_pass": both,
        "both_fail": neither,
        "only_a": only_a,
        "only_b": only_b,
        "p_value": p,
        "reading": reading(a, b, only_a, only_b, p),
        "test": "exact McNemar (two-sided binomial sign test on discordant pairs)",
    }


def aggregate(rows: list[dict]) -> dict:
    present = [arm for arm in ARMS if any(r["arm"] == arm for r in rows)]
    arms: dict[str, dict] = {}
    for arm in present:
        subset = [r for r in rows if r["arm"] == arm]
        wins = sum(1 for r in subset if r["passed"])
        low, high = wilson(wins, len(subset))
        arms[arm] = {
            "runs": len(subset),
            "successes": wins,
            "rate": wins / len(subset),
            "ci95": [low, high],
            "tokens": _figure(subset, _tokens),
            "seconds": _figure(subset, lambda r: _session_sum(r, "seconds")),
            "cost_usd": _figure(subset, lambda r: _session_sum(r, "cost_usd")),
        }
    per_task: dict[str, dict] = {}
    per_kind: dict[str, dict] = {}
    for row in rows:
        entry = per_task.setdefault(
            row["task"], {"kind": row.get("kind"), **{arm: [] for arm in present}}
        )
        entry[row["arm"]].append(row)
        kind = per_kind.setdefault(row.get("kind") or "unknown", {arm: [0, 0] for arm in present})
        kind[row["arm"]][1] += 1
        kind[row["arm"]][0] += 1 if row["passed"] else 0
    pairs = [
        paired(rows, present[i], present[j])
        for i in range(len(present))
        for j in range(i + 1, len(present))
    ]
    return {
        "arm_order": present,
        "arms": arms,
        "per_task": {
            t: {
                "kind": e["kind"],
                "arms": {arm: {"passed": [r["passed"] for r in e[arm]]} for arm in present},
            }
            for t, e in sorted(per_task.items())
        },
        "per_kind": {
            k: {arm: {"successes": v[arm][0], "runs": v[arm][1]} for arm in present}
            for k, v in sorted(per_kind.items())
        },
        "pairs": pairs,
        "memory_use": {
            arm: memory_use([r for r in rows if r["arm"] == arm])
            for arm in present
            if arm in MEMORY_ARMS
        },
        "notes_left": {
            arm: notes_left([r for r in rows if r["arm"] == arm])
            for arm in present
            if arm in ("off", "notes")
        },
    }


def _pct(value):
    return "n/a" if value is None else f"{100 * value:.0f}%"


def _num(value, digits=0):
    return "n/a" if value is None else f"{value:,.{digits}f}"


def _pair(figure, digits=0, prefix=""):
    if figure["mean"] is None:
        return "n/a"
    return f"{prefix}{_num(figure['mean'], digits)} / {prefix}{_num(figure['median'], digits)}"


def render_markdown(agg: dict) -> str:
    order = agg["arm_order"]
    label = ARM_LABELS
    out = ["### Task outcomes by arm", ""]
    out += ["| Arm | Runs | Successes | Success rate | 95% interval |"]
    out += ["| --- | ---: | ---: | ---: | --- |"]
    for arm in order:
        a = agg["arms"][arm]
        out.append(
            f"| {label[arm]} | {a['runs']} | {a['successes']} | {_pct(a['rate'])} | "
            f"{_pct(a['ci95'][0])} to {_pct(a['ci95'][1])} |"
        )
    out += [
        "",
        "Cost per run, both sessions summed (mean / median; n/a when not recorded):",
        "",
        "| Arm | Tokens | Cost (USD) | Wall time (s) |",
        "| --- | ---: | ---: | ---: |",
    ]
    for arm in order:
        a = agg["arms"][arm]
        out.append(
            f"| {label[arm]} | {_pair(a['tokens'])} | {_pair(a['cost_usd'], 2, '$')} | "
            f"{_pair(a['seconds'], 1)} |"
        )
    out += ["", "| Task | Knowledge kind | " + " | ".join(label[a] for a in order) + " |"]
    out += ["| --- | --- | " + " | ".join(":-:" for _ in order) + " |"]

    def cell(entry):
        if not entry["passed"]:
            return "not run"
        return " ".join("pass" if p else "fail" for p in entry["passed"])

    for task, entry in agg["per_task"].items():
        cells = " | ".join(cell(entry["arms"][a]) for a in order)
        out.append(f"| {task} | {entry['kind']} | {cells} |")
    out += ["", "| Knowledge kind | " + " | ".join(label[a] for a in order) + " |"]
    out += ["| --- | " + " | ".join("---:" for _ in order) + " |"]
    for kind, v in agg["per_kind"].items():
        cells = " | ".join(f"{v[a]['successes']} of {v[a]['runs']}" for a in order)
        out.append(f"| {kind} | {cells} |")
    if agg["pairs"]:
        out += [
            "",
            "Paired comparisons (exact McNemar test: a two-sided binomial sign test on the "
            "tasks where exactly one of the two arms passed):",
            "",
            "| Arms | Pairs | Only first passed | Only second passed | Both passed | Neither | p | Reading |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
        for p in agg["pairs"]:
            out.append(
                f"| {label[p['a']]} vs {label[p['b']]} | {p['pairs']} | {p['only_a']} | "
                f"{p['only_b']} | {p['both_pass']} | {p['both_fail']} | {p['p_value']:.4f} | "
                f"{p['reading']} |"
            )
    if agg["memory_use"]:
        out += [
            "",
            "Did the agent use memory? From the store's own counts: written in session 1 means "
            "observations or checkpoints saved by the end of session 1; read in session 2 means "
            "briefings or deliveries served during session 2.",
            "",
            "| Arm | Runs | Wrote in session 1 | Read in session 2 | No store figures |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for arm, u in agg["memory_use"].items():
            out.append(
                f"| {label[arm]} | {u['runs']} | {u['wrote_in_session1']} | "
                f"{u['read_in_session2']} | {u['unknown']} |"
            )
    if agg["notes_left"]:
        out += [
            "",
            "Notes left on disk in session 1 (new non-code files, edited notes or text files, "
            "or Python changes that only add comments or docstrings):",
            "",
            "| Arm | Runs | Left notes | Not recorded |",
            "| --- | ---: | ---: | ---: |",
        ]
        for arm, u in agg["notes_left"].items():
            out.append(f"| {label[arm]} | {u['runs']} | {u['left_notes']} | {u['unknown']} |")
    out += [
        "",
        "Ten tasks can only show a large effect: between two arms, six tasks won by one and "
        "none by the other is the smallest result that reaches p < 0.05, and a small "
        "difference is indistinguishable from chance. With several arms, each comparison is a "
        "separate test and nothing here corrects for that. Success here means the hidden "
        "check passed for one agent, one model and the runs listed.",
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

    def add_agent_options(p):
        p.add_argument(
            "--agent-command",
            required=True,
            help="placeholders: {workdir} {prompt_file} {usage_file} {session} and, in arm mcp, "
            "{mcp_config}",
        )
        p.add_argument(
            "--timeout", type=int, default=3600, help="wall-clock limit per session, in seconds"
        )
        p.add_argument(
            "--prompt-stdin", action="store_true", help="also feed the prompt file on stdin"
        )
        p.add_argument(
            "--usage-jq",
            metavar="FILTER",
            help="a jq filter over the command's stdout that builds an object with input_tokens, "
            "output_tokens, cost_usd and duration_seconds; without it, the stdout's last JSON "
            "object is read for those keys",
        )

    p = add("prepare")
    p.add_argument("--task", required=True)
    p.add_argument("--arm", choices=ARMS, required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument(
        "--rep", type=int, default=1, help="repetition number when a task is run more than once"
    )
    p = add("session")
    p.add_argument("--workdir", required=True)
    p.add_argument("--n", type=int, choices=(1, 2), required=True)
    add_agent_options(p)
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
    p.add_argument("--arm", choices=ARMS, required=True)
    add_agent_options(p)
    p.add_argument("--workdir")
    p.add_argument("--results")
    p.add_argument("--rep", type=int, default=1)
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
