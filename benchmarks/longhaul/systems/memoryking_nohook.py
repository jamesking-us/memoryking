"""Variant arm `memoryking_nohook`: the `memoryking` arm without the prompt-time recall hook (the
three session hooks and the lean bridge only, as the pilot ran). Not in the default arm list."""

from __future__ import annotations

from .memoryking import create_nohook


def create():
    return create_nohook()
