#!/usr/bin/env python3
"""Run reproducible foundation and local-product checks. No SMTP, model calls, or live backend writes."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
        [sys.executable, "-m", "ruff", "check", "src", "tools", "tests", "examples"],
        [sys.executable, "-m", "ruff", "format", "--check", "src", "tools", "tests", "examples"],
        [sys.executable, "-m", "mypy"],
        ["npm", "run", "format:check"],
        ["npm", "test"],
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        [sys.executable, "tools/run_conformance.py", "--output", "tmp/checks/scenarios.json"],
        [sys.executable, "tools/check_docs.py"],
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
                "tools/run_conformance.py",
                "--backend",
                "shared",
                "--output",
                "tmp/checks/shared-scenarios.json",
            ]
        )
    results = []
    failed = False
    for index, command in enumerate(commands, 1):
        started = time.monotonic()
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
        log = out / f"{index:02d}.log"
        log.write_text(result.stdout + result.stderr)
        record = {
            "command": command,
            "exit_code": result.returncode,
            "seconds": round(time.monotonic() - started, 3),
            "log": str(log.relative_to(ROOT)),
        }
        results.append(record)
        print(("PASS" if result.returncode == 0 else "FAIL") + " " + " ".join(command), flush=True)
        if result.returncode:
            print((result.stdout + result.stderr)[-6000:], flush=True)
            failed = True
    (out / "results.json").write_text(
        json.dumps({"checks": results, "passed": not failed}, indent=2) + "\n"
    )
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
