"""Helpers shared by the tasks' check.py files (run after the second session).

A check receives the working directory as its only argument, prints one JSON object
and exits 0 only when every named check passed. Hidden tests are run from a temporary
file outside the working directory, with the working directory on PYTHONPATH.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail[-1500:]})
        return bool(ok)

    def finish(self) -> int:
        passed = bool(self.checks) and all(c["ok"] for c in self.checks)
        print(json.dumps({"passed": passed, "checks": self.checks}))
        return 0 if passed else 1


def workdir_arg() -> Path:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check.py WORKDIR")
    return Path(sys.argv[1]).resolve()


def _env(workdir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONPATH"] = str(workdir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def hidden_tests(workdir: Path, source: str, timeout: int = 60) -> tuple[bool, str]:
    """Run a unittest module given as source text against the working directory."""
    with tempfile.TemporaryDirectory(prefix="mk-hidden-") as tmp:
        script = Path(tmp) / "hidden_tests.py"
        script.write_text(textwrap.dedent(source))
        try:
            proc = subprocess.run(
                [sys.executable, "-B", str(script)],
                cwd=workdir,
                env=_env(workdir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, "hidden tests timed out"
        return proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]


def visible_tests(workdir: Path, timeout: int = 60) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
            cwd=workdir,
            env=_env(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "visible tests timed out"
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-800:]


def tree_digest(root: Path, rel: str) -> str:
    """Digest of every file under root/rel (names and bytes); empty when absent."""
    base = root / rel
    if not base.exists():
        return ""
    digest = hashlib.sha256()
    files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
    for path in files:
        if "__pycache__" in path.parts:
            continue
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def unchanged(workdir: Path, pristine: Path, rel: str) -> bool:
    return tree_digest(workdir, rel) == tree_digest(pristine, rel)


def pristine_repo(check_file: str) -> Path:
    return Path(check_file).resolve().parent / "repo"
