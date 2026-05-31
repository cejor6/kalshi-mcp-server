"""Exchange-level and account-level read tools.

These wrap endpoints that don't fit "markets / portfolio / orders" —
they're system info that's useful for an agent to ground itself
before doing anything else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(server: FastMCP) -> None:
    """Register exchange + account tools against the FastMCP server."""
    client = server._kalshi_client  # type: ignore[attr-defined]
    config = server._kalshi_config  # type: ignore[attr-defined]

    @server.tool
    async def kalshi_get_exchange_status() -> dict[str, Any]:
        """Get current Kalshi exchange status (open/closed, trading hours).

        Use this as a sanity check before placing orders — Kalshi closes
        outside of market hours and during scheduled maintenance windows.
        """
        return await client.get("/exchange/status")

    @server.tool
    async def kalshi_get_exchange_schedule() -> dict[str, Any]:
        """Get the daily/weekly trading hours schedule for the exchange."""
        return await client.get("/exchange/schedule")

    @server.tool
    async def kalshi_get_api_limits() -> dict[str, Any]:
        """Get your account's current API rate-limit tier and remaining headroom.

        Returns Kalshi's view of your tier name, read/write bucket
        capacity, and refill rate. The MCP server uses Basic-tier defaults
        until this is called — invoking this tool periodically (or once
        at session start) lets the rate limiter be tuned to your real
        budget.
        """
        return await client.get("/account/limits")

    @server.tool
    async def kalshi_get_environment() -> dict[str, Any]:
        """Show which Kalshi environment this MCP server is connected to.

        Returns: env (`demo` or `prod`), trading_enabled flag, and the
        REST base URL. Useful for the agent to confirm whether a trade
        would hit real money before placing one.
        """
        return {
            "env": config.env,
            "trading_enabled": config.trading_enabled,
            "rest_base": config.rest_base,
            "ws_url": config.ws_url,
            "safety_limits": {
                "max_order_size_usd": config.max_order_size_usd,
                "daily_limit_usd": config.daily_limit_usd,
                "max_contracts_per_order": config.max_contracts_per_order,
                "cash_reserve_usd": config.cash_reserve_usd,
            },
        }
