"""Market / event / series discovery tools.

These are the read-only endpoints an agent uses to find what to trade
on Kalshi: markets (the actual contracts), events (groups of related
markets), and series (the schedule/template a recurring event follows).

All tools here debit the READ bucket of the rate limiter.

Many of the tools accept a `compact: bool = False` parameter. Kalshi
market objects are ~2KB each (full `rules_primary` + `rules_secondary`,
multiple price representations, etc.) — a 50-market response can blow
~100KB into an LLM's context for casual browsing. With `compact=True`
the response is stripped to the fields an agent actually needs to
decide what to look at: ticker, title, prices, volume, lifecycle
timestamps. Fetch the verbose version (default) when you need rules
text or fine-grained price metadata.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kalshi_mcp_server.errors import KalshiAPIError

if TYPE_CHECKING:
    from fastmcp import FastMCP


# Fields stripped from a market object when `compact=True`. Blacklist
# rather than whitelist: Kalshi may add useful new fields, and we want
# to keep them by default. This list captures only the known-verbose
# fields that don't aid an agent's trading decisions.
_VERBOSE_MARKET_FIELDS: frozenset[str] = frozenset(
    {
        "rules_primary",
        "rules_secondary",
        "previous_price_dollars",
        "previous_yes_ask_dollars",
        "previous_yes_bid_dollars",
        "settlement_timer_seconds",
        "expiration_value",
        "response_price_units",
        "price_level_structure",
        "price_ranges",
        "expected_expiration_time",
        "latest_expiration_time",
        "occurrence_datetime",
        "can_close_early",
        "fractional_trading_enabled",
        "created_time",
        "updated_time",
        "open_time",
    }
)

# Fields stripped from an event object when `compact=True`.
_VERBOSE_EVENT_FIELDS: frozenset[str] = frozenset(
    {
        "last_updated_ts",
        "available_on_brokers",
        "collateral_return_type",
        "strike_period",
        "mutually_exclusive",
    }
)


def _validate_ticker(ticker: str, *, name: str = "ticker") -> str:
    """Reject empty/whitespace tickers before they reach Kalshi.

    Without this, an empty path parameter hits `/markets/` (trailing slash)
    which Kalshi 301-redirects to the LIST endpoint — that would either
    look like success returning the whole markets list, or surface as a
    confusing redirect error. Catching it here gives a clean message.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        raise KalshiAPIError(
            status=0,
            message=f"{name} must be a non-empty string, got {ticker!r}.",
        )
    return ticker.strip()


def _compact_market(market: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in market.items() if k not in _VERBOSE_MARKET_FIELDS}


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in event.items() if k not in _VERBOSE_EVENT_FIELDS}


def register(server: FastMCP) -> None:
    """Register discovery tools against the FastMCP server."""
    client = server._kalshi_client  # type: ignore[attr-defined]

    @server.tool
    async def kalshi_get_markets(
        limit: int = 20,
        cursor: str | None = None,
        status: str | None = None,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        tickers: str | None = None,
        min_close_ts: int | None = None,
        max_close_ts: int | None = None,
        compact: bool = False,
    ) -> dict[str, Any]:
        """List Kalshi markets with optional filters.

        Args:
            limit: 1-1000. Default 20 (kept low — each market is ~2KB and
                the LLM context blowing up is the more common failure
                than missing markets).
            cursor: Pagination cursor from a previous response. Note:
                Kalshi silently returns an empty list if the cursor is
                malformed (no error) — if you expected results and got
                none, double-check the cursor was copied correctly.
            status: Filter by lifecycle: "unopened", "open", "closed",
                "settled". Multiple OK with comma-separated values.
            event_ticker: Return only markets in a specific event.
            series_ticker: Return only markets in a specific series.
            tickers: Comma-separated list of market tickers to fetch.
            min_close_ts: Filter to markets closing on/after this unix ts.
            max_close_ts: Filter to markets closing on/before this unix ts.
            compact: When True, drop verbose fields (rules text, previous
                prices, etc.) from each market. Default False.

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
        body = await client.get("/markets", params=params)
        if compact and "markets" in body:
            body["markets"] = [_compact_market(m) for m in body["markets"]]
        return body

    @server.tool
    async def kalshi_get_market(ticker: str, compact: bool = False) -> dict[str, Any]:
        """Fetch a single market by ticker.

        Args:
            ticker: Full market ticker, e.g. "KXFED-26MAR19-B5.25" or
                "KXHIGHNY-26MAR05-B45".
            compact: When True, drop verbose fields from the response.
                Default False.
        """
        ticker = _validate_ticker(ticker)
        body = await client.get(f"/markets/{ticker}")
        if compact and "market" in body:
            body["market"] = _compact_market(body["market"])
        return body

    @server.tool
    async def kalshi_get_event(
        event_ticker: str,
        with_nested_markets: bool = True,
        compact: bool = False,
    ) -> dict[str, Any]:
        """Fetch an event and (optionally) all markets nested under it.

        Args:
            event_ticker: Event ticker (e.g. "KXFED-26MAR19").
            with_nested_markets: If True (default), response includes
                the full array of markets under this event. If False,
                the `markets` array is stripped client-side — Kalshi
                returns nested markets regardless of this parameter, so
                we drop them locally to honor the requested contract.
            compact: When True, drop verbose fields from the event and
                from each nested market. Default False.
        """
        event_ticker = _validate_ticker(event_ticker, name="event_ticker")
        params = {"with_nested_markets": str(with_nested_markets).lower()}
        body = await client.get(f"/events/{event_ticker}", params=params)

        # Kalshi returns the markets array regardless of the query param;
        # honor the documented contract by stripping it ourselves.
        if not with_nested_markets:
            body.pop("markets", None)

        if compact:
            if "event" in body and isinstance(body["event"], dict):
                body["event"] = _compact_event(body["event"])
            if "markets" in body:
                body["markets"] = [_compact_market(m) for m in body["markets"]]
        return body

    @server.tool
    async def kalshi_get_events(
        limit: int = 20,
        cursor: str | None = None,
        status: str | None = None,
        series_ticker: str | None = None,
        with_nested_markets: bool = False,
        compact: bool = False,
    ) -> dict[str, Any]:
        """List events with optional filters.

        Args:
            limit: 1-200. Default 20.
            cursor: Pagination cursor. Kalshi silently returns an empty
                list on a bad cursor — check carefully if you expected
                results.
            status: "unopened", "open", "closed", "settled".
            series_ticker: Return events from a specific series only.
            with_nested_markets: Include nested market data per event.
                Default False — turning this on for a 20-event listing
                can return ~1MB of JSON. Use cautiously.
            compact: When True, drop verbose fields from events and
                nested markets. Default False.

        Returns:
            `events`: list of event objects.
            `milestones`: list of milestone objects (may be empty).
            `cursor`: pagination cursor for the next page.
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
        body = await client.get("/events", params=params)

        if compact:
            if "events" in body:
                body["events"] = [_compact_event(e) for e in body["events"]]
            # Nested markets within events
            for event in body.get("events", []):
                if "markets" in event:
                    event["markets"] = [_compact_market(m) for m in event["markets"]]
        return body

    @server.tool
    async def kalshi_get_series(series_ticker: str) -> dict[str, Any]:
        """Fetch a single series by ticker.

        A series is the template for a recurring event — e.g. "KXFED"
        is the series for Federal Reserve meeting events.
        """
        series_ticker = _validate_ticker(series_ticker, name="series_ticker")
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
            cursor: Pagination cursor. Kalshi silently returns an empty
                list on a bad cursor.
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
