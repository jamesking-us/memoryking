"""Adapter: the reference MCP memory server (`@modelcontextprotocol/server-memory`).

A knowledge graph (entities, relations, observations) in one JSONL file, nine MCP tools, no
model of its own. The product has no ingest path of its own other than its tools, because its
design is that the agent decides what to write. Seeding a recorded history therefore needs a
fixed rule, stated here and in SYSTEMS.md: one entity per session holding every turn as one
observation, linked to project, user and agent entities. See `SYSTEMS.md` for what that
mapping cannot express.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable

from . import _common as c
from .base import HostConfig, Scope, SeedReport, Session

NAME = "mcp-memory"
OBS_CAP = 1500  # characters kept per turn observation
# The system prompt the project's README suggests for the knowledge graph; the only
# instruction text its documentation tells a user to add. Verbatim, with the user name kept.
README_PROMPT = """Follow these steps for each interaction:

1. User Identification:
   - You should assume that you are interacting with default_user
   - If you have not identified default_user, proactively try to do so.

2. Memory Retrieval:
   - Always begin your chat by saying only "Remembering..." and retrieve all relevant information from your knowledge graph
   - Always refer to your knowledge graph as your "memory"

3. Memory
   - While conversing with the user, be attentive to any new information that falls into these categories:
     a) Basic Identity (age, gender, location, job title, education level, etc.)
     b) Behaviors (interests, habits, etc.)
     c) Preferences (communication style, preferred language, etc.)
     d) Goals (goals, targets, aspirations, etc.)
     e) Relationships (personal and professional relationships up to 3 degrees of separation)

4. Memory Update:
   - If any new information was gathered during the interaction, update your memory as follows:
     a) Create entities for recurring organizations, people, and significant events
     b) Connect them to the current entities using relations
     c) Store facts about them as observations
