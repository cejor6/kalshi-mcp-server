"""MCP tools.

Each submodule under this package exposes a `register(server)` function
that wires its tools onto the FastMCP server via the `@server.tool`
decorator. `register_all_tools` is the single entry point called from
`cli.py` at startup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

from kalshi_mcp_server.tools import (
    discovery,
    exchange,
    live,
    market_data,
    orders,
    portfolio,
)


def register_all_tools(server: FastMCP) -> None:
    """Register every tool module against the FastMCP server.

    Add new modules here as they land.
    """
    exchange.register(server)
    discovery.register(server)
    market_data.register(server)
    portfolio.register(server)
    orders.register(server)
    live.register(server)
