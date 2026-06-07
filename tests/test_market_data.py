"""Tests for market-data helpers and the candlestick pre-flight guards.

Two groups:

1. `_book_is_empty` — orderbook emptiness detection (issue #30). Pure
   function; the event-vs-market hint resolver it feeds is tested in
   test_discovery.py (`_event_hint`).
2. `_validate_candlestick_window` — the local guards that turn Kalshi's
   opaque candlestick 400s (invalid period_interval, inverted window,
   over-5000-candle window) into actionable errors *before* the request
   goes out. Both the pure function and its wiring into the two
   candlestick tools are covered (the wiring tests assert no HTTP call is
   made when validation fails, so the agent never sees a generic 400).
"""

from __future__ import annotations

import httpx
import pytest
from fastmcp import FastMCP

from kalshi_mcp_server.auth import KalshiSigner
from kalshi_mcp_server.client import KalshiClient
from kalshi_mcp_server.config import DEMO_REST_BASE, DEMO_WS_URL, Config
from kalshi_mcp_server.errors import KalshiAPIError
from kalshi_mcp_server.rate_limit import KalshiRateLimiter, TierLimits
from kalshi_mcp_server.safety import SafetyController
from kalshi_mcp_server.tools import market_data
from kalshi_mcp_server.tools.market_data import (
    _MAX_CANDLESTICK_PERIODS,
    _book_is_empty,
    _validate_candlestick_window,
)

# ── _book_is_empty (issue #30) ─────────────────────────────────────────────


def test_book_is_empty_true_for_empty_fp_book():
    assert _book_is_empty({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}) is True


def test_book_is_empty_true_for_empty_container_dict():
    """A book container present but with no side keys at all is still empty."""
    assert _book_is_empty({"orderbook_fp": {}}) is True


def test_book_is_empty_false_when_either_side_has_size():
    assert (
        _book_is_empty({"orderbook_fp": {"yes_dollars": [["0.50", "10.00"]], "no_dollars": []}})
        is False
    )
    assert (
        _book_is_empty({"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.47", "5.00"]]}})
        is False
    )


def test_book_is_empty_handles_legacy_orderbook_keys():
    """The pre-fixed-point shape used `orderbook` with `yes`/`no`."""
    assert _book_is_empty({"orderbook": {"yes": [], "no": []}}) is True
    assert _book_is_empty({"orderbook": {"yes": [[50, 10]], "no": []}}) is False


def test_book_is_empty_safe_on_unexpected_shapes():
    """Unknown layouts must NOT be reported as empty — that would wrongly
    trigger the event-hint path on a real response. Fail safe to False."""
    assert _book_is_empty({}) is False
    assert _book_is_empty({"orderbook_fp": None}) is False
    assert _book_is_empty({"something_else": {"yes_dollars": []}}) is False


# ── _validate_candlestick_window (candlestick 400 guards) ───────────────────


@pytest.mark.parametrize("interval", [1, 60, 1440])
def test_candlestick_window_accepts_valid_intervals(interval):
    """The three intervals Kalshi actually accepts (minute/hour/day), over a
    small window, must pass cleanly."""
    start = 1_000_000
    end = start + interval * 60 * 10  # 10 periods — well under the cap
    _validate_candlestick_window(start, end, interval)  # no raise


@pytest.mark.parametrize("bad", [0, 5, 30, 240, 1439, -60, 1441])
def test_candlestick_window_rejects_invalid_interval(bad):
    """Anything outside {1, 60, 1440} — including the 5/240 our old docstring
    wrongly recommended — must be rejected locally, naming the valid set."""
    with pytest.raises(KalshiAPIError) as exc:
        _validate_candlestick_window(1_000_000, 1_000_600, bad)
    msg = exc.value.message
    assert "period_interval" in msg
    assert "1440" in msg  # the message names the valid set


@pytest.mark.parametrize("start,end", [(1_000_000, 1_000_000), (1_000_000, 999_000)])
def test_candlestick_window_rejects_inverted_or_empty_window(start, end):
    with pytest.raises(KalshiAPIError) as exc:
        _validate_candlestick_window(start, end, 60)
    assert "end_ts" in exc.value.message


def test_candlestick_window_accepts_exactly_max_periods():
    """5000 one-minute candles (a 300000s window) is the documented ceiling —
    confirmed OK live, so it must NOT be rejected."""
    start = 1_000_000
    end = start + _MAX_CANDLESTICK_PERIODS * 60  # interval=1 → exactly 5000
    _validate_candlestick_window(start, end, 1)  # no raise


def test_candlestick_window_rejects_over_max_periods():
    start = 1_000_000
    end = start + (_MAX_CANDLESTICK_PERIODS + 1) * 60  # 5001 one-minute candles
    with pytest.raises(KalshiAPIError) as exc:
        _validate_candlestick_window(start, end, 1)
    assert str(_MAX_CANDLESTICK_PERIODS) in exc.value.message


