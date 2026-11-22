"""Arm `memoryking`: what `memoryking setup claude-code` installs, with the history seeded
through the path a real hook-captured session takes.

Installed for the agent (see `host`): the three session hooks and the lean MCP bridge with
`alwaysLoad`, built by the same code as the setup command (`host_hooks.install`,
`setup_host.mcp_entry`), under one agent credential per project and agent. The agent is told
nothing about memory; the start note, the server's instructions and the hooks are the only
mention.

Seeding (`ingest = "hook"`, the default): each session becomes a Claude Code transcript
(JSONL) and the real `session-end` hook code (`host_hooks.save`) captures it through the
daemon's socket under the session's own project and agent credential: the transcript tail as
one observation (user turns, assistant text and tool errors; successful tool results and tool
inputs are never read) and the user's own turns as a second one. Nothing is held: a real
installation has no source-trust rules unless an operator writes them, and setup writes none,
so text from tool errors reaches memory exactly as it would in a real session. The store
runs on a clock that is moved to each session's start while seeding, so every record carries
the session's date; the daemon an agent talks to afterwards runs on the real clock. The
observed time of each capture is the session's start (the hooks use the day).

`ingest = "turns"` (track T5) records each chat turn as one observation, `role: text`, as the
repository's `tools/evidence_recall.py` does, and `context_for` asks for a briefing.

The source-trust rules, held captures and the review queue are left as setup leaves them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from memoryking import __version__, hooks, host_hooks, reviewing, setup_host
from memoryking.adapters.local_ipc import Server, request
from memoryking.adapters.local_store import LocalStore

from .base import HostConfig, Scope, SeedReport, Session

AGENT_CAPABILITIES = "capture,read,checkpoint,handoff"
TOOLS = ("remember", "recall", "save_progress", "resume", "hand_off", "fetch")
CREDENTIAL_DAYS = "3650d"
SOCKET_LIMIT = 100


def rfc3339_ms(stamp: str) -> int:
    text = stamp.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


class StampClock:
    """The store's clock, moved by hand to each session's time while seeding. Wall time only
    moves forward (the store refuses a step back); the monotonic clock is the real one."""

    def __init__(self, utc_ms: int):
        self.offset = utc_ms - time.time_ns() // 1_000_000

    def utc_ms(self) -> int:
        return time.time_ns() // 1_000_000 + self.offset

    def monotonic_ms(self) -> int:
        return time.monotonic_ns() // 1_000_000

    def advance_to(self, utc_ms: int) -> None:
        self.offset = max(self.offset, utc_ms - time.time_ns() // 1_000_000)


class _TimeShim:
    """Stands in for `time` inside `memoryking.hooks` so the hook's observed time is the
    session's. Everything but `time()` is the real module."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def __getattr__(self, name: str):
        return getattr(time, name)


def transcript_lines(session: Session) -> str:
    """The session as Claude Code writes a transcript: user turns, assistant text, and a tool
    call with its result for each tool turn (an `ERROR: ` result is marked as an error)."""
    lines: list[dict] = []
    for n, turn in enumerate(session.turns):
        if turn.role == "user":
            lines.append({"type": "user", "message": {"role": "user", "content": turn.text}})
        elif turn.role == "assistant":
            lines.append(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": turn.text}],
                    },
                }
            )
        else:
            call = f"toolu_{n:04d}"
            lines.append(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": call,
                                "name": "Bash",
                                "input": {"command": "run"},
                            }
                        ],
                    },
                }
            )
            result = {"type": "tool_result", "tool_use_id": call, "content": turn.text}
            if turn.text.startswith("ERROR: "):
                result["is_error"] = True
            lines.append({"type": "user", "message": {"role": "user", "content": [result]}})
    return "".join(json.dumps(line) + "\n" for line in lines)


