"""Wiring tests for the wide-scan tool surface.

These drive the real tool functions through a mocked HTTP layer
(`httpx.MockTransport`) — no Kalshi credentials, no network. The pure
helpers behind them are unit-tested in test_discovery.py; what's covered
here is the parts only the wired tool can prove:

* `kalshi_find_liquid_markets(scan_all=True)` actually pages to exhaustion
  and reports `complete`/`stopped_by` honestly.
* `kalshi_get_orderbooks` isolates per-ticker failures, truncates to the
  BEST depth levels, and falls back to per-ticker fetches when the batch
  endpoint itself fails.
* `kalshi_get_series_summary` rolls a paged listing up per series.
* `kalshi_get_batch_candlesticks` rejects an over-budget window before the
  request goes out.

The combo / parlay surface lives in test_multivariate.py.
"""

from __future__ import annotations

import httpx
import pytest
from fastmcp import FastMCP

from kalshi_mcp_server.auth import KalshiSigner
from kalshi_mcp_server.client import KalshiClient
from kalshi_mcp_server.config import DEMO_REST_BASE, DEMO_WS_URL, Config
from kalshi_mcp_server.errors import KalshiAPIError, RateLimitError
from kalshi_mcp_server.rate_limit import KalshiRateLimiter, TierLimits
from kalshi_mcp_server.safety import SafetyController
from kalshi_mcp_server.tools import discovery, market_data


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
    discovery.register(server)
    market_data.register(server)
    return server


async def _tool_fn(server: FastMCP, name: str):
    tool = await server.get_tool(name)
    assert tool is not None, f"Tool {name!r} not registered"
    return tool.fn


# ── kalshi_find_liquid_markets: scan_all ───────────────────────────────────


