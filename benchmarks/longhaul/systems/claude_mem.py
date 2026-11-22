"""Adapter: claude-mem (npm `claude-mem`), a Claude Code memory plugin.

claude-mem has no ingest API of its own for transcripts. Its documented capture path is the
Claude Code hook events: `UserPromptSubmit` (session-init), `PostToolUse` (observation) and `Stop`
(summarize), each run as `claude-mem hook claude-code <event>` with the hook's JSON on stdin. A
local worker (Bun, SQLite, Chroma) queues them and an *observer* turns each tool event and each
summary into a stored record by calling a model. By default that model is the user's own Claude
Code login (provider `claude`, model `haiku`), which is a real, billed call; this adapter refuses
to use it unless `LONGHAUL_ALLOW_HOST_AGENT=1`. With `LONGHAUL_LLM_BASE_URL` set it uses the
documented `openai-compatible` provider instead, which is how the offline tests run.

Seeding replays a recorded session as the hook events a real Claude Code session fires, with
the host's own payloads (`_session.py`): `UserPromptSubmit` for each user turn, `PostToolUse` or
`PostToolUseFailure` for each tool turn, one `Stop` per user turn with the last assistant message
of that turn, `SessionEnd`, and the transcript file written as the host would have it. What the
observer is shown, read from the plugin's own code: the first user prompt of a session, each tool
call and result, and at Stop the last assistant message (the summary step does not read the
transcript unless the Stop payload has no `last_assistant_message`). Nothing in the hook payloads
carries a timestamp, so the store's times are ingestion times (order is preserved).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Iterable

from . import _common as c
from . import _session
from .base import HostConfig, Scope, SeedReport, Session

NAME = "claude-mem"
HOOK_TIMEOUT = 120
# The plugin's own hooks.json: the claude-mem subcommand each host event runs.
HOOK_COMMAND = {
    "UserPromptSubmit": "session-init",
    "PostToolUse": "observation",
    "PostToolUseFailure": "observation",
    "Stop": "summarize",
    "SessionEnd": "session-end",
}


class ClaudeMem:
    name = NAME
    # The default observer is the Claude Code login of whoever runs the benchmark, not an
    # endpoint of the product's own: see `uses_host_agent_model`.
    needs_llm = False
    uses_host_agent_model = True

    def __init__(self) -> None:
        self.home = c.install_dir(NAME)
        self.pkg = self.home / "prefix/node_modules/claude-mem"
        self.user_home = self.home / "home"
        self.launcher = self.user_home / ".local/bin/claude-mem"
        self.version = "unknown"
        if (self.pkg / "package.json").is_file():
            self.version = json.loads((self.pkg / "package.json").read_text())["version"]
        self.root: Path | None = None
        self.procs: c.Procs | None = None
        self.port = 0
        self.llm: c.FakeLlm | None = None
        self.llm_cfg: dict[str, str] = {}
        self.reads = 0
        self.writes = 0

    @staticmethod
    def installed() -> bool:
        h = c.install_dir(NAME)
        return (h / "home/.local/bin/claude-mem").is_file() and (
            h / "home/.claude/plugins/cache/thedotmack/claude-mem"
        ).is_dir()

    # -- environment -----------------------------------------------------------
    @property
    def data(self) -> Path:
        assert self.root is not None
        return self.root / "data"

    def _plugin_dir(self) -> Path:
        base = self.user_home / ".claude/plugins/cache/thedotmack/claude-mem"
        return sorted(p for p in base.iterdir() if p.is_dir())[-1]

    def _env_vars(self) -> dict[str, str]:
        env = {
            "CLAUDE_MEM_DATA_DIR": str(self.data),
            "CLAUDE_MEM_WORKER_PORT": str(self.port),
            "CLAUDE_MEM_ONLINE_OPTIN": "false",  # documented: no claude-mem sign-in or account
            "CLAUDE_MEM_AUTO_UPDATE": "false",
        }
        if self.llm_cfg.get("base_url"):
            env.update(
                CLAUDE_MEM_PROVIDER="openai-compatible",
                CLAUDE_MEM_OPENAI_COMPAT_BASE_URL=self.llm_cfg["base_url"],
                CLAUDE_MEM_OPENAI_COMPAT_MODEL=self.llm_cfg["model"] or "fake-model",
                CLAUDE_MEM_OPENAI_COMPAT_API_KEY=self.llm_cfg["api_key"],
                CLAUDE_MEM_QUOTA_FALLBACK_PROVIDER="off",  # never fall back to a real Claude login
            )
        else:
            env.update(CLAUDE_MEM_PROVIDER="claude", CLAUDE_MEM_CLAUDE_AUTH_METHOD="subscription")
            if os.environ.get(
                "LONGHAUL_CLAUDE_CONFIG_DIR"
            ):  # the Claude login the observer may use
                env["CLAUDE_MEM_CLAUDE_CONFIG_DIR"] = os.environ["LONGHAUL_CLAUDE_CONFIG_DIR"]
        return env

    @staticmethod
    def _host_agent_allowed() -> bool:
        return os.environ.get("LONGHAUL_ALLOW_HOST_AGENT") == "1"

    @staticmethod
    def _host_credentials() -> dict[str, str]:
        """With the gate open the worker's observer is a Claude Code agent on the owner's login:
        pass the credential variables an operator already uses for the host CLI."""
        names = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
        return {n: os.environ[n] for n in names if os.environ.get(n)}

    def _env(self) -> dict[str, str]:
        base = c.clean_env(self._env_vars(), home=self.user_home)
        if not self.llm_cfg.get("base_url") and self._host_agent_allowed():
            base.update(self._host_credentials())
        base["PATH"] = ":".join(
            [str(self.launcher.parent), os.path.dirname(self._bun()), base.get("PATH", "")]
        )
        return base

    @staticmethod
    def _bun() -> str:
        for cand in (os.environ.get("LONGHAUL_BUN"), str(Path("/root/.bun/bin/bun"))):
            if cand and Path(cand).is_file():
                return cand
        from shutil import which

        return which("bun") or "bun"

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    # -- lifecycle -------------------------------------------------------------
    def prepare(self, root: Path) -> None:
        self.root = Path(root)
        self.data.mkdir(parents=True, exist_ok=True)
        (self.root / "projects").mkdir(exist_ok=True)
        self.procs = c.Procs(self.root)
        self.reads = self.writes = 0
        self.port = c.free_port()
        self.llm_cfg = c.llm_env()
        if not self.llm_cfg["base_url"] and os.environ.get("LONGHAUL_FAKE_LLM") == "1":
            self.llm = c.FakeLlm(self.procs, self.root)
            self.llm_cfg = {
                "base_url": self.llm.base_url,
                "model": "fake-model",
                "api_key": "sk-not-a-key",
            }
        if not self.llm_cfg.get("base_url") and not self._host_agent_allowed():
            raise RuntimeError(
                "claude-mem's default observer is the host's Claude login: every tool event and "
                "summary, including those of a live session, is a real model call. Set "
                "LONGHAUL_ALLOW_HOST_AGENT=1 to allow it, or LONGHAUL_LLM_BASE_URL for another endpoint."
            )
        self._start_worker()

    def _start_worker(self) -> None:
        assert self.procs is not None and self.root is not None
        # A seeded instance is copied for each run: the copy holds the previous worker's pid and
        # supervisor files, which name a process that is not ours.
        for stale in ("worker.pid", "supervisor.json", "worker-heartbeat.json"):
            (self.data / stale).unlink(missing_ok=True)
        shutil.rmtree(self.data / "run", ignore_errors=True)
        r = subprocess.run(
            [str(self.launcher), "start"],
            env=self._env(),
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=180,
        )
        pid_file = self.data / "worker.pid"
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if pid_file.exists():
                try:
                    pid = json.loads(pid_file.read_text())["pid"]
                    self.procs.adopt(int(pid))
                    c.http_json(self._url("/api/health"))
                    return
                except Exception:
                    pass
            time.sleep(0.3)
        raise RuntimeError(f"claude-mem worker did not start: {r.stdout[-300:]} {r.stderr[-300:]}")

    def close(self) -> None:
        if self.procs is not None:
            try:  # documented stop first, then the recorded PIDs
                subprocess.run(
                    [str(self.launcher), "stop"],
                    env=self._env(),
                    cwd=self.root,
                    capture_output=True,
                    timeout=30,
                )
            except Exception:
                pass
            self.procs.stop_all()

    # -- ingest ----------------------------------------------------------------
    def _hook(self, event: str, payload: dict) -> None:
        assert self.root is not None
        r = subprocess.run(
            [str(self.launcher), "hook", "claude-code", event],
            input=json.dumps(payload),
            env=self._env(),
            cwd=payload["cwd"],
            capture_output=True,
            text=True,
            timeout=HOOK_TIMEOUT,
        )
        if r.returncode:
            raise RuntimeError(f"claude-mem hook {event} failed: {r.stderr[-300:]}")
        self.writes += 1

    def _wait_idle(self, timeout: float = 1800.0) -> None:
        deadline = time.monotonic() + timeout
        quiet = 0
        while time.monotonic() < deadline:
            st = c.http_json(self._url("/api/processing-status"))
            idle = not st.get("isProcessing") and not st.get("queueDepth")
            quiet = quiet + 1 if idle else 0
            if quiet >= 3:
                return
            time.sleep(0.5)
        raise TimeoutError("claude-mem observer queue did not drain")

    def observer_turns(self) -> int:
        """The observer's model turns so far, counted from the worker's own log (one
        `Observer turn usage` line per response): the unit of the call budget in RUNNING.md."""
        total = 0
        for log in (self.data / "logs").glob("claude-mem-*.log"):
            with log.open(errors="replace") as handle:
                total += sum(1 for line in handle if "Observer turn usage" in line)
        return total

    def _calls_since(self, base: dict | None, turns_before: int) -> int:
        """Observer calls since seeding began: the fake endpoint's count, else the worker's."""
        if self.llm and base is not None:
            return self.llm.stats()["chat_calls"] - base["chat_calls"]
        return self.observer_turns() - turns_before

    def seed(self, sessions: Iterable[Session]) -> SeedReport:
        assert self.root is not None
        if not self.llm_cfg.get("base_url") and not self._host_agent_allowed():
            raise RuntimeError(
                "claude-mem's default observer is the host's Claude login (a real, billed model call). "
                "Set LONGHAUL_ALLOW_HOST_AGENT=1 to allow it, or LONGHAUL_LLM_BASE_URL for another endpoint."
            )
        base = self.llm.stats() if self.llm else None
        turns_before = self.observer_turns()
        cap = int(os.environ.get("LONGHAUL_CLAUDE_MEM_CALL_CAP") or 0)  # 0: no cap
        started = time.monotonic()
        n = 0
        for s in sessions:
            cwd = self.root / "projects" / s.scope.project
            cwd.mkdir(parents=True, exist_ok=True)
            tpath = self.root / "transcripts" / f"{s.id}.jsonl"
            # The hook events a real session of these turns fires, with the payloads the host
            # gives them (`_session.py`): the prompt of each user turn, each tool call or failure,
            # the last assistant message of each turn at Stop, the transcript file as written so
            # far, and the end of the session.
            for event in _session.events(s, str(cwd), str(tpath)):
                _session.write_transcript(tpath, event.transcript)
                self._hook(HOOK_COMMAND[event.name], event.payload)
            n += 1
            if cap and n % 10 == 0 and self._calls_since(base, turns_before) > cap:
                raise RuntimeError(
                    f"claude-mem seeding stopped after {n} sessions: more than "
                    f"{cap} observer calls (LONGHAUL_CLAUDE_MEM_CALL_CAP)"
                )
        self._wait_idle()
        calls = tokens = 0
        note = "hook replay as a real session fires it (prompt, tool or failure, stop, end); observer: "
        if self.llm and base is not None:
            now = self.llm.stats()
            calls = now["chat_calls"] - base["chat_calls"]
            tokens = (now["chat_prompt_tokens"] + now["chat_completion_tokens"]) - (
                base["chat_prompt_tokens"] + base["chat_completion_tokens"]
            )
            note += "fake openai-compatible endpoint (counts are the fake server's)"
        else:
            calls = self.observer_turns() - turns_before
            note += (
                "the host Claude login; calls are the worker's own count of observer turns, "
                "tokens are not measured here"
            )
        return SeedReport(
            sessions=n,
            seconds=time.monotonic() - started,
            llm_calls=calls,
            llm_tokens=tokens,
            notes=note,
        )

    # -- host ------------------------------------------------------------------
    def _hooks_settings(self) -> dict:
        """The plugin's own hooks.json, with the launcher given as an absolute path, the plugin
        root filled in, and the one-time Setup and launcher-repair entries left out."""
        plugin = self._plugin_dir()
        src = json.loads((plugin / "hooks/hooks.json").read_text())["hooks"]
        hooks: dict = {}
        for event, groups in src.items():
            if event == "Setup":
                continue
            kept = []
            for g in groups:
                hs = []
                for h in g["hooks"]:
                    h = json.loads(json.dumps(h).replace("${CLAUDE_PLUGIN_ROOT}", str(plugin)))
                    if h.get("command") == "node" and "ensure-launcher" in json.dumps(h):
                        continue
                    if h.get("command") == "claude-mem":
                        h["command"] = str(self.launcher)
                    hs.append(h)
                if hs:
                    kept.append({**g, "hooks": hs})
            if kept:
                hooks[event] = kept
        return {"hooks": hooks}

    def host(self, workdir: Path, scope: Scope) -> HostConfig:
        plugin = self._plugin_dir()
        env = self._env_vars()
        # Hooks given through a settings file do not get the plugin variables Claude Code sets
        # for an installed plugin, so name the plugin root and the Bun install the launcher uses.
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin)
        env["BUN_INSTALL"] = str(Path(self._bun()).parent.parent)
        skill = (plugin / "skills/mem-search/SKILL.md").read_text()
        return HostConfig(
            mcp={
                "mcpServers": {
                    "mcp-search": {
                        "type": "stdio",
                        "command": "node",
                        "args": [str(plugin / "scripts/mcp-server.cjs")],
                        "env": {**env, "HOME": str(self.user_home)},
                    }
                }
            },
            settings=self._hooks_settings(),
            files={".claude/skills/mem-search/SKILL.md": skill},
            allowed_tools=["mcp__mcp-search"],
            env=env,
            prompt_note="",
        )

    # -- retrieval -------------------------------------------------------------
    def context_for(self, query: str, scope: Scope, budget_chars: int) -> str:
        """The plugin's search workflow: `search` for an index, then the full records of the
        top hits (`get_observations`); matching prompts are returned as written."""
        q = urllib.parse.urlencode({"query": query, "limit": 10, "project": scope.project})
        res = c.http_json(self._url(f"/api/search?{q}"))
        self.reads += 1
        text = "\n".join(p.get("text", "") for p in res.get("content", []))
        out = [text]
        summaries: dict | None = None
        for kind, ident in re.findall(r"\|\s*#([PS]?)(\d+)\s*\|", text)[:8]:
            try:
                if kind == "S":
                    if summaries is None:
                        listing = c.http_json(
                            self._url(
                                f"/api/summaries?limit=500&project={urllib.parse.quote(scope.project)}"
                            )
                        )
                        summaries = {str(r["id"]): r for r in listing.get("items", [])}
                    row = summaries.get(ident, {})
                    body = " ".join(
                        str(row.get(k) or "")
                        for k in (
                            "request",
                            "investigated",
                            "learned",
                            "completed",
                            "next_steps",
                            "notes",
                        )
                    )
                    out.append(f"[summary {ident}] {body}")
                elif kind == "P":
                    row = c.http_json(self._url(f"/api/prompt/{ident}"))
                    out.append(f"[prompt {ident}] {row.get('prompt_text', '')}")
                else:
                    row = c.http_json(self._url(f"/api/observation/{ident}"))
                    out.append(
                        f"[observation {ident}] {row.get('title', '')}\n{row.get('narrative', '')}\n"
                        f"{row.get('facts', '')}"
                    )
                self.reads += 1
            except Exception:
                continue
        return c.clip("\n".join(out), budget_chars)

    def forget(self, text_marker: str, scope: Scope) -> bool | None:
        """The worker API's delete routes for observations, session summaries and prompts (what
        the viewer's delete uses). Raw tool input/output kept for `get_tool_uses` is not touched
        by them; see SYSTEMS.md."""
        marker = text_marker.lower()
        db = self.data / "claude-mem.db"
        import sqlite3

        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            targets = [
                ("observation", r[0])
                for r in con.execute(
                    "select id from observations where lower(coalesce(title,'')||' '||coalesce(narrative,'')"
                    "||' '||coalesce(facts,'')||' '||coalesce(text,'')) like ?",
                    (f"%{marker}%",),
                )
            ]
            targets += [
                ("prompt", r[0])
                for r in con.execute(
                    "select id from user_prompts where lower(prompt_text) like ?", (f"%{marker}%",)
                )
            ]
            targets += [
                ("summary", r[0])
                for r in con.execute(
                    "select id from session_summaries where lower(coalesce(request,'')||' '||coalesce(learned,'')"
                    "||' '||coalesce(completed,'')||' '||coalesce(notes,'')||' '||coalesce(investigated,'')) like ?",
                    (f"%{marker}%",),
                )
            ]
        finally:
            con.close()
        for kind, ident in targets:
            c.http_json(self._url(f"/api/{kind}/{ident}"), method="DELETE")
            self.writes += 1
        return bool(targets)

    def usage(self) -> dict:
        return {"reads": self.reads, "writes": self.writes}

    def disk_bytes(self) -> int:
        return c.dir_size(self.data) if self.root else 0


def make() -> ClaudeMem:
    return ClaudeMem()


def installed() -> bool:
    return ClaudeMem.installed()


def create():
    """The arm for the benchmark driver (`systems.load`)."""
    return make()
