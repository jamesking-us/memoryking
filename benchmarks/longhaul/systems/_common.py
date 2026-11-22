"""Shared plumbing for the adapters: process control by recorded PID, a minimal MCP stdio
client, environment lookups and small text helpers. Standard library only."""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from .base import Session

TOOLS_ROOT = Path(os.environ.get("LONGHAUL_SYSTEMS_ROOT", "/tmp"))


def install_dir(name: str) -> Path:
    """Where tools/install_systems.py puts a product (a directory of its own under /tmp)."""
    return TOOLS_ROOT / f"mk-lh-{name}"


def llm_env() -> dict[str, str]:
    """The model endpoint every model-needing product is pointed at (see SYSTEMS.md)."""
    base = os.environ.get("LONGHAUL_LLM_BASE_URL", "")
    return {
        "base_url": base,
        "model": os.environ.get("LONGHAUL_LLM_MODEL", ""),
        "api_key": os.environ.get("LONGHAUL_LLM_API_KEY", "") or ("sk-not-a-key" if base else ""),
    }


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def clean_env(extra: dict[str, str] | None = None, home: Path | None = None) -> dict[str, str]:
    """A minimal environment for a child process: PATH and locale only, HOME redirected into
    the instance, no inherited credentials, proxy and CA settings kept so registries stay
    reachable."""
    keep = {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "LANG", "LC_ALL", "TZ"}
        or k.upper() in {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}
        or k in {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"}
    }
    if home is not None:
        keep["HOME"] = str(home)
        keep["XDG_CACHE_HOME"] = str(home / ".cache")
        keep["XDG_CONFIG_HOME"] = str(home / ".config")
        keep["XDG_DATA_HOME"] = str(home / ".local/share")
    keep.update(extra or {})
    return keep


class Procs:
    """Child processes started by an adapter, recorded by PID in a file under the instance
    root. Each is started in its own session, so its process group is ours and stopping it
    never touches anything else."""

    def __init__(self, root: Path):
        self.file = root / "pids.json"
        self.pids: list[int] = []
        self.popens: list[subprocess.Popen] = []
        self.file.parent.mkdir(parents=True, exist_ok=True)

    def start(self, cmd, *, env, cwd, log: Path, stdin=subprocess.DEVNULL, stdout=None):
        log.parent.mkdir(parents=True, exist_ok=True)
        err = open(log, "ab")
        p = subprocess.Popen(
            cmd,
            env=env,
            cwd=cwd,
            stdin=stdin,
            stdout=stdout if stdout is not None else err,
            stderr=err,
            start_new_session=True,
        )
        err.close()
        self.pids.append(p.pid)
        self.popens.append(p)
        self.file.write_text(json.dumps(self.pids))
        return p

    def adopt(self, pid: int) -> None:
        """Record a process that daemonised itself (its recorded PID file is the authority)."""
        if pid not in self.pids:
            self.pids.append(pid)
            self.file.write_text(json.dumps(self.pids))

    @staticmethod
    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:  # a zombie we have not reaped still answers kill(pid, 0)
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            return False
        return state != "Z"

    def stop_all(self, grace: float = 5.0) -> None:
        pids = list(self.pids)
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2.0)):
            for pid in pids:
                try:
                    os.killpg(pid, sig)  # the group we created with start_new_session
                except (ProcessLookupError, PermissionError):
                    pass
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and any(self.alive(p) for p in pids):
                time.sleep(0.05)
            if not any(self.alive(p) for p in pids):
                break
        for p in self.popens:
            for f in (p.stdin, p.stdout):
                try:
                    if f:
                        f.close()
                except OSError:
                    pass
            try:
                p.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        self.popens = []
        self.pids = []
        self.file.write_text("[]")

    def running(self) -> list[int]:
        return [p for p in self.pids if self.alive(p)]


