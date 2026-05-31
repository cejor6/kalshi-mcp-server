"""Tests for the discovery-tool helpers — ticker validation + compact shaping.

These are pure functions; no FastMCP or HTTP involved.
"""

from __future__ import annotations

import pytest

from kalshi_mcp_server.errors import KalshiAPIError
from kalshi_mcp_server.tools.discovery import (
    _compact_event,
    _compact_market,
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
