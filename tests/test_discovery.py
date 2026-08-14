"""Tests for the discovery-tool helpers — ticker validation + market/event
projection (compact blacklist, minimal whitelist, fields override).

These are pure functions; no FastMCP or HTTP involved.
"""

from __future__ import annotations

import pytest

from kalshi_mcp_server.errors import KalshiAPIError
from kalshi_mcp_server.tools.discovery import (
    _EVENT_HINT_MISS_MAX,
    _MINIMAL_MARKET_FIELDS,
    _compact_event,
    _compact_market,
    _event_hint,
    _event_hint_misses,
    _finalize_series,
    _fold_series_page,
    _minimal_market,
    _parse_fields,
    _project_market,
    _rank_liquid_markets,
    _record_event_hint_miss,
    _scan_markets_excluding_mve,
    _series_of,
    _single_ticker,
    _TopKByVolume,
    _validate_mve_filter,
    _validate_path_ticker,
    _validate_ticker,
    _volume_24h,
    _yes_spread,
)

# Note: the `_event_hint_misses` negative cache is reset around every test by
# the autouse `_reset_event_hint_cache` fixture in conftest.py.


class _FakeClient:
    """Minimal async stand-in for KalshiClient.get used by the discovery
    helpers. Replays queued responses in order (or raises a queued error)
    and records every (path, params) call for assertions."""

    def __init__(self, responses=None, error=None):
        self._responses = list(responses or [])
        self._error = error
        self.calls: list[tuple[str, dict | None]] = []

    async def get(self, path, params=None):
        self.calls.append((path, params))
        if self._error is not None:
            raise self._error
        return self._responses.pop(0) if self._responses else {}


# ── _validate_ticker ───────────────────────────────────────────────────────


def test_validate_ticker_accepts_non_empty():
    assert _validate_ticker("KXFED-26MAR19-B5.25") == "KXFED-26MAR19-B5.25"


def test_validate_ticker_strips_surrounding_whitespace():
    assert _validate_ticker("  KXFED-26MAR19-B5.25  ") == "KXFED-26MAR19-B5.25"


def test_validate_ticker_rejects_empty_string():
    with pytest.raises(KalshiAPIError) as exc:
        _validate_ticker("")
    assert "ticker" in exc.value.message
    assert "non-empty" in exc.value.message


def test_validate_ticker_rejects_whitespace_only():
    with pytest.raises(KalshiAPIError) as exc:
        _validate_ticker("   ")
    assert "non-empty" in exc.value.message


def test_validate_ticker_rejects_non_string():
    with pytest.raises(KalshiAPIError):
        _validate_ticker(None)  # type: ignore[arg-type]
    with pytest.raises(KalshiAPIError):
        _validate_ticker(123)  # type: ignore[arg-type]


def test_validate_ticker_custom_param_name():
    """The `name` kwarg shows up in the error message — important for
    multi-ticker tools (event_ticker, series_ticker, etc.) so the agent
    knows which arg was bad."""
    with pytest.raises(KalshiAPIError) as exc:
        _validate_ticker("", name="event_ticker")
    assert "event_ticker" in exc.value.message


# ── _validate_path_ticker (charset guard on URL-path interpolation) ────────


def test_validate_path_ticker_accepts_real_ticker_shapes():
    for good in (
        "KXFED-26MAR19-B5.25",
        "KXMVECROSSCATEGORY-SHARD1-R",
        "KXMLBGAME-26AUG141910SDCLE-CLE",
        "KX_TEST.1",
    ):
        assert _validate_path_ticker(good) == good


@pytest.mark.parametrize(
    "bad",
    [
        "../portfolio/orders",
        "KX-R/../../x",
        "KX-R?limit=1",
        "KX-R#frag",
        "KX R",
        "KX-R%2F",
        "KX\nR",
    ],
)
def test_validate_path_ticker_rejects_separators_and_whitespace(bad):
    """These strings get interpolated into a request path AND into the message
    we sign, so a separator would change which endpoint is hit. Rejecting beats
    escaping — an escaped '/' would just 404 more confusingly."""
    with pytest.raises(KalshiAPIError) as exc:
        _validate_path_ticker(bad)
    assert "not valid in a Kalshi ticker" in exc.value.message


def test_validate_path_ticker_still_rejects_empty():
    with pytest.raises(KalshiAPIError) as exc:
        _validate_path_ticker("   ", name="collection_ticker")
    assert "collection_ticker" in exc.value.message


# ── _TopKByVolume (streaming fold for scan_all) ────────────────────────────


