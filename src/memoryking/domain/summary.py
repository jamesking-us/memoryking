"""Deterministic "where this left off" summaries from a checkpoint and recent activity.

The extractive summary quotes only the checkpoint body, which its readers may already see,
and counts or names recent observations and open operations without quoting them. It
is a convenience for a returning agent or an operator, never new evidence: every fact
in it is in the checkpoint, and the checkpoint stays the record.
"""

from __future__ import annotations

from typing import Any

SUMMARY_LIMIT = 1200
METHOD = "extractive-v1"


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return (cut or text[: limit - 1]) + "…"


def _sentence(text: str) -> str:
    text = _clip(text, 320).rstrip()
    return text if text.endswith((".", "!", "?", "…")) else text + "."


def compose(
    checkpoint: dict[str, Any],
    observations: list[dict[str, Any]],
    operations: list[dict[str, Any]],
) -> str:
    """Three sentences: where it stands, what was learned or tried, what comes next."""
    objective = _clip(checkpoint.get("objective") or "the task", 160)
    state = checkpoint.get("current_state") or ""
    first = f"Working on {objective}: {state}" if state else f"Working on {objective}."
    parts: list[str] = []
    findings = checkpoint.get("findings") or []
    failed = checkpoint.get("failed_attempts") or []
    verification = checkpoint.get("verification") or []
    if findings:
        latest = findings[-1].get("text", "")
        parts.append(
            f"{len(findings)} finding{'s' if len(findings) != 1 else ''}, most recently: "
            + _clip(latest, 200)
        )
    if failed:
        latest = failed[-1]
        parts.append(
            f"{len(failed)} failed attempt{'s' if len(failed) != 1 else ''}, do not repeat "
            + _clip(latest.get("action", ""), 120)
            + (f" ({_clip(latest.get('outcome', ''), 120)})" if latest.get("outcome") else "")
        )
    pending = [v for v in verification if v.get("result") not in {"passed", "pass"}]
    if verification:
        parts.append(
            f"{len(verification) - len(pending)} of {len(verification)} verifications passed"
        )
    if observations:
        sources = sorted({str(o.get("source", "unknown")) for o in observations})
        parts.append(
            f"{len(observations)} observation{'s' if len(observations) != 1 else ''} recorded "
            f"since the checkpoint (from {', '.join(sources[:4])}"
            + (", …" if len(sources) > 4 else "")
            + ")"
        )
    second = _sentence("; ".join(parts)) if parts else "Nothing has been tried or recorded yet."
    next_actions = checkpoint.get("next_actions") or []
    blockers = checkpoint.get("blockers") or []
    open_ops = [o for o in operations if o.get("reconciliation_required")]
    tail: list[str] = []
    if next_actions:
        tail.append("Next: " + _clip(next_actions[0], 240))
    if blockers:
        tail.append(
            f"{len(blockers)} blocker{'s' if len(blockers) != 1 else ''}: "
            + _clip(blockers[0].get("text", ""), 160)
        )
    if open_ops:
        tail.append(
            f"{len(open_ops)} uncertain operation{'s' if len(open_ops) != 1 else ''} to reconcile before repeating"
        )
    if pending:
        tail.append(f"{len(pending)} verification{'s' if len(pending) != 1 else ''} still pending")
    third = _sentence("; ".join(tail)) if tail else "No next action was recorded."
    return _clip(" ".join((_sentence(first), second, third)), SUMMARY_LIMIT)
