"""MCP resources.

Resources expose Kalshi state as URI-addressable data the model can read
without spending a tool call. A tool call carries an action implication
("the model decided to query this"); a resource read is "give me the
current value of X" with no side effect implication.

Currently registered:

    kalshi://environment   server env + safety config (no API call)
    kalshi://balance       cash balance + buying power
    kalshi://positions     open positions
    kalshi://orders        resting (open / partially filled) orders

As WebSocket support lands, market resources like
`kalshi://markets/{ticker}/orderbook` will be added here too, backed by
live streams when available and falling back to REST polling otherwise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

from kalshi_mcp_server.resources import account


def register_all_resources(server: FastMCP) -> None:
    """Register every resource module against the FastMCP server."""
    account.register(server)