class MemoryKing:
    name = "memoryking"
    version = __version__
    needs_llm = False
    reseed_per_run = False
    persistent_home = False
    ingest = "hook"  # or "turns"

    def __init__(self) -> None:
        self.root: Path | None = None
        self.server: Server | None = None
        self.store: LocalStore | None = None
        self.thread: threading.Thread | None = None
        self.daemon: subprocess.Popen | None = None
        self.baseline: dict | None = None
        self._scope: Scope | None = None

    # ------------------------------------------------------------------ paths

    @property
    def data(self) -> Path:
        assert self.root is not None
        return self.root / "data"

    @property
    def control(self) -> Path:
        assert self.root is not None
        return self.root / "control"

    @property
    def socket(self) -> Path:
        return self.data / "service.sock"

    @property
    def owner(self) -> Path:
        return self.control / "owner.credential"

    @staticmethod
    def _env() -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}

    # -------------------------------------------------------------- lifecycle

    def prepare(self, root: Path) -> None:
        """An empty store under `root`, or the seeded one when `root` already holds it. The
        daemon is started when something needs it, not here."""
        self.root = Path(root).resolve()
        if len(os.fsencode(self.socket)) > SOCKET_LIMIT:
            raise RuntimeError(
                f"{self.socket} is longer than {SOCKET_LIMIT} bytes, too long for a Unix socket; "
                "use a shorter root such as /tmp/mk-lh/<name>"
            )
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        (self.root / "agents").mkdir(exist_ok=True)
        os.chmod(self.root / "agents", 0o700)
        if not self.control.exists():
            LocalStore.initialize(self.data, self.control)
        # Pids recorded by an earlier life of this root are not ours; nothing to stop.
        (self.root / "daemon.pid").unlink(missing_ok=True)

    def _start_daemon(self) -> None:
        self._stop_inprocess()
        if self.daemon is not None and self.daemon.poll() is None:
            return
        assert self.root is not None
        log_path = self.root / "serve.log"
        with log_path.open("wb") as log:
            self.daemon = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "memoryking",
                    "serve",
                    "--store",
                    str(self.data),
                    "--control",
                    str(self.control),
                ],
                stdout=log,
                stderr=log,
                env=self._env(),
                start_new_session=True,
            )
        (self.root / "daemon.pid").write_text(str(self.daemon.pid))
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.daemon.poll() is not None:
                raise RuntimeError("the daemon exited: " + log_path.read_text()[-300:])
            if b'"ready": true' in log_path.read_bytes():
                return
            time.sleep(0.05)
        raise RuntimeError("the daemon did not become ready")

    def _stop_daemon(self) -> None:
        proc, self.daemon = self.daemon, None
        if proc is None:
            return
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            if proc.poll() is not None:
                break
            try:
                os.kill(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        if self.root is not None:
            (self.root / "daemon.pid").unlink(missing_ok=True)

    def _start_inprocess(self, first_ms: int) -> StampClock:
        self._stop_daemon()
        clock = StampClock(first_ms - 1)
        self.store = LocalStore(self.data, self.control, clock=clock)
        self.server = Server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return clock

    def _stop_inprocess(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            if self.thread is not None:
                self.thread.join(timeout=10)
            self.server.server_close()
            self.server = None
        if self.store is not None:
            self.store.close()
            self.store = None

    def close(self) -> None:
        self._stop_inprocess()
        self._stop_daemon()

    # ------------------------------------------------------------ credentials

    def _credential(self, scope: Scope) -> Path:
        """One agent credential per (project, agent), issued on first use with the owner's."""
        assert self.root is not None
        safe = re.sub(r"[^A-Za-z0-9_-]", "-", f"{scope.project}--{scope.agent}")
        path = self.root / "agents" / f"{safe}.credential"
        if not path.exists():
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memoryking",
                    "credential",
                    "issue",
                    scope.agent,
                    "--socket",
                    str(self.socket),
                    "--credential",
                    str(self.owner),
                    "--project",
                    scope.project,
                    "--capabilities",
                    AGENT_CAPABILITIES,
                    "--expires",
                    CREDENTIAL_DAYS,
                    "--output",
                    str(path),
                ],
                capture_output=True,
                text=True,
                env=self._env(),
                timeout=120,
            )
            if proc.returncode != 0:
                raise RuntimeError("credential issue failed: " + proc.stderr.strip()[:300])
        return path

    # ---------------------------------------------------------------- seeding

    def seed(self, sessions: Iterable[Session]) -> SeedReport:
        ordered = sorted(sessions, key=lambda s: (s.started, s.id))
        if not ordered:
            return SeedReport(0, 0.0, 0, 0, "empty history")
        assert self.root is not None
        started = time.perf_counter()
        clock = self._start_inprocess(rfc3339_ms(ordered[0].started))
        shim = _TimeShim()
        real_time = hooks.time
        hooks.time = shim
        scratch = self.root / "scratch"
        scratch.mkdir(exist_ok=True)
        os.chmod(scratch, 0o700)
        try:
            for n, s in enumerate(ordered):
                ms = rfc3339_ms(s.started)
                clock.advance_to(ms)
                shim.now = ms / 1000
                cred = self._credential(s.scope)
                if self.ingest == "turns":
                    self._capture_turns(s, cred, ms)
                else:
                    self._capture_session(s, cred, scratch)
                if n % 200 == 199:
                    while self.store.drain():  # the daemon drains audit jobs between requests
                        pass
            while self.store.drain():
                pass
        finally:
            hooks.time = real_time
            shutil.rmtree(scratch, ignore_errors=True)
        if self.ingest != "turns":
            # Keep the clock honest for the agent's daemon: close the fake-clock store first.
            self._stop_inprocess()
        seconds = time.perf_counter() - started
        return SeedReport(
            len(ordered),
            seconds,
            0,
            0,
            f"ingest={self.ingest}; no model; store {self.size_bytes()} bytes on disk",
        )

    def _capture_session(self, s: Session, cred: Path, scratch: Path) -> None:
        safe = re.sub(r"[^A-Za-z0-9_-]", "-", s.id)
        transcript = scratch / f"{safe}.jsonl"
        transcript.write_text(transcript_lines(s))
        # What Claude Code sends the SessionEnd hook: the session id and the transcript path.
        event = {
            "host": "claude",
            "session": s.id,
            "transcript": str(transcript),
            "cwd": f"/work/{s.scope.project}",
        }
        host_hooks.save(
            event,
            "session-end",
            host="claude",
            socket=self.socket,
            credential=cred,
            project=s.scope.project,
        )

    def _capture_turns(self, s: Session, cred: Path, ms: int) -> None:
        safe = re.sub(r"[^A-Za-z0-9_-]", "-", s.id)[:80]
        for i, t in enumerate(s.turns):
            text = f"{t.role}: {t.text}"
            if not text.strip():
                continue
            hooks.capture(
                text,
                source="longmemeval",
                event=f"{safe}-{i}",
                observed_ms=ms + i,
                socket=self.socket,
                credential=cred,
                project=s.scope.project,
            )

    def size_bytes(self) -> int:
        assert self.root is not None
        total = 0
        for base in (self.data, self.control):
            for p in base.rglob("*"):
                if p.is_file() and not p.is_socket():
                    total += p.stat().st_size
        return total

    # --------------------------------------------------------------- the host

    def host(self, workdir: Path, scope: Scope) -> HostConfig:
        self._start_daemon()
        cred = self._credential(scope)
        installed = host_hooks.install(
            host="claude",
            scope="user",
            project=scope.project,
            cwd=Path(workdir),
            home=Path(workdir) / ".unused-home",
            socket=self.socket,
            credential=cred,
            tools="mcp",
            write=False,
        )
        bridge = [
            sys.executable,
            "-m",
            "memoryking",
            "mcp",
            "--socket",
            str(self.socket),
            "--credential",
            str(cred),
            "--project",
            scope.project,
            "--profile",
            "lean",
        ]
        mcp = {"mcpServers": {"memoryking": setup_host.mcp_entry(bridge)}}
        allowed = [f"mcp__memoryking__{t}" for t in TOOLS]
        self._scope = scope
        self.baseline = self.baseline or self._counts(scope)
        return HostConfig(mcp, installed["config"], {}, allowed, {}, "")

    # ------------------------------------------------------------ T5 retrieval

    def context_for(self, query: str, scope: Scope, budget_chars: int) -> str:
        if self.server is None:
            self._start_daemon()
        cred = self._credential(scope)
        packet = hooks.recall(
            query[:2048],
            budget=max(1, budget_chars // 3),
            text=True,
            socket=self.socket,
            credential=cred,
            project=scope.project,
        )
        return str(packet.get("text", ""))[:budget_chars]

    # ---------------------------------------------------------------- forget

    def forget(self, text_marker: str, scope: Scope) -> bool | None:
        """The documented path: an agent asks for a memory to be retired (`forget`), an admin
        approves it, which purges the record with its dependencies."""
        self._start_daemon()
        cred = self._credential(scope)
        removed = 0
        for _ in range(8):
            packet = hooks.recall(
                text_marker,
                budget=6000,
                text=True,
                socket=self.socket,
                credential=cred,
                project=scope.project,
            )
            text = str(packet.get("text", ""))
            ids = sorted(set(re.findall(r"obs_[0-9a-f]{32}", text)))
            hits = [i for i in ids if self._contains(i, text_marker, scope, cred)]
            if not hits:
                break
            asker = reviewing.Reviewer(scope.project, socket=self.socket, credential=cred)
            admin = reviewing.Reviewer(scope.project, socket=self.socket, credential=self.owner)
            for obs in hits:
                asked = asker.call(
                    "retire", {"observation": obs, "note": "forget this"}, "forget-" + obs
                )
                admin.call(
                    "retire_decide",
                    {
                        "request": asked["request"],
                        "decision": "approve",
                        "reason": "approved_in_cli",
                    },
                    "approve-" + asked["request"],
                )
                removed += 1
        return removed > 0

    def _contains(self, observation: str, marker: str, scope: Scope, cred: Path) -> bool:
        _, token, project = hooks.connection(
            socket=self.socket, credential=cred, project=scope.project
        )
        context = request(self.socket, token, "context", {"project": project})
        found = request(
            self.socket,
            token,
            "inspection",
            {
                "command": {
                    "version": 1,
                    "incarnation": context["incarnation"],
                    "action": "catalog",
                    "args": {"project": project, "id": observation, "limit": 1},
                }
            },
        )["items"]
        return bool(found) and marker in json.dumps(found[0])

    # ----------------------------------------------------------------- usage

    def _counts(self, scope: Scope) -> dict:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                "health",
                "--socket",
                str(self.socket),
                "--credential",
                str(self.owner),
                "--project",
                scope.project,
                "--json",
            ],
            capture_output=True,
            text=True,
            env=self._env(),
            timeout=60,
        )
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout)
        return {
            "observations": data["observations"]["total"],
            "briefings": data["briefings"]["total"],
            "deliveries": data["briefings"]["deliveries"],
        }

    def usage(self) -> dict:
        """Reads and writes since `host` was called: observations added (the hooks and the
        agent's own `remember`), briefings served (the start hook and `recall`) and
        deliveries."""
        if self.baseline is None or self._scope is None:
            return {}
        now = self._counts(self._scope)
        return {
            k: now.get(k, 0) - self.baseline.get(k, 0)
            for k in ("observations", "briefings", "deliveries")
        }


def create() -> MemoryKing:
    return MemoryKing()
