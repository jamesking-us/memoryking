#!/usr/bin/env python3
"""Run reproducible foundation and local-product checks. No SMTP, model calls, or live backend writes."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# Steps that create a memory store, which Windows does not hold (it runs in WSL2 or the
# container there): on Windows they are reported as not applicable, never as passed.
STORE_STEPS = {
    "tools/run_conformance.py",
    "tools/evidence_recall.py",
    "tools/retrieval_benchmark.py",
    "tools/stale_facts_benchmark.py",
}
NOT_ON_WINDOWS = "needs the memory store, which does not run on Windows"

STEP_TIMEOUT = 3600  # seconds per step; a hung step fails instead of blocking the run (the full test suite alone takes about 30 minutes with PostgreSQL on a loaded machine)


def main():
    if os.environ.get("MEMORYKING_REQUIRE_POSTGRES") == "1":
        binary = Path(os.environ.get("MEMORYKING_PG_BIN", "")) / "pg_ctl"
        if not binary.is_absolute() or not binary.is_file():
            raise SystemExit(
                "Native PostgreSQL is mandatory for this job; configure MEMORYKING_PG_BIN"
            )
    out = ROOT / "tmp/checks"
    out.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    env["MEMORYKING_PYTHON"] = sys.executable  # the JavaScript client tests start real services
    commands = [
        [sys.executable, "tools/generate_contracts.py", "--check"],
        [sys.executable, "tools/generate_error_codes.py", "--check"],
        [sys.executable, "-m", "ruff", "check", "src", "tools", "tests", "examples"],
        [sys.executable, "-m", "ruff", "format", "--check", "src", "tools", "tests", "examples"],
        [sys.executable, "-m", "mypy"],
        ["npm", "run", "format:check"],
        ["npm", "test"],
        # Windows has no store: its runner reports the store's refusal as a reasoned skip.
        [sys.executable, "tools/windows_tests.py", "-v"]
        if sys.platform == "win32"
        else [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        [sys.executable, "tools/run_conformance.py", "--output", "tmp/checks/scenarios.json"],
        [sys.executable, "tools/check_docs.py"],
        [
            sys.executable,
            "tools/evidence_recall.py",
            "--data",
            "fixtures/evaluation/longmemeval-regression-sample.json",
            "--gate",
            "--output",
            "tmp/checks/evidence-recall.json",
        ],
        [
            sys.executable,
            "tools/retrieval_benchmark.py",
            "--gate",
            "--output",
            "tmp/checks/retrieval-benchmark.json",
        ],
        [
            sys.executable,
            "tools/stale_facts_benchmark.py",
            "--gate",
            "--output",
            "tmp/checks/stale-facts.json",
        ],
    ]
    if os.environ.get("MEMORYKING_REQUIRE_POSTGRES") == "1":
        commands.append(
            [
                sys.executable,
                "tools/run_conformance.py",
                "--backend",
                "shared",
                "--output",
                "tmp/checks/shared-scenarios.json",
            ]
        )
    results = []
    failed = False
    tracked_before = tracked_state()
    for index, command in enumerate(commands, 1):
        if sys.platform == "win32" and command[1:2] and command[1] in STORE_STEPS:
            results.append({"command": command, "not_applicable": NOT_ON_WINDOWS})
            print("N/A  " + " ".join(command) + f" ({NOT_ON_WINDOWS})", flush=True)
            continue
        started = time.monotonic()
        try:
            # npm is a .cmd file on Windows, which only a lookup (not CreateProcess) finds.
            program = shutil.which(command[0]) or command[0]
            result = subprocess.run(
                [program, *command[1:]],
                cwd=ROOT,
                env=env,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=STEP_TIMEOUT,
            )
            returncode, output = result.returncode, result.stdout + result.stderr
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            output = (
                (exc.stdout or b"").decode(errors="replace")
                + (exc.stderr or b"").decode(errors="replace")
                + f"\nstep exceeded {STEP_TIMEOUT} seconds\n"
            )
        log = out / f"{index:02d}.log"
        log.write_text(output, encoding="utf-8")
        record = {
            "command": command,
            "exit_code": returncode,
            "seconds": round(time.monotonic() - started, 3),
            "log": str(log.relative_to(ROOT)),
        }
        results.append(record)
        print(("PASS" if returncode == 0 else "FAIL") + " " + " ".join(command), flush=True)
        if returncode:
            print(output[-6000:], flush=True)
            failed = True
    tracked_after = tracked_state()
    if tracked_after != tracked_before:
        # Generators and bundlers must not rewrite tracked artifacts behind the author.
        print("FAIL checks rewrote tracked files; review `git status` and commit intentionally")
        failed = True
    results.append({"command": ["tracked-files-unchanged"], "exit_code": int(failed)})
    (out / "results.json").write_text(
        json.dumps({"checks": results, "passed": not failed}, indent=2) + "\n", encoding="utf-8"
    )
    raise SystemExit(1 if failed else 0)


def tracked_state() -> str:
    """A digest of every tracked file's content as git sees it; empty outside a checkout."""
    if not (ROOT / ".git").exists():
        return ""
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=no"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    diff = subprocess.run(
        ["git", "diff", "--no-color", "--no-ext-diff"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return status.stdout + diff.stdout


if __name__ == "__main__":
    main()
