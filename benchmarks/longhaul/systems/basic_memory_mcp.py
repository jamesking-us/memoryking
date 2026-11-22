"""Arm `basic_memory_mcp`: the MCP server alone, without the Claude Code plugin or hooks.

See `basic_memory.py` and SYSTEMS.md.
"""

from . import basic_memory

installed = basic_memory.installed


def create():
    return basic_memory.make_mcp_only()