def _listing_pages(page_count: int, per_page: int, *, terminal: bool = True):
    """Build a `/markets` handler serving `page_count` cursor-linked pages."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        cursor = request.url.params.get("cursor")
        index = 0 if cursor is None else int(cursor.removeprefix("c")) + 1
        markets = [
            {
                "ticker": f"KXS{index}-E-{i}",
                "event_ticker": f"KXS{index}-E",
                "volume_24h_fp": str(100 - index),
                "yes_bid_dollars": "0.4000",
                "yes_ask_dollars": "0.4200",
                "close_time": f"2026-08-{20 + index:02d}T00:00:00Z",
                "rules_primary": "verbose text that must not survive projection",
            }
            for i in range(per_page)
        ]
        last = index == page_count - 1
        return httpx.Response(
            200,
            json={"markets": markets, "cursor": "" if (last and terminal) else f"c{index}"},
        )

    return handler, calls


@pytest.mark.asyncio
async def test_find_liquid_markets_scan_all_sweeps_full_listing(rsa_private_key):
    """scan_all must page past `scan_limit` to the end of the listing and
    report `complete: true` — the whole point is that the ranking stops being
    an arbitrary slice."""
    handler, calls = _listing_pages(4, 3)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_find_liquid_markets")

    out = await fn(limit=2, scan_limit=3, scan_all=True)

    assert len(calls) == 4
    assert out["scanned"] == 12  # not 3
    assert out["requests"] == 4
    assert out["complete"] is True
    assert out["stopped_by"] is None
    assert out["scan_all"] is True
    assert out["scan_limit"] is None  # meaningless in sweep mode, so reported as null
    # Shortlist stays small and minimal-projected regardless of scan size.
    assert len(out["markets"]) == 2
    assert "rules_primary" not in out["markets"][0]
    # Ranked by 24h volume: page 0's markets carry the highest volume.
    assert out["markets"][0]["volume_24h_fp"] == "100"


@pytest.mark.asyncio
async def test_find_liquid_markets_scan_all_streams_instead_of_retaining(rsa_private_key):
    """A full sweep must fold each page into a running top-K, not retain every
    market. Tens of thousands of ~2KB objects held only to sort once and keep
    `limit` is a lot of memory for nothing — the pager's `on_page` hook exists
    for exactly this, and the ranking must come out identical either way."""
    handler, calls = _listing_pages(4, 3)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_find_liquid_markets")

    out = await fn(limit=2, scan_all=True)

    assert len(calls) == 4
    assert out["scanned"] == 12  # everything was still counted
    # Streaming top-K is exact: page 0 carries the highest volume (100).
    assert [m["volume_24h_fp"] for m in out["markets"]] == ["100", "100"]
    assert len(out["markets"]) == 2


@pytest.mark.asyncio
async def test_find_liquid_markets_scan_all_applies_min_volume(rsa_private_key):
    """The streaming fold must apply min_volume too, not just top-K."""
    handler, _ = _listing_pages(4, 3)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_find_liquid_markets")

    # Pages carry volumes 100, 99, 98, 97 — cut everything below 99.
    out = await fn(limit=10, scan_all=True, min_volume=99)

    assert {m["volume_24h_fp"] for m in out["markets"]} == {"100", "99"}
    assert len(out["markets"]) == 6  # two pages x 3 markets
    assert out["scanned"] == 12  # the scan still saw everything


@pytest.mark.asyncio
async def test_find_liquid_markets_default_is_unchanged(rsa_private_key):
    """Backwards compatibility: without scan_all the tool still windows at
    `scan_limit` and reports it, exactly as before."""
    handler, calls = _listing_pages(4, 3, terminal=False)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_find_liquid_markets")

    out = await fn(limit=2, scan_limit=3)

    assert len(calls) == 1
    assert out["scanned"] == 3
    assert out["scan_limit"] == 3
    assert out["scan_all"] is False
    assert out["complete"] is False
    assert out["stopped_by"] == "scan_limit"


@pytest.mark.asyncio
async def test_find_liquid_markets_scan_all_reports_request_cap_honestly(
    rsa_private_key, monkeypatch
):
    """When a sweep hits its internal request budget the result must say
    `complete: false` with the binding wall named — never a silent partial
    scan dressed up as exhaustive."""
    monkeypatch.setattr(discovery, "_SCAN_ALL_MAX_REQUESTS", 2)
    handler, calls = _listing_pages(50, 3, terminal=False)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_find_liquid_markets")

    out = await fn(limit=2, scan_all=True)

    assert len(calls) == 2
    assert out["requests"] == 2
    assert out["complete"] is False
    assert out["stopped_by"] == "request_budget"


# ── kalshi_get_series_summary ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_series_summary_rolls_up_per_series(rsa_private_key):
    handler, calls = _listing_pages(3, 2)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_series_summary")

    out = await fn()

    assert len(calls) == 3
    assert out["complete"] is True
    assert out["scanned"] == 6
    # Each page carries its own series (KXS0/KXS1/KXS2), 2 markets apiece.
    assert out["series_count"] == 3
    assert [row["series_ticker"] for row in out["series"]] == ["KXS0", "KXS1", "KXS2"]
    first = out["series"][0]
    assert first["market_count"] == 2
    assert first["event_count"] == 1
    assert first["volume_24h"] == 200.0
    assert first["min_spread_dollars"] == pytest.approx(0.02)
    assert first["soonest_close_time"] == "2026-08-20T00:00:00Z"


@pytest.mark.asyncio
async def test_series_summary_top_caps_rows_not_the_scan(rsa_private_key):
    """`top` bounds the RESPONSE. `series_count` must still report everything
    the sweep saw, or a census would quietly under-report new supply."""
    handler, _ = _listing_pages(3, 2)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_series_summary")

    out = await fn(top=1)

    assert len(out["series"]) == 1
    assert out["series_count"] == 3
    assert out["scanned"] == 6


@pytest.mark.asyncio
async def test_series_summary_excludes_combos_server_side(rsa_private_key):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"markets": [], "cursor": ""})

    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_series_summary")
    await fn(status="open")

    assert seen[0].url.params["mve_filter"] == "exclude"
    assert seen[0].url.params["status"] == "open"


# ── kalshi_get_orderbooks ──────────────────────────────────────────────────

# A full book: ascending by price, both sides are BIDS, so the BEST levels
# are the LAST ones. Truncation must keep the tail.
_FULL_YES = [["0.0100", "5.00"], ["0.2000", "9.00"], ["0.4200", "166.00"], ["0.4600", "449.05"]]
_FULL_NO = [["0.0100", "251.00"], ["0.3500", "140.00"], ["0.5100", "522.00"], ["0.5200", "349.00"]]


def _batch_handler(books: dict[str, dict], *, status: int = 200):
    """Handler for `GET /markets/orderbooks` returning only the tickers in
    `books` (mirroring Kalshi omitting unknown tickers)."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "bad tickers"}})
        requested = request.url.params.get_list("tickers")
        return httpx.Response(
            200,
            json={
                "orderbooks": [
                    {"ticker": t, "orderbook_fp": books[t]} for t in requested if t in books
                ]
            },
        )

    return handler, calls


