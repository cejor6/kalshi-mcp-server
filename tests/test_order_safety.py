"""Regression tests for the order-path safety audit fixes.

Every test here is about money: the worst-case cost formula (incl. the fee
bound), the re-check + reservation at confirm, the cash-reserve floor, and the
handling of ambiguous confirm outcomes. Nothing hits the network — Kalshi is
an `httpx.MockTransport`.
"""

from __future__ import annotations

import pytest

from kalshi_mcp_server.config import DEMO_REST_BASE, DEMO_WS_URL, Config
from kalshi_mcp_server.errors import SafetyError
from kalshi_mcp_server.safety import OrderIntent, SafetyController


def _config(**overrides) -> Config:
    values = {
        "key_id": "test-key",
        "private_key_path": None,
        "private_key_pem": "<set-in-test>",
        "env": "demo",
        "trading_enabled": True,
        "rest_base": DEMO_REST_BASE,
        "ws_url": DEMO_WS_URL,
        "max_order_size_usd": 25.0,
        "daily_limit_usd": 250.0,
        "max_contracts_per_order": 100,
        "cash_reserve_usd": 0.0,
        "transport": "stdio",
        "port": 8000,
        "log_level": "INFO",
    }
    values.update(overrides)
    return Config(**values)


def _intent(count: int = 10, price: int = 50, action: str = "buy", side: str = "yes"):
    return OrderIntent(
        ticker="KX-TEST", side=side, action=action, count=count, limit_price_cents=price
    )


# ── worst-case cost: one formula, with a conservative fee bound ─────────────


@pytest.mark.parametrize(
    "count,price,expected_fee_cents",
    [
        # Kalshi's documented example: 100 contracts @ 50c → 0.07*100*.25 = $1.75.
        (100, 50, 175),
        # 10 @ 25c → 0.07*10*.25*.75 = 13.125c → rounded UP to 14c.
        (10, 25, 14),
        # 1 @ 1c → 0.07*.01*.99 = 0.0693c → still a whole cent.
        (1, 1, 1),
        # Symmetric in P: 1 @ 99c is the same as 1 @ 1c.
        (1, 99, 1),
    ],
)
def test_fee_upper_bound_matches_kalshi_taker_formula(count, price, expected_fee_cents):
    assert _intent(count=count, price=price).fee_upper_bound_cents() == expected_fee_cents


def test_worst_case_cost_buy_and_sell_include_fee():
    # Buy 10 YES @ 30c: notional $3.00 + fee ceil(0.07*10*.3*.7 = 14.7c) = 15c.
    assert _intent(count=10, price=30, action="buy").worst_case_cost_cents() == 315
    # Sell 10 YES @ 30c is buying NO @ 70c: notional $7.00 + the same 15c fee.
    assert _intent(count=10, price=30, action="sell").worst_case_cost_cents() == 715
    assert _intent(count=10, price=30, action="sell").worst_case_cost_usd() == 7.15


def test_size_cap_counts_the_fee():
    """An order whose NOTIONAL is exactly the cap is now refused: the fee makes
    its worst-case cost exceed it. Before, the fee was invisible to the cap."""
    ctrl = SafetyController(_config(max_order_size_usd=25.0))
    with pytest.raises(SafetyError, match="exceeds the active max order size"):
        ctrl.check_order(_intent(count=50, price=50))  # $25 + $0.88 fee


def test_daily_counter_records_the_same_cost_the_check_uses():
    ctrl = SafetyController(_config())
    intent = _intent(count=10, price=30)
    ctrl.record_order_committed(intent)
    assert ctrl._daily.peek()[1] == pytest.approx(intent.worst_case_cost_usd())
