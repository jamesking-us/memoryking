"""All-attempt token/cost accounting; unknown observations never become zero."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any

from memoryking.domain.local import LocalError

STAGES = frozenset(
    {
        "capture_extraction",
        "compression",
        "query_embedding",
        "embedding_backfill",
        "query_expansion",
        "reranking",
        "context_input",
        "agent_output",
        "handoff_reconstruction",
        "retry",
        "recovery",
    }
)
STATUSES = frozenset({"started", "succeeded", "failed", "unknown"})


@dataclass(frozen=True)
class UsageRecord:
    attempt: str
    provider: str
    fingerprint: str
    stage: str
    status: str
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    cost_microusd: int | None
    duration_ms: int

    @property
    def uncached_input_tokens(self) -> int | None:
        if self.input_tokens is None or self.cached_input_tokens is None:
            return None
        return self.input_tokens - self.cached_input_tokens

    def validate(self) -> None:
        if self.stage not in STAGES or self.status not in STATUSES:
            raise LocalError("usage_record_invalid")
        if any(
            not value or len(value) > 256
            for value in (self.attempt, self.provider, self.fingerprint)
        ):
            raise LocalError("usage_record_invalid")
        for value in (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.cost_microusd,
            self.duration_ms,
        ):
            if value is not None and (
                type(value) is not int or not 0 <= value <= 9_007_199_254_740_991
            ):
                raise LocalError("usage_record_invalid")
        if self.duration_ms is None or (
            self.input_tokens is not None
            and self.cached_input_tokens is not None
            and self.cached_input_tokens > self.input_tokens
        ):
            raise LocalError("usage_record_invalid")

    def document(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


def summarize(records: list[UsageRecord], successful_tasks: int) -> dict[str, Any]:
    """The caller supplies observed task outcomes; a successful API call is not a task.

    Input tokens include cached tokens. Failed calls, retries and background work
    remain in the numerator. Ratios stay exact integer pairs, not false precision.
    """
    if (
        type(successful_tasks) is not int
        or not 0 <= successful_tasks <= 9_007_199_254_740_991
        or len(records) > 100000
    ):
        raise LocalError("usage_cohort_invalid")
    seen: set[str] = set()
    groups: dict[tuple[str, str], list[UsageRecord]] = defaultdict(list)
    for record in records:
        record.validate()
        if record.attempt in seen:
            raise LocalError("usage_attempt_duplicate")
        seen.add(record.attempt)
        groups[(record.provider, record.fingerprint)].append(record)

    def total(items: list[UsageRecord], name: str) -> dict[str, int | None]:
        values = [getattr(record, name) for record in items]
        known = sum(value for value in values if value is not None)
        if known > 9_007_199_254_740_991:
            raise LocalError("usage_cohort_limit")
        unknown = sum(value is None for value in values)
        return {
            "total": None if unknown else known,
            "known_lower_bound": known,
            "unknown_attempts": unknown,
        }

    def aggregate(items: list[UsageRecord]) -> dict[str, Any]:
        inputs, outputs = total(items, "input_tokens"), total(items, "output_tokens")
        known = int(inputs["known_lower_bound"] or 0) + int(outputs["known_lower_bound"] or 0)
        if known > 9_007_199_254_740_991:
            raise LocalError("usage_cohort_limit")
        unknown = sum(
            record.input_tokens is None or record.output_tokens is None for record in items
        )
        return {
            "attempts": len(items),
            "statuses": {
                status: sum(i.status == status for i in items) for status in sorted(STATUSES)
            },
            "input_tokens": inputs,
            "cached_input_tokens": total(items, "cached_input_tokens"),
            "uncached_input_tokens": total(items, "uncached_input_tokens"),
            "output_tokens": outputs,
            "cost_microusd": total(items, "cost_microusd"),
            "total_tokens": None if unknown else known,
            "known_token_lower_bound": known,
            "unknown_token_attempts": unknown,
        }

    result = aggregate(records)
    result.update(
        scope="all_observed_attempts_including_failures_and_background",
        successful_tasks=successful_tasks,
        tokens_per_successful_task={
            "numerator": result["total_tokens"],
            "denominator": successful_tasks,
            "known_lower_bound_numerator": result["known_token_lower_bound"],
            "status": "undefined_no_success"
            if not successful_tasks
            else "unknown_usage"
            if result["unknown_token_attempts"]
            else "observed",
        },
        providers=[
            {
                "provider": provider,
                "fingerprint": model,
                **aggregate(items),
                "stages": {
                    stage: aggregate([i for i in items if i.stage == stage])
                    for stage in sorted({i.stage for i in items})
                },
            }
            for (provider, model), items in sorted(groups.items())
        ],
        transport_bytes="separate_metric_not_model_tokens",
    )
    return result
