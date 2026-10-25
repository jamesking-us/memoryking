"""Backend-neutral scenario harness. Missing adapters are never passing evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ScenarioAdapter(Protocol):
    """Adapters must reset isolated state and execute real operations before asserting."""

    name: str
    supported_scenarios: frozenset[str]

    def reset(self, seed: dict[str, Any]) -> None: ...

    def execute(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    backend: str
    status: str
    assertions: int
    reason: str


def run_scenario(case: dict[str, Any], adapter: ScenarioAdapter | None) -> ScenarioResult:
    identifier = str(case["id"])
    backend = adapter.name if adapter else "none"
    if adapter is None or identifier not in adapter.supported_scenarios:
        return ScenarioResult(identifier, backend, "not_evaluated", 0, "adapter_not_implemented")
    checks = 0
    try:
        adapter.reset(case["seed"])
        for step in case["steps"]:
            observed = adapter.execute(step["operation"], step["arguments"])
            for key, expected in step["expect"].items():
                checks += 1
                if key not in observed or observed[key] != expected:
                    return ScenarioResult(
                        identifier, backend, "failed", checks, "assertion_mismatch"
                    )
        if checks == 0:
            return ScenarioResult(identifier, backend, "failed", 0, "no_assertions")
        return ScenarioResult(identifier, backend, "passed", checks, "observed")
    except Exception:
        # Evidence logs must be explicitly sanitized; exception text may contain source content.
        return ScenarioResult(identifier, backend, "failed", checks, "adapter_error")
