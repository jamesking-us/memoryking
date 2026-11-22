"""The adapter interface types.

`base.py` is the authority. Until it exists in a checkout this module carries a private,
identical copy so the adapters can be developed and run on their own.
"""

from __future__ import annotations

try:  # the harness's own definitions, when they have landed
    from .base import HostConfig, Scope, SeedReport, Session, System, Turn
except ImportError:  # pragma: no cover - exercised only before base.py exists
    from dataclasses import dataclass, field
    from pathlib import Path
    from typing import Iterable, Protocol

    @dataclass(frozen=True)
    class Scope:
        user: str
        project: str
        agent: str

    @dataclass(frozen=True)
    class Turn:
        role: str
        text: str

    @dataclass(frozen=True)
    class Session:
        id: str
        scope: Scope
        started: str
        turns: tuple[Turn, ...]

    @dataclass
    class SeedReport:
        sessions: int
        seconds: float
        llm_calls: int
        llm_tokens: int
        notes: str

    @dataclass
    class HostConfig:
        mcp: dict = field(default_factory=dict)
        settings: dict = field(default_factory=dict)
        files: dict = field(default_factory=dict)
        allowed_tools: list = field(default_factory=list)
        env: dict = field(default_factory=dict)
        prompt_note: str = ""

    class System(Protocol):
        name: str
        version: str
        needs_llm: bool

        def prepare(self, root: Path) -> None: ...
        def seed(self, sessions: Iterable[Session]) -> SeedReport: ...
        def host(self, workdir: Path, scope: Scope) -> HostConfig: ...
        def context_for(self, query: str, scope: Scope, budget_chars: int) -> str: ...
        def forget(self, text_marker: str, scope: Scope) -> bool | None: ...
        def usage(self) -> dict: ...
        def close(self) -> None: ...


__all__ = ["HostConfig", "Scope", "SeedReport", "Session", "System", "Turn"]