def test_topk_by_volume_matches_a_full_sort():
    """Top-K of the union equals top-K of the running top-K — the property the
    streaming fold relies on. Pin it against the batch implementation."""
    pages = [
        [{"ticker": f"P{p}-{i}", "volume_24h_fp": str((p * 7 + i * 3) % 20)} for i in range(5)]
        for p in range(4)
    ]
    fold = _TopKByVolume(limit=3)
    for page in pages:
        fold(page)
    streamed = fold.result()

    flat = [m for page in pages for m in page]
    batched = _rank_liquid_markets(flat, limit=3)
    assert [m["ticker"] for m in streamed] == [m["ticker"] for m in batched]


def test_topk_by_volume_bounds_retention():
    fold = _TopKByVolume(limit=2)
    for p in range(50):
        fold([{"ticker": f"T{p}-{i}", "volume_24h_fp": str(p)} for i in range(100)])
    assert len(fold.result()) == 2
    # Highest volume wins; 5000 markets crossed, 2 retained.
    assert all(m["volume_24h_fp"] == "49" for m in fold.result())


def test_topk_by_volume_applies_min_volume():
    fold = _TopKByVolume(limit=10, min_volume=5)
    fold([{"ticker": "A", "volume_24h_fp": "10"}, {"ticker": "B", "volume_24h_fp": "1"}])
    assert [m["ticker"] for m in fold.result()] == ["A"]


def test_topk_by_volume_projects_minimally():
    fold = _TopKByVolume(limit=1)
    fold([{"ticker": "A", "volume_24h_fp": "10", "rules_primary": "long text"}])
    assert "rules_primary" not in fold.result()[0]


# ── _compact_market ────────────────────────────────────────────────────────


def test_compact_market_keeps_essential_fields():
    full = {
        "ticker": "KX-TEST",
        "event_ticker": "KX",
        "title": "Test market",
        "yes_bid_dollars": "0.50",
        "yes_ask_dollars": "0.51",
        "no_bid_dollars": "0.49",
        "no_ask_dollars": "0.50",
        "last_price_dollars": "0.50",
        "close_time": "2026-12-31T00:00:00Z",
        "volume_24h_fp": "100.00",
        "status": "active",
    }
    compact = _compact_market(full)
    for k in full:
        assert k in compact


def test_compact_market_drops_verbose_fields():
    full = {
        "ticker": "KX-TEST",
        "rules_primary": "...multi-paragraph legal text...",
        "rules_secondary": "...more multi-paragraph text...",
        "previous_price_dollars": "0.48",
        "previous_yes_ask_dollars": "0.49",
        "previous_yes_bid_dollars": "0.47",
        "settlement_timer_seconds": 3600,
        "expiration_value": "",
        "response_price_units": "usd_cent",
        "price_level_structure": "linear_cent",
        "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
        "expected_expiration_time": "2026-06-07T08:00:00Z",
        "latest_expiration_time": "2026-06-07T08:00:00Z",
        "occurrence_datetime": "2026-05-31T08:05:00Z",
        "can_close_early": True,
        "fractional_trading_enabled": True,
        "created_time": "2026-05-31T06:49:09Z",
        "updated_time": "2026-05-31T07:17:59Z",
        "open_time": "2026-05-31T07:17:59Z",
    }
    compact = _compact_market(full)
    # ticker stays
    assert compact == {"ticker": "KX-TEST"}


def test_compact_market_strips_liquidity_dollars():
    """liquidity_dollars is always 0.0000 from Kalshi (issue #31) — compact
    must drop it so a naive caller can't read 0 as 'no liquidity'."""
    full = {"ticker": "KX-TEST", "liquidity_dollars": "0.0000", "yes_bid_dollars": "0.50"}
    compact = _compact_market(full)
    assert "liquidity_dollars" not in compact
    assert compact == {"ticker": "KX-TEST", "yes_bid_dollars": "0.50"}


