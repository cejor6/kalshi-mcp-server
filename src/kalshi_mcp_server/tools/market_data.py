"""Market data tools — orderbook + candlesticks.

These are the read-time endpoints an agent needs to actually price a
trade decision: live orderbook depth, and historical OHLC bars.

Both endpoints are read-bucket. The orderbook in particular updates fast
on liquid markets — for sustained polling, prefer the WebSocket
`orderbook_delta` channel when that lands (see resources/ in a future
commit).
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from kalshi_mcp_server.errors import KalshiAPIError, RateLimitError
from kalshi_mcp_server.rate_limit import DEFAULT_ENDPOINT_COST
from kalshi_mcp_server.tools.discovery import _event_hint, _validate_ticker

if TYPE_CHECKING:
    from fastmcp import FastMCP


# Kalshi's candlestick endpoints accept ONLY these three bar widths (minutes):
# minute, hour, day. Any other value (e.g. 5, 240) is rejected with an opaque
# "400 bad request" — which an agent loops on. Reject locally with the valid
# set named so the caller can self-correct. (Confirmed live against prod.)
_VALID_PERIOD_INTERVALS: tuple[int, ...] = (1, 60, 1440)

# Kalshi caps a single candlestick request at this many periods. The count is
# CEIL((end_ts - start_ts) / (period_interval * 60)) — a partial TRAILING
# period still counts toward the cap. Confirmed live against prod: a 300000s
# window at 1m (exactly 5000) returns 200, but 300000+1s (ceil -> 5001) AND a
# full 5001 periods BOTH 400. So this is `ceil`, NOT `floor`/`//` — do not
# "simplify" it or the one-second-over case slips through to an opaque Kalshi
# 400 (the exact failure this guard exists to prevent).
_MAX_CANDLESTICK_PERIODS = 5000


def _validate_candlestick_window(start_ts: int, end_ts: int, period_interval: int) -> None:
    """Pre-flight the candlestick params Kalshi silently 400s on.

    Three failure modes, all confirmed live, all surfacing as an opaque
    "400 bad request" that an agent retries blindly (the hang the issue
    reports):

      1. `period_interval` not in {1, 60, 1440} (minute/hour/day).
      2. an inverted/empty window (`end_ts <= start_ts`).
      3. more than 5000 periods requested in a single window.

    Reject locally with an actionable message — naming the valid set, the
    ordering, or the overshoot — so the caller fixes the call instead of
    looping on a generic 400. Raises `KalshiAPIError(status=0, ...)` to
    match the repo's pre-flight-validation convention (`_validate_ticker`).
    """
    if period_interval not in _VALID_PERIOD_INTERVALS:
        raise KalshiAPIError(
            status=0,
            message=(
                f"period_interval must be one of 1 (minute), 60 (hour), or "
                f"1440 (day) — Kalshi rejects every other value with a 400. "
                f"Got {period_interval!r}."
            ),
        )
    if end_ts <= start_ts:
        raise KalshiAPIError(
            status=0,
            message=(
                f"end_ts ({end_ts}) must be greater than start_ts ({start_ts}); "
                "Kalshi 400s on an inverted or empty window. Both are unix seconds."
            ),
        )
    periods = math.ceil((end_ts - start_ts) / (period_interval * 60))
    if periods > _MAX_CANDLESTICK_PERIODS:
        raise KalshiAPIError(
            status=0,
            message=(
                f"This window spans ~{periods} candles at period_interval="
                f"{period_interval}m, over Kalshi's {_MAX_CANDLESTICK_PERIODS}-candle "
                "cap (the request would 400). Narrow the window or use a larger "
                "period_interval (1 -> 60 -> 1440)."
            ),
        )


def _book_is_empty(body: dict[str, Any]) -> bool:
    """True if an orderbook response has no resting size on either side.

    Kalshi nests the book under `orderbook_fp` (`yes_dollars` / `no_dollars`)
    after its fixed-point migration; the legacy `orderbook` used `yes` / `no`.
    Check both so the empty-detection survives either shape; if Kalshi
    changes the layout again, this returns False (book passed through as-is),
    which is the safe failure mode.
    """
    if not isinstance(body, dict):
        return False
    book = body.get("orderbook_fp")
    if book is None:
        book = body.get("orderbook")
    # No recognized book container → don't treat as empty (fail safe): an
    # unknown layout must not wrongly trigger the event-hint path.
    if not isinstance(book, dict):
        return False
    return not (
        book.get("yes_dollars") or book.get("no_dollars") or book.get("yes") or book.get("no")
    )


# ── Batch orderbooks ───────────────────────────────────────────────────────
#
# Kalshi's `GET /markets/orderbooks` accepts up to 100 tickers. We cap at 25
# because the binding constraint here is the CALLER's context, not Kalshi's:
# 25 books x 2 sides x `depth` levels is already a chunky tool result, and the
# tool exists to save read budget for scan lenses, not to dump the exchange.
# Raise this only alongside a smaller default `depth`.
_MAX_ORDERBOOK_TICKERS = 25

# The batch endpoint has NO `depth` query param (unlike the single-market one,
# which takes depth 0-100) — confirmed in the API reference. So depth is
# applied client-side, and it must slice the TAIL: Kalshi returns price levels
# ASCENDING by price, and both sides are BIDS, so the BEST level is the LAST
# element. Verified live against prod — a full book's last `yes_dollars` entry
# equals the market's `yes_bid_dollars`/`yes_bid_size_fp`, and asking the
# single-market endpoint for depth=3 returns exactly the last 3 entries in the
# same ascending order. Slicing [:depth] instead of [-depth:] would silently
# return the WORST levels — i.e. a scan lens would price every market off dust
# orders. Don't "fix" this.
_ORDERBOOK_SIDE_KEYS = ("yes_dollars", "no_dollars", "yes", "no")


def _truncate_book(book: Any, depth: int) -> dict[str, Any]:
    """Keep only the best `depth` levels per side of one orderbook container.

    Best = last, since Kalshi orders levels ascending by price and both sides
    are bids. Unrecognized shapes pass through untouched rather than being
    dropped — losing data is worse than returning a little extra.
    """
    if not isinstance(book, dict):
        return {}
    out: dict[str, Any] = {}
    for key, levels in book.items():
        if key in _ORDERBOOK_SIDE_KEYS and isinstance(levels, list):
            out[key] = levels[-depth:] if depth > 0 else levels
        else:
            out[key] = levels
    return out


def _project_orderbook_entry(entry: dict[str, Any], depth: int) -> dict[str, Any]:
    """Shape one `{ticker, orderbook_fp: {...}}` entry from the batch response."""
    projected: dict[str, Any] = {"ticker": entry.get("ticker")}
    for container in ("orderbook_fp", "orderbook"):
        if container in entry:
            projected[container] = _truncate_book(entry[container], depth)
    return projected


def _dedupe_tickers(tickers: list[str]) -> list[str]:
    """Validate, strip, and order-preservingly de-dupe a ticker list."""
    cleaned = [_validate_ticker(t) for t in tickers]
    return list(dict.fromkeys(cleaned))


def register(server: FastMCP) -> None:
    """Register market-data tools against the FastMCP server."""
    client = server._kalshi_client  # type: ignore[attr-defined]

    @server.tool
    async def kalshi_get_orderbook(
        ticker: str,
        depth: Annotated[int, Field(ge=0)] = 10,
    ) -> dict[str, Any]:
        """Get the current orderbook for a market.

        Args:
            ticker: MARKET ticker, e.g. "KXFED-26MAR19-B5.25". Must carry
                the outcome suffix — an EVENT ticker (e.g. "…PITHOU" without
                "-HOU") has no single book and would otherwise return an
                empty book with no error; this tool detects that case and
                raises a hint naming the real market tickers instead.
            depth: Number of price levels per side to return (default 10).
                Note: Kalshi rejects very large depth values with a 400.
                `depth=0` is interpreted as "no limit" and returns the
                full book.

        Returns a nested object: `{"orderbook_fp": {"yes_dollars": [...],
        "no_dollars": [...]}}`. The `_fp` ("fixed-point") prefix is
        Kalshi's naming convention from their fixed-point price
        migration — the values inside are STRING-formatted dollars, not
        fixed-point integers. Each level is `[price, size]` as strings,
        e.g. `["0.50", "10.00"]` means $0.50 price x 10 contracts.

        Remember Kalshi's two-sided book: YES at price X cents is
        equivalent to NO at (100 - X) cents.
        """
        ticker = _validate_ticker(ticker)
        params: dict[str, Any] = {"depth": depth}
        body = await client.get(f"/markets/{ticker}/orderbook", params=params)

        # An EVENT ticker (or a genuinely dead market) returns an empty book
        # with no error. Disambiguate: if the ticker resolves to an event,
        # raise an actionable hint rather than letting the caller read the
        # empty book as "no liquidity". A real but illiquid market is not an
        # event, so `_event_hint` returns None and the empty book passes through.
        if _book_is_empty(body):
            hint = await _event_hint(client, ticker)
            if hint:
                raise KalshiAPIError(status=0, message=hint)
        return body

    # Keep the emitted tool DESCRIPTION (everything before "Args:") under 1024
    # chars — OpenAI rejects longer tool descriptions. Per-field detail belongs
    # in the Args: block, which FastMCP routes to the parameter schema.
    @server.tool
    async def kalshi_get_orderbooks(
        tickers: list[str],
        depth: Annotated[int, Field(ge=1, le=100)] = 5,
    ) -> dict[str, Any]:
        """Fetch orderbooks for up to 25 markets in ONE call.

        The batch form of `kalshi_get_orderbook`. Use it whenever a scan or
        anomaly lens needs books for a shortlist: one request instead of N
        keeps the read bucket for the next sweep, and the shallow default
        depth keeps 25 books inside a normal tool-result budget.

        Errors are isolated per ticker. A bad, unknown, or event-level ticker
        yields an `error` entry for THAT ticker only — the batch still returns
        every book it could resolve, so one typo never costs you the call.

        Args:
            tickers: 1-25 MARKET tickers (with outcome suffixes, e.g.
                "KXFED-26MAR19-B5.25"). Duplicates are collapsed and the cap
                applies AFTER de-duping. Over 25 is rejected with a message
                telling you to split the batch — the cap protects your context,
                not Kalshi (its endpoint allows 100). An EVENT ticker has no
                single book and comes back as an error entry.
            depth: Price levels to keep PER SIDE, 1-100. Default 5 — enough
                for top-of-book plus a few levels of context, which is what a
                liquidity or anomaly lens actually reads. Raise it only for a
                handful of tickers; depth x tickers is what drives the response
                size. Kalshi's batch endpoint has no depth parameter, so this
                is applied server-side by this MCP after fetching the full
                books — it saves YOUR context, not Kalshi's bandwidth.

        Returns:
            `orderbooks`: one entry per requested ticker, in request order.
                Success entries match `kalshi_get_orderbook`'s shape —
                `{"ticker": …, "orderbook_fp": {"yes_dollars": [[price, size],
                …], "no_dollars": […]}}` with each side truncated to the BEST
                `depth` levels. Failure entries are
                `{"ticker": …, "error": {"status": …, "message": …}}`.
            `requested`, `returned`, `errors`: counts, so a caller can assert
                completeness without walking the list.
            `depth`, `source`: the applied depth, and whether the data came
                from Kalshi's batch endpoint ("batch") or the per-ticker
                fallback ("per_ticker") used when the batch call itself fails.

        Both sides are BIDS, ascending by price — the best level is the LAST
        one in each list, and a YES bid at X is a NO ask at (1 - X). This
        matches `kalshi_get_orderbook` exactly; only the batching differs.
        """
        if not tickers:
            raise KalshiAPIError(
                status=0,
                message="tickers must contain at least one market ticker.",
            )
        cleaned = _dedupe_tickers(tickers)
        if len(cleaned) > _MAX_ORDERBOOK_TICKERS:
            raise KalshiAPIError(
                status=0,
                message=(
                    f"kalshi_get_orderbooks accepts at most {_MAX_ORDERBOOK_TICKERS} "
                    f"tickers per call, got {len(cleaned)} (after de-duping). Split the "
                    "list into batches — the cap bounds the response size, since each "
                    "book carries up to `depth` levels per side."
                ),
            )

        # Kalshi bills batch operations per item, so debit the local limiter
        # per item too rather than treating this as one cheap read. Clamped to
        # the read bucket's capacity: `TokenBucket.acquire` REJECTS any cost
        # above capacity outright (an empty bucket could never satisfy it), and
        # on the Basic tier 25 items x 10 = 250 already exceeds the 200-token
        # read budget. Without the clamp every large batch would raise
        # RateLimitError locally and silently degrade to the per-ticker
        # fallback — the exact opposite of what this tool is for.
        cost = min(DEFAULT_ENDPOINT_COST * len(cleaned), client.rate_limiter.read.capacity)
        by_ticker: dict[str, dict[str, Any]] = {}
        source = "batch"
        try:
            body = await client.get("/markets/orderbooks", params={"tickers": cleaned}, cost=cost)
        except RateLimitError:
            # Never answer "you are going too fast" by firing N more requests.
            raise
        except KalshiAPIError:
            # The batch endpoint is all-or-nothing: one malformed ticker can
            # 400 the whole request. Fall back to per-ticker fetches so a
            # single bad entry degrades to one error row instead of losing
            # every book. Costs N reads, but only on this already-failed path.
            source = "per_ticker"
            results = await asyncio.gather(
                *(client.get(f"/markets/{t}/orderbook", params={"depth": depth}) for t in cleaned),
                return_exceptions=True,
            )
            for ticker, result in zip(cleaned, results, strict=True):
                if isinstance(result, KalshiAPIError):
                    by_ticker[ticker] = {
                        "ticker": ticker,
                        "error": {"status": result.status, "message": result.message},
                    }
                elif isinstance(result, BaseException):
                    # Not a Kalshi API failure (cancellation, a bug) — isolating
                    # it as a per-ticker "error" row would hide a real problem.
                    # `from None` drops the batch failure from the chain: it's
                    # incidental context, not the cause.
                    raise result from None
                else:
                    # Already depth-limited server-side; truncate anyway so the
                    # two paths return identical shapes regardless of what
                    # Kalshi honored.
                    # `ticker` last: it's the one we asked for, so it wins over
                    # anything the single-market response happens to carry.
                    by_ticker[ticker] = _project_orderbook_entry(
                        {**result, "ticker": ticker}, depth
                    )
        else:
            for entry in body.get("orderbooks") or []:
                if not isinstance(entry, dict):
                    continue
                entry_ticker = entry.get("ticker")
                if isinstance(entry_ticker, str) and entry_ticker:
                    by_ticker[entry_ticker] = _project_orderbook_entry(entry, depth)

        # Anything requested but absent from the response is reported as an
        # error row rather than silently dropped — a caller iterating the
        # result must not mistake a missing book for an empty one.
        orderbooks: list[dict[str, Any]] = []
        errors = 0
        for ticker in cleaned:
            entry = by_ticker.get(ticker)
            if entry is None:
                entry = {
                    "ticker": ticker,
                    "error": {
                        "status": 404,
                        "message": (
                            f"Kalshi returned no orderbook for {ticker!r}. Likely an "
                            "unknown ticker, or an EVENT ticker passed where a MARKET "
                            "ticker (with its outcome suffix) is required."
                        ),
                    },
                }
            if "error" in entry:
                errors += 1
            orderbooks.append(entry)

        return {
            "orderbooks": orderbooks,
            "requested": len(cleaned),
            "returned": len(orderbooks) - errors,
            "errors": errors,
            "depth": depth,
            "source": source,
        }

    @server.tool
    async def kalshi_get_market_candlesticks(
        ticker: str,
        series_ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: Literal[1, 60, 1440] = 60,
    ) -> dict[str, Any]:
        """Get OHLC candles for a single market over a time window.

        Args:
            ticker: Market ticker.
            series_ticker: The series this market belongs to (Kalshi's
                candlestick endpoint requires both — series scopes the
                lookup, ticker filters the result).
            start_ts: Window start as unix seconds. Must be < end_ts.
            end_ts: Window end as unix seconds. Must be > start_ts —
                Kalshi returns a 400 "bad request" on inverted windows.
            period_interval: Bar width in MINUTES. Kalshi accepts ONLY
                1 (minute), 60 (hour), or 1440 (day) — ANY other value
                (e.g. 5 or 240) is rejected with a 400. Default 60 (hourly).

        The window may span at most 5000 candles — i.e.
        (end_ts - start_ts) / (period_interval * 60) <= 5000 — or Kalshi
        400s. Widen `period_interval` or narrow the window if you hit that.
        Both limits are validated locally first, so an out-of-range call
        returns a clear message instead of an opaque 400.

        Returns an array of candlesticks with open/high/low/close
        prices and per-bar volume.
        """
        ticker = _validate_ticker(ticker)
        series_ticker = _validate_ticker(series_ticker, name="series_ticker")
        _validate_candlestick_window(start_ts, end_ts, period_interval)
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
        period_interval: Literal[1, 60, 1440] = 60,
    ) -> dict[str, Any]:
        """Get OHLC candles per market in an event over a time window.

        Args:
            event_ticker: Event ticker.
            series_ticker: The series this event belongs to.
            start_ts: Window start (unix seconds). Must be < end_ts.
            end_ts: Window end (unix seconds).
            period_interval: Bar width in MINUTES. Kalshi accepts ONLY
                1 (minute), 60 (hour), or 1440 (day); any other value
                (e.g. 5 or 240) 400s. Default 60. Same 5000-candle window
                cap as `kalshi_get_market_candlesticks`; both are validated
                locally first so you get a clear message, not an opaque 400.

        Returns a PARALLEL-ARRAY response (not the same shape as
        `kalshi_get_market_candlesticks`):

            {
                "market_tickers":       ["KX-A-T1", "KX-A-T2", ...],
                "market_candlesticks":  [[...],     [...],     ...],
                "adjusted_end_ts":      ...,
            }

        The Nth entry of `market_candlesticks` is the candle list for
        the Nth ticker in `market_tickers`. Use Python's `zip()` (or
        equivalent) to pair them up.
        """
        event_ticker = _validate_ticker(event_ticker, name="event_ticker")
        series_ticker = _validate_ticker(series_ticker, name="series_ticker")
        _validate_candlestick_window(start_ts, end_ts, period_interval)
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
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
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
            cursor: Pagination cursor. Kalshi silently returns an empty
                list on bad cursors.
            min_ts: Lower bound on trade ts (unix seconds).
            max_ts: Upper bound on trade ts (unix seconds).
        """
        ticker = _validate_ticker(ticker)
        params: dict[str, Any] = {"limit": limit, "ticker": ticker}
        if cursor:
            params["cursor"] = cursor
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        return await client.get("/markets/trades", params=params)
