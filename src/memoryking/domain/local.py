"""Backend-independent local service errors and bounded authorization decisions."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol


class LocalError(Exception):
    """Stable non-disclosing error code; never embeds supplied content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Clock(Protocol):
    def utc_ms(self) -> int: ...
    def monotonic_ms(self) -> int: ...


class SystemClock:
    def utc_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def monotonic_ms(self) -> int:
        return time.monotonic_ns() // 1_000_000


@dataclass(frozen=True)
class Decision:
    evaluated_ms: int
    monotonic_ms: int
    deadline_ms: int

    def check(self, clock: Clock) -> None:
        utc, mono = clock.utc_ms(), clock.monotonic_ms()
        elapsed = mono - self.monotonic_ms
        if utc < self.evaluated_ms or elapsed < 0:
            raise LocalError("clock_untrusted")
        if utc >= self.deadline_ms or elapsed >= 5000:
            raise LocalError("release_expired")
        if abs((utc - self.evaluated_ms) - elapsed) > 1000:
            raise LocalError("clock_untrusted")


def temporal_valid(at: int, start: int | None, end: int | None) -> bool:
    """All grant, constraint and verification intervals use [start, end)."""
    return (start is None or at >= start) and (end is None or at < end)


@dataclass
class Released:
    """A committed receipt plus bounded bytes; check immediately before first byte."""

    body: bytes
    decision: Decision
    clock: Clock
    access_receipt: str | None = None

    def emit(self, writer: Callable[[bytes], None]) -> None:
        self.decision.check(self.clock)
        writer(self.body)
