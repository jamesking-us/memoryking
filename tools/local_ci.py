#!/usr/bin/env python3
"""Build and verify an immutable checkout locally; never invokes hosted jobs or SMTP."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from report_evidence import revision

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--revision", required=True, help="Full committed SHA; ignores working edits"
    )
    parser.add_argument("--output", type=Path, required=True, help="New local evidence directory")
    parser.add_argument(
        "--postgres-bin",
        type=Path,
        help="Explicit isolated PostgreSQL binary directory; makes native tests mandatory",
    )
    parser.add_argument(
        "--macos-supervision",
        action="store_true",
        help="Exercise and remove a temporary launchd service",
    )
    parser.add_argument(
        "--compose",
        type=Path,
        help="Explicit Docker Compose executable for disposable local shared rehearsal",
    )
    parser.add_argument(
        "--container-state",
        type=Path,
        help="New private directory outside the disposable checkout; retained after container rehearsal",
    )
    parser.add_argument("--container-port", type=int, default=19765)
    args = parser.parse_args()
    if bool(args.compose) != bool(args.container_state):
        parser.error("--compose and --container-state must be supplied together")
    if args.macos_supervision and sys.platform != "darwin":
        parser.error("--macos-supervision requires macOS")
    commit = revision(ROOT, args.revision)
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Run with the pinned Python 3.12 environment")
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit("Choose an empty output directory; retain previous job evidence")
    output.mkdir(parents=True, exist_ok=True)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "GMAIL_FROM", "GMAIL_APP_KEY", "KINDLE_TO"}
    }
    environment.pop("MEMORYKING_PG_BIN", None)
    environment.pop("MEMORYKING_REQUIRE_POSTGRES", None)
    pg_bin = args.postgres_bin.resolve() if args.postgres_bin else None
    if pg_bin is not None:
        if not all((pg_bin / name).is_file() for name in ("postgres", "initdb", "pg_ctl", "psql")):
            raise SystemExit("--postgres-bin must contain the isolated PostgreSQL runtime")
        environment["MEMORYKING_PG_BIN"] = str(pg_bin)
        environment["MEMORYKING_REQUIRE_POSTGRES"] = "1"
    result = {
        "revision": commit,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "scope": "Local clean-checkout foundation/local-product job; no hosted CI, SMTP or provider calls",
        "steps": [],
        "passed": False,
        "postgres_binary_directory": str(pg_bin) if pg_bin else None,
    }
    with tempfile.TemporaryDirectory(prefix="memoryking-local-ci-") as temporary:
        checkout = Path(temporary) / "checkout"
        python = checkout / ".venv/bin/python"
        steps = [
            ("clone", ["git", "clone", "--no-local", str(ROOT), str(checkout)], ROOT),
            ("checkout", ["git", "checkout", "--detach", commit], checkout),
            ("venv", [sys.executable, "-I", "-m", "venv", ".venv"], checkout),
            ("node", ["node", "--version"], checkout),
            ("npm", ["npm", "--version"], checkout),
            (
                "python-dependencies",
                [
                    str(python),
                    "-I",
                    "-m",
                    "pip",
                    "install",
                    "--require-hashes",
                    "-r",
                    "requirements-dev.lock",
                ],
                checkout,
            ),
            (
                "package",
                [
                    str(python),
                    "-I",
                    "-m",
                    "pip",
                    "install",
                    "--no-build-isolation",
                    "--no-deps",
                    "-e",
                    ".",
                ],
                checkout,
            ),
            ("node-dependencies", ["npm", "ci", "--ignore-scripts"], checkout),
            ("checks", [str(python), "tools/check.py"], checkout),
            ("build", [str(python), "-m", "build", "--no-isolation"], checkout),
            ("wheel", [str(python), "tools/check_wheel.py"], checkout),
            ("release", [str(python), "tools/build_release.py"], checkout),
            (
                "contracts-package",
                [
                    str(python),
                    "tools/contracts_package_rehearsal.py",
                    "--output",
                    "tmp/contracts-package",
                ],
                checkout,
            ),
            (
                "runtime-install",
                [
                    str(python),
                    "tools/runtime_install_rehearsal.py",
                    "--output",
                    "tmp/runtime-install",
                ],
                checkout,
            ),
            ("dependencies", [str(python), "-I", "-m", "pip", "check"], checkout),
            ("dependency-inventory", [str(python), "tools/dependency_inventory.py"], checkout),
            ("worktree", ["git", "status", "--porcelain"], checkout),
        ]
        if pg_bin is not None:
            steps.insert(5, ("postgresql", [str(pg_bin / "postgres"), "--version"], checkout))
            checks_index = next(i for i, step in enumerate(steps) if step[0] == "checks")
            steps.insert(
                checks_index + 1, ("shared-demo", [str(python), "tools/shared_demo.py"], checkout)
            )
            steps.insert(
                checks_index + 2,
                (
                    "shared-pilot",
                    [
                        str(python),
                        "tools/shared_contention_pilot.py",
                        "--output",
                        "tmp/shared-pilot.json",
                        "--repetitions",
                        "5",
                    ],
                    checkout,
                ),
            )
        if args.macos_supervision:
            steps.insert(
                -1,
                (
                    "supervision",
                    [str(python), "tools/supervision_rehearsal.py", "--output", "tmp/supervision"],
                    checkout,
                ),
            )
        if args.compose:
            steps.insert(
                -1,
                (
                    "containers",
                    [
                        str(python),
                        "tools/shared_compose_rehearsal.py",
                        "--compose",
                        str(args.compose.resolve()),
                        "--directory",
                        str(args.container_state.resolve()),
                        "--output",
                        "tmp/containers",
                        "--port",
                        str(args.container_port),
                    ],
                    checkout,
                ),
            )
        for name, command, cwd in steps:
            started = time.monotonic()
            with (output / f"{name}.log").open("w") as log:
                try:
                    completed = subprocess.run(
                        command, cwd=cwd, env=environment, stdout=log, stderr=subprocess.STDOUT
                    )
                    code = completed.returncode
                except OSError:
                    log.write("Command could not start; check local prerequisites.\n")
                    code = 127
            if name == "worktree" and (output / "worktree.log").read_text().strip():
                code = 1
            result["steps"].append(
                {
                    "name": name,
                    "command": command,
                    "exit_code": code,
                    "seconds": round(time.monotonic() - started, 3),
                    "log": f"{name}.log",
                }
            )
            print(f"{'PASS' if code == 0 else 'FAIL'} {name}", flush=True)
            if code:
                break
        if (checkout / "tmp/checks").exists():
            shutil.copytree(checkout / "tmp/checks", output / "checks")
        if (checkout / "tmp/shared-pilot.json").exists():
            shutil.copyfile(checkout / "tmp/shared-pilot.json", output / "shared-pilot.json")
        if (checkout / "tmp/runtime-install").exists():
            shutil.copytree(checkout / "tmp/runtime-install", output / "runtime-install")
        if (checkout / "tmp/contracts-package").exists():
            shutil.copytree(checkout / "tmp/contracts-package", output / "contracts-package")
        for directory in ("supervision", "containers"):
            if (checkout / "tmp" / directory).exists():
                shutil.copytree(checkout / "tmp" / directory, output / directory)
        if (checkout / "dist").exists():
            shutil.copytree(checkout / "dist", output / "distributions")
        result["passed"] = len(result["steps"]) == len(steps) and all(
            step["exit_code"] == 0 for step in result["steps"]
        )
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    (output / "job.json").write_text(json.dumps(result, indent=2) + "\n")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
