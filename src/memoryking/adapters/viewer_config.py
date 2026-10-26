"""Closed, explicitly opted-in viewer configuration; does not open a listener."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from memoryking.adapters.local_files import private_file
from memoryking.contracts.canonical import load_strict
from memoryking.contracts.viewer import validate_viewer
from memoryking.domain.local import LocalError


def validate_config(value: dict[str, Any]) -> dict[str, Any]:
    validate_viewer("ViewerConfig", value)
    try:
        origin = urlsplit(value["public_origin"])
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path
            or origin.query
            or origin.fragment
            or origin.port == 0
            or "*" in origin.netloc
            or origin.geturl() != value["public_origin"]
            or not re.fullmatch(r"[A-Za-z0-9.:-]+", origin.hostname)
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value["public_origin"])
        ):
            raise ValueError("origin")
    except ValueError as exc:
        raise LocalError("invalid_viewer_configuration") from exc
    if value["session_idle_seconds"] > value["session_absolute_seconds"]:
        raise LocalError("invalid_viewer_configuration")
    direct = value["transport"] == "direct_tls"
    if direct != bool(value["tls_cert_file"] and value["tls_key_file"]):
        raise LocalError("invalid_viewer_configuration")
    if not direct and (value["tls_cert_file"] is not None or value["tls_key_file"] is not None):
        raise LocalError("invalid_viewer_configuration")
    return value


def load_config(path: Path) -> dict[str, Any]:
    private_file(path)
    with path.open("rb") as stream:
        raw = stream.read(32769)
    if len(raw) > 32768:
        raise LocalError("invalid_viewer_configuration")
    value = load_strict(raw)
    if not isinstance(value, dict):
        raise LocalError("invalid_viewer_configuration")
    return validate_config(value)
