"""Adapter: Graphiti (the open-source engine behind Zep), through its reference MCP server.

Graphiti builds a temporal knowledge graph: each episode is run through a chat model that
extracts entities and facts, which are deduplicated against the graph and embedded. The
project's reference MCP server (`mcp_server/`, installed from a pinned commit with the project's
own lock file) is both the documented ingest path (`add_memory`, which takes the episode's
`reference_time`) and the retrieval path (`search_memory_facts`, `search_nodes`), so this adapter
talks to it over MCP stdio exactly as an agent would.

Parts and where they come from:
  * graph database: the FalkorDB server that ships inside the `falkordblite` wheel
    (`redis-server` plus `falkordb.so`), started on a local port, data under the instance root;
    no container;
  * embeddings: all-MiniLM-L6-v2 through `tools/local_embeddings.py`, because the server can only
    reach an embedder through an OpenAI-compatible endpoint;
  * chat model: `LONGHAUL_LLM_BASE_URL` / `LONGHAUL_LLM_MODEL` / `LONGHAUL_LLM_API_KEY`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Iterable

from . import _common as c
from .base import HostConfig, Scope, SeedReport, Session

NAME = "graphiti"
EMBED_MODEL = "all-MiniLM-L6-v2"
EMBED_DIMS = 384
LOCAL_EMBEDDINGS = Path(__file__).resolve().parents[1] / "tools" / "local_embeddings.py"
CHUNK = 1500  # an episode is one session; long sessions are cut at this many characters per episode


class Graphiti:
    name = NAME
    needs_llm = True
    # delete_episode removes the episode and what only it created; entity summaries that other
    # episodes share keep the text they absorbed (seen with the fake model; expected of real ones).
    forget_is_complete = False

    def __init__(self) -> None:
        self.home = c.install_dir(NAME)
        self.py = self.home / "venv/bin/python"
        self.server_dir = self.home / "src/mcp_server"
        self.version = c.dist_version(self.home / "venv", "graphiti_core")
        self.root: Path | None = None
        self.procs: c.Procs | None = None
        self.client: c.McpStdio | None = None
        self.llm: c.FakeLlm | None = None
        self.llm_cfg: dict[str, str] = {}
        self.db_port = 0
        self.embed_port = 0
        self.added: dict[str, int] = {}
        self.reads = 0
        self.writes = 0

    @staticmethod
    def installed() -> bool:
        h = c.install_dir(NAME)
        return (
            (h / "venv/bin/python").is_file()
            and (h / "src/mcp_server/main.py").is_file()
            and (h / "venv/lib/python3.12/site-packages/redislite/bin/redis-server").is_file()
        )

    # -- configuration -----------------------------------------------------------
    def _config_yaml(self) -> Path:
        """The project's own config.yaml with the embedder pointed at the local embedding endpoint
        (the file expands ${VAR:default}; the embedder and the chat model otherwise share one URL)."""
        assert self.root is not None
        text = (self.server_dir / "config/config.yaml").read_text()
        head, _, tail = text.partition("embedder:")
        tail = tail.replace(
            "api_url: ${OPENAI_API_URL:https://api.openai.com/v1}",
            "api_url: ${EMBEDDER_API_URL}",
            1,
        )
        tail = re.sub(
            r"model: \$\{EMBEDDER_MODEL:[^}]*\}", "model: ${EMBEDDER_MODEL}", tail, count=1
        )
        tail = tail.replace("dimensions: 1536", f"dimensions: {EMBED_DIMS}", 1)
        out = self.root / "config.yaml"
        out.write_text(head + "embedder:" + tail)
        return out

    def _server_env(self) -> dict[str, str]:
        assert self.root is not None
        return {
            "CONFIG_PATH": str(self.root / "config.yaml"),
            "OPENAI_API_KEY": self.llm_cfg.get("api_key") or "sk-not-a-key",
            "OPENAI_API_URL": self.llm_cfg.get("base_url", ""),
            "MODEL_NAME": self.llm_cfg.get("model") or "fake-model",
            "EMBEDDER_API_URL": f"http://127.0.0.1:{self.embed_port}/v1",
            "EMBEDDER_MODEL": EMBED_MODEL,
            "FALKORDB_URI": f"redis://127.0.0.1:{self.db_port}",
            "GRAPHITI_GROUP_ID": "main",
            "GRAPHITI_TELEMETRY_ENABLED": "false",  # graphiti-core's documented telemetry opt-out
            "SEMAPHORE_LIMIT": "4",
            "HF_HUB_DISABLE_XET": "1",
            "HF_HOME": str(self.home / "hf"),
        }

    # -- lifecycle -------------------------------------------------------------
    def prepare(self, root: Path) -> None:
        self.root = Path(root)
        (self.root / "home").mkdir(parents=True, exist_ok=True)
        (self.root / "db").mkdir(exist_ok=True)
        self.procs = c.Procs(self.root)
        self.reads = self.writes = 0
        self.added = {}
        self.llm_cfg = c.llm_env()
        if not self.llm_cfg["base_url"] and os.environ.get("LONGHAUL_FAKE_LLM") == "1":
            self.llm = c.FakeLlm(self.procs, self.root)
            self.llm_cfg = {
                "base_url": self.llm.base_url,
                "model": "fake-model",
                "api_key": "sk-not-a-key",
            }
        env = c.clean_env(
            {"HF_HUB_DISABLE_XET": "1", "HF_HOME": str(self.home / "hf")}, home=self.root / "home"
        )
        # embedded graph database
        self.db_port = c.free_port()
        pkg = self.home / "venv/lib/python3.12/site-packages/redislite/bin"
        self.procs.start(
            [
                str(pkg / "redis-server"),
                "--port",
                str(self.db_port),
                "--bind",
                "127.0.0.1",
                "--loadmodule",
                str(pkg / "falkordb.so"),
                "--dir",
                str(self.root / "db"),
                "--save",
                "5 1",
            ],
            env=env,
            cwd=self.root,
            log=self.root / "falkordb.log",
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            r = subprocess.run(
                [str(pkg / "redis-cli"), "-p", str(self.db_port), "ping"],
                capture_output=True,
                text=True,
            )
            if "PONG" in r.stdout:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("embedded FalkorDB did not start")
        # local embedding model
        port_file = self.root / "embed.port"
        port_file.unlink(missing_ok=True)
        self.procs.start(
            [
                str(self.py),
                "-I",
                str(LOCAL_EMBEDDINGS),
                "--model",
                EMBED_MODEL,
                "--port-file",
                str(port_file),
            ],
            env=env,
            cwd=self.root,
            log=self.root / "embeddings.log",
        )
        deadline = time.monotonic() + 600
        while not (port_file.exists() and port_file.read_text().strip()):
            if time.monotonic() > deadline:
                raise RuntimeError("local embedding server did not start")
            time.sleep(0.3)
        self.embed_port = int(port_file.read_text())
        self._config_yaml()

    def _client(self) -> c.McpStdio:
        if self.client is None:
            assert self.root is not None and self.procs is not None
            env = c.clean_env(self._server_env(), home=self.root / "home")
            self.client = c.McpStdio(
                self.procs,
                [
                    str(self.py),
                    "main.py",
                    "--transport",
                    "stdio",
                    "--config",
                    str(self.root / "config.yaml"),
                ],
                env=env,
                cwd=self.server_dir,
                log=self.root / "mcp_server.log",
                timeout=900,
            )
        return self.client

    def close(self) -> None:
        if self.procs is not None:
            self.procs.stop_all()
        self.client = None

    # -- ingest ----------------------------------------------------------------
    def _call(self, name: str, args: dict):
        text, data, err = self._client().call(name, args)
        if err:
            raise RuntimeError(f"graphiti {name} failed: {text[:300]}")
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        return (
            data
            if data is not None
            else (json.loads(text) if text.startswith(("{", "[")) else text)
        )

    def _episodes(self, group: str, limit: int) -> list:
        res = self._call("get_episodes", {"group_ids": [group], "max_episodes": limit})
        return res.get("episodes", []) if isinstance(res, dict) else []

    def seed(self, sessions: Iterable[Session]) -> SeedReport:
        base = self.llm.stats() if self.llm else None
        started = time.monotonic()
        n = 0
        for s in sessions:
            sc = s.scope
            body = "\n".join(f"{t.role}: {t.text}" for t in s.turns)
            parts = [body[i : i + CHUNK] for i in range(0, len(body), CHUNK)] or [body]
            for k, part in enumerate(parts):
                self._call(
                    "add_memory",
                    {
                        "name": f"session {s.id}" + (f" part {k + 1}" if len(parts) > 1 else ""),
                        "episode_body": part,
                        "group_id": sc.project,
                        "source": "message",
                        "source_description": f"user {sc.user}, agent {sc.agent}, session {s.id}",
                        "reference_time": s.started,
                    },
                )
                self.added[sc.project] = self.added.get(sc.project, 0) + 1
                self.writes += 1
            n += 1
        self._drain()
        calls = tokens = 0
        note = (
            "add_memory per session (group_id=project, reference_time=session start, user/agent/session in the "
            "source description); episodes are processed asynchronously and awaited"
        )
        if self.llm and base is not None:
            now = self.llm.stats()
            calls = now["chat_calls"] - base["chat_calls"]
            tokens = (now["chat_prompt_tokens"] + now["chat_completion_tokens"]) - (
                base["chat_prompt_tokens"] + base["chat_completion_tokens"]
            )
            note += "; counts are the fake server's"
        return SeedReport(
            sessions=n,
            seconds=time.monotonic() - started,
            llm_calls=calls,
            llm_tokens=tokens,
            notes=note,
        )

    def _drain(self, timeout: float = 7200.0) -> None:
        """The server queues episodes and exposes no queue size over MCP, so completion is read
        from its own log: one 'Successfully processed' or 'Failed to process' line per episode."""
        assert self.root is not None
        want = sum(self.added.values())
        deadline = time.monotonic() + timeout
        log = self.root / "mcp_server.log"
        while True:
            text = log.read_text(errors="replace") if log.exists() else ""
            done = len(
                re.findall(r"Successfully processed episode|Failed to process episode", text)
            )
            if done >= want:
                if "Failed to process episode" in text:
                    raise RuntimeError("graphiti failed to process an episode; see " + str(log))
                return
            if time.monotonic() > deadline:
                raise TimeoutError(f"graphiti processed {done} of {want} episodes")
            time.sleep(1.0)

    # -- host ------------------------------------------------------------------
    def host(self, workdir: Path, scope: Scope) -> HostConfig:
        env = self._server_env()
        env["GRAPHITI_GROUP_ID"] = scope.project
        env["OPENAI_API_KEY"] = "${LONGHAUL_LLM_API_KEY}"  # filled from the runner's environment
        rules = (self.server_dir / "docs/cursor_rules.md").read_text()
        return HostConfig(
            mcp={
                "mcpServers": {
                    "graphiti-memory": {
                        "command": str(self.py),
                        "args": [
                            str(self.server_dir / "main.py"),
                            "--transport",
                            "stdio",
                            "--config",
                            str(self.root / "config.yaml"),
                        ],
                        "env": env,
                    }
                }
            },
            settings={},
            # The project's own agent rules (written for Cursor) are the only instruction text its
            # docs give; a Claude Code user would put them in CLAUDE.md.
            files={"CLAUDE.md": rules},
            allowed_tools=["mcp__graphiti-memory"],
            env={},
            prompt_note="",
        )

    # -- retrieval -------------------------------------------------------------
    def context_for(self, query: str, scope: Scope, budget_chars: int) -> str:
        facts = self._call(
            "search_memory_facts", {"query": query, "group_ids": [scope.project], "max_facts": 10}
        )
        nodes = self._call(
            "search_nodes", {"query": query, "group_ids": [scope.project], "max_nodes": 5}
        )
        self.reads += 2
        out = []
        for f in (facts or {}).get("facts", []) if isinstance(facts, dict) else []:
            out.append(f"- {f.get('fact', '')} (valid {f.get('valid_at')} - {f.get('invalid_at')})")
        for nd in (nodes or {}).get("nodes", []) if isinstance(nodes, dict) else []:
            out.append(f"[{nd.get('name')}] {nd.get('summary', '')}")
        return c.clip("\n".join(out), budget_chars)

    def forget(self, text_marker: str, scope: Scope) -> bool | None:
        """`delete_episode` (cascades to what the episode alone created) for episodes whose body
        holds the marker, and `delete_entity_edge` for facts that quote it."""
        group = scope.project
        done = False
        eps = self._episodes(group, max(10, self.added.get(group, 0) + 50))
        for e in eps:
            if text_marker.lower() in json.dumps(e).lower():
                self._call("delete_episode", {"uuid": e["uuid"], "group_id": group})
                self.writes += 1
                done = True
        facts = self._call(
            "search_memory_facts", {"query": text_marker, "group_ids": [group], "max_facts": 50}
        )
        for f in (facts or {}).get("facts", []) if isinstance(facts, dict) else []:
            if text_marker.lower() in (f.get("fact") or "").lower():
                self._call("delete_entity_edge", {"uuid": f["uuid"], "group_id": group})
                self.writes += 1
                done = True
        return done

    def usage(self) -> dict:
        return {"reads": self.reads, "writes": self.writes}

    def disk_bytes(self) -> int:
        return c.dir_size(self.root / "db") if self.root else 0


def make() -> Graphiti:
    return Graphiti()


def installed() -> bool:
    return Graphiti.installed()


def create():
    """The arm for the benchmark driver (`systems.load`)."""
    return make()