@pytest.mark.asyncio
async def test_get_orderbooks_batches_and_keeps_best_levels(rsa_private_key):
    """One request for N books, and depth must slice the TAIL — the best
    levels. Slicing the head would price every market off dust orders."""
    books = {
        "KX-A": {"yes_dollars": _FULL_YES, "no_dollars": _FULL_NO},
        "KX-B": {"yes_dollars": _FULL_YES, "no_dollars": _FULL_NO},
    }
    handler, calls = _batch_handler(books)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_orderbooks")

    out = await fn(tickers=["KX-A", "KX-B"], depth=2)

    assert len(calls) == 1  # one call, two books
    assert calls[0].url.path.endswith("/markets/orderbooks")
    assert calls[0].url.params.get_list("tickers") == ["KX-A", "KX-B"]
    assert out["source"] == "batch"
    assert out["requested"] == 2
    assert out["returned"] == 2
    assert out["errors"] == 0
    book = out["orderbooks"][0]["orderbook_fp"]
    assert book["yes_dollars"] == [["0.4200", "166.00"], ["0.4600", "449.05"]]
    assert book["no_dollars"] == [["0.5100", "522.00"], ["0.5200", "349.00"]]


@pytest.mark.asyncio
async def test_get_orderbooks_isolates_a_missing_ticker(rsa_private_key):
    """A ticker Kalshi doesn't return must come back as an error ENTRY, not
    be silently dropped and not fail the batch."""
    handler, _ = _batch_handler({"KX-A": {"yes_dollars": _FULL_YES, "no_dollars": []}})
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_orderbooks")

    out = await fn(tickers=["KX-A", "KX-BOGUS"])

    assert [e["ticker"] for e in out["orderbooks"]] == ["KX-A", "KX-BOGUS"]  # request order
    assert "orderbook_fp" in out["orderbooks"][0]
    assert out["orderbooks"][1]["error"]["status"] == 404
    assert "EVENT ticker" in out["orderbooks"][1]["error"]["message"]
    assert out["requested"] == 2
    assert out["returned"] == 1
    assert out["errors"] == 1


