"""The interface every memory system (arm) of the long-history benchmark implements.

A system lives in `benchmarks/longhaul/systems/<name>.py`, which exposes `create()`, a
function that returns a new `System` for the benchmark driver (`tools/longhaul_benchmark.py`).
Arms are named by that module name (`off`, `notes`, `log`, `native`, `memoryking`, ...).

How the driver uses a system:

* `seed` (once per arm, track, history size and group): `prepare(root)` on an empty
  directory, `seed(sessions)`, `close()`. The directory is kept.
* `run` (once per task, arm and repetition): the seeded directory is copied to a private
  one, then `prepare(copy)` is called on it. So `prepare` must also accept a directory that
  already holds a seeded instance and open it as it is: it is empty only the first time.
  A system that cannot be copied sets `reseed_per_run = True` and is seeded again for each
  run instead (its seed cost is then a cost of every run).
* `host(workdir, scope)` says how the agent reaches the system. `HostConfig.files` maps a
  path relative to the working directory to file content; a key that starts with `~/` is
  relative to the session's fresh `HOME` instead (the driver creates the home and passes it
  to the agent command). Nothing here may name a path outside the working directory, the
  session home or the system's own root. `HostConfig.env` is added to the agent's scrubbed
  environment; an empty value removes a variable the driver sets (auto-memory is switched
  off for every arm unless the arm's host config says otherwise).
* `context_for` is what a question-answering run (track T5) asks: the text the system would
  hand a reader for the query, at most `budget_chars` characters.
* `forget(marker, scope)` uses the product's own documented delete path for what contains
  the marker text, and answers whether something was removed (None when the product has no
  delete path).
* Optional class attributes the driver reads with `getattr`: `reseed_per_run` (default
  False) and `persistent_home` (default False; True keeps one `HOME` across the sessions of
  a lived run, for a host whose own memory lives there).
* `close` stops every process the system started, by the process ids it recorded; it must
  never stop processes by name or pattern.

Turn convention: `role` is `user`, `assistant` or `tool`. A `tool` turn whose text starts
with `ERROR: ` is a failed tool call; any other `tool` turn is a successful tool result.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Scope:
    user: str
    project: str
    agent: str


@dataclass(frozen=True)
class Turn:
    role: str  # user | assistant | tool
    text: str


@dataclass(frozen=True)
class Session:
    id: str
    scope: Scope
    started: str  # RFC 3339
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
    mcp: dict  # value for an --mcp-config file ("mcpServers": {...}), may be empty
    settings: dict  # value for a --settings file (hooks, env), may be empty
    files: dict[str, str]  # relative path in the working directory -> content
    allowed_tools: list[str]
    env: dict[str, str]
    prompt_note: (
        str  # only what the product's own docs tell a user to tell the agent; "" if nothing
    )


@runtime_checkable
class System(Protocol):
    name: str
    version: str
    needs_llm: bool  # needs its own model endpoint to ingest or retrieve

    def prepare(
        self, root: Path
    ) -> None: ...  # empty isolated instance under root; no global or HOME state

    def seed(self, sessions: Iterable[Session]) -> SeedReport: ...

    def host(self, workdir: Path, scope: Scope) -> HostConfig: ...

    def context_for(self, query: str, scope: Scope, budget_chars: int) -> str: ...  # T5

    def forget(self, text_marker: str, scope: Scope) -> bool | None: ...  # documented delete path

    def usage(self) -> dict: ...  # reads and writes seen since prepare

    def close(self) -> None: ...  # stop every process it started, by recorded PID