"""


class McpMemory:
    name = NAME
    needs_llm = False

    def __init__(self) -> None:
        self.home = c.install_dir(NAME)
        self.pkg = self.home / "prefix/node_modules/@modelcontextprotocol/server-memory"
        self.node = os.environ.get("LONGHAUL_NODE24", "/opt/node24/bin/node")
        self.version = "unknown"
        if (self.pkg / "package.json").is_file():
            self.version = json.loads((self.pkg / "package.json").read_text())["version"]
        self.root: Path | None = None
        self.procs: c.Procs | None = None
        self.client: c.McpStdio | None = None
        self.reads = 0
        self.writes = 0

    @staticmethod
    def installed() -> bool:
        return (
            c.install_dir(NAME)
            / "prefix/node_modules/@modelcontextprotocol/server-memory/dist/index.js"
        ).is_file() and Path(os.environ.get("LONGHAUL_NODE24", "/opt/node24/bin/node")).is_file()

    # -- lifecycle -------------------------------------------------------------
    @property
    def graph(self) -> Path:
        assert self.root is not None
        return self.root / "memory.jsonl"

    def _env(self) -> dict[str, str]:
        assert self.root is not None
        return c.clean_env({"MEMORY_FILE_PATH": str(self.graph)}, home=self.root / "home")

    def _command(self) -> list[str]:
        return [self.node, str(self.pkg / "dist/index.js")]

    def prepare(self, root: Path) -> None:
        self.root = Path(root)
        (self.root / "home").mkdir(parents=True, exist_ok=True)
        self.procs = c.Procs(self.root)
        self.reads = self.writes = 0

    def _client(self) -> c.McpStdio:
        if self.client is None:
            assert self.root is not None and self.procs is not None
            self.client = c.McpStdio(
                self.procs,
                self._command(),
                env=self._env(),
                cwd=self.root,
                log=self.root / "server.log",
            )
        return self.client

    def _stop_client(self) -> None:
        if self.procs is not None:
            self.procs.stop_all()
        self.client = None

    def close(self) -> None:
        self._stop_client()

    # -- ingest ----------------------------------------------------------------
    def seed(self, sessions: Iterable[Session]) -> SeedReport:
        cl = self._client()
        started = time.monotonic()
        n = 0
        known: set[str] = set()

        def entity(name: str, kind: str, obs: list[str]) -> dict:
            return {"name": name, "entityType": kind, "observations": obs}

        for s in sessions:
            sc = s.scope
            ents, rels = [], []
            for nm, kind in (
                (f"project:{sc.project}", "project"),
                (f"user:{sc.user}", "user"),
                (f"agent:{sc.agent}", "agent"),
            ):
                if nm not in known:
                    known.add(nm)
                    ents.append(entity(nm, kind, []))
            sname = f"session:{s.id}"
            obs = [f"started {s.started}; user {sc.user}; project {sc.project}; agent {sc.agent}"]
            obs += [f"{t.role}: {c.clip(t.text, OBS_CAP)}" for t in s.turns]
            ents.append(entity(sname, "session", obs))
            for to, rel in (
                (f"project:{sc.project}", "part_of"),
                (f"user:{sc.user}", "held_by"),
                (f"agent:{sc.agent}", "run_by"),
            ):
                rels.append({"from": sname, "to": to, "relationType": rel})
            cl.call("create_entities", {"entities": ents})
            cl.call("create_relations", {"relations": rels})
            self.writes += 2
            n += 1
        return SeedReport(
            sessions=n,
            seconds=time.monotonic() - started,
            llm_calls=0,
            llm_tokens=0,
            notes="one entity per session (turns as observations), relations to project/user/agent entities; "
            "no model involved",
        )

    # -- host ------------------------------------------------------------------
    def host(self, workdir: Path, scope: Scope) -> HostConfig:
        return HostConfig(
            mcp={
                "mcpServers": {
                    "memory": {
                        "command": self.node,
                        "args": self._command()[1:],
                        "env": {"MEMORY_FILE_PATH": str(self.graph)},
                    }
                }
            },
            settings={},
            files={"CLAUDE.md": README_PROMPT},
            allowed_tools=["mcp__memory"],
            env={},
            prompt_note="",
        )

    # -- retrieval -------------------------------------------------------------
    def context_for(self, query: str, scope: Scope, budget_chars: int) -> str:
        """search_nodes matches one literal substring, so a sentence finds nothing. The query
        is split into its content words and each is searched (what an agent calling the tool
        would do); entities are ranked by how many distinct words they matched."""
        cl = self._client()
        words = c.keywords(query)
        hits: dict[str, dict] = {}
        for w in words:
            _, data, err = cl.call("search_nodes", {"query": w})
            self.reads += 1
            if err or not data:
                continue
            for e in data.get("entities", []):
                h = hits.setdefault(e["name"], {"entity": e, "words": set()})
                h["words"].add(w)
        ranked = sorted(hits.values(), key=lambda h: -len(h["words"]))
        out, used = [], 0
        for h in ranked:
            e = h["entity"]
            lines = [
                o for o in e.get("observations", []) if any(w in o.lower() for w in h["words"])
            ] or e.get("observations", [])[:3]
            block = f"[{e['name']}]\n" + "\n".join(f"- {o}" for o in lines)
            if used + len(block) > budget_chars:
                break
            out.append(block)
            used += len(block) + 1
        return "\n".join(out)

    def forget(self, text_marker: str, scope: Scope) -> bool | None:
        cl = self._client()
        _, data, err = cl.call("search_nodes", {"query": text_marker})
        self.reads += 1
        if err or not data:
            return False
        deletions = []
        for e in data.get("entities", []):
            gone = [o for o in e.get("observations", []) if text_marker.lower() in o.lower()]
            if gone:
                deletions.append({"entityName": e["name"], "observations": gone})
        if not deletions:
            return False
        cl.call("delete_observations", {"deletions": deletions})
        self.writes += 1
        return True

    def usage(self) -> dict:
        return {"reads": self.reads, "writes": self.writes}

    def disk_bytes(self) -> int:
        return self.graph.stat().st_size if self.graph.exists() else 0


def make() -> McpMemory:
    return McpMemory()


def installed() -> bool:
    return McpMemory.installed()


def create():
    """The arm for the benchmark driver (`systems.load`)."""
    return make()