@pytest.mark.asyncio
async def test_get_orderbooks_falls_back_per_ticker_when_batch_fails(rsa_private_key):
    """The batch endpoint is all-or-nothing: one malformed ticker can 400 the
    whole request. The fallback must still return every good book, with the
    bad one isolated to its own error entry."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/markets/orderbooks"):
            return httpx.Response(400, json={"error": {"message": "invalid ticker in batch"}})
        if "KX-BAD" in path:
            return httpx.Response(404, json={"error": {"message": "market not found"}})
        return httpx.Response(200, json={"orderbook_fp": {"yes_dollars": _FULL_YES}})

    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_orderbooks")

    out = await fn(tickers=["KX-A", "KX-BAD", "KX-C"], depth=1)

    assert out["source"] == "per_ticker"
    assert out["requested"] == 3
    assert out["returned"] == 2
    assert out["errors"] == 1
    by_ticker = {e["ticker"]: e for e in out["orderbooks"]}
    assert by_ticker["KX-BAD"]["error"]["status"] == 404
    assert by_ticker["KX-A"]["orderbook_fp"]["yes_dollars"] == [["0.4600", "449.05"]]
    # One failed batch attempt, then one request per ticker.
    assert sum(1 for p in calls if p.endswith("/markets/orderbooks")) == 1
    assert sum(1 for p in calls if p.endswith("/orderbook")) == 3


@pytest.mark.asyncio
async def test_get_orderbooks_rejects_oversized_batch(rsa_private_key):
    """The cap is enforced BEFORE any wire call, with a message that says how
    to fix it."""
    calls: list[str] = []
    server = _make_server(
        rsa_private_key,
        lambda r: (calls.append(r.url.path), httpx.Response(200, json={}))[1],
    )
    fn = await _tool_fn(server, "kalshi_get_orderbooks")

    with pytest.raises(KalshiAPIError) as exc:
        await fn(tickers=[f"KX-{i}" for i in range(26)])

    assert str(market_data._MAX_ORDERBOOK_TICKERS) in exc.value.message
    assert "Split the list" in exc.value.message
    assert calls == []


@pytest.mark.asyncio
async def test_get_orderbooks_dedupes_before_applying_the_cap(rsa_private_key):
    """25 distinct tickers repeated must pass, and be requested once each."""
    tickers = [f"KX-{i}" for i in range(25)] * 2
    handler, calls = _batch_handler({})
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_orderbooks")

    out = await fn(tickers=tickers)

    assert out["requested"] == 25
    assert len(calls[0].url.params.get_list("tickers")) == 25


@pytest.mark.asyncio
async def test_get_orderbooks_cost_never_exceeds_read_capacity(rsa_private_key):
    """Regression: `TokenBucket.acquire` rejects any cost above capacity, and
    a full 25-item batch bills 250 against the Basic tier's 200-token read
    budget. Unclamped, every large batch would raise locally and silently
    degrade to the per-ticker fallback — the opposite of this tool's purpose."""
    handler, calls = _batch_handler({})
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_orderbooks")

    out = await fn(tickers=[f"KX-{i}" for i in range(25)])

    assert out["source"] == "batch"  # not "per_ticker"
    assert len(calls) == 1
    assert calls[0].url.path.endswith("/markets/orderbooks")


@pytest.mark.asyncio
async def test_get_orderbooks_does_not_fan_out_on_rate_limit(rsa_private_key):
    """A 429 must propagate, not trigger N more requests — answering "you are
    going too fast" with a fan-out is how a soft limit becomes a hard one."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_orderbooks")

    with pytest.raises(RateLimitError):
        await fn(tickers=["KX-A", "KX-B", "KX-C"])
    assert len(calls) == 1  # the batch attempt only


@pytest.mark.asyncio
async def test_get_orderbooks_rejects_empty_and_blank_tickers(rsa_private_key):
    calls: list[str] = []
    server = _make_server(
        rsa_private_key,
        lambda r: (calls.append(r.url.path), httpx.Response(200, json={}))[1],
    )
    fn = await _tool_fn(server, "kalshi_get_orderbooks")

    with pytest.raises(KalshiAPIError):
        await fn(tickers=[])
    with pytest.raises(KalshiAPIError):
        await fn(tickers=["KX-A", "   "])
    assert calls == []


# ── kalshi_get_series_list (context-blowup hardening) ──────────────────────


def _series_catalog_handler(n: int):
    """A /series handler returning `n` series with descending volume plus the
    bulky fields the minimal projection must strip."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/series")
        series = [
            {
                "ticker": f"KXS{i:04d}",
                "title": f"Series {i}",
                "category": "Economics",
                "volume_fp": str((n - i) * 100),  # S0 highest, descending
                "settlement_sources": [{"name": "src", "url": "https://x"}] * 20,
                "product_metadata": {"blob": "x" * 500},
            }
            for i in range(n)
        ]
        return httpx.Response(200, json={"series": series})

    return handler


@pytest.mark.asyncio
async def test_series_list_projects_and_caps_by_volume(rsa_private_key):
    """The live failure: one category returned 556KB. The tool must bound the
    result — top-`limit` by volume, minimal projection — not dump everything."""
    server = _make_server(rsa_private_key, _series_catalog_handler(500))
    fn = await _tool_fn(server, "kalshi_get_series_list")

    out = await fn(category="Economics", limit=10)

    assert out["total_matched"] == 500  # Kalshi returned all 500
    assert out["returned"] == 10  # but we cap the response
    assert out["truncated"] is True
    # Highest volume first (S0000 has the largest), minimal projection.
    assert out["series"][0]["ticker"] == "KXS0000"
    for entry in out["series"]:
        assert "settlement_sources" not in entry
        assert "product_metadata" not in entry
        assert set(entry) <= {
            "ticker",
            "title",
            "category",
            "frequency",
            "tags",
            "fee_type",
            "volume_fp",
            "last_updated_ts",
        }


