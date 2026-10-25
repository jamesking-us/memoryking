"""Wire dispatch port; transports do not own policy or database transactions."""

from typing import Any, Protocol

from memoryking.domain.local import Released


class LocalOperations(Protocol):
    def execute(self, request: dict[str, Any]) -> dict[str, Any] | Released: ...
