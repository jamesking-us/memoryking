"""Shared by the plain-file arms (`off`, `notes`, `log`, `native`): the seeded sessions are kept
in the arm's root and each run's files are derived from them.

The extractive rule for `notes` and `native` is fixed here, applied to every seeded session in
the same way, and knows nothing about which facts a task needs:

* for each session, oldest first, one heading with the date and the session id;
* then each user turn cut to `TURN_CHARS` characters, and the session's last assistant turn
  cut to `TURN_CHARS` characters, one bullet each (tool turns are dropped);
* the file is held to `NOTES_CAP` characters by dropping the oldest sessions first, and says
  how many were dropped (a notes file cannot grow without bound: it is read in full into the
  prompt).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from .base import HostConfig, Scope, SeedReport, Session, Turn

TURN_CHARS = 300
NOTES_CAP = 24_000
SESSIONS_FILE = "sessions.jsonl"
FORGOTTEN_FILE = "forgotten.json"


def session_to_json(session: Session) -> dict:
    return {
        "id": session.id,
        "scope": {
            "user": session.scope.user,
            "project": session.scope.project,
            "agent": session.scope.agent,
        },
        "started": session.started,
        "turns": [[t.role, t.text] for t in session.turns],
    }


def session_from_json(row: dict) -> Session:
    return Session(
        id=row["id"],
        scope=Scope(**row["scope"]),
        started=row["started"],
        turns=tuple(Turn(role, text) for role, text in row["turns"]),
    )


def one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def extract(sessions: Iterable[Session], cap: int = NOTES_CAP) -> str:
    """The notes the extractive rule produces from these sessions (oldest first)."""
    blocks: list[str] = []
    for s in sorted(sessions, key=lambda s: (s.started, s.id)):
        lines = [f"## {s.started[:10]} ({s.id})"]
        for t in s.turns:
            if t.role == "user":
                lines.append(f"- user: {one_line(t.text, TURN_CHARS)}")
        assistant = [t for t in s.turns if t.role == "assistant"]
        if assistant:
            lines.append(f"- assistant: {one_line(assistant[-1].text, TURN_CHARS)}")
        blocks.append("\n".join(lines))
    kept: list[str] = []
    used = 0
    for block in reversed(blocks):
        if used + len(block) + 2 > cap and kept:
            break
        kept.append(block)
        used += len(block) + 2
    kept.reverse()
    dropped = len(blocks) - len(kept)
    head = f"({dropped} older session(s) are not listed here.)\n\n" if dropped else ""
    return head + "\n\n".join(kept) + ("\n" if kept else "")


def render_transcript(s: Session) -> str:
    out = [
        f"# Session {s.id}",
        f"started: {s.started}",
        f"project: {s.scope.project}",
        f"agent: {s.scope.agent}",
        "",
    ]
    for t in s.turns:
        out.append(f"[{t.role}] {t.text}")
    return "\n".join(out) + "\n"


class FileSystemBase:
    """Common lifecycle for the arms that keep no process: root holds the seeded sessions."""

    name = "base"
    version = "1"
    needs_llm = False
    reseed_per_run = False
    persistent_home = False

    def __init__(self) -> None:
        self.root: Path | None = None

    def prepare(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def seed(self, sessions: Iterable[Session]) -> SeedReport:
        assert self.root is not None
        count = 0
        with (self.root / SESSIONS_FILE).open("a", encoding="utf-8") as handle:
            for s in sessions:
                handle.write(json.dumps(session_to_json(s)) + "\n")
                count += 1
        return SeedReport(count, 0.0, 0, 0, "sessions stored as files; no model, no index")

    def sessions_for(self, scope: Scope) -> list[Session]:
        """This project's seeded sessions, with turns containing a forgotten text removed."""
        assert self.root is not None
        path = self.root / SESSIONS_FILE
        if not path.exists():
            return []
        forgotten = self.forgotten()
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["scope"]["project"] != scope.project:
                continue
            s = session_from_json(row)
            if forgotten:
                turns = tuple(t for t in s.turns if not any(m in t.text for m in forgotten))
                s = Session(s.id, s.scope, s.started, turns)
            out.append(s)
        return out

    def forgotten(self) -> list[str]:
        assert self.root is not None
        path = self.root / FORGOTTEN_FILE
        return json.loads(path.read_text()) if path.exists() else []

    def host(self, workdir: Path, scope: Scope) -> HostConfig:  # pragma: no cover - overridden
        return HostConfig({}, {}, {}, [], {}, "")

    def context_for(self, query: str, scope: Scope, budget_chars: int) -> str:
        """Files are searched by the reader's own tools, so T5 hands over the newest text that
        fits: the most recent sessions of the project, which is all a plain file offers."""
        sessions = self.sessions_for(scope)
        text = extract(sessions, cap=budget_chars)
        return text[-budget_chars:]

    def forget(self, text_marker: str, scope: Scope) -> bool | None:
        """Delete the turns that contain the text from the files (what a person would do)."""
        assert self.root is not None
        found = any(text_marker in t.text for s in self.sessions_for(scope) for t in s.turns)
        if found:
            marks = self.forgotten()
            marks.append(text_marker)
            (self.root / FORGOTTEN_FILE).write_text(json.dumps(marks))
        return found

    def usage(self) -> dict:
        return {"reads": None, "writes": None, "note": "plain files: use the event stream"}

    def close(self) -> None:
        return None
