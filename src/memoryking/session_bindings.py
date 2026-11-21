"""Session bindings: where a simple caller is working, supplied once instead of per call.

A constraint can apply only to an environment, role, task stage, repository, path or
commit. Evaluating it needs those facts; without them its applicability is unknown, and
an unknown essential constraint blocks a briefing. The lean tools, the CLI and the hooks
take no per-call bindings, so a session states them once:

* `--binding key=value`, repeatable, on the stdio bridge and the commands that brief or resume;
* `MEMORYKING_BINDINGS`: comma-separated `key=value` pairs, or a JSON object when a value
  contains a comma (`environment=prod,role=implement`);
* `bindings = { environment = "prod" }` in a named store profile.

Later sources win key by key: profile, then environment, then flags. Keys are exactly the
selector vocabulary (`domain.selectors.BINDING_KEYS`); `dependencies` is a map and is
supplied per call by full-profile clients, not here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from memoryking.domain import selectors
from memoryking.domain.local import LocalError

ENVIRONMENT = "MEMORYKING_BINDINGS"
KEYS = tuple(sorted(selectors.BINDING_KEYS - {"dependencies"}))


def validate(values: Any) -> dict[str, str]:
    """One binding of non-empty strings under known keys, checked by the selector module."""
    if not isinstance(values, Mapping) or any(
        not isinstance(k, str) or not isinstance(v, str) or not v for k, v in values.items()
    ):
        raise LocalError("invalid_binding")
    clean = dict(values)
    if set(clean) - set(KEYS):
        raise LocalError("invalid_binding")
    selectors.bindings([clean])
    return clean


def parse_pairs(items: Iterable[str]) -> dict[str, str]:
    """`key=value` strings, as repeated `--binding` flags give them."""
    result: dict[str, str] = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise LocalError("invalid_binding")
        result[key.strip()] = value.strip()
    return validate(result)


def parse_environment(text: str) -> dict[str, str]:
    """The `MEMORYKING_BINDINGS` value: `k=v,k=v`, or a JSON object."""
    text = text.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            return validate(json.loads(text))
        except ValueError as exc:
            raise LocalError("invalid_binding") from exc
    return parse_pairs(part for part in text.split(",") if part.strip())


def resolve(
    flags: Iterable[str] | None = None,
    *,
    profile: str | None = None,
    config: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """The binding list a session sends: profile, then environment, then flags, key by key."""
    from memoryking import profiles

    environ = os.environ if environ is None else environ
    name = profile or environ.get("MEMORYKING_PROFILE")
    merged: dict[str, str] = {}
    if name:
        merged.update(profiles.resolve(name, config).get("bindings", {}))
    merged.update(parse_environment(environ.get(ENVIRONMENT, "")))
    merged.update(parse_pairs(flags or []))
    return [merged]
