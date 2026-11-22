"""Arm `notes`: a notes file in the working directory, seeded by the fixed extractive rule in
`_files.py`, and the agent told it may keep notes there (the plain-file baseline)."""

from __future__ import annotations

from pathlib import Path

from ._files import NOTES_CAP, FileSystemBase, extract
from .base import HostConfig, Scope

HEAD = "# Notes\n\nNotes for later sessions of this project, oldest first.\n\n"
NOTE = "You can keep notes for later sessions in NOTES.md in this directory."


class Notes(FileSystemBase):
    name = "notes"

    def host(self, workdir: Path, scope: Scope) -> HostConfig:
        body = extract(self.sessions_for(scope), cap=NOTES_CAP)
        return HostConfig({}, {}, {"NOTES.md": HEAD + body}, [], {}, NOTE)


def create() -> Notes:
    return Notes()
