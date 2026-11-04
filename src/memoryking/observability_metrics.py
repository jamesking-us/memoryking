"""In-process request counters, rendered as Prometheus text for `/metrics`."""

from __future__ import annotations

import threading
from typing import Any

LATENCY_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)


class Metrics:
    """Counts and latencies per operation, errors per code; no content, no identities."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: dict[str, int] = {}
        self.errors: dict[str, int] = {}
        self.latency_sum_ms: dict[str, float] = {}
        self.latency_buckets: dict[str, list[int]] = {}

    def observe(self, operation: str, elapsed_ms: float, error: str | None) -> None:
        with self.lock:
            self.requests[operation] = self.requests.get(operation, 0) + 1
            if error is not None:
                self.errors[error] = self.errors.get(error, 0) + 1
            self.latency_sum_ms[operation] = self.latency_sum_ms.get(operation, 0.0) + elapsed_ms
            buckets = self.latency_buckets.setdefault(
                operation, [0] * (len(LATENCY_BUCKETS_MS) + 1)
            )
            for index, bound in enumerate(LATENCY_BUCKETS_MS):
                if elapsed_ms <= bound:
                    buckets[index] += 1
            buckets[-1] += 1

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "requests": dict(self.requests),
                "errors": dict(self.errors),
                "latency_sum_ms": dict(self.latency_sum_ms),
                "latency_buckets": {k: list(v) for k, v in self.latency_buckets.items()},
            }


def render(metrics: Metrics, status: dict[str, Any]) -> str:
    """Prometheus exposition text from the counters and a status result."""
    snap = metrics.snapshot()
    lines = [
        "# HELP memoryking_requests_total Requests handled, by operation.",
        "# TYPE memoryking_requests_total counter",
    ]
    for op, n in sorted(snap["requests"].items()):
        lines.append(f'memoryking_requests_total{{operation="{op}"}} {n}')
    lines += [
        "# HELP memoryking_errors_total Error replies, by code.",
        "# TYPE memoryking_errors_total counter",
    ]
    for code, n in sorted(snap["errors"].items()):
        lines.append(f'memoryking_errors_total{{code="{code}"}} {n}')
    lines += [
        "# HELP memoryking_request_duration_ms Request latency histogram, by operation.",
        "# TYPE memoryking_request_duration_ms histogram",
    ]
    for op, buckets in sorted(snap["latency_buckets"].items()):
        for bound, count in zip(LATENCY_BUCKETS_MS, buckets, strict=False):
            lines.append(
                f'memoryking_request_duration_ms_bucket{{operation="{op}",le="{bound}"}} {count}'
            )
        lines.append(
            f'memoryking_request_duration_ms_bucket{{operation="{op}",le="+Inf"}} {buckets[-1]}'
        )
        lines.append(
            f'memoryking_request_duration_ms_sum{{operation="{op}"}} {snap["latency_sum_ms"][op]:.3f}'
        )
        lines.append(f'memoryking_request_duration_ms_count{{operation="{op}"}} {buckets[-1]}')
    lines += [
        "# HELP memoryking_capacity_used Used amount of a bounded tenant resource.",
        "# TYPE memoryking_capacity_used gauge",
        "# HELP memoryking_capacity_limit Limit of a bounded tenant resource.",
        "# TYPE memoryking_capacity_limit gauge",
    ]
    for name, value in sorted(status.get("capacity", {}).items()):
        lines.append(f'memoryking_capacity_used{{resource="{name}"}} {value["used"]}')
        lines.append(f'memoryking_capacity_limit{{resource="{name}"}} {value["limit"]}')
    lines += [
        "# HELP memoryking_jobs Jobs by state.",
        "# TYPE memoryking_jobs gauge",
    ]
    for state, n in sorted(status.get("jobs", {}).items()):
        lines.append(f'memoryking_jobs{{state="{state}"}} {n}')
    projection = status.get("projection", {})
    lines += [
        "# HELP memoryking_position Latest event position.",
        "# TYPE memoryking_position gauge",
        f"memoryking_position {status.get('position', 0)}",
        "# HELP memoryking_frontier Projection frontier.",
        "# TYPE memoryking_frontier gauge",
        f"memoryking_frontier {projection.get('frontier', 0)}",
    ]
    return "\n".join(lines) + "\n"
