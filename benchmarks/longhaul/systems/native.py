"""Arm `native`: Claude Code's own memory.

Where the CLI reads memory in headless mode (version 2.1.330; read from its own binary and
help text, not by running a model): a `CLAUDE.md` in the working directory (project memory,
loaded at the start of every session whatever the prompt says) and `~/.claude/CLAUDE.md`
(user memory, every project). It also keeps an auto-memory folder under
`~/.claude/projects/<working-directory>/memory/` that the agent itself writes and reads.

This arm seeds the project `CLAUDE.md` with the same extractive notes as the `notes` arm
(the same rule, the same cap) and leaves auto-memory ON, as a user of the CLI has it. The
session home is fresh and empty, so auto-memory starts empty and only the seeded file
carries history. The agent is told nothing: the host loads the file. In a lived run the
home persists across the sessions (`persistent_home`), so what the agent writes itself
carries. Every other arm runs with auto-memory off (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`) and
no user-level memory file.
"""

from __future__ import annotations

from pathlib import Path

from ._files import NOTES_CAP, FileSystemBase, extract
from .base import HostConfig, Scope

HEAD = "# Project memory\n\nWhat earlier sessions of this project recorded, oldest first.\n\n"


class Native(FileSystemBase):
    name = "native"
    persistent_home = True

    def host(self, workdir: Path, scope: Scope) -> HostConfig:
        body = extract(self.sessions_for(scope), cap=NOTES_CAP)
        # An empty value removes the variable the driver sets for the other arms.
        env = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": ""}
        return HostConfig({}, {}, {"CLAUDE.md": HEAD + body}, [], env, "")


def create() -> Native:
    return Native()