def test_compact_market_is_significantly_smaller():
    """The whole point of compact is LLM context savings — verify the
    compression ratio is meaningful (not just a few hundred bytes)."""
    import json

    full_market = {
        "ticker": "KXTEMPNYCH-26MAY3104-T61.99",
        "title": "Will the temp in NYC be above 61.99° on May 31, 2026 at 4am EDT?",
        "rules_primary": (
            "If the temperature recorded at Central Park, New York City "
            "for May 31, 2026 4 AM EDT as reported by Accuweather (for "
            "coordinates 40.7812,-73.9665), is above 61.99°, then the "
            "market resolves to Yes."
        ),
        "rules_secondary": (
            "The official, final value for this market is the temperature "
            "reported by the AccuWeather, not any other weather service. "
            "NWS Climatological Reports, Google Weather, etc. may be useful "
            "references, but are not authoritative for resolution. "
            "Preliminary AccuWeather data may be subject to rounding and "
            "conversion differences from the final reported value. "
            "Use caution when interpreting preliminary AccuWeather readings."
        ),
        "yes_bid_dollars": "0.01",
        "yes_ask_dollars": "1.00",
    }
    full_size = len(json.dumps(full_market))
    compact_size = len(json.dumps(_compact_market(full_market)))
    # Expect at least 4x compression on this representative market
    assert compact_size * 4 < full_size, (
        f"Compact compression weaker than expected: {full_size} -> {compact_size}"
    )


# ── _compact_event ─────────────────────────────────────────────────────────


def test_compact_event_keeps_useful_fields():
    full = {
        "event_ticker": "KXFED-26MAR19",
        "series_ticker": "KXFED",
        "title": "Fed funds rate after Mar 2026 meeting",
        "sub_title": "Mar 19, 2026",
        "category": "Economy",
        "strike_date": "2026-03-19T00:00:00Z",
    }
    compact = _compact_event(full)
    for k in full:
        assert k in compact


def test_compact_event_drops_verbose_fields():
    full = {
        "event_ticker": "KX-EVENT",
        "last_updated_ts": "2026-05-31T07:32:35Z",
        "available_on_brokers": False,
        "collateral_return_type": "",
        "strike_period": "",
        "mutually_exclusive": False,
    }
    compact = _compact_event(full)
    assert compact == {"event_ticker": "KX-EVENT"}


# ── _minimal_market (whitelist projection, issue #28) ──────────────────────


def _mve_market() -> dict:
    """A representative multivariate (combo) market — the kind that blows
    up an LLM context even with `compact=True` because its bulk lives in
    fields the compact blacklist doesn't strip."""
    return {
        "ticker": "KXMVECROSSCATEGORY-S2026ABC-DEF",
        "event_ticker": "KXMVECROSSCATEGORY-S2026ABC",
        "title": "no Over 9.5,yes Over 7.5,yes Over 5.5,no Over 11.5,yes Over 5.5",
        "yes_sub_title": "no Over 9.5,yes Over 7.5,yes Over 5.5,no Over 11.5,yes Over 5.5",
        "no_sub_title": "no Over 9.5,yes Over 7.5,yes Over 5.5,no Over 11.5,yes Over 5.5",
        "status": "active",
        "close_time": "2026-06-07T17:05:00Z",
        "last_price_dollars": "0.0000",
        "yes_bid_dollars": "0.0000",
        "yes_ask_dollars": "0.0000",
        "no_bid_dollars": "1.0000",
        "no_ask_dollars": "1.0000",
        "yes_bid_size_fp": "0.00",
        "yes_ask_size_fp": "0.00",
        "volume_24h_fp": "0.00",
        "open_interest_fp": "0.00",
        "market_type": "binary",
        "liquidity_dollars": "0.0000",
        "mve_collection_ticker": "KXMVECROSSCATEGORY-R",
        "mve_selected_legs": [
            {"event_ticker": f"KXMLBTOTAL-{i}", "market_ticker": f"KXMLBTOTAL-{i}-6", "side": "yes"}
            for i in range(9)
        ],
        "custom_strike": {
            "Associated Events": "KXMLBTOTAL-A,KXMLBTOTAL-B,KXMLBTOTAL-C",
            "Associated Markets": "KXMLBTOTAL-A-6,KXMLBTOTAL-B-6,KXMLBTOTAL-C-6",
            "Associated Market Sides": "yes,yes,yes",
        },
        "rules_primary": "...long legal text...",
        "rules_secondary": "...more long legal text...",
    }


def test_minimal_market_keeps_only_whitelist():
    minimal = _minimal_market(_mve_market())
    assert set(minimal) <= set(_MINIMAL_MARKET_FIELDS)
    # The bulk-carrying fields are gone.
    for dropped in (
        "mve_selected_legs",
        "custom_strike",
        "mve_collection_ticker",
        "no_sub_title",
        "rules_primary",
        "rules_secondary",
        "liquidity_dollars",
    ):
        assert dropped not in minimal
    # The triage essentials survive.
    for kept in ("ticker", "yes_bid_dollars", "yes_ask_dollars", "volume_24h_fp", "status"):
        assert kept in minimal


