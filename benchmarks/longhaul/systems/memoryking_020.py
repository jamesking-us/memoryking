"""Arm `memoryking_020`: MemoryKing 0.2.0 as released on PyPI, in a virtualenv of its own
(`tools/install_systems.py memoryking-0.2.0`, default `/tmp/mk-lh-memoryking-0.2.0`).

It is fed and asked exactly as the current `memoryking_semantic` arm is: the arm's code runs in a
worker process under the released package's own interpreter (`_released_worker.py`), so the store,
the capture path, the daemon (`python -m memoryking serve` by that interpreter), the socket protocol
and the briefing's receipt are 0.2.0's, not this checkout's. The version is checked three times: by
`--version` when the arm is created, by the worker before it imports anything of the harness, and
by the version the library reports in the worker; a different one stops the arm."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

from . import _common as c
from .base import HostConfig, Scope, SeedReport, Session

EXPECTED = "0.2.0"
REPO = Path(__file__).resolve().parents[3]
WORKER = Path(__file__).resolve().parent / "_released_worker.py"


def venv_dir() -> Path:
    return Path(
        os.environ.get("LONGHAUL_MEMORYKING_020", str(c.install_dir("memoryking-0.2.0") / "venv"))
    )


def released_version(venv: Path) -> str:
    """What the released package's own command line reports (`memoryking --version`), run isolated."""
    proc = subprocess.run(
        [str(venv / "bin/python"), "-I", "-m", "memoryking", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd="/",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{venv}: `memoryking --version` failed: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip().split()[-1]


class MemoryKingReleased:
    name = "memoryking_020"
    needs_llm = False
    reseed_per_run = False
    persistent_home = False
    ingest = "turns"

    def __init__(self) -> None:
        self.venv = venv_dir()
        if not (self.venv / "bin/python").is_file():
            raise RuntimeError(
                f"no released MemoryKing at {self.venv}: run "
                "`python benchmarks/longhaul/tools/install_systems.py memoryking-0.2.0`"
            )
        self.version = released_version(self.venv)
        if self.version != EXPECTED:
            raise RuntimeError(f"{self.venv} holds MemoryKing {self.version}, not {EXPECTED}")
        self.proc: subprocess.Popen | None = None
        self.retrieval_note: dict = {}
        self.root: Path | None = None
        self._log = None

    def _call(self, **cmd):
        assert self.proc is not None and self.proc.stdin and self.proc.stdout
        if self.proc.poll() is not None:
            raise RuntimeError(f"the 0.2.0 worker exited ({self.proc.returncode}); see worker.log")
        self.proc.stdin.write(json.dumps(cmd) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("the 0.2.0 worker closed its output; see worker.log")
        answer = json.loads(line)
        if not answer.get("ok"):
            raise RuntimeError("0.2.0 worker: " + str(answer.get("error")))
        return answer["result"]

    def prepare(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._log = open(self.root / "worker.log", "ab")
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
        self.proc = subprocess.Popen(
            [str(self.venv / "bin/python"), "-I", str(WORKER), str(REPO), EXPECTED, str(self.venv)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            text=True,
            cwd=self.root,  # a neutral directory: nothing of this checkout on the path
            env=env,
            start_new_session=True,
        )
        first = self.proc.stdout.readline() if self.proc.stdout else ""
        if not first:
            raise RuntimeError("the 0.2.0 worker did not start; see " + str(self.root / "worker.log"))
        ready = json.loads(first)
        if ready.get("version") != EXPECTED:
            raise RuntimeError(f"the worker runs MemoryKing {ready.get('version')}, not {EXPECTED}")
        self._call(op="prepare", root=str(self.root / "store"))

    def seed(self, sessions: Iterable[Session]) -> SeedReport:
        started = time.perf_counter()
        wire = [dataclasses.asdict(s) for s in sessions]
        result = self._call(op="seed", sessions=wire)
        result["seconds"] = time.perf_counter() - started
        return SeedReport(**result)

    def before_retrieval(self, scope: Scope) -> dict:
        done = self._call(op="before_retrieval", scope=dataclasses.asdict(scope))
        return {**done, "version": self.version}

    def context_for(self, query: str, scope: Scope, budget_chars: int) -> str:
        result = self._call(
            op="context_for", query=query, scope=dataclasses.asdict(scope), budget=budget_chars
        )
        self.retrieval_note = result["note"]
        return result["context"]

    def host(self, workdir: Path, scope: Scope) -> HostConfig:
        raise NotImplementedError("memoryking_020 is a question-answering arm (T5/T6) only")

    def forget(self, text_marker: str, scope: Scope) -> bool | None:
        return None

    def usage(self) -> dict:
        return {}

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            try:
                assert proc.stdin and proc.stdout
                proc.stdin.write(json.dumps({"op": "close"}) + "\n")
                proc.stdin.flush()
                proc.stdout.readline()
            except Exception:
                pass
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()  # this worker only: we hold its process object
                proc.wait()
            for pipe in (proc.stdin, proc.stdout):
                if pipe is not None:
                    pipe.close()
        if self._log is not None:
            self._log.close()
            self._log = None


def create() -> MemoryKingReleased:
    return MemoryKingReleased()
