#!/usr/bin/env python3
"""Run reproducible foundation and local-product checks. No SMTP, model calls, or live backend writes."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


STEP_TIMEOUT = 1800  # seconds per step; a hung step fails instead of blocking the run


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
    commands = [
        [sys.executable, "tools/generate_contracts.py", "--check"],
        [sys.executable, "tools/generate_error_codes.py", "--check"],
        [sys.executable, "-m", "ruff", "check", "src", "tools", "tests", "examples"],
        [sys.executable, "-m", "ruff", "format", "--check", "src", "tools", "tests", "examples"],
        [sys.executable, "-m", "mypy"],
        ["npm", "run", "format:check"],
        ["npm", "test"],
        [sys.executable, "tools/mobile_asset_audit.py"],
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        [sys.executable, "tools/run_conformance.py", "--output", "tmp/checks/scenarios.json"],
        [sys.executable, "tools/check_docs.py"],
        [
            sys.executable,
            "tools/evidence_recall.py",
            "--data",
            "fixtures/evaluation/longmemeval-sample.json",
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
    ]
    if os.environ.get("MEMORYKING_REQUIRE_POSTGRES") == "1":
        commands.append(
            [
                sys.executable,
                "tools/mobile_conformance.py",
                "--phase",
                "M01",
                "--output",
                "tmp/checks/mobile-M01.json",
            ]
        )
        commands.append(
            [
                sys.executable,
                "tools/mobile_conformance.py",
                "--phase",
                "M03",
                "--output",
                "tmp/checks/mobile-M03.json",
            ]
        )
        commands.append(
            [
                sys.executable,
                "tools/mobile_conformance.py",
                "--phase",
                "M04",
                "--output",
                "tmp/checks/mobile-M04.json",
            ]
        )
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
        started = time.monotonic()
        try:
            result = subprocess.run(
                command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=STEP_TIMEOUT
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
        log.write_text(output)
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
        json.dumps({"checks": results, "passed": not failed}, indent=2) + "\n"
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