def test_minimal_market_does_not_fabricate_missing_fields():
    """Whitelist intersection — a market missing a whitelisted field must
    not gain a key with a None/empty value."""
    minimal = _minimal_market({"ticker": "KX-TEST", "yes_bid_dollars": "0.50"})
    assert minimal == {"ticker": "KX-TEST", "yes_bid_dollars": "0.50"}


def test_minimal_market_fields_override():
    """An explicit `fields` list overrides the default whitelist, in order,
    keeping only present keys."""
    market = _mve_market()
    out = _minimal_market(market, fields="ticker, volume_24h_fp ,does_not_exist")
    assert list(out) == ["ticker", "volume_24h_fp"]


def test_minimal_much_smaller_than_compact_for_mve():
    """The point of `minimal` (issue #28): on a combo market it must be far
    smaller than `compact`, which barely helps."""
    import json

    market = _mve_market()
    compact_size = len(json.dumps(_compact_market(market)))
    minimal_size = len(json.dumps(_minimal_market(market)))
    assert minimal_size * 2 < compact_size, (
        f"minimal not meaningfully smaller than compact: {compact_size} -> {minimal_size}"
    )


# ── _project_market (view precedence) ──────────────────────────────────────


def test_project_market_precedence():
    market = _mve_market()
    # fields wins over everything
    assert list(_project_market(market, compact=True, minimal=True, fields="ticker")) == ["ticker"]
    # minimal wins over compact
    assert set(_project_market(market, compact=True, minimal=True)) <= set(_MINIMAL_MARKET_FIELDS)
    # compact alone strips the blacklist but keeps non-blacklist bulk
    compact = _project_market(market, compact=True)
    assert "mve_selected_legs" in compact and "liquidity_dollars" not in compact
    # full passthrough returns the object unchanged (by value)
    assert _project_market(market) == market


def test_project_market_full_returns_a_copy():
    """All four branches must return a NEW dict — full passthrough included —
    so the result never aliases the caller's parsed Kalshi response."""
    market = _mve_market()
    out = _project_market(market)
    assert out == market
    assert out is not market


def test_parse_fields_strips_and_dedups_preserving_order():
    assert _parse_fields(" ticker , yes_bid_dollars ,ticker, ") == ["ticker", "yes_bid_dollars"]


def test_minimal_market_rejects_blank_fields():
    """A `fields` string that resolves to no field names is a malformed
    request — must raise rather than silently return an empty market."""
    for blank in ("", "   ", ",", ",, ,"):
        with pytest.raises(KalshiAPIError) as exc:
            _minimal_market(_mve_market(), fields=blank)
        assert "fields" in exc.value.message


def test_minimal_market_all_unknown_fields_returns_empty():
    """Unknown (non-blank) field names are silently ignored; if NONE match,
    the projection is legitimately empty — distinct from the blank-fields
    error above."""
    assert _minimal_market(_mve_market(), fields="does_not_exist,also_missing") == {}


def test_full_view_preserves_liquidity_dollars():
    """liquidity_dollars is only stripped from the curated (compact/minimal)
    views — the full passthrough must keep it, so this guards against a
    future refactor that accidentally strips it globally."""
    market = {"ticker": "KX-TEST", "liquidity_dollars": "0.0000"}
    assert _project_market(market)["liquidity_dollars"] == "0.0000"


# ── _validate_mve_filter (issue #29) ───────────────────────────────────────


def test_validate_mve_filter_accepts_valid():
    assert _validate_mve_filter("exclude") == "exclude"
    assert _validate_mve_filter("only") == "only"


def test_validate_mve_filter_rejects_invalid():
    for bad in ("Exclude", "all", "", "yes", "none"):
        with pytest.raises(KalshiAPIError) as exc:
            _validate_mve_filter(bad)
        assert "mve_filter" in exc.value.message


# ── _volume_24h / _rank_liquid_markets (issue #29) ─────────────────────────


def test_volume_24h_parses_and_defaults_to_zero():
    assert _volume_24h({"volume_24h_fp": "8566.60"}) == pytest.approx(8566.60)
    assert _volume_24h({}) == 0.0
    assert _volume_24h({"volume_24h_fp": None}) == 0.0
    assert _volume_24h({"volume_24h_fp": "garbage"}) == 0.0