@pytest.mark.asyncio
async def test_series_list_minimal_false_keeps_everything(rsa_private_key):
    """The escape hatch for a narrow query: `minimal=False` returns the full
    objects and requests the product-metadata blob."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"series": [{"ticker": "KXA", "settlement_sources": [{"name": "s"}]}]},
        )

    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_series_list")

    out = await fn(tags="rare", minimal=False)

    assert "settlement_sources" in out["series"][0]  # full object kept
    assert seen[0].url.params["include_product_metadata"] == "true"


@pytest.mark.asyncio
async def test_series_list_minimal_default_does_not_request_product_metadata(rsa_private_key):
    """Default (minimal) must NOT ask Kalshi for the heavy product-metadata blob."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"series": [{"ticker": "KXA", "volume_fp": "1"}]})

    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_series_list")
    await fn(category="Economics")
    assert seen[0].url.params["include_product_metadata"] == "false"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_limit", [0, -1, -50])
async def test_series_list_clamps_nonpositive_limit_to_empty(rsa_private_key, bad_limit):
    """REGRESSION: the client-side cut is the ONLY backstop (the endpoint has
    no limit param), so a negative limit must clamp to [] — unclamped it would
    slice from the END and return a near-full list. Mirrors the clamp in
    `_rank_liquid_markets` / `_TopKByVolume`."""
    server = _make_server(rsa_private_key, _series_catalog_handler(10))
    fn = await _tool_fn(server, "kalshi_get_series_list")
    out = await fn(limit=bad_limit)
    assert out["series"] == []
    assert out["returned"] == 0
    assert out["total_matched"] == 10


@pytest.mark.asyncio
async def test_series_list_not_truncated_when_under_limit(rsa_private_key):
    server = _make_server(rsa_private_key, _series_catalog_handler(3))
    fn = await _tool_fn(server, "kalshi_get_series_list")
    out = await fn(limit=100)
    assert out["total_matched"] == 3
    assert out["returned"] == 3
    assert out["truncated"] is False


# ── kalshi_get_event_forecast_history (opaque-400 → actionable) ────────────


@pytest.mark.asyncio
async def test_forecast_history_reshapes_kalshis_opaque_400(rsa_private_key):
    """Live, this endpoint 400s on doc-valid requests. The tool must turn the
    bare 'bad request' into an actionable message instead of passing it through
    for an agent to loop on."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_event_forecast_history")

    with pytest.raises(KalshiAPIError) as exc:
        await fn(
            event_ticker="KXHIGHNY-26AUG14",
            series_ticker="KXHIGHNY",
            start_ts=1_000_000,
            end_ts=1_086_400,
            period_interval=60,
        )
    msg = exc.value.message
    assert exc.value.status == 400
    assert "does not publish percentile-forecast data" in msg
    assert "KXHIGHNY-26AUG14" in msg
    assert "bad request" in msg  # Kalshi's own text is preserved


@pytest.mark.asyncio
async def test_forecast_history_passes_percentiles_as_repeated_params(rsa_private_key):
    """Encoding is confirmed correct against Kalshi's API reference (exploded
    form), so pin it: percentiles go out as repeated `percentiles=` params."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"forecast_history": []})

    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_event_forecast_history")

    await fn(
        event_ticker="KXHIGHNY-26AUG14",
        series_ticker="KXHIGHNY",
        start_ts=1_000_000,
        end_ts=1_086_400,
        percentiles=[2500, 5000, 7500],
    )
    assert seen[0].url.params.get_list("percentiles") == ["2500", "5000", "7500"]


