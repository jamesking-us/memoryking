"""Scheduled backups with retention, run by the daemon that owns the store.

Backups land in `DIR/backup-<UTC timestamp>` next to each other; retention keeps the
newest N directories that carry a signed manifest and never touches anything else in
the directory. The daemon checks the policy between requests, so a backup happens while
the store is quiet and the exclusive owner is the daemon itself.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memoryking.adapters.local_files import private_dir
from memoryking.domain.local import LocalError
from memoryking.observability import event, failure

PATTERN = re.compile(r"^backup-(\d{8}T\d{6}Z)$")
DURATIONS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}


def interval_ms(text: str) -> int:
    if len(text) < 2 or text[-1] not in DURATIONS or not text[:-1].isdigit() or int(text[:-1]) < 1:
        raise LocalError("invalid_duration")
    return int(text[:-1]) * DURATIONS[text[-1]]


def stamp(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_stamp(name: str) -> int:
    match = PATTERN.match(name)
    if match is None:
        raise ValueError(name)
    moment = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def existing(directory: Path) -> list[tuple[int, Path]]:
    """(timestamp ms, path) for every complete backup, oldest first."""
    if not directory.exists():
        return []
    found = []
    for path in directory.iterdir():
        if path.is_dir() and PATTERN.match(path.name) and (path / "manifest").is_file():
            found.append((parse_stamp(path.name), path))
    return sorted(found)


def due(directory: Path, every_ms: int, now_ms: int) -> bool:
    backups = existing(directory)
    return not backups or now_ms - backups[-1][0] >= every_ms


def prune(directory: Path, keep: int) -> list[Path]:
    """Remove the oldest complete backups beyond `keep`; returns what was removed."""
    removed = []
    for _, path in existing(directory)[:-keep] if keep > 0 else []:
        shutil.rmtree(path)
        removed.append(path)
    return removed


class Policy:
    def __init__(self, directory: Path, every: str, keep: int):
        if keep < 1:
            raise LocalError("invalid_request")
        self.directory, self.every_ms, self.keep = directory, interval_ms(every), keep
        self.checked_ms = 0

    def maybe_run(self, store: Any, now_ms: int, check_every_ms: int = 60_000) -> Path | None:
        """Back up when due; at most one check per minute, errors logged and swallowed."""
        if now_ms - self.checked_ms < check_every_ms:
            return None
        self.checked_ms = now_ms
        if not due(self.directory, self.every_ms, now_ms):
            return None
        return self.run(store, now_ms)

    def run(self, store: Any, now_ms: int) -> Path | None:
        private_dir(self.directory, create=True)
        target = self.directory / ("backup-" + stamp(now_ms))
        try:
            manifest = store.backup(target)
            removed = prune(self.directory, self.keep)
            event(
                "backup",
                "complete",
                archive_bytes=manifest.get("archive_bytes", 0),
                blobs=len(manifest.get("blobs", [])),
                pruned=len(removed),
            )
            return target
        except (LocalError, OSError) as exc:
            failure("backup", exc)
            return None


def check(directory: Path | None, every: str | None, now_ms: int) -> tuple[str, str]:
    """A doctor finding: (level, detail) about the newest backup against the policy."""
    if directory is None:
        return (
            "warn",
            "no backup directory configured; `serve --backup-dir DIR --backup-every 1d` schedules them",
        )
    backups = existing(directory)
    if not backups:
        return "warn", f"no complete backup in {directory} yet"
    age_ms = now_ms - backups[-1][0]
    age = f"{age_ms / 3_600_000:.1f} h"
    if every is not None and age_ms > 2 * interval_ms(every):
        return (
            "fail",
            f"newest backup {backups[-1][1].name} is {age} old, past twice the {every} policy",
        )
    return "ok", f"{len(backups)} backups, newest {backups[-1][1].name} ({age} old)"
