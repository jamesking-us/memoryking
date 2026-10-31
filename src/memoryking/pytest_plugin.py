"""Record failing tests as observations: `pytest -p memoryking.pytest_plugin`.

Enable with `MEMORYKING_PROFILE` (or `MEMORYKING_SOCKET` and `MEMORYKING_CREDENTIAL`) and
`MEMORYKING_PROJECT` in the environment; without them the plugin stays silent. Each
failure captures the test id and the tail of its report under source `pytest`, with an
event id derived from the test id and message so a rerun replays rather than duplicates.
The plugin never fails the test run: capture errors are reported once to stderr.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from memoryking import hooks
from memoryking.domain.local import LocalError

REPORT_TAIL = 4000
_state = {"announced": False, "captured": 0}


def enabled() -> bool:
    env = os.environ
    return bool(env.get("MEMORYKING_PROJECT")) and bool(
        env.get("MEMORYKING_PROFILE")
        or (env.get("MEMORYKING_SOCKET") and env.get("MEMORYKING_CREDENTIAL"))
    )


def summarize(report: Any) -> str:
    """A bounded, readable failure text from a pytest report-like object."""
    longrepr = getattr(report, "longreprtext", None) or str(getattr(report, "longrepr", "") or "")
    tail = longrepr[-REPORT_TAIL:]
    when = getattr(report, "when", "call")
    return f"pytest {when} failed: {report.nodeid}\n{tail}".strip()


def record_failure(report: Any) -> dict[str, Any] | None:
    """Capture one failed report; returns the capture result or None when disabled."""
    if not enabled() or not getattr(report, "failed", False):
        return None
    text = summarize(report)
    try:
        result = hooks.capture(
            text,
            source="pytest",
            event=hooks.event_id("pytest", report.nodeid, text.splitlines()[-1] if text else ""),
        )
    except (LocalError, OSError) as exc:
        if not _state["announced"]:
            _state["announced"] = True
            code = exc.code if isinstance(exc, LocalError) else type(exc).__name__
            print(f"memoryking: capture disabled for this run ({code})", file=sys.stderr)
        return None
    _state["captured"] += 1
    return result


def pytest_runtest_logreport(report: Any) -> None:  # pytest hook
    record_failure(report)


def pytest_terminal_summary(terminalreporter: Any) -> None:  # pytest hook
    if _state["captured"]:
        terminalreporter.write_line(
            f"memoryking: {_state['captured']} failing test(s) captured as observations"
        )
