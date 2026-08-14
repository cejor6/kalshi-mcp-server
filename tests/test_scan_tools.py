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
* `kalshi_get_combo_legs` resolves legs, enriches titles, fails open on a
  title-lookup error, and reports a structured not-resolvable result
  instead of guessing.
* `kalshi_get_series_summary` rolls a paged listing up per series.
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


# ── kalshi_get_combo_legs ──────────────────────────────────────────────────

_COMBO = {
    "ticker": "KXMVECROSS-S1-ABC",
    "event_ticker": "KXMVECROSS-S1",
    "title": "yes Boston,no Milwaukee wins by over 1.5 runs",
    "mve_collection_ticker": "KXMVECROSS-R",
    "strike_type": "custom",
    "mve_selected_legs": [
        {
            "event_ticker": "KXMLBGAME-26AUG141840BOSPIT",
            "market_ticker": "KXMLBGAME-26AUG141840BOSPIT-BOS",
            "side": "yes",
        },
        {
            "event_ticker": "KXMLBSPREAD-26AUG132210MILLAD",
            "market_ticker": "KXMLBSPREAD-26AUG132210MILLAD-MIL2",
            "side": "no",
        },
    ],
}

_LEG_TITLES = {
    "KXMLBGAME-26AUG141840BOSPIT-BOS": "Will Boston beat Pittsburgh?",
    "KXMLBSPREAD-26AUG132210MILLAD-MIL2": "Will Milwaukee win by over 1.5 runs?",
}


def _combo_handler(market: dict, *, titles_status: int = 200):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path.endswith("/markets") and request.url.params.get("tickers"):
            if titles_status != 200:
                return httpx.Response(titles_status, json={"error": {"message": "nope"}})
            requested = request.url.params["tickers"].split(",")
            return httpx.Response(
                200,
                json={
                    "markets": [
                        {"ticker": t, "title": _LEG_TITLES[t], "yes_sub_title": f"{t} yes"}
                        for t in requested
                        if t in _LEG_TITLES
                    ]
                },
            )
        if path.endswith("/multivariate_event_collections/KXMVECROSS-R"):
            return httpx.Response(
                200, json={"multivariate_contract": {"size_min": 2, "size_max": 9}}
            )
        return httpx.Response(200, json={"market": market})

    return handler, calls


@pytest.mark.asyncio
async def test_combo_legs_resolves_structured_legs_with_titles(rsa_private_key):
    handler, calls = _combo_handler(_COMBO)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    out = await fn(ticker="KXMVECROSS-S1-ABC")

    assert out["resolvable"] is True
    assert out["source"] == "mve_selected_legs"
    assert out["leg_count"] == 2
    assert out["titles_resolved"] is True
    assert out["collection_ticker"] == "KXMVECROSS-R"
    assert out["legs"][0] == {
        "market_ticker": "KXMLBGAME-26AUG141840BOSPIT-BOS",
        "event_ticker": "KXMLBGAME-26AUG141840BOSPIT",
        "side": "yes",
        "title": "Will Boston beat Pittsburgh?",
        "yes_sub_title": "KXMLBGAME-26AUG141840BOSPIT-BOS yes",
    }
    assert out["legs"][1]["side"] == "no"
    # Exactly two reads: the combo, then ONE batched title lookup for all legs.
    assert len(calls) == 2
    assert calls[1].url.params["tickers"].count(",") == 1


@pytest.mark.asyncio
async def test_combo_legs_skips_title_lookup_when_not_wanted(rsa_private_key):
    handler, calls = _combo_handler(_COMBO)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    out = await fn(ticker="KXMVECROSS-S1-ABC", with_titles=False)

    assert len(calls) == 1
    assert out["titles_resolved"] is False
    assert "title" not in out["legs"][0]
    assert out["legs"][0]["market_ticker"] == "KXMLBGAME-26AUG141840BOSPIT-BOS"


@pytest.mark.asyncio
async def test_combo_legs_title_lookup_fails_open(rsa_private_key):
    """A failed title lookup must not lose the legs — titles are a
    convenience, the legs are the answer."""
    handler, _ = _combo_handler(_COMBO, titles_status=500)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    out = await fn(ticker="KXMVECROSS-S1-ABC")

    assert out["resolvable"] is True
    assert out["leg_count"] == 2
    assert out["titles_resolved"] is False
    assert out["legs"][0]["side"] == "yes"


@pytest.mark.asyncio
async def test_combo_legs_include_collection_attaches_context(rsa_private_key):
    handler, calls = _combo_handler(_COMBO)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    out = await fn(ticker="KXMVECROSS-S1-ABC", with_titles=False, include_collection=True)

    assert out["collection"] == {"size_min": 2, "size_max": 9}
    assert any("multivariate_event_collections" in c.url.path for c in calls)


@pytest.mark.asyncio
async def test_combo_legs_unresolvable_combo_returns_structured_error(rsa_private_key):
    """A real combo with no published leg breakdown must say so — and must
    NOT fall back to splitting the title string."""
    opaque = {
        "ticker": "KXMVEOPAQUE-S1-XYZ",
        "event_ticker": "KXMVEOPAQUE-S1",
        "title": "yes Boston,no Milwaukee",
        "mve_collection_ticker": "KXMVEOPAQUE-R",
    }
    handler, _ = _combo_handler(opaque)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    out = await fn(ticker="KXMVEOPAQUE-S1-XYZ")

    assert out["resolvable"] is False
    assert out["reason"] == "legs_not_published"
    assert "legs" not in out
    assert out["collection_ticker"] == "KXMVEOPAQUE-R"
    assert "will not parse it" in out["message"]


@pytest.mark.asyncio
async def test_combo_legs_on_a_plain_market_says_not_a_combo(rsa_private_key):
    plain = {
        "ticker": "KXFED-26MAR19-B5.25",
        "event_ticker": "KXFED-26MAR19",
        "title": "Fed funds above 5.25%",
    }
    handler, _ = _combo_handler(plain)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    out = await fn(ticker="KXFED-26MAR19-B5.25")

    assert out["resolvable"] is False
    assert out["reason"] == "not_a_combo_market"
    assert "kalshi_get_market" in out["message"]


@pytest.mark.asyncio
async def test_combo_legs_falls_back_to_custom_strike(rsa_private_key):
    csv_only = {
        "ticker": "KXMVECSV-S1-ABC",
        "event_ticker": "KXMVECSV-S1",
        "mve_collection_ticker": "KXMVECSV-R",
        "custom_strike": {
            "Associated Markets": "KXMLBGAME-26AUG141840BOSPIT-BOS",
            "Associated Market Sides": "yes",
            "Associated Events": "KXMLBGAME-26AUG141840BOSPIT",
        },
    }
    handler, _ = _combo_handler(csv_only)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    out = await fn(ticker="KXMVECSV-S1-ABC", with_titles=False)

    assert out["resolvable"] is True
    assert out["source"] == "custom_strike"
    assert out["legs"] == [
        {
            "market_ticker": "KXMLBGAME-26AUG141840BOSPIT-BOS",
            "event_ticker": "KXMLBGAME-26AUG141840BOSPIT",
            "side": "yes",
        }
    ]


@pytest.mark.asyncio
async def test_combo_legs_rejects_blank_ticker_before_any_call(rsa_private_key):
    calls: list[str] = []
    server = _make_server(
        rsa_private_key,
        lambda r: (calls.append(r.url.path), httpx.Response(200, json={}))[1],
    )
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    with pytest.raises(KalshiAPIError):
        await fn(ticker="  ")
    assert calls == []
