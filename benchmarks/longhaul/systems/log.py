"""Arm `log`: the raw seeded transcripts as files the agent can search, one per session.

The agent is told where they are, as a person would tell it. This is the cheapest serious
baseline at scale: nothing is extracted, nothing is indexed, grep is the retrieval."""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from ._files import FileSystemBase, render_transcript
from .base import HostConfig, Scope

NOTE = (
    "Transcripts of earlier sessions of this project are in ./history/, one file per "
    "session; you can search them."
)
NOTE_ALL = (
    "Transcripts of earlier sessions of all your projects are in ./history/, one folder per "
    "project and one file per session; you can search them."
)


class Log(FileSystemBase):
    name = "log"

    def host(self, workdir: Path, scope: Scope) -> HostConfig:
        files = {}
        for s in self.sessions_for(scope):
            stamp = re.sub(r"[^0-9]", "", s.started)[:12]
            safe = re.sub(r"[^A-Za-z0-9_-]", "-", s.id)
            folder = re.sub(r"[^A-Za-z0-9_-]", "-", s.scope.project) + "/" if self.all_projects else ""
            files[f"history/{folder}{stamp}-{safe}.md"] = render_transcript(s)
        return HostConfig({}, {}, files, [], {}, NOTE_ALL if self.all_projects else NOTE)

    def context_for(self, query: str, scope: Scope, budget_chars: int) -> str:
        """What grep with a ranking gives: the turns that share the most (rarer) words with the
        query, best first until the budget is spent, then shown oldest first with their
        session's date. No model, no index beyond word counts."""
        words = {w for w in re.findall(r"[a-z0-9]{4,}", query.lower())}
        rows: list[tuple[float, str, str, str]] = []
        document_frequency: Counter[str] = Counter()
        turns = []
        for s in self.sessions_for(scope):
            for t in s.turns:
                found = set(re.findall(r"[a-z0-9]{4,}", t.text.lower()))
                document_frequency.update(found & words)
                turns.append((s, t, found & words))
        total = max(1, len(turns))
        for s, t, found in turns:
            if found:
                score = sum(math.log(1 + total / document_frequency[w]) for w in found)
                rows.append((score, s.started, f"[{s.started[:10]}] {t.role}: {t.text}", s.id))
        rows.sort(key=lambda r: (-r[0], r[1]))
        picked, used = [], 0
        for _score, started, line, sid in rows:
            if used + len(line) + 1 > budget_chars:
                continue
            picked.append((started, sid, line))
            used += len(line) + 1
        picked.sort()
        return "\n".join(line for _, _, line in picked)


def create() -> Log:
    return Log()
