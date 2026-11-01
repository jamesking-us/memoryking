"""HTTP status for a LocalError code: conflicts, capacity and unavailability stay distinct.

Clients should retry 503 after a pause, slow down on 429, and resolve 409 by reconciling.
Anything else keeps the transport's default, usually 400 or 403.
"""

from __future__ import annotations

CONFLICT_SUFFIXES = ("_conflict", "_stale", "_changed", "_consumed", "_superseded")
CONFLICT_CODES = {"reconciliation_required", "operation_reconciliation_required"}
# 429 means "retry later": stored capacity that frees with time or retention, and the
# few limits that clear on their own. A fixed input bound (tool_input_limit,
# checkpoint_input_limit, ...) never clears, so it keeps the transport's 400.
CAPACITY_SUFFIXES = ("_capacity",)
CAPACITY_CODES = {
    "queue_full",
    "rate_limited",
    "concurrency_limit",
    "session_limit",
    "bootstrap_limit",
    "operation_limit",
    "usage_retention_limit",
}
UNAVAILABLE_CODES = {
    "recovery_required",
    "service_busy",
    "service_unavailable",
    "dashboard_unavailable",
    "journal_rollback",
    "owner_busy_or_unsafe",
    "index_not_ready",
    "provider_unavailable",
    "database_durability_required",
    "transport_unavailable",
}


def http_status(code: str, default: int) -> int:
    if code in UNAVAILABLE_CODES:
        return 503
    if code in CAPACITY_CODES or code.endswith(CAPACITY_SUFFIXES):
        return 429
    if code in CONFLICT_CODES or code.endswith(CONFLICT_SUFFIXES):
        return 409
    return default
