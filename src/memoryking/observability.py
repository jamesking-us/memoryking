"""Redacted operator logging.

Log lines carry event names, request identifiers, error types and code locations.
They never carry SQL, file paths outside the package, caller content, credentials or
exception messages, which may embed any of those. Access logging stays off.
"""

from __future__ import annotations

import logging
import secrets
import sys
import traceback
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("memoryking")
LEVELS = ("debug", "info", "warning", "error")


def configure(level: str = "warning") -> None:
    """Send package logs to stderr at the requested level; idempotent."""
    if level not in LEVELS:
        raise ValueError("log level must be one of " + ", ".join(LEVELS))
    LOGGER.setLevel(getattr(logging, level.upper()))
    if not any(getattr(h, "_memoryking", False) for h in LOGGER.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler._memoryking = True  # type: ignore[attr-defined]
        LOGGER.addHandler(handler)
    LOGGER.propagate = False


def event(component: str, name: str, **fields: Any) -> None:
    """An informational line built only from identifiers and counts."""
    LOGGER.info("component=%s event=%s%s", component, name, _fields(fields))


def failure(component: str, exc: BaseException, **fields: Any) -> str:
    """Record an unexpected failure and return the request id handed to the caller."""
    request_id = secrets.token_hex(8)
    frames = " <- ".join(
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in traceback.extract_tb(exc.__traceback__)[-6:]
    )
    LOGGER.error(
        "component=%s event=internal_error request_id=%s error=%s frames=%s%s",
        component,
        request_id,
        type(exc).__name__,
        frames or "none",
        _fields(fields),
    )
    return request_id


def _fields(fields: dict[str, Any]) -> str:
    return "".join(f" {key}={value}" for key, value in sorted(fields.items()))
