"""Portfolio tools — your own account's balance, positions, fills, orders.

These all hit /portfolio/* endpoints. Reads only — order placement lives
in `tools/orders.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(server: FastMCP) -> None:
    """Register portfolio read tools against the FastMCP server."""
    client = server._kalshi_client  # type: ignore[attr-defined]

    @server.tool
    async def kalshi_get_balance() -> dict[str, Any]:
        """Get the current cash balance and unrealized P&L.

        Returns a dict with both raw and pre-formatted values:
            balance: integer cents (e.g. 12500 = $125.00)
            balance_dollars: string-formatted USD (e.g. "125.0000")
            portfolio_value: integer cents — total cash + position value
            balance_breakdown: per-subaccount breakdown if applicable

        Prefer `balance_dollars` for display — it's already formatted.
        """
        return await client.get("/portfolio/balance")

    @server.tool
    async def kalshi_get_positions(
        limit: int = 200,
        cursor: str | None = None,
        ticker: str | None = None,
        event_ticker: str | None = None,
        settlement_status: str = "all",
        count_filter: str | None = None,
    ) -> dict[str, Any]:
        """List your positions.

        Returns BOTH:
            market_positions: per-market position details (ticker, count,
                avg fill price, realized/unrealized P&L)
            event_positions: position summaries grouped by event
            cursor: pagination cursor

        Args:
            limit: 1-1000. Default 200.
            cursor: Pagination cursor. Kalshi silently returns an empty
                list on bad cursors — verify carefully if you expected
                results.
            ticker: Restrict to a specific market ticker.
            event_ticker: Restrict to positions in a specific event.
            settlement_status: "all" (default), "settled", or "unsettled".
            count_filter: Comma-separated filter: "position", "total_traded",
                "resting_order_count". Useful to ignore stale 0-position rows.
        """
        params: dict[str, Any] = {
            "limit": limit,
            "settlement_status": settlement_status,
        }
        if cursor:
            params["cursor"] = cursor
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if count_filter:
            params["count_filter"] = count_filter
        return await client.get("/portfolio/positions", params=params)

    @server.tool
    async def kalshi_get_orders(
        limit: int = 100,
        cursor: str | None = None,
        ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
    ) -> dict[str, Any]:
        """List your orders (open, filled, cancelled, etc.).

        Args:
            limit: 1-1000. Default 100.
            cursor: Pagination cursor.
            ticker: Restrict to a specific market.
            event_ticker: Restrict to a specific event.
            status: "resting" (open / partially-filled), "canceled",
                "executed". Multiple OK with comma-separated values.
            min_ts: Lower bound on order creation ts (unix seconds).
            max_ts: Upper bound on order creation ts (unix seconds).
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        return await client.get("/portfolio/orders", params=params)

    @server.tool
    async def kalshi_get_fills(
        limit: int = 100,
        cursor: str | None = None,
        ticker: str | None = None,
        order_id: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
    ) -> dict[str, Any]:
        """List your fills (executions against your orders).

        Args:
            limit: 1-1000. Default 100.
            cursor: Pagination cursor.
            ticker: Restrict to a specific market.
            order_id: Restrict to fills against a specific order.
            min_ts: Lower bound on fill ts (unix seconds).
            max_ts: Upper bound on fill ts (unix seconds).
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if ticker:
            params["ticker"] = ticker
        if order_id:
            params["order_id"] = order_id
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        return await client.get("/portfolio/fills", params=params)

    @server.tool
    async def kalshi_get_settlements(
        limit: int = 100,
        cursor: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
    ) -> dict[str, Any]:
        """List markets that have settled and the P&L impact on your account."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        return await client.get("/portfolio/settlements", params=params)
