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

import time
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

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


_MVE_FILTER_VALUES: frozenset[str] = frozenset({"exclude", "only"})


def _validate_mve_filter(value: str) -> str:
    """Validate the `mve_filter` passthrough before it reaches Kalshi.

    Kalshi accepts only "exclude" (drop multivariate/combo markets) or
    "only" (return just combos). Anything else 400s server-side with a
    less helpful message, so reject it locally.
    """
    if value not in _MVE_FILTER_VALUES:
        raise KalshiAPIError(
            status=0,
            message=(
                f"mve_filter must be 'exclude' or 'only', got {value!r}. "
                "Use 'exclude' to drop multivariate (KXMVE…) combo markets, "
                "or 'only' to return just those."
            ),
        )
    return value


def _volume_24h(market: dict[str, Any]) -> float:
    """Best-effort parse of a market's 24h volume.

    Kalshi sends it as a string in `volume_24h_fp`. Missing/garbage → 0.0
    so it sorts to the bottom rather than blowing up the ranking.
    """
    try:
        return float(market.get("volume_24h_fp") or 0)
    except (TypeError, ValueError):
        return 0.0


def _rank_liquid_markets(
    markets: list[dict[str, Any]],
    *,
    min_volume: float = 0.0,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Filter by min 24h volume, sort by 24h volume (desc), take the top
    `limit`, and project each survivor to the minimal triage view."""
    eligible = [m for m in markets if _volume_24h(m) >= min_volume]
    eligible.sort(key=_volume_24h, reverse=True)
    return [_minimal_market(m) for m in eligible[:limit]]


async def _scan_markets_excluding_mve(
    client: Any,
    *,
    scan_limit: int,
    status: str,
    series_ticker: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Page the markets listing with combos excluded, collecting up to
    `scan_limit` markets. De-dupes by ticker and caps the result at
    `scan_limit`. `scan_limit` is clamped to [1, 1000] to bound read-bucket
    cost.

    Returns `(markets, exhausted)`. `exhausted` is True when the scan reached
    the end of all matching markets — an empty page, a terminal cursor (""),
    or a non-advancing cursor (Kalshi's known quirk of returning the same
    cursor forever). It is False when the scan stopped because the window
    filled (`scan_limit` reached) while more markets remained — i.e. the
    caller is seeing a windowed subset, not the full set.
    """
    scan_limit = max(1, min(scan_limit, 1000))
    collected: list[dict[str, Any]] = []
    seen_tickers: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None
    exhausted = False
    while len(collected) < scan_limit:
        params: dict[str, Any] = {
            "limit": min(1000, scan_limit - len(collected)),
            "status": status,
            "mve_filter": "exclude",
        }
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        body = await client.get("/markets", params=params)
        page = body.get("markets") or []
        for market in page:
            ticker = market.get("ticker")
            # De-dupe by ticker so a non-advancing cursor can't pad the list
            # with repeats. Markets without a ticker (shouldn't happen) are
            # kept as-is rather than collapsed into one.
            if ticker is not None and ticker in seen_tickers:
                continue
            if ticker is not None:
                seen_tickers.add(ticker)
            collected.append(market)
            if len(collected) >= scan_limit:
                break
        cursor = body.get("cursor")
        # Stop on empty page, terminal cursor (""), or a cursor we've already
        # followed (no forward progress) — all mean no more markets exist.
        if not page or not cursor or cursor in seen_cursors:
            exhausted = True
            break
        seen_cursors.add(cursor)
    return collected[:scan_limit], exhausted


# Negative cache for `_event_hint`: ticker -> monotonic time we last confirmed
# it is NOT an event. The event-resolution probe runs on already-failed paths
# (404 / empty book / empty list); without this, an agent polling a real but
# illiquid market's orderbook would fire a fresh `/events` lookup on EVERY
# poll. Caching the "not an event" verdict bounds that to one probe per ticker
# per TTL. A confirmed event clears its entry so a later miss re-probes.
_EVENT_HINT_MISS_TTL_S: float = 300.0
_EVENT_HINT_MISS_MAX: int = 4096
_event_hint_misses: dict[str, float] = {}


def _record_event_hint_miss(ticker: str, now: float) -> None:
    # Record (or refresh) a "not an event" verdict. Staleness is enforced at
    # READ time via the TTL check; here we only keep the cache HARD-bounded.
    # dict preserves insertion order, so evicting from the front is a simple
    # O(overflow) FIFO — no full O(n) scan on every miss when at capacity.
    _event_hint_misses[ticker] = now
    overflow = len(_event_hint_misses) - _EVENT_HINT_MISS_MAX
    if overflow > 0:
        for stale in list(_event_hint_misses)[:overflow]:
            del _event_hint_misses[stale]


async def _event_hint(client: Any, ticker: str) -> str | None:
    """If `ticker` is actually an EVENT ticker, return an actionable hint
    listing its market tickers; otherwise return None.

    Kalshi tools take MARKET tickers (with an outcome suffix, e.g.
    `…PITHOU-HOU`); an EVENT ticker (`…PITHOU`) passed instead fails
    silently — an empty orderbook, an empty markets list, or a blunt 404.
    This resolver is called ONLY on that already-failed path, so the happy
    path never pays for the extra read. It fails open: any error resolving
    the event returns None rather than masking the caller's real problem.
    A short-lived negative cache (`_event_hint_misses`) prevents a repeated
    poll of the same non-event ticker from re-probing `/events` every time.
    """
    now = time.monotonic()
    missed_at = _event_hint_misses.get(ticker)
    if missed_at is not None and (now - missed_at) < _EVENT_HINT_MISS_TTL_S:
        return None
    try:
        body = await client.get(f"/events/{ticker}", params={"with_nested_markets": "true"})
    except KalshiAPIError:
        _record_event_hint_miss(ticker, now)
        return None
    markets = (body.get("markets") or []) if isinstance(body, dict) else []
    tickers = [m.get("ticker") for m in markets if isinstance(m, dict) and m.get("ticker")]
    if not tickers:
        _record_event_hint_miss(ticker, now)
        return None
    _event_hint_misses.pop(ticker, None)  # it IS an event — clear any stale miss
    shown = ", ".join(tickers[:20])
    more = f" (+{len(tickers) - 20} more)" if len(tickers) > 20 else ""
    return (
        f"'{ticker}' is an EVENT ticker, not a MARKET ticker. Its markets are: "
        f"{shown}{more}. Retry with one of those market tickers, or call "
        f"kalshi_get_event('{ticker}') to fetch the whole event."
    )


def _single_ticker(tickers: str) -> str | None:
    """Return the sole ticker if `tickers` names exactly one, tolerating
    surrounding whitespace and a trailing comma; otherwise None.

    Used to decide whether an empty `kalshi_get_markets(tickers=…)` result
    warrants an event-vs-market hint — only meaningful for a single ticker.
    """
    parts = [t.strip() for t in tickers.split(",") if t.strip()]
    return parts[0] if len(parts) == 1 else None


def register(server: FastMCP) -> None:
    """Register discovery tools against the FastMCP server."""
    client = server._kalshi_client  # type: ignore[attr-defined]

    @server.tool
    async def kalshi_get_markets(
        limit: Annotated[int, Field(ge=1, le=1000)] = 20,
        cursor: str | None = None,
        status: str | None = None,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        tickers: str | None = None,
        min_close_ts: int | None = None,
        max_close_ts: int | None = None,
        mve_filter: Literal["exclude", "only"] | None = None,
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
            tickers: Comma-separated list of market tickers to fetch. These
                must be MARKET tickers (with an outcome suffix), not EVENT
                tickers — an event ticker returns an empty list. (When a
                single event ticker is passed, this tool raises a hint
                naming the real market tickers instead of an empty result.)
            min_close_ts: Filter to markets closing on/after this unix ts.
            max_close_ts: Filter to markets closing on/before this unix ts.
            mve_filter: Multivariate (combo) market filter. "exclude" drops
                `KXMVE…` combo markets server-side — strongly recommended for
                discovery, since the default open listing is dominated by
                combos with empty/one-sided books. "only" returns just combos.
                Default None (no filter). See also `kalshi_find_liquid_markets`.
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
        if mve_filter is not None:
            params["mve_filter"] = _validate_mve_filter(mve_filter)
        body = await client.get("/markets", params=params)

        # A single EVENT ticker passed via `tickers` yields an empty list
        # with no error — surface an actionable hint instead of a silent [].
        if tickers and not body.get("markets"):
            sole = _single_ticker(tickers)
            if sole:
                hint = await _event_hint(client, sole)
                if hint:
                    raise KalshiAPIError(status=0, message=hint)

        if "markets" in body:
            body["markets"] = [
                _project_market(m, compact=compact, minimal=minimal, fields=fields)
                for m in body["markets"]
            ]
        return body

    @server.tool
    async def kalshi_find_liquid_markets(
        limit: Annotated[int, Field(ge=1, le=1000)] = 20,
        scan_limit: Annotated[int, Field(ge=1, le=1000)] = 200,
        status: str = "open",
        series_ticker: str | None = None,
        min_volume: Annotated[float, Field(ge=0)] = 0.0,
    ) -> dict[str, Any]:
        """Find the most liquid SINGLE (non-combo) markets, ranked by 24h volume.

        Kalshi's default market listing is dominated by multivariate
        (`KXMVE…`) combo markets with empty/one-sided books, and the API
        offers NO server-side sort. This helper does the de-noising for you:
        it pages the listing with combos excluded (`mve_filter=exclude`),
        ranks the result by 24h volume locally, and returns a short
        minimal-projection shortlist — the page an agent actually wants.

        Args:
            limit: Size of the returned shortlist (top-N by volume). Default 20.
            scan_limit: How many markets to fetch+rank before taking the top
                `limit`. Higher = more thorough but more read-bucket cost.
                Default 200, capped at 1000.
            status: Lifecycle filter (default "open"). Same values as
                `kalshi_get_markets`.
            series_ticker: Restrict the scan to one series (e.g. "KXMLBGAME").
            min_volume: Drop markets whose 24h volume is below this (same
                units as `volume_24h_fp`). Default 0.0 (keep all).

        IMPORTANT — windowed ranking: Kalshi has no server-side sort, so the
        ranking is over the SCANNED WINDOW only (the top markets among the
        first `scan_limit` results), NOT a global exchange-wide ranking unless
        the scan exhausted all matching markets. Check `complete` (and
        `scanned`) in the response; raise `scan_limit` (up to 1000) to look
        deeper when `complete` is False.

        Returns:
            `markets`: ranked shortlist (minimal projection), highest 24h
                volume first.
            `scanned`: number of distinct markets fetched and ranked.
            `scan_limit`: the effective scan cap (after clamping to 1-1000).
            `complete`: True if the scan reached the end of all markets
                matching `status` + combo-exclusion before hitting `scan_limit`
                — the ranking then covers every such market (after `min_volume`
                is applied locally). False means the window filled and more
                matching markets exist beyond it; raise `scan_limit` to see them.
        """
        effective_scan = max(1, min(scan_limit, 1000))
        collected, exhausted = await _scan_markets_excluding_mve(
            client, scan_limit=effective_scan, status=status, series_ticker=series_ticker
        )
        ranked = _rank_liquid_markets(collected, min_volume=min_volume, limit=limit)
        return {
            "markets": ranked,
            "scanned": len(collected),
            "scan_limit": effective_scan,
            "complete": exhausted,
        }

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
        try:
            body = await client.get(f"/markets/{ticker}")
        except KalshiAPIError as exc:
            # A 404 is often an EVENT ticker passed where a market ticker
            # was expected — give an actionable hint instead of a blunt 404.
            if exc.status == 404:
                hint = await _event_hint(client, ticker)
                if hint:
                    raise KalshiAPIError(status=404, message=hint) from exc
            raise
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
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
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
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
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
