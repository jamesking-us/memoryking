"""Adapter: MCP Memory Service (`mcp-memory-service`, doobidoo), added to the list as a widely used
local memory server for coding agents.

SQLite with sqlite-vec, a local ONNX embedding model, no chat model needed for storing or
searching. Seeding uses its own session tool, `memory_store_session(turns, session_id, tags,
metadata, agent_id)`, over MCP stdio; search is `memory_search`; deletion is `memory_delete` by
content hash. Its `claude-hooks/README.md` recommends installing it as a Claude Code plugin
(`claude-hooks/PLUGIN.md`: an MCP server entry plus SessionStart, SessionEnd, UserPromptSubmit and
Stop hooks that talk to the service's HTTP API), so the default variant (`mcp-memory-service`)
returns that; `mcp-memory-service-mcp` is the MCP server alone (the main README's one-line quick
start).
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Iterable

from . import _common as c
from .base import HostConfig, Scope, SeedReport, Session

NAME = "mcp-memory-service"


class McpMemoryService:
    needs_llm = False

    def __init__(self, plugin: bool = True) -> None:
        self.plugin = plugin
        self.name = NAME if plugin else NAME + "-mcp"
        self.http_port = 0
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

    @staticmethod
    def hooks_source() -> Path:
        return c.install_dir(NAME) / "src/claude-hooks"

    @staticmethod
    def plugin_available() -> bool:
        return (McpMemoryService.hooks_source() / ".claude-plugin/hooks.json").is_file() and bool(
            shutil.which("node")
        )

    def _vars(self) -> dict[str, str]:
        assert self.root is not None
        return {
            "MCP_MEMORY_BASE_DIR": str(self.root / "base"),
            "MCP_MEMORY_SQLITE_PATH": str(self.root / "memory.db"),
            "MCP_MEMORY_STORAGE_BACKEND": "sqlite_vec",
            "HF_HUB_DISABLE_XET": "1",  # harness fix: the xet transport does not pass this proxy
            "HF_HOME": str(self.home / "hf"),  # model files cached once under the install directory
            # The default embedding model is cached under `~/.cache/mcp_memory`, whatever the
            # settings; the server's HOME is the install directory's, so a session's fresh HOME
            # does not download the 90 MB model again.
            "HOME": str(self.home / "home"),
        }

    def prepare(self, root: Path) -> None:
        self.root = Path(root)
        (self.home / "home").mkdir(parents=True, exist_ok=True)
        self.procs = c.Procs(self.root)
        self.reads = self.writes = 0
        if self.plugin and self.plugin_available():
            self._start_plugin_side()

    def _start_plugin_side(self) -> None:
        """What the plugin's SessionStart hook (`ensure-server.js`) would otherwise start: the HTTP
        server the hooks talk to, on this instance's database; and a copy of the hook package whose
        bundled config.json (the fallback the hooks use when `~/.claude/hooks/config.json` does
        not exist) names this instance's endpoint."""
        assert self.root is not None and self.procs is not None
        self.http_port = c.free_port()
        # The README's local-use form is `MCP_ALLOW_ANONYMOUS_ACCESS=true memory server --http`
        # (loopback only); the hooks' `apiKey: auto-detect` placeholder is not a key.
        env = c.clean_env(
            {**self._vars(), "MCP_ALLOW_ANONYMOUS_ACCESS": "true"}, home=self.home / "home"
        )
        self.procs.start(
            [
                str(self.bin),
                "server",
                "--http",
                "--http-host",
                "127.0.0.1",
                "--http-port",
                str(self.http_port),
            ],
            env=env,
            cwd=self.root,
            log=self.root / "http.log",
        )
        deadline = time.monotonic() + 300
        while True:
            try:
                c.http_json(f"http://127.0.0.1:{self.http_port}/api/health", timeout=3)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"memory HTTP server did not start; see {self.root}/http.log"
                    ) from None
                time.sleep(0.5)
        dest = self.root / "plugin"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(
            self.hooks_source(),
            dest,
            ignore=shutil.ignore_patterns("tests", "test-*.js", "debug-*.js", "*.ps1"),
        )
        cfg = json.loads((dest / "config.json").read_text())
        cfg["memoryService"]["http"]["endpoint"] = f"http://127.0.0.1:{self.http_port}"
        (dest / "config.json").write_text(json.dumps(cfg, indent=2))

    def _client(self) -> c.McpStdio:
        if self.client is None:
            assert self.root is not None and self.procs is not None
            env = c.clean_env(self._vars(), home=self.home / "home")
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
    def _hooks_settings(self) -> dict:
        """`.claude-plugin/hooks.json` with the plugin root filled in and `node` absolute."""
        assert self.root is not None
        node = shutil.which("node") or "node"
        text = (self.hooks_source() / ".claude-plugin/hooks.json").read_text()
        text = text.replace("${CLAUDE_PLUGIN_ROOT}", str(self.root / "plugin")).replace(
            '"node ', f'"{node} '
        )
        return json.loads(text)

    def host(self, workdir: Path, scope: Scope) -> HostConfig:
        env = self._vars()
        if not (self.plugin and self.plugin_available() and self.http_port):
            return HostConfig(
                mcp={
                    "mcpServers": {
                        "memory": {"command": str(self.bin), "args": ["server"], "env": env}
                    }
                },
                settings={},
                files={},
                allowed_tools=["mcp__memory"],
                env={},
                prompt_note="",
            )
        # The plugin's .mcp.json runs `python -m mcp_memory_service.server` with the hybrid
        # backend, which syncs to Cloudflare (an account); the local sqlite-vec backend is used
        # instead, on the same database the HTTP server and the hooks use.
        py = self.home / "venv/bin/python"
        skill = (self.hooks_source() / "skills/remember-session/SKILL.md").read_text()
        return HostConfig(
            mcp={
                "mcpServers": {
                    "memory": {
                        "command": str(py),
                        "args": ["-m", "mcp_memory_service.server"],
                        "env": env,
                    }
                }
            },
            settings=self._hooks_settings(),
            files={".claude/skills/remember-session/SKILL.md": skill},
            allowed_tools=["mcp__memory", "Skill"],
            # ensure-server.js only probes this endpoint; the adapter keeps the server up.
            env={
                **env,
                "MCP_MEMORY_ENDPOINT": f"http://127.0.0.1:{self.http_port}",
                "ENSURE_SERVER_NO_SPAWN": "1",
            },
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


def make(plugin: bool = True) -> McpMemoryService:
    return McpMemoryService(plugin)


def make_mcp_only() -> McpMemoryService:
    return McpMemoryService(plugin=False)


def installed() -> bool:
    return McpMemoryService.installed()


def create():
    """The arm for the benchmark driver (`systems.load`)."""
    return make()
