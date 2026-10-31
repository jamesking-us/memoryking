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


_TRACING = False


def configure(level: str = "warning", trace: bool = False) -> None:
    """Send package logs to stderr at the requested level; idempotent.

    With `trace`, one timing line per request is written at info level, so a level
    above info is lowered to info; an explicit debug level is kept.
    """
    global _TRACING
    if level not in LEVELS:
        raise ValueError("log level must be one of " + ", ".join(LEVELS))
    _TRACING = trace
    if trace and LEVELS.index(level) > LEVELS.index("info"):
        level = "info"
    LOGGER.setLevel(getattr(logging, level.upper()))
    if not any(getattr(h, "_memoryking", False) for h in LOGGER.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler._memoryking = True  # type: ignore[attr-defined]
        LOGGER.addHandler(handler)
    LOGGER.propagate = False


def tracing() -> bool:
    """Whether per-request timing lines are wanted; checked before any timing work."""
    return _TRACING


def trace(component: str, **fields: Any) -> None:
    """One timing line per request: operation names, outcome codes, durations and sizes.

    Never the project, principal, request body, SQL or paths: identifiers that name
    content stay out of the log exactly as for every other line.
    """
    if _TRACING:
        LOGGER.info("component=%s event=trace%s", component, _fields(fields))


def timings(store: Any) -> list[dict[str, float]]:
    """Start collecting this thread's transaction timings on a store; returns the list."""
    items: list[dict[str, float]] = []
    store.timings.items = items
    return items


def summarize(elapsed_ms: float, spans: list[dict[str, float]]) -> dict[str, Any]:
    """Fields for a trace line from a request's elapsed time and its transaction spans."""
    return {
        "elapsed_ms": round(elapsed_ms, 3),
        "transactions": len(spans),
        "lock_wait_ms": round(sum(span.get("lock_wait_ms", 0.0) for span in spans), 3),
        "transaction_ms": round(sum(span.get("transaction_ms", 0.0) for span in spans), 3),
    }


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
