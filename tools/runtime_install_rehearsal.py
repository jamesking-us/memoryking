"""Install the built wheel into a fresh core-only environment and execute packaged paths."""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("Use a new output directory; retain previous evidence")
    output.mkdir(parents=True)
    wheels = list((ROOT / "dist").glob("memoryking-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Build exactly one candidate wheel in dist first")
    wheel = wheels[0]
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "PYTHONHOME", "GMAIL_FROM", "GMAIL_APP_KEY", "KINDLE_TO"}
    }
    report = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "wheel": wheel.name,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "lock_sha256": hashlib.sha256((ROOT / "requirements-core.lock").read_bytes()).hexdigest(),
        "steps": [],
        "passed": False,
        "scope": "Fresh isolated core-only wheel install on this host; no shared/model extras",
    }

    def run(name, command, cwd):
        started = time.monotonic()
        result = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True, timeout=180
        )
        (output / (name + ".log")).write_text(result.stdout + result.stderr)
        report["steps"].append(
            {
                "name": name,
                "exit_code": result.returncode,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        )
        print(("PASS " if result.returncode == 0 else "FAIL ") + name, flush=True)
        if result.returncode:
            raise RuntimeError("Fresh install rehearsal failed: " + name)
        return result.stdout

    try:
        with tempfile.TemporaryDirectory(prefix="mk-install-", dir="/tmp") as temp:
            directory = Path(temp)
            python = directory / "venv/bin/python"
            run(
                "create-environment",
                [sys.executable, "-I", "-m", "venv", str(directory / "venv")],
                directory,
            )
            run(
                "locked-core",
                [
                    str(python),
                    "-I",
                    "-m",
                    "pip",
                    "install",
                    "--require-hashes",
                    "-r",
                    str(ROOT / "requirements-core.lock"),
                ],
                directory,
            )
            run(
                "install-wheel",
                [str(python), "-I", "-m", "pip", "install", "--no-deps", str(wheel)],
                directory,
            )
            run("dependencies", [str(python), "-I", "-m", "pip", "check"], directory)
            run(
                "installed-module",
                [
                    str(python),
                    "-I",
                    "-c",
                    "import memoryking, importlib.util; from pathlib import Path; p=Path(memoryking.__file__); assert 'site-packages' in p.parts; assert all(importlib.util.find_spec(n) is None for n in ['psycopg','onnxruntime','tiktoken','reportlab','mypy','ruff']); print(p)",
                ],
                directory,
            )
            for entry in [
                "memoryking",
                "memoryking-migrate",
                "memoryking-reference-agent",
                "memoryking-repository-worker",
                "memoryking-embedding-worker",
            ]:
                run("entry-" + entry, [str(directory / "venv/bin" / entry), "--help"], directory)
            for example in ["local_demo", "stdio_demo", "migration_demo"]:
                command = [str(python), str(ROOT / "tools" / (example + ".py"))]
                if example == "stdio_demo":
                    command += ["--tokenizer", "utf8-byte-estimate-v1"]
                run(example, command, directory)
            run(
                "remember-and-resume",
                [str(python), str(ROOT / "examples/remember_and_resume.py")],
                directory,
            )
            report["distributions"] = json.loads(
                run(
                    "inventory",
                    [str(python), "-I", "-m", "pip", "list", "--format=json"],
                    directory,
                )
            )
            report["passed"] = True
    finally:
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