def test_rank_liquid_markets_sorts_filters_and_projects():
    markets = [
        {"ticker": "A", "volume_24h_fp": "10", "rules_primary": "x"},
        {"ticker": "B", "volume_24h_fp": "100", "rules_primary": "y"},
        {"ticker": "C", "volume_24h_fp": "1"},
        {"ticker": "D", "volume_24h_fp": "50"},
    ]
    ranked = _rank_liquid_markets(markets, min_volume=5, limit=2)
    # desc by volume, C dropped (below min_volume), capped at limit=2
    assert [m["ticker"] for m in ranked] == ["B", "D"]
    # results are minimal-projected — verbose fields gone
    assert "rules_primary" not in ranked[0]


# ── _scan_markets_excluding_mve (issue #29) ────────────────────────────────


async def test_scan_excludes_mve_and_paginates():
    client = _FakeClient(
        responses=[
            {"markets": [{"ticker": "A"}, {"ticker": "B"}], "cursor": "c1"},
            {"markets": [{"ticker": "C"}], "cursor": ""},  # "" = terminal cursor
        ]
    )
    scan = await _scan_markets_excluding_mve(client, scan_limit=200, status="open")
    assert [m["ticker"] for m in scan.markets] == ["A", "B", "C"]
    assert scan.complete is True  # terminal cursor reached
    assert scan.stopped_by is None
    assert scan.scanned == 3
    assert scan.requests == 2
    # every request excluded combos server-side
    assert all(params["mve_filter"] == "exclude" for _, params in client.calls)
    # the second request carried the first page's cursor
    assert client.calls[1][1]["cursor"] == "c1"


async def test_scan_caps_result_at_scan_limit():
    client = _FakeClient(responses=[{"markets": [{"ticker": str(i)} for i in range(5)]}])
    scan = await _scan_markets_excluding_mve(client, scan_limit=3, status="open")
    # first request asked for exactly scan_limit
    assert client.calls[0][1]["limit"] == 3
    # one page already satisfied the window — no second request
    assert len(client.calls) == 1
    # result is capped at scan_limit even if the page came back larger
    assert [m["ticker"] for m in scan.markets] == ["0", "1", "2"]


async def test_scan_exhausted_true_when_terminal_at_exactly_scan_limit():
    """Regression: a terminal cursor that fires exactly when the window is
    full must still report complete=True (the exchange ran out), not False."""
    client = _FakeClient(
        responses=[{"markets": [{"ticker": str(i)} for i in range(3)], "cursor": ""}]
    )
    scan = await _scan_markets_excluding_mve(client, scan_limit=3, status="open")
    assert len(scan.markets) == 3
    assert scan.complete is True
    assert scan.stopped_by is None


async def test_scan_not_exhausted_when_window_fills_with_more_available():
    client = _FakeClient(
        responses=[{"markets": [{"ticker": str(i)} for i in range(3)], "cursor": "more"}]
    )
    scan = await _scan_markets_excluding_mve(client, scan_limit=3, status="open")
    assert len(scan.markets) == 3
    assert scan.complete is False  # window filled, a live cursor means more remain
    assert scan.stopped_by == "scan_limit"
    assert len(client.calls) == 1


async def test_scan_clamps_nonpositive_scan_limit():
    client = _FakeClient(responses=[{"markets": [{"ticker": "A"}], "cursor": ""}])
    scan = await _scan_markets_excluding_mve(client, scan_limit=0, status="open")
    # scan_limit clamped up to 1 — one request asking for 1
    assert client.calls[0][1]["limit"] == 1
    assert len(scan.markets) == 1


async def test_scan_dedupes_and_stops_on_nonadvancing_cursor():
    """A non-advancing cursor (Kalshi pagination quirk) must not pad the
    result with duplicates or loop forever."""
    client = _FakeClient(
        responses=[
            {"markets": [{"ticker": "A"}], "cursor": "stuck"},
            {"markets": [{"ticker": "A"}], "cursor": "stuck"},  # same page + cursor
        ]
    )
    scan = await _scan_markets_excluding_mve(client, scan_limit=10, status="open")
    assert [m["ticker"] for m in scan.markets] == ["A"]  # de-duped
    assert scan.complete is True  # no forward progress == exhausted
    assert scan.scanned == 1
    assert len(client.calls) == 2  # one fetch, one to discover the cursor is stuck


async def test_scan_passes_series_ticker_when_given():
    client = _FakeClient(responses=[{"markets": [], "cursor": ""}])
    await _scan_markets_excluding_mve(
        client, scan_limit=50, status="open", series_ticker="KXMLBGAME"
    )
    assert client.calls[0][1]["series_ticker"] == "KXMLBGAME"


# ── scan_all: full-sweep paging + its budgets ──────────────────────────────


