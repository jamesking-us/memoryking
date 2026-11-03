"""Read-only owner plan route shared by SQLite and PostgreSQL dashboards."""

from typing import Any
from urllib.parse import parse_qsl, urlsplit

from memoryking.adapters.local_store import identifier
from memoryking.adapters.plans import Plans
from memoryking.adapters.working import Working
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError


def read(store: Any, credential: str, target: str, at: int) -> dict[str, Any]:
    url = urlsplit(target)
    parts = url.path.split("/")
    if len(parts) not in {4, 5} or parts[:3] != ["", "api", "plans"]:
        raise LocalError("invalid_request")
    project = identifier(parts[3])
    pairs = parse_qsl(url.query, keep_blank_values=True)
    query = dict(pairs)
    if len(query) != len(pairs) or set(query) - {"cursor"} or (len(parts) == 5 and query):
        raise LocalError("invalid_request")
    if len(query.get("cursor", "")) > 8192:
        raise LocalError("invalid_request")
    who = store._auth(credential, "read", project, at)
    plans = Plans(Working(store))
    result = {
        "schema_version": 1,
        "project": project,
        "incarnation": store.incarnation,
        "tenant": who["tenant"],
        "principal": who["principal"],
        "fresh_until_ms": at + 15000,
    }
    if len(parts) == 5:
        result.update(
            kind="plan",
            detail=plans.get(
                who, {"project": project, "task": identifier(parts[4]), "revision": None}, at
            ),
        )
    else:
        result.update(
            kind="plans",
            **plans.listing(
                who, {"project": project, "cursor": query.get("cursor"), "limit": 10}, at
            ),
        )
    if len(canonical_bytes(result)) > 262144:
        raise LocalError("response_limit")
    return result
