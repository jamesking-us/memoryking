"""Adapter: MCP Memory Service (`mcp-memory-service`, doobidoo), added to the list as a widely used
local memory server for coding agents.

SQLite with sqlite-vec, a local ONNX embedding model, no chat model needed for storing or
searching. Seeding uses its own session tool, `memory_store_session(turns, session_id, tags,
metadata, agent_id)`, over MCP stdio; search is `memory_search`; deletion is `memory_delete` by
content hash. Its Claude Code quick start is one command, `claude mcp add memory -- memory
server`; the optional session hooks (`claude-hooks/`, installed from a repository checkout) are not
part of that quick start and are not included here; SYSTEMS.md says so.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Iterable

from . import _common as c
from ._types import HostConfig, Scope, SeedReport, Session

NAME = "mcp-memory-service"


class McpMemoryService:
    name = NAME
    needs_llm = False

    def __init__(self) -> None:
        self.home = c.install_dir(NAME)
        self.bin = self.home / "venv/bin/memory"
        self.version = c.dist_version(self.home / "venv", "mcp_memory_service")
        self.root: Path | None = None
        self.procs: c.Procs | None = None
        self.client: c.McpStdio | None = None
        self.reads = 0
        self.writes = 0

    @staticmethod
    def installed() -> bool:
        return (c.install_dir(NAME) / "venv/bin/memory").is_file()

    def _vars(self) -> dict[str, str]:
        assert self.root is not None
        return {
            "MCP_MEMORY_BASE_DIR": str(self.root / "base"),
            "MCP_MEMORY_SQLITE_PATH": str(self.root / "memory.db"),
            "MCP_MEMORY_STORAGE_BACKEND": "sqlite_vec",
            "HF_HUB_DISABLE_XET": "1",  # harness fix: the xet transport does not pass this proxy
            "HF_HOME": str(self.home / "hf"),  # model files cached once under the install directory
        }

    def prepare(self, root: Path) -> None:
        self.root = Path(root)
        (self.root / "home").mkdir(parents=True, exist_ok=True)
        self.procs = c.Procs(self.root)
        self.reads = self.writes = 0

    def _client(self) -> c.McpStdio:
        if self.client is None:
            assert self.root is not None and self.procs is not None
            env = c.clean_env(self._vars(), home=self.root / "home")
            self.client = c.McpStdio(
                self.procs,
                [str(self.bin), "server"],
                env=env,
                cwd=self.root,
                log=self.root / "server.log",
                timeout=900,
            )
        return self.client

    def close(self) -> None:
        if self.procs is not None:
            self.procs.stop_all()
        self.client = None

    # -- ingest ----------------------------------------------------------------
    def seed(self, sessions: Iterable[Session]) -> SeedReport:
        cl = self._client()
        started = time.monotonic()
        n = 0
        for s in sessions:
            sc = s.scope
            text, _, err = cl.call(
                "memory_store_session",
                {
                    "session_id": s.id,
                    "turns": [
                        {"role": "assistant" if t.role == "tool" else t.role, "content": t.text}
                        for t in s.turns
                    ],
                    "tags": [f"project:{sc.project}", f"user:{sc.user}"],
                    "metadata": {"started": s.started, "project": sc.project, "user": sc.user},
                    "agent_id": sc.agent,
                },
            )
            if err or "stored successfully" not in text:
                raise RuntimeError(f"memory_store_session failed for {s.id}: {text[:200]}")
            self.writes += 1
            n += 1
        return SeedReport(
            sessions=n,
            seconds=time.monotonic() - started,
            llm_calls=0,
            llm_tokens=0,
            notes="memory_store_session per session (tags project:/user:, agent_id, session id; start time in "
            "metadata only); local ONNX embeddings; no chat model",
        )

    # -- host ------------------------------------------------------------------
    def host(self, workdir: Path, scope: Scope) -> HostConfig:
        return HostConfig(
            mcp={
                "mcpServers": {
                    "memory": {"command": str(self.bin), "args": ["server"], "env": self._vars()}
                }
            },
            settings={},
            files={},
            allowed_tools=["mcp__memory"],
            env={},
            prompt_note="",
        )

    # -- retrieval -------------------------------------------------------------
    def context_for(self, query: str, scope: Scope, budget_chars: int) -> str:
        text, _, err = self._client().call(
            "memory_search",
            {"query": query, "tags": [f"project:{scope.project}"], "tag_match": "any"},
        )
        self.reads += 1
        return "" if err else c.clip(text, budget_chars)

    def forget(self, text_marker: str, scope: Scope) -> bool | None:
        cl = self._client()
        text, _, err = cl.call("memory_search", {"query": text_marker, "mode": "exact"})
        self.reads += 1
        if err:
            return False
        done = False
        # results are numbered blocks ending in "Hash: <sha256>"; delete the ones holding the text
        for block in re.split(r"\n(?=\d+\. )", text):
            m = re.search(r"Hash:\s*([0-9a-f]{64})", block)
            if m and text_marker.lower() in block.lower():
                cl.call("memory_delete", {"content_hash": m.group(1)})
                self.writes += 1
                done = True
        return done

    def usage(self) -> dict:
        return {"reads": self.reads, "writes": self.writes}

    def disk_bytes(self) -> int:
        return c.dir_size(self.root) if self.root else 0


def make() -> McpMemoryService:
    return McpMemoryService()


def installed() -> bool:
    return McpMemoryService.installed()