def _pages(count: int, *, per_page: int = 2, terminal: bool = True) -> list[dict]:
    """`count` pages of `per_page` distinct markets, each with a live cursor.
    The last page's cursor is terminal ("") when `terminal`, so the sweep
    reports complete; otherwise every page advertises more."""
    pages = []
    for p in range(count):
        markets = [{"ticker": f"M{p}-{i}"} for i in range(per_page)]
        last = p == count - 1
        pages.append({"markets": markets, "cursor": "" if (last and terminal) else f"c{p}"})
    return pages


async def test_scan_all_pages_past_scan_limit_to_exhaustion():
    """The whole point of scan_all: `scan_limit` no longer bounds the sweep,
    so the ranking input is the full listing, not an arbitrary slice."""
    client = _FakeClient(responses=_pages(4))
    scan = await _scan_markets_excluding_mve(client, scan_limit=2, status="open", scan_all=True)
    # scan_limit=2 would have stopped after one page in windowed mode.
    assert scan.scanned == 8
    assert len(client.calls) == 4
    assert scan.complete is True
    assert scan.stopped_by is None
    # Full-sweep pages request Kalshi's max page size, not scan_limit.
    assert client.calls[0][1]["limit"] == 1000


async def test_scan_all_respects_request_budget():
    """A listing that never terminates must stop at the request cap and say
    so — an honest `complete: false`, not an unbounded cursor chase."""
    client = _FakeClient(responses=_pages(50, terminal=False))
    scan = await _scan_markets_excluding_mve(
        client, scan_limit=2, status="open", scan_all=True, max_requests=3
    )
    assert len(client.calls) == 3  # the cap is a ceiling, not cap+1
    assert scan.scanned == 6
    assert scan.complete is False
    assert scan.stopped_by == "request_budget"


async def test_scan_all_respects_time_budget():
    """The wall-clock cap is the backstop for a listing that pages forever
    within the request budget. One page always goes out, then it stops."""
    client = _FakeClient(responses=_pages(50, terminal=False))
    scan = await _scan_markets_excluding_mve(
        client, scan_limit=2, status="open", scan_all=True, max_seconds=0.0
    )
    assert len(client.calls) == 1
    assert scan.complete is False
    assert scan.stopped_by == "time_budget"


async def test_scan_all_respects_market_cap():
    client = _FakeClient(responses=_pages(50, terminal=False))
    scan = await _scan_markets_excluding_mve(
        client, scan_limit=2, status="open", scan_all=True, max_markets=5
    )
    assert scan.scanned == 5
    assert scan.complete is False
    assert scan.stopped_by == "market_cap"


async def test_scan_on_page_folds_without_retaining_markets():
    """`on_page` is what keeps a full sweep from holding every market dict in
    memory: pages are handed to the fold and NOT accumulated."""
    seen: list[str] = []
    client = _FakeClient(responses=_pages(3))
    scan = await _scan_markets_excluding_mve(
        client,
        scan_limit=2,
        status="open",
        scan_all=True,
        on_page=lambda page: seen.extend(m["ticker"] for m in page),
    )
    assert scan.markets == []  # nothing retained
    assert scan.scanned == 6  # but everything counted
    assert len(seen) == 6  # and everything folded


# ── _event_hint (issue #30) ────────────────────────────────────────────────


async def test_event_hint_returns_actionable_message():
    event = "KXMLBGAME-26JUN042010PITHOU"
    markets = [f"{event}-HOU", f"{event}-PIT"]
    client = _FakeClient(
        responses=[
            {
                "event": {"event_ticker": event},
                "markets": [{"ticker": t} for t in markets],
            }
        ]
    )
    hint = await _event_hint(client, event)
    assert hint is not None
    assert "EVENT ticker" in hint
    for t in markets:
        assert t in hint
    # resolved via the events endpoint, requesting nested markets
    assert client.calls[0][0] == f"/events/{event}"
    assert client.calls[0][1]["with_nested_markets"] == "true"


async def test_event_hint_none_when_not_an_event():
    """A market ticker (or junk) 404s on /events/{ticker} — fail open."""
    client = _FakeClient(error=KalshiAPIError(status=404, message="not found"))
    assert await _event_hint(client, "KXMLBGAME-26JUN042010PITHOU-HOU") is None


async def test_event_hint_none_when_event_has_no_markets():
    client = _FakeClient(responses=[{"event": {"event_ticker": "X"}, "markets": []}])
    assert await _event_hint(client, "X") is None


async def test_event_hint_truncates_long_market_lists():
    markets = [{"ticker": f"EVT-{i}"} for i in range(25)]
    client = _FakeClient(responses=[{"markets": markets}])
    hint = await _event_hint(client, "EVT")
    assert "(+5 more)" in hint


