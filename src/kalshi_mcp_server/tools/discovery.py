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
from collections.abc import Callable
from dataclasses import dataclass, field
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


# ── Listing pager (shared by find_liquid_markets and get_series_summary) ────
#
# Budgets for the opt-in `scan_all` full sweep. Without a hard wall the pager
# would follow Kalshi's cursor indefinitely — a stuck-but-advancing cursor, or
# simply an exchange with far more open markets than we expect, would hang the
# tool call until the MCP client times out with nothing to show for it. Every
# sweep is bounded by ALL THREE of these; whichever binds first stops the scan
# and is reported back as `stopped_by`, so `complete` is never a guess.
#
# Sizing (Aug 2026): the open, non-combo listing is a few thousand markets, so
# a full sweep is ~5-10 pages. 30 pages x 1000/page = 30k markets of headroom
# — generous now, and the wall-clock cap is the real backstop if that changes.
_SCAN_PAGE_SIZE = 1000  # Kalshi's max `limit` on GET /markets
_SCAN_ALL_MAX_REQUESTS = 30
_SCAN_ALL_MAX_SECONDS = 25.0
_SCAN_ALL_MAX_MARKETS = 30_000


@dataclass
class _ScanResult:
    """Outcome of a listing sweep.

    `complete` is the honest answer to "did this cover every matching
    market?" — True only when the pager reached the end of the listing.
    `stopped_by` names the wall that bound first when it didn't:
    "scan_limit" (the caller's window filled), "request_budget",
    "time_budget", or "market_cap". It is None when `complete` is True.

    `scanned` counts every distinct market the sweep saw, which is NOT
    `len(markets)` when an `on_page` fold was used (the pager then retains
    nothing).
    """

    markets: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = False
    scanned: int = 0
    requests: int = 0
    stopped_by: str | None = None


async def _scan_markets_excluding_mve(
    client: Any,
    *,
    scan_limit: int,
    status: str,
    series_ticker: str | None = None,
    scan_all: bool = False,
    max_requests: int | None = None,
    max_seconds: float | None = None,
    max_markets: int | None = None,
    on_page: Callable[[list[dict[str, Any]]], None] | None = None,
) -> _ScanResult:
    """Page `GET /markets` with combos excluded server-side (`mve_filter=exclude`).

    Two modes:

    * **Windowed** (`scan_all=False`, the default): collect up to `scan_limit`
      markets, clamped to [1, 1000] to bound read-bucket cost. This is the
      historical behavior and it is unchanged.
    * **Full sweep** (`scan_all=True`): ignore `scan_limit` and follow the
      cursor to the end of the listing, bounded by `max_requests`,
      `max_seconds`, and `max_markets`.

    De-dupes by ticker, so a non-advancing cursor (a known Kalshi quirk —
    the same cursor returned forever) can neither pad the result with repeats
    nor spin the loop. The scan is considered complete on an empty page, a
    terminal cursor (""), or a cursor already followed; all three mean the
    listing is exhausted.

    Pass `on_page` to fold each page incrementally instead of retaining it —
    `_ScanResult.markets` is then left empty and only `scanned` grows. That is
    what keeps a 30k-market sweep from holding ~60MB of parsed market dicts in
    memory just to compute per-series totals.

    The three budgets default to the module constants, resolved HERE rather
    than in the signature so the constants stay a live knob — a default bound
    at def-time would ignore any later override (and silently no-op a test's
    monkeypatch, which is how this was caught).
    """
    max_requests = _SCAN_ALL_MAX_REQUESTS if max_requests is None else max_requests
    max_seconds = _SCAN_ALL_MAX_SECONDS if max_seconds is None else max_seconds
    max_markets = _SCAN_ALL_MAX_MARKETS if max_markets is None else max_markets
    target = max_markets if scan_all else max(1, min(scan_limit, 1000))
    started = time.monotonic()
    result = _ScanResult()
    seen_tickers: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None

    while result.scanned < target:
        # Budget checks happen BEFORE the request, so the caps are ceilings on
        # what we spend, not on what we spend plus one more page.
        if result.requests >= max_requests:
            result.stopped_by = "request_budget"
            return result
        if result.requests and (time.monotonic() - started) >= max_seconds:
            result.stopped_by = "time_budget"
            return result

        params: dict[str, Any] = {
            "limit": min(_SCAN_PAGE_SIZE, target - result.scanned),
            "status": status,
            "mve_filter": "exclude",
        }
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        body = await client.get("/markets", params=params)
        result.requests += 1

        page = body.get("markets") or []
        fresh: list[dict[str, Any]] = []
        for market in page:
            ticker = market.get("ticker")
            # Markets without a ticker (shouldn't happen) are kept as-is
            # rather than collapsed into one.
            if ticker is not None and ticker in seen_tickers:
                continue
            if ticker is not None:
                seen_tickers.add(ticker)
            fresh.append(market)
            result.scanned += 1
            if result.scanned >= target:
                break
        if on_page is not None:
            if fresh:
                on_page(fresh)
        else:
            result.markets.extend(fresh)

        cursor = body.get("cursor")
        if not page or not cursor or cursor in seen_cursors:
            result.complete = True
            return result
        seen_cursors.add(cursor)

    # Fell out of the loop with the target filled and a live cursor left over
    # — more matching markets exist beyond what we looked at.
    result.stopped_by = "market_cap" if scan_all else "scan_limit"
    return result


