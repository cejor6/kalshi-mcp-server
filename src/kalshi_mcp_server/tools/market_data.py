"""Market data tools — orderbook + candlesticks.

These are the read-time endpoints an agent needs to actually price a
trade decision: live orderbook depth, and historical OHLC bars.

Both endpoints are read-bucket. The orderbook in particular updates fast
on liquid markets — for sustained polling, prefer the WebSocket
`orderbook_delta` channel when that lands (see resources/ in a future
commit).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(server: FastMCP) -> None:
    """Register market-data tools against the FastMCP server."""
    client = server._kalshi_client  # type: ignore[attr-defined]

    @server.tool
    async def kalshi_get_orderbook(
        ticker: str,
        depth: int = 10,
    ) -> dict[str, Any]:
        """Get the current orderbook for a market.

        Args:
            ticker: Market ticker, e.g. "KXFED-26MAR19-B5.25".
            depth: Number of price levels per side to return (default 10).

        Returns YES-side and NO-side levels with price (cents) and total
        contracts at each level. Remember Kalshi's two-sided book: YES at
        price X cents is equivalent to NO at (100 - X) cents.
        """
        params: dict[str, Any] = {"depth": depth}
        return await client.get(f"/markets/{ticker}/orderbook", params=params)

    @server.tool
    async def kalshi_get_market_candlesticks(
        ticker: str,
        series_ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 60,
    ) -> dict[str, Any]:
        """Get OHLC candles for a single market over a time window.

        Args:
            ticker: Market ticker.
            series_ticker: The series this market belongs to (Kalshi's
                candlestick endpoint requires both — series scopes the
                lookup, ticker filters the result).
            start_ts: Window start as unix seconds.
            end_ts: Window end as unix seconds.
            period_interval: Bar width in MINUTES. Common values: 1, 5,
                60, 240 (4h), 1440 (1d). Default 60 (hourly).

        Returns an array of candlesticks with open/high/low/close
        prices and per-bar volume.
        """
        params: dict[str, Any] = {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": period_interval,
        }
        return await client.get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params=params,
        )

    @server.tool
    async def kalshi_get_event_candlesticks(
        event_ticker: str,
        series_ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 60,
    ) -> dict[str, Any]:
        """Get OHLC candles aggregated across all markets in an event.

        Same arg shape as `kalshi_get_market_candlesticks`, but the
        candles describe the event-level series (commonly the
        ensemble/mean of nested markets).
        """
        params: dict[str, Any] = {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": period_interval,
        }
        return await client.get(
            f"/series/{series_ticker}/events/{event_ticker}/candlesticks",
            params=params,
        )

    @server.tool
    async def kalshi_get_market_trades(
        ticker: str,
        limit: int = 100,
        cursor: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
    ) -> dict[str, Any]:
        """List public trades for a single market.

        Distinct from `kalshi_get_trades` (which spans all markets) —
        this one is scoped to one ticker, which is what you usually want.

        Args:
            ticker: Market ticker.
            limit: 1-1000. Default 100.
            cursor: Pagination cursor.
            min_ts: Lower bound on trade ts (unix seconds).
            max_ts: Upper bound on trade ts (unix seconds).
        """
        params: dict[str, Any] = {"limit": limit, "ticker": ticker}
        if cursor:
            params["cursor"] = cursor
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        return await client.get("/markets/trades", params=params)