async def test_event_hint_truncation_boundary():
    """Exactly 20 markets → all shown, no '+N more'; 21 → '+1 more'."""
    c20 = _FakeClient(responses=[{"markets": [{"ticker": f"E20-{i}"} for i in range(20)]}])
    hint20 = await _event_hint(c20, "E20")
    assert "more)" not in hint20

    c21 = _FakeClient(responses=[{"markets": [{"ticker": f"E21-{i}"} for i in range(21)]}])
    hint21 = await _event_hint(c21, "E21")
    assert "(+1 more)" in hint21


async def test_event_hint_negative_cache_skips_repeat_probe():
    """A non-event ticker must be probed once, then served from the negative
    cache on repeat calls — no second /events request (issue: polling cost)."""
    client = _FakeClient(error=KalshiAPIError(status=404, message="not found"))
    assert await _event_hint(client, "REAL-MARKET-XYZ") is None
    assert await _event_hint(client, "REAL-MARKET-XYZ") is None
    # Only the first call hit the API; the second was cached.
    assert len(client.calls) == 1


def test_event_hint_miss_cache_is_hard_bounded():
    """The negative cache must never grow past its cap, even if a process
    probes far more distinct non-event tickers than the limit."""
    for i in range(_EVENT_HINT_MISS_MAX + 100):
        _record_event_hint_miss(f"T-{i}", float(i))
    assert len(_event_hint_misses) <= _EVENT_HINT_MISS_MAX
    # Oldest entries are evicted first (FIFO) — newest survives.
    assert f"T-{_EVENT_HINT_MISS_MAX + 99}" in _event_hint_misses
    assert "T-0" not in _event_hint_misses


async def test_event_hint_ignores_markets_without_ticker():
    client = _FakeClient(responses=[{"markets": [{"no_ticker": "x"}, {"ticker": "EVT-A"}]}])
    hint = await _event_hint(client, "EVT")
    assert "EVT-A" in hint


# ── series rollup (kalshi_get_series_summary) ──────────────────────────────


def test_series_of_derives_prefix_from_event_ticker():
    assert _series_of({"event_ticker": "KXMLBGAME-26AUG141910SDCLE"}) == "KXMLBGAME"


def test_series_of_falls_back_to_market_ticker():
    assert _series_of({"ticker": "KXFED-26MAR19-B5.25"}) == "KXFED"


def test_series_of_none_when_underivable():
    assert _series_of({}) is None
    assert _series_of({"event_ticker": "   "}) is None
    assert _series_of({"event_ticker": 42}) is None


def test_yes_spread_requires_a_two_sided_book():
    """An empty/one-sided book reports 0.0000 on a side. Treating that as a
    0.00 spread would rank every dead market as the tightest in its series —
    exactly backwards, so those must be skipped entirely."""
    assert _yes_spread({"yes_bid_dollars": "0.4600", "yes_ask_dollars": "0.4800"}) == pytest.approx(
        0.02
    )
    assert _yes_spread({"yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0000"}) is None
    assert _yes_spread({"yes_bid_dollars": "0.0000", "yes_ask_dollars": "1.0000"}) is None
    assert _yes_spread({"yes_bid_dollars": "0.5000"}) is None  # no ask at all
    assert _yes_spread({"yes_bid_dollars": "x", "yes_ask_dollars": "0.5"}) is None
    # Crossed book (ask below bid) is nonsense data, not a negative spread.
    assert _yes_spread({"yes_bid_dollars": "0.6000", "yes_ask_dollars": "0.5000"}) is None


def _series_markets() -> list[dict]:
    return [
        {
            "ticker": "KXA-E1-X",
            "event_ticker": "KXA-E1",
            "volume_24h_fp": "100.00",
            "yes_bid_dollars": "0.4000",
            "yes_ask_dollars": "0.4500",
            "close_time": "2026-08-20T00:00:00Z",
        },
        {
            "ticker": "KXA-E1-Y",
            "event_ticker": "KXA-E1",
            "volume_24h_fp": "50.00",
            "yes_bid_dollars": "0.4000",
            "yes_ask_dollars": "0.4100",  # tighter — becomes the series min
            "close_time": "2026-08-18T00:00:00Z",  # earlier — becomes soonest
        },
        {
            "ticker": "KXA-E2-X",
            "event_ticker": "KXA-E2",  # second event in the same series
            "volume_24h_fp": "5.00",
            "yes_bid_dollars": "0.0000",  # dead book — must not set min spread
            "yes_ask_dollars": "0.0000",
            "close_time": "2026-09-01T00:00:00Z",
        },
        {
            "ticker": "KXB-E1-X",
            "event_ticker": "KXB-E1",
            "volume_24h_fp": "1000.00",
            "yes_bid_dollars": "0.1000",
            "yes_ask_dollars": "0.9000",
            "close_time": "2026-12-01T00:00:00Z",
        },
    ]