# ── Series-level rollup (kalshi_get_series_summary) ─────────────────────────
#
# Kalshi does NOT return `series_ticker` on a market object (confirmed against
# both the API reference and a live prod response) — it exists only as a
# *query filter*. So the series is derived from the ticker prefix, relying on
# Kalshi's `SERIES-EVENTSUFFIX-OUTCOME` convention: the event ticker
# "KXMLBGAME-26AUG141910SDCLE" belongs to series "KXMLBGAME". This is a
# convention, not a contract — the tool says so in its docstring rather than
# presenting derived series as authoritative.


def _series_of(market: dict[str, Any]) -> str | None:
    """Derive a market's series ticker from its event/market ticker prefix."""
    for key in ("event_ticker", "ticker"):
        value = market.get(key)
        if isinstance(value, str) and value.strip():
            head = value.strip().split("-", 1)[0]
            if head:
                return head
    return None


def _dollars(market: dict[str, Any], key: str) -> float | None:
    """Best-effort parse of one of Kalshi's `*_dollars` string prices."""
    try:
        return float(market[key])
    except (KeyError, TypeError, ValueError):
        return None


def _yes_spread(market: dict[str, Any]) -> float | None:
    """YES-side bid/ask spread in dollars, or None if the book isn't two-sided.

    Both sides must be strictly positive: Kalshi reports an empty or one-sided
    book as `0.0000`, and treating that as a 0.00 spread would make every dead
    market look like the tightest one in its series — exactly backwards.
    """
    bid = _dollars(market, "yes_bid_dollars")
    ask = _dollars(market, "yes_ask_dollars")
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return ask - bid


def _fold_series_page(acc: dict[str, dict[str, Any]], markets: list[dict[str, Any]]) -> None:
    """Fold one page of markets into the running per-series accumulator.

    Called as the pager's `on_page` hook so a full-exchange sweep never
    retains the raw markets — only this one small row per series.
    """
    for market in markets:
        series = _series_of(market)
        if series is None:
            continue
        row = acc.get(series)
        if row is None:
            row = acc[series] = {
                "series_ticker": series,
                "market_count": 0,
                "events": set(),
                "volume_24h": 0.0,
                "min_spread_dollars": None,
                "soonest_close_time": None,
            }
        row["market_count"] += 1
        row["volume_24h"] += _volume_24h(market)

        event_ticker = market.get("event_ticker")
        if isinstance(event_ticker, str) and event_ticker:
            row["events"].add(event_ticker)

        spread = _yes_spread(market)
        if spread is not None:
            current = row["min_spread_dollars"]
            if current is None or spread < current:
                row["min_spread_dollars"] = spread

        close_time = market.get("close_time")
        if isinstance(close_time, str) and close_time:
            # Kalshi's close_time is whole-second RFC3339 in UTC ("…:00Z"), so
            # a lexicographic min is a chronological min — no parsing needed.
            current_close = row["soonest_close_time"]
            if current_close is None or close_time < current_close:
                row["soonest_close_time"] = close_time


_SERIES_SORT_KEYS: dict[str, Callable[[dict[str, Any]], Any]] = {
    # Descending sorts negate; "soonest_close" is the one ascending sort, and
    # series with no close_time sort last rather than crashing the comparison.
    "volume_24h": lambda row: -row["volume_24h"],
    "market_count": lambda row: -row["market_count"],
    "soonest_close": lambda row: (
        row["soonest_close_time"] is None,
        row["soonest_close_time"] or "",
    ),
}


