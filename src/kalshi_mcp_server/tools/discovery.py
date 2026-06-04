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

`compact` is a *blacklist* — it removes known-verbose fields but keeps
everything else. That isn't enough for multivariate (`KXMVE…`) combo
markets, whose bulk lives in `custom_strike`, `mve_selected_legs`, and
long repeated `title`/`*_sub_title` strings. For listing/scanning, prefer
`minimal=True` (a *whitelist* — see `_MINIMAL_MARKET_FIELDS`) which keeps
only the dozen-odd fields needed to triage a market and is small enough
to stay under an LLM tool-result token cap even for combo markets.
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
        # Kalshi's `liquidity_dollars` is currently always "0.0000" even on
        # deep, actively-traded books — surfacing it invites a naive caller
        # to read 0 as "no liquidity". Strip it from curated views; see the
        # docstring note on `kalshi_get_markets`.
        "liquidity_dollars",
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


# Whitelist for `minimal=True`. Unlike `compact` (a blacklist), this keeps
# ONLY these fields — small enough to stay under an LLM tool-result token
# cap even for multivariate combo markets, whose bulk lives in fields not
# listed here (`custom_strike`, `mve_selected_legs`, long `*_sub_title`s).
# Ordered for readable output; names match Kalshi's real response keys.
_MINIMAL_MARKET_FIELDS: tuple[str, ...] = (
    "ticker",
    "event_ticker",
    "title",
    "yes_sub_title",
    "status",
    "close_time",
    "last_price_dollars",
    "yes_bid_dollars",
    "yes_ask_dollars",
    "no_bid_dollars",
    "no_ask_dollars",
    "yes_bid_size_fp",
    "yes_ask_size_fp",
    "volume_24h_fp",
    "open_interest_fp",
    "market_type",
)


def _compact_market(market: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in market.items() if k not in _VERBOSE_MARKET_FIELDS}


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in event.items() if k not in _VERBOSE_EVENT_FIELDS}


def _parse_fields(fields: str) -> list[str]:
    """Split a comma-separated fields string into a clean, ordered, de-duped list."""
    parsed = [f.strip() for f in fields.split(",") if f.strip()]
    # Order-preserving dedup so callers can't pass the same field twice.
    return list(dict.fromkeys(parsed))


def _minimal_market(market: dict[str, Any], fields: str | None = None) -> dict[str, Any]:
    """Project a market down to a small whitelist of fields.

    Keeps only whitelisted keys that are actually present — never
    fabricates a missing field. Unknown field names are silently ignored
    (the result simply omits them). A caller-supplied `fields` (comma-
    separated) overrides the default `_MINIMAL_MARKET_FIELDS` whitelist.

    Raises KalshiAPIError if `fields` is given but contains no usable
    field name (e.g. "" or ",,") — that's a malformed request, and
    silently returning an empty market would hide the mistake.
    """
    if fields is not None:
        keys: list[str] | tuple[str, ...] = _parse_fields(fields)
        if not keys:
            raise KalshiAPIError(
                status=0,
                message=(
                    f"`fields` must list at least one field name, got {fields!r}. "
                    "Pass a comma-separated list like "
                    "'ticker,yes_bid_dollars,volume_24h_fp', or omit it to use "
                    "the default minimal projection."
                ),
            )
    else:
        keys = _MINIMAL_MARKET_FIELDS
    return {k: market[k] for k in keys if k in market}


