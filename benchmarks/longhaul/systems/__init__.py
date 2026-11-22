"""Memory systems of the long-history benchmark: one module per arm, each with `create()`."""

from __future__ import annotations

import importlib


def load(name: str):
    """A new System for the arm called `name` (the module `systems/<name>.py`)."""
    if not name.replace("_", "").isalnum() or name.startswith("_") or name == "base":
        raise ValueError(f"not an arm name: {name!r}")
    module = importlib.import_module(f"{__name__}.{name}")
    return module.create()