def test_candlestick_window_cap_scales_with_interval():
    """The 5000-candle cap is on the period COUNT, not the wall-clock span —
    so a larger interval permits a proportionally larger window."""
    start = 1_000_000
    ok_end = start + _MAX_CANDLESTICK_PERIODS * 60 * 60  # 5000 hourly bars
    _validate_candlestick_window(start, ok_end, 60)  # no raise
    over_end = start + (_MAX_CANDLESTICK_PERIODS + 1) * 60 * 60  # 5001 hourly bars
    with pytest.raises(KalshiAPIError):
        _validate_candlestick_window(start, over_end, 60)


# ── candlestick tool wiring (validation fires before the HTTP call) ─────────


def _make_config() -> Config:
    return Config(
        key_id="test-key",
        private_key_path=None,
        private_key_pem="<set-in-test>",
        env="demo",
        trading_enabled=False,
        rest_base=DEMO_REST_BASE,
        ws_url=DEMO_WS_URL,
        max_order_size_usd=25.0,
        daily_limit_usd=250.0,
        max_contracts_per_order=100,
        cash_reserve_usd=0.0,
        transport="stdio",
        port=8000,
        log_level="INFO",
    )


def _make_server(rsa_private_key, handler) -> FastMCP:
    config = _make_config()
    signer = KalshiSigner(key_id="test-key", private_key=rsa_private_key)
    limiter = KalshiRateLimiter(TierLimits.basic())
    http = httpx.AsyncClient(
        base_url=config.rest_base,
        transport=httpx.MockTransport(handler),
    )
    client = KalshiClient(config=config, signer=signer, rate_limiter=limiter, http_client=http)
    server = FastMCP(name="kalshi-test")
    server._kalshi_client = client  # type: ignore[attr-defined]
    server._kalshi_config = config  # type: ignore[attr-defined]
    server._kalshi_signer = signer  # type: ignore[attr-defined]
    server._kalshi_safety = SafetyController(config)  # type: ignore[attr-defined]
    server._kalshi_rate_limiter = limiter  # type: ignore[attr-defined]
    market_data.register(server)
    return server


async def _get_tool_fn(server: FastMCP, name: str):
    tool = await server.get_tool(name)
    assert tool is not None, f"Tool {name!r} not registered"
    return tool.fn


@pytest.mark.asyncio
async def test_market_candlesticks_rejects_invalid_interval_before_request(rsa_private_key):
    """An invalid period_interval must raise locally — no HTTP request goes
    out — so the caller gets the actionable message, not Kalshi's 400."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"candlesticks": []})

    server = _make_server(rsa_private_key, handler)
    fn = await _get_tool_fn(server, "kalshi_get_market_candlesticks")
    with pytest.raises(KalshiAPIError):
        await fn(
            ticker="KX-T",
            series_ticker="KX",
            start_ts=1_000_000,
            end_ts=1_000_600,
            period_interval=240,
        )
    assert calls == []  # validation fired before any wire call


@pytest.mark.asyncio
async def test_market_candlesticks_passes_valid_params_through(rsa_private_key):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"candlesticks": [], "ticker": "KX-T"})

    server = _make_server(rsa_private_key, handler)
    fn = await _get_tool_fn(server, "kalshi_get_market_candlesticks")
    body = await fn(
        ticker="KX-T",
        series_ticker="KX",
        start_ts=1_000_000,
        end_ts=1_000_600,
        period_interval=60,
    )
    assert body == {"candlesticks": [], "ticker": "KX-T"}
    assert len(calls) == 1
    assert calls[0].endswith("/series/KX/markets/KX-T/candlesticks")


@pytest.mark.asyncio
async def test_period_interval_enum_is_in_tool_schema(rsa_private_key):
    """The accepted bar widths are baked into the tool's input schema as a
    JSON-Schema `enum`, so an MCP client / the LLM is steered to 1/60/1440 at
    generation time — the runtime guard is the backstop, not the only line of
    defense. Locks in the schema-level contract for both candlestick tools."""
    server = _make_server(rsa_private_key, lambda _: httpx.Response(200, json={}))
    for name in ("kalshi_get_market_candlesticks", "kalshi_get_event_candlesticks"):
        tool = await server.get_tool(name)
        prop = tool.parameters["properties"]["period_interval"]
        assert prop["enum"] == [1, 60, 1440]
        assert prop["default"] == 60


@pytest.mark.asyncio
async def test_event_candlesticks_rejects_over_cap_before_request(rsa_private_key):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    server = _make_server(rsa_private_key, handler)
    fn = await _get_tool_fn(server, "kalshi_get_event_candlesticks")
    over_end = 1_000_000 + (_MAX_CANDLESTICK_PERIODS + 1) * 60  # 5001 one-min candles
    with pytest.raises(KalshiAPIError):
        await fn(
            event_ticker="KXEV",
            series_ticker="KX",
            start_ts=1_000_000,
            end_ts=over_end,
            period_interval=1,
        )
    assert calls == []
