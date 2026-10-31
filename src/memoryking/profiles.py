"""Named store profiles so commands take `--profile work` instead of four absolute paths.

The file is `~/.memoryking/config.toml` (override with `MEMORYKING_CONFIG`). It holds
paths only, never secrets: a credential entry is the path of a private credential file.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from memoryking.adapters.local_files import atomic, private_dir, private_file
from memoryking.domain.local import LocalError

FIELDS = ("store", "control", "socket", "credential", "dashboard_port", "embedding_model")
PATH_FIELDS = ("store", "control", "socket", "credential", "embedding_model")


def config_path() -> Path:
    return Path(os.environ.get("MEMORYKING_CONFIG") or Path.home() / ".memoryking" / "config.toml")


def load(path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = path or config_path()
    if not path.exists():
        return {}
    private_file(path)
    try:
        data = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, UnicodeError) as exc:
        raise LocalError("profile_config_invalid") from exc
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict):
        raise LocalError("profile_config_invalid")
    result: dict[str, dict[str, Any]] = {}
    for name, values in profiles.items():
        result[name] = validate(name, values)
    return result


def validate(name: str, values: Any) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or not isinstance(values, dict):
        raise LocalError("profile_config_invalid")
    if set(values) - set(FIELDS) or not {"store", "control"} <= set(values):
        raise LocalError("profile_config_invalid")
    clean: dict[str, Any] = {}
    for field in PATH_FIELDS:
        if field in values:
            if not isinstance(values[field], str) or not Path(values[field]).is_absolute():
                raise LocalError("profile_config_invalid")
            clean[field] = str(Path(values[field]))
    if "dashboard_port" in values:
        port = values["dashboard_port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise LocalError("profile_config_invalid")
        clean["dashboard_port"] = port
    return clean


def resolve(name: str, path: Path | None = None) -> dict[str, Any]:
    """A profile with its implied socket and credential paths filled in."""
    profiles = load(path)
    if name not in profiles:
        raise LocalError("profile_unknown")
    profile = dict(profiles[name])
    profile.setdefault("socket", str(Path(profile["store"]) / "service.sock"))
    profile.setdefault("credential", str(Path(profile["control"]) / "owner.credential"))
    return profile


def apply(args: Any, name: str | None, path: Path | None = None) -> None:
    """Fill omitted path arguments from the named profile; explicit flags always win."""
    if name is None:
        return
    profile = resolve(name, path)
    for field in FIELDS:
        if hasattr(args, field) and getattr(args, field) is None and field in profile:
            value = profile[field]
            setattr(args, field, Path(value) if field in PATH_FIELDS else value)


def save(name: str, values: dict[str, Any], path: Path | None = None) -> Path:
    """Add or replace one profile, keeping the file private."""
    path = path or config_path()
    profiles = load(path)
    profiles[name] = validate(name, values)
    private_dir(path.parent, create=True)
    atomic(path, render(profiles).encode())
    return path


def remove(name: str, path: Path | None = None) -> Path:
    path = path or config_path()
    profiles = load(path)
    if name not in profiles:
        raise LocalError("profile_unknown")
    del profiles[name]
    atomic(path, render(profiles).encode())
    return path


def render(profiles: dict[str, dict[str, Any]]) -> str:
    lines = ["# MemoryKing store profiles. Paths only; credential is the path of a private file."]
    for name in sorted(profiles):
        lines.append(f"\n[profiles.{name}]")
        for field in FIELDS:
            if field in profiles[name]:
                value = profiles[name][field]
                lines.append(
                    f"{field} = {value}" if isinstance(value, int) else f'{field} = "{value}"'
                )
    return "\n".join(lines) + "\n"
