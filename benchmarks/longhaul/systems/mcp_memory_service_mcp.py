"""Arm `mcp_memory_service_mcp`: the MCP server alone, without the Claude Code plugin or hooks.

See `mcp_memory_service.py` and SYSTEMS.md.
"""

from . import mcp_memory_service

installed = mcp_memory_service.installed


def create():
    return mcp_memory_service.make_mcp_only()
