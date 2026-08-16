"""Portfolio tools — your own account's balance, positions, fills, orders.

These all hit /portfolio/* endpoints. Reads only — order placement lives
in `tools/orders.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(server: FastMCP) -> None:
    """Register portfolio read tools against the FastMCP server."""
    client = server._kalshi_client  # type: ignore[attr-defined]

    @server.tool
    async def kalshi_get_balance() -> dict[str, Any]:
        """Get the account's cash balance and total value.

        Kalshi returns: `balance` (available CASH, integer cents),
        `balance_dollars` (same, formatted), `portfolio_value`, `updated_ts`,
        and `balance_breakdown` (per-subaccount, if any).

        WATCH `portfolio_value`: it is the value of OPEN POSITIONS ONLY, not
        cash and not the account total, so it is legitimately 0 on a flat
        account. Do NOT read `portfolio_value == 0` as "account drained" — that
        is the single most common misread here. Key "is it funded / drained?"
        on `balance` (cash) or on `total_value` below.

        This MCP adds (computed, so no caller needs the tribal knowledge):
        `total_value` = balance + portfolio_value (integer cents, the real
        "what is this account worth"), and `total_value_dollars` (formatted).
        """
        body = await client.get("/portfolio/balance")
        # Compute a trustworthy total so downstream never has to hand-combine
        # cash + positions (or worse, alarm on portfolio_value==0). Additive
        # and defensive: if either field is missing/non-integer, skip rather
        # than fabricate a wrong total.
        cash = body.get("balance")
        positions = body.get("portfolio_value")
        if isinstance(cash, int) and isinstance(positions, int):
            total = cash + positions
            body["total_value"] = total
            body["total_value_dollars"] = f"{total / 100:.4f}"
        return body

    @server.tool
    async def kalshi_get_positions(
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
        cursor: str | None = None,
        ticker: str | None = None,
        event_ticker: str | None = None,
        settlement_status: Literal["all", "settled", "unsettled"] = "all",
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
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
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
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
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
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
        cursor: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
    ) -> dict[str, Any]:
        """List markets that have settled and the P&L impact on your account.

        Args:
            limit: 1-1000. Default 100.
            cursor: Pagination cursor. Kalshi silently returns an empty list
                on a bad cursor.
            min_ts: Lower bound on settlement timestamp (unix seconds).
            max_ts: Upper bound on settlement timestamp (unix seconds).
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        return await client.get("/portfolio/settlements", params=params)
