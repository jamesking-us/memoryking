#!/usr/bin/env python3
"""A deterministic stand-in for a coding agent, used to test the benchmark offline.

It calls no model. It reads the task's meta.json and reference solutions, which a real
agent never sees, and behaves like an agent that works faithfully:

* Session 1 applies the reference first-session work. With memory on it also saves what
  the task says session 1 learns, using the public `memoryking capture` command.
* Session 2 asks the memory (`memoryking recall`) for what it needs. When the answer
  holds the facts the task says must carry over, it applies the reference solution;
  otherwise (memory off, or nothing useful recalled) it applies the plausible wrong one.

    stub_agent.py WORKDIR PROMPT_FILE [--usage-file F] [--force solve|fail]

`--force solve` or `--force fail` skips the memory and decides the outcome directly, which
is how the tests exercise the report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "benchmarks" / "outcomes" / "tasks"
PROJECT = "work"


def overlay(source: Path, workdir: Path) -> None:
    if not source.is_dir():
        return
    shutil.copytree(source, workdir, dirs_exist_ok=True)
    marker = workdir / "_delete"
    if marker.exists():
        marker.unlink()
    delete = source / "_delete"
    if delete.is_file():
        for name in delete.read_text().split():
            target = workdir / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()


def memory(*args: str) -> str:
    """Run the `memoryking` command exactly as an agent would: from PATH, no paths."""
    proc = subprocess.run(["memoryking", *args], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise SystemExit(f"memoryking {args[0]} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir")
    parser.add_argument("prompt_file")
    parser.add_argument("--usage-file")
    parser.add_argument("--force", choices=("solve", "fail"))
    args = parser.parse_args()
    workdir = Path(args.workdir).resolve()
    prompt = Path(args.prompt_file)
    session = int(re.search(r"session(\d)", prompt.name).group(1))
    state = json.loads((workdir.parent / (workdir.name + ".bench") / "state.json").read_text())
    task_dir = TASKS / state["task"]
    meta = json.loads((task_dir / "meta.json").read_text())
    stub = meta["stub"]
    with_memory = (workdir / "MEMORY.md").is_file()
    if with_memory != (state["arm"] == "on"):
        raise SystemExit("MEMORY.md does not match the arm")
    assert prompt.read_text().strip(), "empty prompt"

    if session == 1:
        overlay(task_dir / "reference" / "s1", workdir)
        if with_memory:
            for note in stub["learn"]:
                memory("capture", "--project", PROJECT, "--source", "agent", "--text", note)
            memory(
                "capture", "--project", PROJECT, "--source", "progress",
                "--text", f"Progress: first part of the work is done in the repository ({meta['title']}).",
            )
    else:
        informed = False
        if args.force:
            informed = args.force == "solve"
        elif with_memory:
            briefing = memory("recall", "--project", PROJECT, "--query", stub["recall_query"])
            informed = all(m.casefold() in briefing.casefold() for m in stub["must_recall"])
        overlay(task_dir / ("reference" if informed else "reference_wrong") / "s2", workdir)

    if args.usage_file:
        # Synthetic figures so the report's token columns are exercised; not a measurement.
        Path(args.usage_file).write_text(
            json.dumps({"input_tokens": 1000 * session, "output_tokens": 100 * session})
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
