"""MCP tools.

Tools land here in subsequent commits. The shape is:

    from fastmcp import FastMCP

    def register(server: FastMCP) -> None:
        @server.tool
        def my_tool(...) -> ...:
            ...

`register_all_tools(server)` is called from cli.py during boot. Add new
tool modules by importing them in this file and invoking their `register`
function from `register_all_tools`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_all_tools(server: FastMCP) -> None:
    """Register every tool module against the FastMCP server.

    No tools are registered yet. As modules land under this package, add
    them here:

        from kalshi_mcp_server.tools import discovery
        discovery.register(server)
    """
    return None
