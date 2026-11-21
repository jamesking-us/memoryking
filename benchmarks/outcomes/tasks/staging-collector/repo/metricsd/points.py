"""Metric points."""

import time


def make_point(name, value, ts=None):
    """A point is a plain dict: name, numeric value, unix timestamp."""
    if not isinstance(name, str) or not name:
        raise ValueError("name must be a non-empty string")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("value must be a number")
    return {"name": name, "value": value, "ts": int(time.time() if ts is None else ts)}


def validate(point):
    return make_point(point["name"], point["value"], point["ts"])