class McpStdio:
    """A minimal MCP client over a stdio child process (newline-delimited JSON-RPC)."""

    def __init__(self, procs: Procs, cmd, *, env, cwd, log: Path, timeout: float = 180.0):
        self.timeout = timeout
        self.proc = procs.start(
            cmd, env=env, cwd=cwd, log=log, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        self._id = 0
        self._lock = threading.Lock()
        self.initialize()

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        self.proc.stdin.flush()

    def _recv(self, want: int) -> dict:
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed its output")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == want and ("result" in msg or "error" in msg):
                return msg

    def request(self, method: str, params: dict | None = None) -> dict:
        with self._lock:
            self._id += 1
            self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
            msg = self._recv(self._id)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg["result"]

    def initialize(self) -> dict:
        res = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "longhaul", "version": "1"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return res

    def tools(self) -> list[dict]:
        return self.request("tools/list")["tools"]

    def call(self, name: str, arguments: dict) -> tuple[str, dict | None, bool]:
        res = self.request("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(
            c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"
        )
        return text, res.get("structuredContent"), bool(res.get("isError"))


_STOP = set(
    "a an and are as at be but by do does did for from had has have how i in is it its me my of on "
    "or our so that the their them then there these they this to was we were what when where which "
    "who why will with would you your about can could should".split()
)


def keywords(text: str, limit: int = 8) -> list[str]:
    """Distinct content words of a query, longest first (for products that match literal
    substrings only)."""
    seen: dict[str, None] = {}
    for w in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.\-]{2,}", text):
        lw = w.lower().strip(".-")
        if lw and lw not in _STOP:
            seen.setdefault(lw)
    return sorted(seen, key=lambda w: -len(w))[:limit]


def transcript(s: Session, max_chars: int | None = None) -> str:
    return "\n".join(
        f"{t.role}: {t.text if max_chars is None else t.text[:max_chars]}" for t in s.turns
    )


def clip(text: str, budget: int) -> str:
    return text if len(text) <= budget else text[: max(0, budget - 1)] + "…"


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def http_json(url: str, body: dict | None = None, method: str | None = None, timeout: float = 60.0):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method or ("POST" if data is not None else "GET"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # local services only
        raw = r.read()
    return json.loads(raw) if raw else None


FAKE_LLM = Path(__file__).resolve().parents[1] / "tools" / "fake_openai.py"


class FakeLlm:
    """The fake OpenAI-compatible server (tools/fake_openai.py), started as a recorded process."""

    def __init__(self, procs: Procs, root: Path, dims: int = 384):
        port_file = root / "fake_llm.port"
        port_file.unlink(missing_ok=True)
        self.procs = procs
        self.proc = procs.start(
            [
                sys.executable,
                "-I",
                str(FAKE_LLM),
                "--port-file",
                str(port_file),
                "--log",
                str(root / "fake_llm.jsonl"),
                "--embedding-dims",
                str(dims),
                "--entities",
                os.environ.get("LONGHAUL_FAKE_ENTITIES", "3"),
                *(
                    ["--dump", os.environ["LONGHAUL_FAKE_DUMP"]]
                    if os.environ.get("LONGHAUL_FAKE_DUMP")
                    else []
                ),
            ],
            env=clean_env(),
            cwd=root,
            log=root / "fake_llm.log",
        )
        deadline = time.monotonic() + 20
        while not port_file.exists() or not port_file.read_text().strip():
            if time.monotonic() > deadline:
                raise RuntimeError("fake LLM server did not start")
            time.sleep(0.05)
        self.port = int(port_file.read_text())
        self.base_url = f"http://127.0.0.1:{self.port}/v1"

    def stats(self) -> dict:
        return http_json(f"http://127.0.0.1:{self.port}/_stats")

    def reset(self) -> None:
        http_json(f"http://127.0.0.1:{self.port}/_reset", {})


def dist_version(venv: Path, dist: str) -> str:
    """Version of an installed distribution in a virtualenv, from its .dist-info directory."""
    for d in (venv / "lib").glob(f"python3*/site-packages/{dist}-*.dist-info"):
        return d.name[: -len(".dist-info")].split("-", 1)[1]
    return "unknown"
