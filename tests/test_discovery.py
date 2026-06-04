"""Tests for the discovery-tool helpers — ticker validation + market/event
projection (compact blacklist, minimal whitelist, fields override).

These are pure functions; no FastMCP or HTTP involved.
"""

from __future__ import annotations

import pytest

from kalshi_mcp_server.errors import KalshiAPIError
from kalshi_mcp_server.tools.discovery import (
    _MINIMAL_MARKET_FIELDS,
    _compact_event,
    _compact_market,
    _minimal_market,
    _parse_fields,
    _project_market,
    _validate_ticker,
)

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
