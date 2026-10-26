"""Closed, explicitly opted-in viewer configuration; does not open a listener."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

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
