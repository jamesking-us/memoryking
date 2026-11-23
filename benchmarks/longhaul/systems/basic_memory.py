"""Adapter: Basic Memory (`basic-memory`), Markdown notes exposed over MCP.

No model of its own. Notes are plain Markdown files in a project directory; a local SQLite
index (full text plus FastEmbed vectors, `bge-small-en-v1.5`) makes them searchable. Seeding
uses its documented `bm_write` MCP tool, one note per session in the project's own note format
(frontmatter, `- [category] observation` lines, `[[wiki]]` relations). Embeddings are built by
the documented `basic-memory reindex` command after the seed.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Iterable

from . import _common as c
from .base import HostConfig, Scope, SeedReport, Session

NAME = "basic-memory"
PROJECT = "main"  # the project a fresh install creates; the quick-start never makes another
OBS_CAP = 1500


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


class BasicMemory:
    """`plugin=True` (the default, name `basic-memory`) is the MCP server plus the Claude Code plugin
    the README tells a Claude Code user to install; `plugin=False` (name `basic-memory-mcp`) is
    the MCP server alone."""

    needs_llm = False
    obs_cap: int | None = OBS_CAP  # None keeps every turn whole (track T5)
    # A real session does not give this product what seeding writes (SYSTEMS.md): it is
    # measured on the lived track only, and seeded runs need --allow-unfaithful-seeding.
    seeding_faithful = False

    def __init__(self, plugin: bool = True) -> None:
        self.plugin = plugin
        self.name = NAME if plugin else NAME + "-mcp"
        self.home = c.install_dir(NAME)
        self.bin = self.home / "venv/bin/basic-memory"
        self.version = c.dist_version(self.home / "venv", "basic_memory")
        self.root: Path | None = None
        self.procs: c.Procs | None = None
        self.client: c.McpStdio | None = None
        self.reads = 0
        self.writes = 0

    @staticmethod
    def installed() -> bool:
        return (c.install_dir(NAME) / "venv/bin/basic-memory").is_file()

    @staticmethod
    def plugin_dir() -> Path:
        return c.install_dir(NAME) / "src/plugins/claude-code"

    @staticmethod
    def plugin_available() -> bool:
        return (BasicMemory.plugin_dir() / "hooks/hooks.json").is_file() and bool(
            shutil.which("uv")
        )

    # -- lifecycle -------------------------------------------------------------
    def _env_vars(self) -> dict[str, str]:
        assert self.root is not None
        return {
            "BASIC_MEMORY_CONFIG_DIR": str(self.root / "cfg"),
            "BASIC_MEMORY_HOME": str(self.root / "notes"),
            "BASIC_MEMORY_NO_PROMOS": "1",  # documented opt-out of promos and telemetry
            # Harness fixes, not product settings: model files are cached once under the install
            # directory, and the Hugging Face xet transport does not pass this machine's proxy.
            "BASIC_MEMORY_SEMANTIC_EMBEDDING_CACHE_DIR": str(self.home / "models"),
            "HF_HUB_DISABLE_XET": "1",
            "HOME": str(self.root / "home"),
        }

    def _env(self) -> dict[str, str]:
        assert self.root is not None
        return c.clean_env(self._env_vars(), home=self.root / "home")

    def _cli(self, *args: str, timeout: float = 900) -> str:
        r = subprocess.run(
            [str(self.bin), *args], env=self._env(), capture_output=True, text=True, timeout=timeout
        )
        if r.returncode:
            raise RuntimeError(
                f"basic-memory {' '.join(args)}: {r.stderr[-400:] or r.stdout[-400:]}"
            )
        return r.stdout

    @property
    def notes(self) -> Path:
        assert self.root is not None
        return self.root / "notes"

    def prepare(self, root: Path) -> None:
        self.root = Path(root)
        for d in ("home", "cfg", "notes"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.procs = c.Procs(self.root)
        self.reads = self.writes = 0
        self._rebase()
        # The README documents `auto_update` as the one switch that keeps the version still.
        self._cli("config", "set", "auto_update", "false")

    def _rebase(self) -> None:
        """A seeded instance is copied to a new directory for each run. The product records the
        absolute path of its project in `config.json` and in the `project` table of its database;
        point both at this copy, so a run never reads or writes the seeded original."""
        assert self.root is not None
        cfg = self.root / "cfg" / "config.json"
        if not cfg.is_file():
            return
        data = json.loads(cfg.read_text())
        old = data.get("projects", {}).get(PROJECT, {}).get("path")
        new = str(self.root / "notes")
        if not old or old == new:
            return
        data["projects"][PROJECT]["path"] = new
        cfg.write_text(json.dumps(data, indent=2))
        db = self.root / "cfg" / "memory.db"
        if db.is_file():
            con = sqlite3.connect(db)
            try:
                con.execute("update project set path = ? where path = ?", (new, old))
                con.commit()
            finally:
                con.close()

    def _client(self) -> c.McpStdio:
        if self.client is None:
            assert self.root is not None and self.procs is not None
            self.client = c.McpStdio(
                self.procs,
                [str(self.bin), "mcp"],
                env=self._env(),
                cwd=self.root,
                log=self.root / "server.log",
                timeout=600,
            )
        return self.client

    def _stop_client(self) -> None:
        if self.procs is not None:
            self.procs.stop_all()
        self.client = None

    def close(self) -> None:
        self._stop_client()

    # -- ingest ----------------------------------------------------------------
    @staticmethod
    def render(s: Session, obs_cap: int | None = OBS_CAP) -> str:
        sc = s.scope
        title = f"Session {s.id}"
        lines = [
            "---",
            f"title: {title}",
            "type: session",
            f"tags: [session, {_slug(sc.project)}]",
            f"user: {sc.user}",
            f"project: {sc.project}",
            f"agent: {sc.agent}",
            f"started: '{s.started}'",
            "---",
            "",
            f"# {title}",
            "",
            f"Session of project {sc.project} by {sc.user} (agent {sc.agent}), started {s.started}.",
            "",
            "## Observations",
        ]
        for t in s.turns:
            text = " ".join((c.clip(t.text, obs_cap) if obs_cap else t.text).split())
            lines.append(f"- [{t.role}] {text}")
        lines += ["", "## Relations", f"- part_of [[Project {sc.project}]]"]
        return "\n".join(lines) + "\n"

    def path_for(self, s: Session) -> str:
        return f"{_slug(s.scope.project)}/{s.started[:10]}-{_slug(s.id)}.md"

    def seed(self, sessions: Iterable[Session]) -> SeedReport:
        cl = self._client()
        started = time.monotonic()
        n = 0
        for s in sessions:
            _, _, err = cl.call(
                "bm_write",
                {"identifier": self.path_for(s), "content": self.render(s, self.obs_cap), "project": PROJECT},
            )
            if err:
                raise RuntimeError(f"bm_write failed for {s.id}")
            self.writes += 1
            n += 1
        # Stop the server first: only one process should write the SQLite index, then build the
        # vector embeddings with the documented maintenance command.
        self._stop_client()
        self._cli("reindex")
        return SeedReport(
            sessions=n,
            seconds=time.monotonic() - started,
            llm_calls=0,
            llm_tokens=0,
            notes="one Markdown note per session via bm_write; local FastEmbed embeddings via `reindex`; "
            "no chat model",
        )

    # -- host ------------------------------------------------------------------
    def _hooks_settings(self) -> dict:
        """The plugin's own hooks.json: `${CLAUDE_PLUGIN_ROOT}` filled in and `uv` given as an
        absolute path. The hooks are PEP 723 scripts run with `uv run --script`."""
        plugin = self.plugin_dir()
        uv = shutil.which("uv") or "uv"
        text = (plugin / "hooks/hooks.json").read_text()
        text = text.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin)).replace('"uv run ', f'"{uv} run ')
        return {"hooks": json.loads(text)["hooks"]}

    def _plugin_files(self) -> dict[str, str]:
        plugin = self.plugin_dir()
        files: dict[str, str] = {}
        # The plugin's skills (a plugin's skills are namespaced `basic-memory:`; as project
        # skills they carry their own names). The `basic-memory` output style is documented as
        # opt-in and is not enabled.
        for f in sorted((plugin / "skills").rglob("*")):
            if f.is_file():
                files[f".claude/skills/{f.relative_to(plugin / 'skills').as_posix()}"] = (
                    f.read_text()
                )
        # Where the hooks read their configuration: a `basicMemory` block in project settings.
        files[".claude/settings.json"] = json.dumps(
            {"basicMemory": {"primaryProject": PROJECT, "captureFolder": "sessions"}}, indent=2
        )
        return files

    def host(self, workdir: Path, scope: Scope) -> HostConfig:
        env = self._env_vars()
        mcp = {
            "mcpServers": {"basic-memory": {"command": str(self.bin), "args": ["mcp"], "env": env}}
        }
        if not (self.plugin and self.plugin_available()):
            return HostConfig(
                mcp=mcp,
                settings={},
                files={},
                allowed_tools=["mcp__basic-memory"],
                env={},
                prompt_note="",
            )
        return HostConfig(
            mcp=mcp,
            settings=self._hooks_settings(),
            files=self._plugin_files(),
            allowed_tools=["mcp__basic-memory", "Skill"],
            # BM_BIN is the plugin's documented override: the hooks run the pinned binary against
            # this instance instead of resolving basic-memory again; UV_CACHE_DIR keeps uv's cache
            # out of the home directory.
            env={**env, "BM_BIN": str(self.bin), "UV_CACHE_DIR": str(self.home / "uvcache")},
            prompt_note="",
        )

    # -- retrieval -------------------------------------------------------------
    def context_for(self, query: str, scope: Scope, budget_chars: int) -> str:
        cl = self._client()
        text, data, err = cl.call(
            "bm_search",
            {"query": query, "project": PROJECT, "page_size": 10, "output_format": "json"},
        )
        self.reads += 1
        if err:
            return ""
        results = []
        try:
            results = json.loads(text).get("results", [])
        except ValueError:
            if data and isinstance(data.get("results"), list):
                results = data["results"]
        out, used = [], 0
        for r in results:
            body = r.get("matched_chunk") or r.get("content") or ""
            block = f"[{r.get('file_path') or r.get('title')}]\n{body}"
            if used + len(block) > budget_chars:
                block = c.clip(block, max(0, budget_chars - used))
                if block:
                    out.append(block)
                break
            out.append(block)
            used += len(block) + 1
        return "\n".join(out)

    def forget(self, text_marker: str, scope: Scope) -> bool | None:
        """Documented delete path: find the notes holding the text, rewrite each without the
        matching observation lines (`bm_write` with `overwrite`); a note left with no
        observations is deleted with `bm_delete`."""
        cl = self._client()
        done = False
        for md in sorted(self.notes.rglob("*.md")):
            body = md.read_text()
            if text_marker.lower() not in body.lower():
                continue
            rel = md.relative_to(self.notes).as_posix()
            kept = [ln for ln in body.splitlines() if text_marker.lower() not in ln.lower()]
            has_obs = any(ln.startswith("- [") and not ln.startswith("- part_of") for ln in kept)
            if has_obs:
                cl.call(
                    "bm_write",
                    {
                        "identifier": rel,
                        "content": "\n".join(kept) + "\n",
                        "project": PROJECT,
                        "overwrite": True,
                    },
                )
            else:
                cl.call("bm_delete", {"identifier": rel, "project": PROJECT})
            self.writes += 1
            done = True
        if done:
            self._stop_client()
            self._cli("reindex")  # refresh the vector index so the removed text is not returned
        return done

    def usage(self) -> dict:
        return {"reads": self.reads, "writes": self.writes}

    def disk_bytes(self) -> int:
        return c.dir_size(self.root) if self.root else 0


def make(plugin: bool = True) -> BasicMemory:
    return BasicMemory(plugin)


def make_mcp_only() -> BasicMemory:
    return BasicMemory(plugin=False)


def installed() -> bool:
    return BasicMemory.installed()


def create():
    """The arm for the benchmark driver (`systems.load`)."""
    return make()