@pytest.mark.asyncio
async def test_forecast_history_reraises_non_400_untouched(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_event_forecast_history")

    with pytest.raises(KalshiAPIError) as exc:
        await fn(
            event_ticker="KXHIGHNY-26AUG14",
            series_ticker="KXHIGHNY",
            start_ts=1_000_000,
            end_ts=1_086_400,
        )
    assert exc.value.status == 500
    assert "does not publish" not in exc.value.message


# ── read-path ticker injection (security hardening) ────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,kwargs",
    [
        ("kalshi_get_market", {"ticker": "KX/../../portfolio/balance"}),
        ("kalshi_get_orderbook", {"ticker": "KX?foo=bar"}),
        ("kalshi_get_event", {"event_ticker": "KX/../x"}),
        ("kalshi_get_series", {"series_ticker": "KX#frag"}),
        (
            "kalshi_get_market_candlesticks",
            {"ticker": "KX/../x", "series_ticker": "KXS", "start_ts": 1, "end_ts": 2},
        ),
        (
            "kalshi_get_event_forecast_history",
            {"event_ticker": "KX/../x", "series_ticker": "KXS", "start_ts": 1, "end_ts": 2},
        ),
    ],
)
async def test_read_path_tickers_reject_injection_before_the_wire(
    rsa_private_key, tool_name, kwargs
):
    """A model-supplied ticker with a path separator would steer a GET to a
    different endpoint under a valid signature (auth.py strips the query before
    signing). The read tools now validate with the path-segment guard, so this
    must fail locally with no wire call."""
    calls: list[str] = []
    server = _make_server(
        rsa_private_key,
        lambda r: (calls.append(r.url.path), httpx.Response(200, json={}))[1],
    )
    fn = await _tool_fn(server, tool_name)
    with pytest.raises(KalshiAPIError):
        await fn(**kwargs)
    assert calls == []


@pytest.mark.asyncio
async def test_batch_orderbooks_rejects_injection_ticker_before_the_wire(rsa_private_key):
    """`_dedupe_tickers` now uses the path-segment guard, so a ticker that could
    reach the per-ticker fallback path is rejected up front."""
    calls: list[str] = []
    server = _make_server(
        rsa_private_key,
        lambda r: (calls.append(r.url.path), httpx.Response(200, json={}))[1],
    )
    fn = await _tool_fn(server, "kalshi_get_orderbooks")
    with pytest.raises(KalshiAPIError):
        await fn(tickers=["KX-A", "KX/../../x"])
    assert calls == []


@pytest.mark.asyncio
async def test_get_markets_injection_ticker_never_reaches_event_hint_wire(rsa_private_key):
    """REGRESSION (qa NIT): `kalshi_get_markets` is the one path that reaches
    `_event_hint` with an UNVALIDATED value — its `tickers` arg. On an empty
    result it extracts the sole ticker and probes `/events/{ticker}`; a ticker
    with a path separator must be rejected by `_event_hint`'s defensive guard
    (fail-open) so it never steers that probe. The `/markets` query call still
    happens (httpx encodes the query), but no `/events` call does."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"markets": []})  # empty → triggers the hint path

    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_markets")

    out = await fn(tickers="KX/../../portfolio/balance")

    assert out == {"markets": []}  # clean passthrough, no crash, no hint raised
    assert not any(p.endswith("/events/KX/../../portfolio/balance") for p in paths)
    assert not any("/events/" in p for p in paths)  # the injection never probed /events


# ── find_liquid_markets `fields` projection (issue #82) ────────────────────


@pytest.mark.asyncio
async def test_find_liquid_markets_fields_narrows_the_projection(rsa_private_key):
    """A `fields` whitelist shrinks each shortlist entry so a high `limit`
    stays under the client token cap — the default minimal view is ~500 B/mkt,
    ~50 KB at limit=100."""
    handler, _ = _listing_pages(4, 3, terminal=False)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_find_liquid_markets")

    out = await fn(limit=5, scan_limit=3, fields="ticker,volume_24h_fp")

    assert out["markets"]  # non-empty
    for m in out["markets"]:
        assert set(m) <= {"ticker", "volume_24h_fp"}  # only the whitelist
        assert "yes_bid_dollars" not in m  # a default-minimal field is gone


@pytest.mark.asyncio
async def test_find_liquid_markets_fields_applies_under_scan_all(rsa_private_key):
    """The streaming (scan_all) path must honor `fields` too, not just the
    windowed path."""
    handler, _ = _listing_pages(3, 2)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_find_liquid_markets")

    out = await fn(limit=10, scan_all=True, fields="ticker")

    assert out["markets"]
    for m in out["markets"]:
        assert set(m) == {"ticker"}