def test_fold_series_page_aggregates_across_pages():
    """The fold must be incremental — two calls with half the markets each
    must produce exactly what one call with all of them would."""
    acc: dict = {}
    markets = _series_markets()
    _fold_series_page(acc, markets[:2])
    _fold_series_page(acc, markets[2:])

    assert set(acc) == {"KXA", "KXB"}
    kxa = acc["KXA"]
    assert kxa["market_count"] == 3
    assert kxa["events"] == {"KXA-E1", "KXA-E2"}
    assert kxa["volume_24h"] == pytest.approx(155.0)
    assert kxa["min_spread_dollars"] == pytest.approx(0.01)  # not 0.00 from the dead book
    assert kxa["soonest_close_time"] == "2026-08-18T00:00:00Z"


def test_fold_series_page_skips_markets_without_a_derivable_series():
    acc: dict = {}
    _fold_series_page(acc, [{"volume_24h_fp": "10"}, {"event_ticker": "KXA-E1"}])
    assert set(acc) == {"KXA"}
    assert acc["KXA"]["market_count"] == 1


def test_finalize_series_sorts_by_volume_and_caps_at_top():
    acc: dict = {}
    _fold_series_page(acc, _series_markets())
    rows = _finalize_series(acc, top=1, sort_by="volume_24h")
    assert len(rows) == 1
    assert rows[0]["series_ticker"] == "KXB"  # 1000 > 155
    assert rows[0]["volume_24h"] == 1000.0


def test_finalize_series_sort_modes():
    acc: dict = {}
    _fold_series_page(acc, _series_markets())
    by_count = _finalize_series(acc, top=10, sort_by="market_count")
    assert [r["series_ticker"] for r in by_count] == ["KXA", "KXB"]  # 3 markets vs 1
    by_close = _finalize_series(acc, top=10, sort_by="soonest_close")
    assert [r["series_ticker"] for r in by_close] == ["KXA", "KXB"]  # Aug 18 before Dec 1


def test_finalize_series_row_shape_is_json_safe():
    """The accumulator holds a `set` of event tickers; the emitted row must
    carry a count instead, or the tool result won't serialize."""
    acc: dict = {}
    _fold_series_page(acc, _series_markets())
    row = next(
        r
        for r in _finalize_series(acc, top=10, sort_by="volume_24h")
        if r["series_ticker"] == "KXA"
    )
    assert row == {
        "series_ticker": "KXA",
        "market_count": 3,
        "event_count": 2,
        "volume_24h": 155.0,
        "min_spread_dollars": 0.01,
        "soonest_close_time": "2026-08-18T00:00:00Z",
    }


def test_finalize_series_null_spread_when_no_two_sided_book():
    acc: dict = {}
    _fold_series_page(
        acc, [{"event_ticker": "KXZ-1", "yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0000"}]
    )
    assert _finalize_series(acc, top=10, sort_by="volume_24h")[0]["min_spread_dollars"] is None


def test_finalize_series_sorts_missing_close_time_last():
    acc: dict = {}
    _fold_series_page(
        acc,
        [
            {"event_ticker": "KXNOCLOSE-1"},
            {"event_ticker": "KXHASCLOSE-1", "close_time": "2026-08-18T00:00:00Z"},
        ],
    )
    rows = _finalize_series(acc, top=10, sort_by="soonest_close")
    assert [r["series_ticker"] for r in rows] == ["KXHASCLOSE", "KXNOCLOSE"]


# ── _single_ticker (issue #30 gate) ────────────────────────────────────────


def test_single_ticker_detects_lone_ticker_tolerating_noise():
    assert _single_ticker("KXFED-26MAR19") == "KXFED-26MAR19"
    assert _single_ticker("  KXFED-26MAR19  ") == "KXFED-26MAR19"
    assert _single_ticker("KXFED-26MAR19,") == "KXFED-26MAR19"  # trailing comma


def test_single_ticker_none_for_multi_or_empty():
    assert _single_ticker("A,B") is None
    assert _single_ticker("") is None
    assert _single_ticker("   ") is None
    assert _single_ticker(",") is None
