"""Market / event / series discovery tools.

These are the read-only endpoints an agent uses to find what to trade
on Kalshi: markets (the actual contracts), events (groups of related
markets), and series (the schedule/template a recurring event follows).

All tools here debit the READ bucket of the rate limiter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(server: FastMCP) -> None:
    """Register discovery tools against the FastMCP server."""
    client = server._kalshi_client  # type: ignore[attr-defined]

    @server.tool
    async def kalshi_get_markets(
        limit: int = 50,
        cursor: str | None = None,
        status: str | None = None,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        tickers: str | None = None,
        min_close_ts: int | None = None,
        max_close_ts: int | None = None,
    ) -> dict[str, Any]:
        """List Kalshi markets with optional filters.

        Args:
            limit: 1-1000. Default 50.
            cursor: Pagination cursor from a previous response.
            status: Filter by lifecycle: "unopened", "open", "closed",
                "settled". Multiple OK with comma-separated values.
            event_ticker: Return only markets in a specific event.
            series_ticker: Return only markets in a specific series.
            tickers: Comma-separated list of market tickers to fetch.
            min_close_ts: Filter to markets closing on/after this unix ts.
            max_close_ts: Filter to markets closing on/before this unix ts.

        Returns a list of markets (each with ticker, prices, volume,
        open/close timestamps, settlement value if settled) plus a
        `cursor` for pagination.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if tickers:
            params["tickers"] = tickers
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts
        if max_close_ts is not None:
            params["max_close_ts"] = max_close_ts
        return await client.get("/markets", params=params)

    @server.tool
    async def kalshi_get_market(ticker: str) -> dict[str, Any]:
        """Fetch a single market by ticker.

        Args:
            ticker: Full market ticker, e.g. "KXFED-26MAR19-B5.25" or
                "KXHIGHNY-26MAR05-B45".
        """
        return await client.get(f"/markets/{ticker}")

    @server.tool
    async def kalshi_get_event(
        event_ticker: str,
        with_nested_markets: bool = True,
    ) -> dict[str, Any]:
        """Fetch an event and (optionally) all markets nested under it.

        Args:
            event_ticker: Event ticker (e.g. "KXFED-26MAR19").
            with_nested_markets: If true, response includes the full
                array of markets under this event. Default true.
        """
        params = {"with_nested_markets": str(with_nested_markets).lower()}
        return await client.get(f"/events/{event_ticker}", params=params)

    @server.tool
    async def kalshi_get_events(
        limit: int = 50,
        cursor: str | None = None,
        status: str | None = None,
        series_ticker: str | None = None,
        with_nested_markets: bool = False,
    ) -> dict[str, Any]:
        """List events with optional filters.

        Args:
            limit: 1-200. Default 50.
            cursor: Pagination cursor from a previous response.
            status: "unopened", "open", "closed", "settled".
            series_ticker: Return events from a specific series only.
            with_nested_markets: Include nested market data per event
                (more bytes, but saves a follow-up call per event).
        """
        params: dict[str, Any] = {
            "limit": limit,
            "with_nested_markets": str(with_nested_markets).lower(),
        }
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        return await client.get("/events", params=params)

    @server.tool
    async def kalshi_get_series(series_ticker: str) -> dict[str, Any]:
        """Fetch a single series by ticker.

        A series is the template for a recurring event — e.g. "KXFED"
        is the series for Federal Reserve meeting events.
        """
        return await client.get(f"/series/{series_ticker}")

    @server.tool
    async def kalshi_get_trades(
        ticker: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
    ) -> dict[str, Any]:
        """List recent public trades (everyone's, not your own).

        Args:
            ticker: Restrict to a single market. If omitted, returns
                trades across all markets — usually you want to scope this.
            limit: 1-1000. Default 100.
            cursor: Pagination cursor.
            min_ts: Lower bound on trade timestamp (unix seconds).
            max_ts: Upper bound on trade timestamp (unix seconds).
        """
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        return await client.get("/markets/trades", params=params)
