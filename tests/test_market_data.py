"""Tests for market-data helpers — orderbook emptiness detection (issue #30).

Pure functions; no FastMCP or HTTP involved. The event-vs-market hint
resolver itself is tested in test_discovery.py (`_event_hint`); here we
only cover the orderbook-shape check that decides whether to consult it.
"""

from __future__ import annotations

from kalshi_mcp_server.tools.market_data import _book_is_empty


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
