"""Arm `off`: no memory. The agent gets the task and nothing else."""

from __future__ import annotations

from pathlib import Path

from ._files import FileSystemBase
from .base import HostConfig, Scope


class Off(FileSystemBase):
    name = "off"

    def host(self, workdir: Path, scope: Scope) -> HostConfig:
        return HostConfig({}, {}, {}, [], {}, "")

    def context_for(self, query: str, scope: Scope, budget_chars: int) -> str:
        return ""

    def forget(self, text_marker: str, scope: Scope) -> bool | None:
        return None


def create() -> Off:
    return Off()
