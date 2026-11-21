"""Say what is wrong with a rejected JSON value, in terms a model or a person can act on.

`explain` names the first failing JSON path, what was expected (type, allowed values, a
pattern in words, a length or range) and, for a missing required field, its name. It never
echoes the offending value, only its type and size, so a secret or a large payload put in
the wrong field does not come back in an error. Property names are caller text, so they are
cut and limited to plain characters.
"""

from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, best_match

NAMED_PATTERNS = {
    "^[A-Za-z0-9_-]{1,96}$": "letters, digits, `_` or `-`, 1 to 96 characters",
    "^[A-Za-z0-9_-]{1,96}(?![\\s\\S])": "letters, digits, `_` or `-`, 1 to 96 characters",
}
ENUM_SHOWN = 12
NAME_LIMIT = 40


def _name(text: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "?", str(text)[:NAME_LIMIT])


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return f"string of {len(value)} characters"
    if isinstance(value, list):
        return f"array of {len(value)} items"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _path(parts: Any) -> str:
    out = "$"
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else "." + _name(part)
    return out


def _schema_words(schema: Any) -> str:
    """What a schema accepts, in a short phrase."""
    if not isinstance(schema, dict):
        return "a valid value"
    if "enum" in schema:
        shown = ", ".join(repr(v) for v in schema["enum"][:ENUM_SHOWN])
        more = len(schema["enum"]) - ENUM_SHOWN
        return "one of " + shown + (f" (and {more} more)" if more > 0 else "")
    if "const" in schema:
        return f"exactly {schema['const']!r}"
    options = schema.get("anyOf") or schema.get("oneOf")
    if options:
        return " or ".join(_schema_words(o) for o in options[:4])
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = " or ".join(kind)
    if kind == "string":
        words = "a string"
        if "pattern" in schema:
            words += ": " + NAMED_PATTERNS.get(schema["pattern"], "matching the documented pattern")
        low, high = schema.get("minLength"), schema.get("maxLength")
        if low is not None and high is not None:
            words += f" ({low} to {high} characters)"
        elif high is not None:
            words += f" (at most {high} characters)"
        elif low is not None:
            words += f" (at least {low} characters)"
        return words
    if kind in ("integer", "number"):
        low, high = schema.get("minimum"), schema.get("maximum")
        words = "an integer" if kind == "integer" else "a number"
        if low is not None and high is not None:
            return f"{words} from {low} to {high}"
        if high is not None:
            return f"{words} up to {high}"
        if low is not None:
            return f"{words} of at least {low}"
        return words
    if kind == "array":
        words = "an array"
        if "items" in schema and schema["items"]:
            words += " of " + _schema_words(schema["items"])
        low, high = schema.get("minItems"), schema.get("maxItems")
        if high is not None:
            words += f" (at most {high} items)"
        elif low is not None:
            words += f" (at least {low} items)"
        return words
    if kind == "object":
        return "an object"
    if kind == "boolean":
        return "true or false"
    if kind == "null":
        return "null"
    return "a valid value"


def _locate(error: ValidationError) -> dict[str, Any]:
    kind = error.validator
    parent = list(error.absolute_path)
    schema = error.schema if isinstance(error.schema, dict) else {}
    if kind == "required":
        missing = [r for r in error.validator_value if r not in error.instance]
        field = missing[0] if missing else "?"
        properties = schema.get("properties", {})
        return {
            "path": _path([*parent, field]),
            "problem": f"missing required field `{_name(field)}`",
            "expected": _schema_words(properties.get(field)),
            "missing": [_name(m) for m in missing[:8]],
        }
    if kind == "additionalProperties":
        known = set(schema.get("properties", {}))
        extra = sorted(k for k in error.instance if k not in known)
        field = extra[0] if extra else "?"
        allowed = ", ".join(sorted(known)[:24])
        return {
            "path": _path([*parent, field]),
            "problem": f"unknown property `{_name(field)}`",
            "expected": f"only these properties: {allowed}" if allowed else "no properties",
        }
    problem = {
        "type": f"wrong type: got {_kind(error.instance)}",
        "enum": f"not an allowed value (got {_kind(error.instance)})",
        "const": f"not the required value (got {_kind(error.instance)})",
        "pattern": f"wrong format (got {_kind(error.instance)})",
        "minLength": f"too short (got {_kind(error.instance)})",
        "maxLength": f"too long (got {_kind(error.instance)})",
        "minimum": "below the minimum",
        "maximum": "above the maximum",
        "minItems": f"too few items (got {_kind(error.instance)})",
        "maxItems": f"too many items (got {_kind(error.instance)})",
        "uniqueItems": "items must be unique",
        "anyOf": f"matches none of the allowed shapes (got {_kind(error.instance)})",
        "oneOf": f"matches none of the allowed shapes (got {_kind(error.instance)})",
    }.get(str(kind), "does not match the schema")
    return {
        "path": _path(parent),
        "problem": problem,
        "expected": _schema_words(schema),
    }


def _branch(error: ValidationError) -> ValidationError:
    """Descend a oneOf/anyOf failure into the branch the caller meant.

    The branch with the fewest problems is the one whose discriminator (such as `action`)
    matched, so its own error names the field to fix instead of "matches none of"."""
    while error.validator in ("oneOf", "anyOf") and error.context:
        groups: dict[Any, list[ValidationError]] = {}
        for sub in error.context:
            groups.setdefault(sub.schema_path[0] if sub.schema_path else 0, []).append(sub)
        chosen = min(groups.values(), key=len)
        error = best_match(chosen) or chosen[0]
    return error


def explain(validator: Draft202012Validator, value: Any) -> dict[str, Any] | None:
    """The first problem with `value`, or None when it is valid."""
    error = best_match(validator.iter_errors(value))
    if error is None:
        return None
    return _locate(_branch(error))


def sentence(found: dict[str, Any]) -> str:
    """One line for a human reader of the same problem."""
    return f"{found['path']}: {found['problem']}; expected {found['expected']}."
