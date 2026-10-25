"""Bounded selector v1. Three-valued evaluation and conservative region proofs.

No filesystem, database, regular-expression predicate or caller-supplied code executes.
Ancestry evidence is an internal port; request bindings cannot assert completeness.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError

Truth = Literal["true", "false", "unknown"]
Relation = Literal["proven", "disproven", "indeterminate"]
FIELDS = {"repository_id", "component_id", "symbol_id", "environment", "task_stage", "role"}
Selector = dict[str, Any]
Atom = dict[str, Any]


@dataclass
class Work:
    remaining: int = 4096

    def take(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise LocalError("selector_limit_exceeded")


def path_valid(value: Any, *, root: bool = False) -> bool:
    return isinstance(value, str) and (
        (value == "" and root)
        or (
            0 < len(value.encode()) <= 256
            and len(value.split("/")) <= 32
            and all(x not in {"", ".", ".."} for x in value.split("/"))
            and not any(c in value for c in "\\\x00*?[]")
        )
    )


def triplet(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and all(type(x) is int and 0 <= x <= 2147483647 for x in value)
    )


def _key(atom: Atom) -> tuple[str, str]:
    return atom.get("field", "any"), atom.get("dependency", "")


def _under(path: str, prefix: str) -> bool:
    return bool(path) if prefix == "" else path.startswith(prefix + "/")


def _atom(atom: Any) -> Atom:
    if atom == {"op": "any"}:
        return dict(atom)
    if not isinstance(atom, dict) or not {"field", "op", "value"} <= atom.keys():
        raise LocalError("selector_unsupported")
    field, op, value = atom["field"], atom["op"], atom["value"]
    if not isinstance(field, str) or not isinstance(op, str):
        raise LocalError("selector_unsupported")
    expected = {"field", "op", "value"}
    if op == "version_in":
        expected |= {"dependency", "scheme"}
    if set(atom) != expected:
        raise LocalError("selector_unsupported")
    result = dict(atom)
    if field in FIELDS and op in {"eq", "in"}:
        values = [value] if op == "eq" else value
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 32
            or not all(isinstance(v, str) and 0 < len(v.encode()) <= 256 for v in values)
        ):
            raise LocalError("selector_unsupported")
        result.update(op="in", value=sorted(set(values)))
    elif field == "path" and op in {"path_exact", "path_under"}:
        if not path_valid(value, root=op == "path_under"):
            raise LocalError("selector_unsupported")
    elif field == "dependency_version" and op == "version_in":
        if (
            atom["scheme"] != "numeric_triplet_v1"
            or not isinstance(atom["dependency"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", atom["dependency"])
            or not isinstance(value, dict)
            or set(value) != {"lower", "upper"}
            or not triplet(value["lower"])
            or (
                value["upper"] is not None
                and (not triplet(value["upper"]) or value["lower"] >= value["upper"])
            )
        ):
            raise LocalError("selector_unsupported")
    elif field == "commit" and op in {"commit_is", "descendant_of"}:
        if not isinstance(value, str) or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value):
            raise LocalError("selector_unsupported")
    else:
        raise LocalError("selector_unsupported")
    return result


def _subset(a: Atom, b: Atom, work: Work) -> Relation:
    work.take()
    if b["op"] == "any" or a == b:
        return "proven"
    if a["op"] == "any" or _key(a) != _key(b):
        return "indeterminate"
    av, bv = a["value"], b["value"]
    if a["op"] == b["op"] == "in":
        return "proven" if set(av) <= set(bv) else "disproven"
    if a["op"] == b["op"] == "version_in":
        inside = av["lower"] >= bv["lower"] and (
            bv["upper"] is None or (av["upper"] is not None and av["upper"] <= bv["upper"])
        )
        return "proven" if inside else "disproven"
    if a["field"] == "path":
        if b["op"] == "path_exact":
            return "proven" if a["op"] == "path_exact" and av == bv else "disproven"
        inside = _under(av, bv) or (a["op"] == "path_under" and av == bv)
        return "proven" if inside else "disproven"
    if a["op"] == "commit_is":
        if av == bv:
            return "proven"
        return "disproven" if b["op"] == "commit_is" else "indeterminate"
    return "indeterminate"


def _merge(a: Atom, b: Atom, work: Work) -> list[Atom] | None:
    work.take()
    if a["op"] == "any":
        return [b]
    if b["op"] == "any" or a == b:
        return [a]
    if _key(a) != _key(b):
        return [a, b]
    av, bv = a["value"], b["value"]
    if a["op"] == b["op"] == "in":
        overlap = sorted(set(av) & set(bv))
        return [{**a, "value": overlap}] if overlap else None
    if a["op"] == b["op"] == "version_in":
        lower = max(av["lower"], bv["lower"])
        ends = [v for v in (av["upper"], bv["upper"]) if v is not None]
        upper = min(ends) if ends else None
        return (
            [{**a, "value": {"lower": lower, "upper": upper}}]
            if upper is None or lower < upper
            else None
        )
    if a["field"] == "path":
        if _subset(a, b, work) == "proven":
            return [a]
        if _subset(b, a, work) == "proven":
            return [b]
        return None
    if a["op"] == b["op"] == "commit_is" and av != bv:
        return None
    if av == bv:
        return [a if a["op"] == "commit_is" else b]
    # A merge commit can descend from incomparable roots; retain both.
    return [a, b]


def _clause(atoms: list[Atom], work: Work) -> list[Atom] | None:
    result: list[Atom] = []
    for atom in atoms:
        keep = atom
        others = []
        for existing in result:
            merged = _merge(existing, keep, work)
            if merged is None:
                return None
            if len(merged) == 1:
                keep = merged[0]
            else:
                others.append(existing)
        result = others + [keep]
    result.sort(key=lambda x: canonical_bytes(x))
    if len(result) > 8:
        raise LocalError("selector_limit_exceeded")
    return result


def _output(clauses: list[list[Atom]]) -> Selector | None:
    unique = {canonical_bytes(c): c for c in clauses}
    if not unique:
        return None
    if len(unique) > 16:
        raise LocalError("selector_limit_exceeded")
    result = {"selector_version": "1", "any_of": [{"all_of": unique[k]} for k in sorted(unique)]}
    if len(canonical_bytes(result)) > 16384:
        raise LocalError("selector_limit_exceeded")
    return result


def canonical(selector: Any, work: Work | None = None) -> Selector:
    work = work or Work()
    if (
        not isinstance(selector, dict)
        or set(selector) != {"selector_version", "any_of"}
        or selector["selector_version"] != "1"
    ):
        raise LocalError("selector_unsupported")
    if len(canonical_bytes(selector)) > 16384:
        raise LocalError("selector_limit_exceeded")
    clauses = selector["any_of"]
    if not isinstance(clauses, list) or not 1 <= len(clauses) <= 16:
        raise LocalError("selector_limit_exceeded")
    output = []
    for clause in clauses:
        if not isinstance(clause, dict) or set(clause) != {"all_of"}:
            raise LocalError("selector_unsupported")
        atoms = clause["all_of"]
        if not isinstance(atoms, list) or not 1 <= len(atoms) <= 8:
            raise LocalError("selector_limit_exceeded")
        parsed = [_atom(a) for a in atoms]
        if any(a["op"] == "any" for a in parsed) and len(parsed) != 1:
            raise LocalError("selector_unsupported")
        if any(a.get("field") in {"path", "commit"} for a in parsed) and not any(
            a.get("field") == "repository_id" and len(a["value"]) == 1 for a in parsed
        ):
            raise LocalError("selector_unsupported")
        simplified = _clause(parsed, work)
        if simplified is not None:
            output.append(simplified)
    normalized = _output(output)
    if normalized is None:
        raise LocalError("selector_empty")
    return normalized


def intersection(left: Selector, right: Selector, work: Work | None = None) -> Selector | None:
    work = work or Work()
    a, b = canonical(left, work), canonical(right, work)
    clauses = []
    for ac in a["any_of"]:
        for bc in b["any_of"]:
            result = _clause(ac["all_of"] + bc["all_of"], work)
            if result is not None:
                clauses.append(result)
    return _output(clauses)


def contains(region: Selector, container: Selector, work: Work | None = None) -> Relation:
    """Prove region is contained in container; no arbitrary theorem proving."""
    work = work or Work()
    a, b = canonical(region, work), canonical(container, work)
    unknown = False
    for ac in a["any_of"]:
        clause_proved = False
        for bc in b["any_of"]:
            if all(
                any(_subset(x, y, work) == "proven" for x in ac["all_of"]) for y in bc["all_of"]
            ):
                clause_proved = True
                break
        if not clause_proved:
            if len(b["any_of"]) == 1:
                # Canonical single-dimension counterexamples are exact. Missing
                # dimensions and ancestry remain indeterminate.
                for y in b["any_of"][0]["all_of"]:
                    relevant = [x for x in ac["all_of"] if _key(x) == _key(y)]
                    if len(relevant) == 1 and _subset(relevant[0], y, work) == "disproven":
                        return "disproven"
            unknown = True
    return "indeterminate" if unknown else "proven"


def _truth(values: list[Truth], *, conjunction: bool) -> Truth:
    decisive: Truth = "false" if conjunction else "true"
    if decisive in values:
        return decisive
    if "unknown" in values:
        return "unknown"
    return "true" if conjunction else "false"


def evaluate(
    selector: Selector,
    binding: dict[str, Any],
    ancestry: Callable[[str, str, str], Truth] | None = None,
) -> Truth:
    def atom(a: Atom) -> Truth:
        op = a["op"]
        if op == "any":
            return "true"
        field, value = a["field"], a["value"]
        observed = binding.get(field)
        if op == "version_in":
            dependencies = binding.get("dependencies", {})
            if not isinstance(dependencies, dict) or a["dependency"] not in dependencies:
                return "unknown"
            observed = dependencies[a["dependency"]]
            if observed is None:
                return "false"
            if not triplet(observed):
                return "unknown"
            matches = observed >= value["lower"] and (
                value["upper"] is None or observed < value["upper"]
            )
        elif not isinstance(observed, str):
            return "unknown"
        elif op == "in":
            matches = observed in value
        elif op in {"path_exact", "path_under"}:
            if not path_valid(observed):
                return "unknown"
            matches = observed == value if op == "path_exact" else _under(observed, value)
        elif op == "commit_is":
            if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", observed):
                return "unknown"
            matches = observed == value
        else:
            if observed == value:
                return "true"
            repository = binding.get("repository_id")
            return (
                ancestry(repository, value, observed)
                if ancestry and isinstance(repository, str)
                else "unknown"
            )
        return "true" if matches else "false"

    normalized = canonical(selector)
    return _truth(
        [_truth([atom(a) for a in c["all_of"]], conjunction=True) for c in normalized["any_of"]],
        conjunction=False,
    )


def bindings(value: Any) -> list[dict[str, Any]]:
    allowed = FIELDS | {"path", "commit", "dependencies", "snapshot_id"}
    if not isinstance(value, list) or not 1 <= len(value) <= 256:
        raise LocalError("binding_coverage_incomplete")
    if any(not isinstance(b, dict) or not set(b) <= allowed for b in value):
        raise LocalError("invalid_binding")
    if any(
        any(
            v is not None and (not isinstance(v, str) or len(v.encode()) > 256)
            for k, v in b.items()
            if k != "dependencies"
        )
        for b in value
    ):
        raise LocalError("invalid_binding")
    # Copy prevents mutation during evaluation and enforces the canonical byte bound.
    if len(canonical_bytes(value)) > 131072:
        raise LocalError("binding_coverage_incomplete")
    return json.loads(json.dumps(value))  # type: ignore[no-any-return]
