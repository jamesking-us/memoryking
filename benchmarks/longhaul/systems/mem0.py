"""Adapter: Mem0 open source (`mem0ai`), the Python library.

Ingest: `Memory.add(messages, user_id, agent_id, run_id, metadata)`, one call per session; the
library makes its own chat-model call to extract facts (since v2 a single ADD-only pass) and
embeds them. Retrieval: `Memory.search(query, filters=...)`; no model call. State: a local Qdrant
directory and a SQLite history file under the instance root. Embeddings: FastEmbed
`BAAI/bge-small-en-v1.5`, run locally. Chat model: whatever `LONGHAUL_LLM_BASE_URL` points at,
through the library's OpenAI provider (`openai_base_url`).

The library runs in a worker process under the Mem0 virtualenv (`_mem0_worker.py --rpc`), started
and stopped by this adapter by recorded PID. `host()` returns the harness's own MCP bridge over
the library (`_mem0_worker.py --mcp`), because the project's MCP server and Claude Code plugin
need the hosted Platform; see SYSTEMS.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Iterable

from . import _common as c
from .base import HostConfig, Scope, SeedReport, Session

NAME = "mem0"
WORKER = Path(__file__).resolve().parent / "_mem0_worker.py"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"


class Mem0:
    name = NAME
    needs_llm = True

    def __init__(self) -> None:
        self.home = c.install_dir(NAME)
        self.py = self.home / "venv/bin/python"
        self.version = c.dist_version(self.home / "venv", "mem0ai")
        self.root: Path | None = None
        self.procs: c.Procs | None = None
        self.proc: subprocess.Popen | None = None
        self.llm: c.FakeLlm | None = None
        self.llm_cfg: dict[str, str] = {}
        self.reads = 0
        self.writes = 0

    @staticmethod
    def installed() -> bool:
        return (c.install_dir(NAME) / "venv/bin/python").is_file()

    # -- environment -----------------------------------------------------------
    def _vars(self) -> dict[str, str]:
        assert self.root is not None
        return {
            "MEM0_DIR": str(self.root / "mem0"),
            "MEM0_TELEMETRY": "false",  # documented opt-out of anonymous telemetry
            "LONGHAUL_LLM_BASE_URL": self.llm_cfg.get("base_url", ""),
            "LONGHAUL_LLM_MODEL": self.llm_cfg.get("model", ""),
            "LONGHAUL_LLM_API_KEY": self.llm_cfg.get("api_key", ""),
            "LONGHAUL_EMBED_MODEL": EMBED_MODEL,
            # harness fixes: cache the embedding model once, and avoid the xet transport that
            # does not pass this machine's proxy
            "FASTEMBED_CACHE_PATH": str(self.home / "models"),
            "HF_HUB_DISABLE_XET": "1",
        }

    def _env(self) -> dict[str, str]:
        assert self.root is not None
        return c.clean_env(self._vars(), home=self.root / "home")

    # -- lifecycle -------------------------------------------------------------
    def prepare(self, root: Path) -> None:
        self.root = Path(root)
        for d in ("home", "mem0"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.procs = c.Procs(self.root)
        self.reads = self.writes = 0
        self.llm_cfg = c.llm_env()
        if not self.llm_cfg["base_url"] and os.environ.get("LONGHAUL_FAKE_LLM") == "1":
            self.llm = c.FakeLlm(self.procs, self.root)
            self.llm_cfg = {
                "base_url": self.llm.base_url,
                "model": "fake-model",
                "api_key": "sk-not-a-key",
            }

    def _worker(self) -> subprocess.Popen:
        if self.proc is None or self.proc.poll() is not None:
            assert self.procs is not None and self.root is not None
            self.proc = self.procs.start(
                [str(self.py), "-I", str(WORKER), "--rpc"],
                env=self._env(),
                cwd=self.root,
                log=self.root / "worker.log",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            assert self.proc.stdout is not None
            ready = json.loads(self.proc.stdout.readline() or b"{}")
            if not ready.get("ready"):
                raise RuntimeError(f"mem0 worker did not start; see {self.root}/worker.log")
        return self.proc

    def _rpc(self, req: dict):
        p = self._worker()
        assert p.stdin is not None and p.stdout is not None
        p.stdin.write((json.dumps(req) + "\n").encode())
        p.stdin.flush()
        line = p.stdout.readline()
        if not line:
            raise RuntimeError("mem0 worker closed its output")
        ans = json.loads(line)
        if not ans.get("ok"):
            raise RuntimeError(f"mem0 {req['op']} failed: {ans.get('error')}")
        return ans["result"]

    def _stop_worker(self) -> None:
        if self.procs is not None:
            self.procs.stop_all()
        self.proc = None

    def close(self) -> None:
        # The fake LLM server (if any) is stopped with the worker: both are in the same Procs.
        self._stop_worker()

    # -- ingest ----------------------------------------------------------------
    @staticmethod
    def messages(s: Session) -> list[dict]:
        out = []
        for t in s.turns:
            if t.role == "user":
                out.append({"role": "user", "content": t.text})
            elif t.role == "assistant":
                out.append({"role": "assistant", "content": t.text})
            else:  # tool output is not the user's; Mem0 treats agent-side text as first-class
                out.append({"role": "assistant", "content": f"[tool output] {t.text}"})
        return out

    def seed(self, sessions: Iterable[Session]) -> SeedReport:
        base = self.llm.stats() if self.llm else None
        started = time.monotonic()
        n = 0
        for s in sessions:
            self._rpc(
                {
                    "op": "add",
                    "messages": self.messages(s),
                    "user_id": s.scope.user,
                    "agent_id": s.scope.agent,
                    "run_id": s.id,
                    "metadata": {"project": s.scope.project, "started": s.started},
                }
            )
            self.writes += 1
            n += 1
        calls = tokens = 0
        note = "Memory.add per session (user_id=user, agent_id=agent, run_id=session; project and start time in metadata)"
        if self.llm and base is not None:
            now = self.llm.stats()
            calls = now["chat_calls"] - base["chat_calls"]
            tokens = (now["chat_prompt_tokens"] + now["chat_completion_tokens"]) - (
                base["chat_prompt_tokens"] + base["chat_completion_tokens"]
            )
            note += "; counts are the fake server's"
        else:
            note += "; model usage is not measured here (see the endpoint's own accounting)"
        return SeedReport(
            sessions=n,
            seconds=time.monotonic() - started,
            llm_calls=calls,
            llm_tokens=tokens,
            notes=note,
        )

    # -- host ------------------------------------------------------------------
    def host(self, workdir: Path, scope: Scope) -> HostConfig:
        env = {k: v for k, v in self._vars().items() if k not in ("LONGHAUL_LLM_API_KEY",)}
        env["LONGHAUL_LLM_API_KEY"] = (
            "${LONGHAUL_LLM_API_KEY}"  # filled from the runner's environment
        )
        env["MEM0_DEFAULT_USER_ID"] = scope.user
        env["MEM0_DEFAULT_PROJECT"] = scope.project
        return HostConfig(
            mcp={
                "mcpServers": {
                    "mem0": {
                        "command": str(self.py),
                        "args": ["-I", str(WORKER), "--mcp"],
                        "env": env,
                    }
                }
            },
            settings={},
            files={},
            allowed_tools=["mcp__mem0"],
            env={},
            prompt_note="",
        )

    # -- retrieval -------------------------------------------------------------
    def context_for(self, query: str, scope: Scope, budget_chars: int) -> str:
        res = self._rpc(
            {
                "op": "search",
                "query": query,
                "top_k": 10,
                "filters": {"user_id": scope.user, "project": scope.project},
            }
        )
        self.reads += 1
        out, used = [], 0
        for r in res.get("results", []):
            line = f"- {r.get('memory', '')}"
            if used + len(line) > budget_chars:
                break
            out.append(line)
            used += len(line) + 1
        return "\n".join(out)

    def forget(self, text_marker: str, scope: Scope) -> bool | None:
        """`Memory.get_all` to find the memories whose text holds the marker, then `Memory.delete`."""
        res = self._rpc({"op": "get_all", "filters": {"user_id": scope.user}, "top_k": 100000})
        done = False
        for r in res.get("results", []):
            if text_marker.lower() in (r.get("memory") or "").lower():
                self._rpc({"op": "delete", "id": r["id"]})
                self.writes += 1
                done = True
        return done

    def usage(self) -> dict:
        return {"reads": self.reads, "writes": self.writes}

    def disk_bytes(self) -> int:
        return c.dir_size(self.root / "mem0") if self.root else 0


def make() -> Mem0:
    return Mem0()


def installed() -> bool:
    return Mem0.installed()


def create():
    """The arm for the benchmark driver (`systems.load`)."""
    return make()