def _project_market(
    market: dict[str, Any],
    *,
    compact: bool = False,
    minimal: bool = False,
    fields: str | None = None,
) -> dict[str, Any]:
    """Shape a single market per the requested view.

    Precedence: `fields` > `minimal` > `compact` > full passthrough.
    Always returns a NEW dict so the result never aliases the caller's
    parsed response (uniform contract across all four branches).
    """
    if fields:
        return _minimal_market(market, fields=fields)
    if minimal:
        return _minimal_market(market)
    if compact:
        return _compact_market(market)
    return dict(market)


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
        minimal: bool = False,
        fields: str | None = None,
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
                prices, etc.) from each market. Default False. NOTE:
                compact is a blacklist and does NOT shrink multivariate
                (`KXMVE…`) combo markets much — prefer `minimal` for those.
            minimal: When True, project each market to a small whitelist
                of triage fields (ticker, prices, sizes, volume, status,
                close_time, …). This is the right mode for listing/scanning:
                it stays under an LLM tool-result token cap even for combo
                markets. Default False.
            fields: Comma-separated whitelist of exact field names to keep,
                overriding `minimal`'s default set (e.g.
                "ticker,yes_bid_dollars,yes_ask_dollars,volume_24h_fp").
                Implies a minimal-style projection. Unknown field names are
                silently ignored; an empty/blank list is rejected.

        View precedence: `fields` > `minimal` > `compact` > full.

        Returns a list of markets (each with ticker, prices, volume,
        open/close timestamps, settlement value if settled) plus a
        `cursor` for pagination.

        Liquidity note: Kalshi's `liquidity_dollars` is currently always
        `0.0000` even on deep, actively-traded books — do NOT gate on it.
        Assess liquidity from the orderbook (best bid/ask + resting size)
        and `volume_24h_fp` / `open_interest_fp` instead. It is stripped
        from the default `compact` and `minimal` views (an explicit
        `fields=` can still request it, but it'll just be 0.0000).
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
        if "markets" in body:
            body["markets"] = [
                _project_market(m, compact=compact, minimal=minimal, fields=fields)
                for m in body["markets"]
            ]
        return body

    @server.tool
    async def kalshi_get_market(
        ticker: str,
        compact: bool = False,
        minimal: bool = False,
        fields: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a single market by ticker.

        Args:
            ticker: Full MARKET ticker, e.g. "KXFED-26MAR19-B5.25" or
                "KXMLBGAME-26JUN042010PITHOU-HOU". This is NOT an event
                ticker — a market ticker carries the outcome suffix
                (…-HOU, …-B5.25). Passing an event ticker returns a 404.
            compact: When True, drop verbose fields from the response.
                Default False.
            minimal: When True, project to the triage-field whitelist
                (see `kalshi_get_markets`). Default False.
            fields: Comma-separated whitelist overriding `minimal`'s
                default set. Implies a minimal-style projection. Unknown
                field names are silently ignored; an empty list is rejected.

        View precedence: `fields` > `minimal` > `compact` > full.

        Liquidity note: `liquidity_dollars` is always `0.0000` from Kalshi
        and is stripped from the default compact/minimal views — gate on
        the orderbook and `volume_24h_fp` / `open_interest_fp` instead.
        """
        ticker = _validate_ticker(ticker)
        body = await client.get(f"/markets/{ticker}")
        if "market" in body:
            body["market"] = _project_market(
                body["market"], compact=compact, minimal=minimal, fields=fields
            )
        return body

    @server.tool
    async def kalshi_get_event(
        event_ticker: str,
        with_nested_markets: bool = True,
        compact: bool = False,
        minimal: bool = False,
        fields: str | None = None,
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
            minimal: When True, project each NESTED MARKET to the triage-
                field whitelist (see `kalshi_get_markets`). The event
                object itself is unaffected. Strongly recommended for
                multivariate events whose nested combo markets are large.
            fields: Comma-separated whitelist applied to each nested market,
                overriding `minimal`'s default set. Unknown field names are
                silently ignored; an empty list is rejected.

        Nested-market view precedence: `fields` > `minimal` > `compact` > full.
        """
        event_ticker = _validate_ticker(event_ticker, name="event_ticker")
        params = {"with_nested_markets": str(with_nested_markets).lower()}
        body = await client.get(f"/events/{event_ticker}", params=params)

        # Kalshi returns the markets array regardless of the query param;
        # honor the documented contract by stripping it ourselves.
        if not with_nested_markets:
            body.pop("markets", None)

        # The event object only has a `compact` (blacklist) view; minimal/
        # fields are market-specific and apply to the nested markets.
        if compact and "event" in body and isinstance(body["event"], dict):
            body["event"] = _compact_event(body["event"])
        if (compact or minimal or fields) and "markets" in body:
            body["markets"] = [
                _project_market(m, compact=compact, minimal=minimal, fields=fields)
                for m in body["markets"]
            ]
        return body

    @server.tool
    async def kalshi_get_events(
        limit: int = 20,
        cursor: str | None = None,
        status: str | None = None,
        series_ticker: str | None = None,
        with_nested_markets: bool = False,
        compact: bool = False,
        minimal: bool = False,
        fields: str | None = None,
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
            minimal: When True, project each NESTED MARKET to the triage-
                field whitelist (see `kalshi_get_markets`). The event
                objects are unaffected. Pair with `with_nested_markets`
                to keep large multi-event listings under a token cap.
            fields: Comma-separated whitelist applied to each nested market,
                overriding `minimal`'s default set. Unknown field names are
                silently ignored; an empty list is rejected.

        Nested-market view precedence: `fields` > `minimal` > `compact` > full.

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

        if compact and "events" in body:
            body["events"] = [_compact_event(e) for e in body["events"]]
        # Nested markets get the full projection (compact/minimal/fields);
        # the event objects only have a compact view.
        if compact or minimal or fields:
            for event in body.get("events", []):
                if "markets" in event:
                    event["markets"] = [
                        _project_market(m, compact=compact, minimal=minimal, fields=fields)
                        for m in event["markets"]
                    ]
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
