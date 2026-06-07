"""Market data tools — orderbook + candlesticks.

These are the read-time endpoints an agent needs to actually price a
trade decision: live orderbook depth, and historical OHLC bars.

Both endpoints are read-bucket. The orderbook in particular updates fast
on liquid markets — for sustained polling, prefer the WebSocket
`orderbook_delta` channel when that lands (see resources/ in a future
commit).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from kalshi_mcp_server.errors import KalshiAPIError
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