def _finalize_series(
    acc: dict[str, dict[str, Any]], *, top: int, sort_by: str
) -> list[dict[str, Any]]:
    """Sort the accumulator, take the top `top`, and make each row JSON-safe."""
    rows = sorted(acc.values(), key=_SERIES_SORT_KEYS[sort_by])
    out: list[dict[str, Any]] = []
    for row in rows[:top]:
        spread = row["min_spread_dollars"]
        out.append(
            {
                "series_ticker": row["series_ticker"],
                "market_count": row["market_count"],
                "event_count": len(row["events"]),
                # Round to cents: these are summed float parses of Kalshi's
                # string decimals, so the raw value carries binary-float noise.
                "volume_24h": round(row["volume_24h"], 2),
                "min_spread_dollars": None if spread is None else round(spread, 4),
                "soonest_close_time": row["soonest_close_time"],
            }
        )
    return out


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
        scan_all: bool = False,
    ) -> dict[str, Any]:
        """Find the most liquid SINGLE (non-combo) markets, ranked by 24h volume.

        Kalshi's default market listing is dominated by multivariate
        (`KXMVE…`) combo markets with empty/one-sided books, and the API
        offers NO server-side sort. This helper does the de-noising for you:
        it pages the listing with combos excluded (`mve_filter=exclude`),
        ranks the result by 24h volume locally, and returns a short
        minimal-projection shortlist — the page an agent actually wants.

        By default the ranking covers a WINDOW (`scan_limit` markets), not the
        whole exchange. Pass `scan_all=True` for a true exchange-wide ranking.

        Args:
            limit: Size of the returned shortlist (top-N by volume). Default 20.
                The shortlist stays minimal-projected at any `scan_limit`, so
                a full sweep costs read budget, not context.
            scan_limit: How many markets to fetch+rank before taking the top
                `limit`. Higher = more thorough but more read-bucket cost.
                Default 200, range 1-1000 (the schema bounds it; direct
                callers are clamped to the same range). IGNORED when
                `scan_all=True`.
            status: Lifecycle filter (default "open"). Same values as
                `kalshi_get_markets` ("unopened"/"open"/"closed"/"settled";
                multiple OK comma-separated, which is why this stays a free
                string rather than an enum).
            series_ticker: Restrict the scan to one series (e.g. "KXMLBGAME").
            min_volume: Drop markets whose 24h volume is below this (same
                units as `volume_24h_fp`). Default 0.0 (keep all).
            scan_all: Sweep the ENTIRE matching listing before ranking, instead
                of the first `scan_limit` markets. This is what makes the
                ranking exchange-wide rather than an arbitrary slice. Costs
                one read per 1000 markets (~5-10 requests for the open listing
                as of Aug 2026) and is bounded by internal request / wall-clock
                / market caps, so it always terminates. Default False.

        IMPORTANT — windowed ranking: Kalshi has no server-side sort, so with
        `scan_all=False` the ranking is over the SCANNED WINDOW only (the top
        markets among the first `scan_limit` results), NOT a global
        exchange-wide ranking unless the scan happened to exhaust the listing.
        Always check `complete` before treating the shortlist as "the most
        liquid markets on Kalshi". If `complete` is False, `stopped_by` names
        the wall that bound: "scan_limit" (raise it, or set `scan_all=True`),
        or "request_budget"/"time_budget"/"market_cap" (an internal cap on a
        full sweep — narrow with `status` or `series_ticker`).

        Returns:
            `markets`: ranked shortlist (minimal projection), highest 24h
                volume first.
            `scanned`: number of distinct markets fetched and ranked (the
                scanned count — the denominator for `complete`).
            `requests`: how many listing requests the scan spent.
            `scan_limit`: the effective scan cap, or null when `scan_all=True`.
            `scan_all`: echoes the mode the scan actually ran in.
            `complete`: True only if the scan reached the END of all markets
                matching `status` + combo-exclusion — the ranking then covers
                every such market (after `min_volume` is applied locally).
                False means more matching markets exist beyond what was ranked.
            `stopped_by`: null when `complete`, else the binding wall (above).
        """
        effective_scan = max(1, min(scan_limit, 1000))
        scan = await _scan_markets_excluding_mve(
            client,
            scan_limit=effective_scan,
            status=status,
            series_ticker=series_ticker,
            scan_all=scan_all,
        )
        ranked = _rank_liquid_markets(scan.markets, min_volume=min_volume, limit=limit)
        return {
            "markets": ranked,
            "scanned": scan.scanned,
            "requests": scan.requests,
            "scan_limit": None if scan_all else effective_scan,
            "scan_all": scan_all,
            "complete": scan.complete,
            "stopped_by": scan.stopped_by,
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
            status: "unopened", "open", "closed", "settled". Multiple OK
                with comma-separated values.
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

        Args:
            series_ticker: The series ticker, e.g. "KXFED".
        """
        series_ticker = _validate_ticker(series_ticker, name="series_ticker")
        return await client.get(f"/series/{series_ticker}")

    @server.tool
    async def kalshi_get_series_list(
        category: str | None = None,
        tags: str | None = None,
        include_volume: bool = True,
        min_updated_ts: int | None = None,
        include_product_metadata: bool = False,
    ) -> dict[str, Any]:
        """List Kalshi's SERIES catalog — the authoritative names behind the tickers.

        Where `kalshi_get_series_summary` derives series from ticker prefixes
        and measures live activity, this returns Kalshi's own catalog entry
        per series: real title, category, tags, settlement sources, fee type,
        and (with `include_volume`) lifetime traded volume. Pair them — the
        summary tells you what's active, this tells you what it IS.

        `min_updated_ts` makes this a cheap change-feed: pass yesterday's
        timestamp to get only series whose metadata moved, which is the low-
        cost way to spot a new event class listing.

        Args:
            category: Filter to one category (e.g. "Sports", "Economics",
                "Politics"). Omit for all.
            tags: Filter by tag. Omit for all.
            include_volume: Include `volume_fp`, total volume traded across
                all events in each series. Default True — it's the single
                most useful ranking field here and costs nothing extra.
            min_updated_ts: Only series whose metadata was updated after this
                unix timestamp (seconds). The change-feed knob described above.
            include_product_metadata: Include Kalshi's internal
                `product_metadata` blob. Default False — it is verbose and
                rarely useful to a trading agent.

        Token-cap note: this endpoint does NOT paginate — it returns every
        matching series in one response. Unfiltered that is the whole catalog
        (hundreds of entries, each with settlement sources and tags), so
        prefer `category=` or `min_updated_ts=` when you can, and leave
        `include_product_metadata` off.
        """
        params: dict[str, Any] = {
            "include_volume": str(include_volume).lower(),
            "include_product_metadata": str(include_product_metadata).lower(),
        }
        if category:
            params["category"] = category
        if tags:
            params["tags"] = tags
        if min_updated_ts is not None:
            params["min_updated_ts"] = min_updated_ts
        return await client.get("/series", params=params)

    @server.tool
    async def kalshi_get_milestones(
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        cursor: str | None = None,
        category: str | None = None,
        competition: str | None = None,
        type: str | None = None,
        related_event_ticker: str | None = None,
        minimum_start_date: str | None = None,
        min_updated_ts: int | None = None,
    ) -> dict[str, Any]:
        """List MILESTONES — the real-world schedule behind Kalshi's markets.

        A milestone is a scheduled real-world happening (a game, a race, a
        match) with its own start/end dates, category, competition, and the
        event tickers Kalshi has attached to it. That makes it the leading
        indicator for new supply: a milestone can exist before its markets
        list, so this answers "what is coming" rather than "what is listed",
        which is the one thing a market-listing sweep structurally cannot tell
        you.

        Args:
            limit: 1-500. Default 50. Milestone objects are small, but they
                carry `related_event_tickers` arrays — keep it modest.
            cursor: Pagination cursor. Kalshi silently returns an empty list
                on a malformed cursor.
            category: e.g. "Sports", "Elections", "Esports", "Crypto".
            competition: e.g. "Pro Football", "Pro Basketball".
            type: Milestone type, e.g. "football_game", "basketball_game".
            related_event_ticker: Find the milestone(s) behind a known event
                — the reverse lookup, useful for pulling schedule context onto
                a market you're already looking at.
            minimum_start_date: RFC3339 timestamp; only milestones starting
                on/after it. The forward-looking filter — pass "now" to see
                what is upcoming rather than what already happened.
            min_updated_ts: Only milestones whose metadata changed after this
                unix timestamp (seconds). Use as a daily change-feed.

        Pair with `kalshi_get_series_summary`: milestones tell you a
        competition is coming, the series census confirms when its markets
        actually list.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if category:
            params["category"] = category
        if competition:
            params["competition"] = competition
        if type:
            params["type"] = type
        if related_event_ticker:
            params["related_event_ticker"] = related_event_ticker
        if minimum_start_date:
            params["minimum_start_date"] = minimum_start_date
        if min_updated_ts is not None:
            params["min_updated_ts"] = min_updated_ts
        return await client.get("/milestones", params=params)

    @server.tool
    async def kalshi_get_series_summary(
        status: str = "open",
        top: Annotated[int, Field(ge=1, le=200)] = 50,
        sort_by: Literal["volume_24h", "market_count", "soonest_close"] = "volume_24h",
        scan_all: bool = True,
    ) -> dict[str, Any]:
        """Roll the whole market listing up to one row per SERIES.

        A supply census: which series are listed right now, how many markets
        each has, how much 24h volume, the tightest spread seen in it, and
        when its soonest market closes. Run it daily and diff the series list
        to detect new event classes coming online (NFL, a tennis major, a new
        economic series) without paging thousands of markets into context.

        Cheap in CONTEXT, not free in READS: it sweeps the listing internally
        (one request per 1000 markets, combos excluded server-side) and returns
        only the top `top` rows — a few hundred bytes each — so a full-exchange
        census fits in a normal tool result. Check `complete` before treating
        the census as exhaustive.

        Args:
            status: Lifecycle filter (default "open"). Same values as
                `kalshi_get_markets` ("unopened"/"open"/"closed"/"settled";
                comma-separated multiples OK, which is why this is a free
                string rather than an enum).
            top: How many series rows to return, 1-200. Default 50. This caps
                the RESPONSE, not the scan — `series_count` always reports how
                many distinct series the sweep actually saw.
            sort_by: Which metric picks the top `top`. "volume_24h" (default,
                descending) for where the money is; "market_count"
                (descending) for raw supply — the right one for spotting a
                newly-listed event class; "soonest_close" (ascending) for what
                is about to resolve.
            scan_all: Sweep the entire listing (default True — a partial census
                is a misleading one). Set False to cap the scan at 1000 markets
                for a fast, deliberately partial sample.

        Multivariate (`KXMVE…`) combos are excluded server-side, matching
        `kalshi_find_liquid_markets`. A combo census would be dominated by
        auto-generated parlays with empty books.

        Series-ticker caveat: Kalshi does NOT return `series_ticker` on market
        objects (it exists only as a query filter), so it is DERIVED from the
        ticker prefix per Kalshi's `SERIES-EVENTSUFFIX-OUTCOME` convention —
        "KXMLBGAME-26AUG141910SDCLE-CLE" rolls up to "KXMLBGAME". That holds
        across every series observed in prod, but it is a convention, not a
        contract: confirm a newly-spotted series with `kalshi_get_series`.

        Returns:
            `series`: top-`top` rows of `{series_ticker, market_count,
                event_count, volume_24h, min_spread_dollars,
                soonest_close_time}`. `min_spread_dollars` is null when no
                market in the series had a two-sided book (an empty or
                one-sided book is skipped, not counted as a 0.00 spread).
            `series_count`: distinct series seen by the sweep (>= len(series)).
            `scanned`, `requests`: markets seen and listing requests spent.
            `complete`: True only if the sweep reached the END of the listing.
                False means the census is partial — `stopped_by` names the
                wall ("scan_limit" when `scan_all=False`, else
                "request_budget"/"time_budget"/"market_cap").
            `stopped_by`: null when `complete`.
        """
        acc: dict[str, dict[str, Any]] = {}
        scan = await _scan_markets_excluding_mve(
            client,
            scan_limit=1000,
            status=status,
            scan_all=scan_all,
            on_page=lambda page: _fold_series_page(acc, page),
        )
        return {
            "series": _finalize_series(acc, top=top, sort_by=sort_by),
            "series_count": len(acc),
            "scanned": scan.scanned,
            "requests": scan.requests,
            "status": status,
            "sort_by": sort_by,
            "complete": scan.complete,
            "stopped_by": scan.stopped_by,
        }

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
