"""Regression tests for the order-path safety audit fixes.

Every test here is about money: the worst-case cost formula (incl. the fee
bound), the re-check + reservation at confirm, the cash-reserve floor, and the
handling of ambiguous confirm outcomes. Nothing hits the network — Kalshi is
an `httpx.MockTransport`.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import math
import time
from collections.abc import Awaitable

import httpx
import pytest
from fastmcp import FastMCP

from kalshi_mcp_server.auth import KalshiSigner
from kalshi_mcp_server.client import KalshiClient
from kalshi_mcp_server.config import DEMO_REST_BASE, DEMO_WS_URL, Config
from kalshi_mcp_server.errors import KalshiAPIError, SafetyError
from kalshi_mcp_server.rate_limit import KalshiRateLimiter, TierLimits
from kalshi_mcp_server.safety import OrderIntent, SafetyController
from kalshi_mcp_server.tools import orders


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


# ── confirm-time wiring: re-check, reserve, cash floor, ambiguity ──────────


class _Kalshi:
    """A tiny fake Kalshi: records requests, scripted per-path responses."""

    def __init__(self, *, balance_cents: int | None = 1_000_000) -> None:
        self.posts: list[dict] = []
        self.balance_reads = 0
        self.balance_cents = balance_cents
        self.balance_response: httpx.Response | None = None
        # Each POST pops the next scripted outcome; default is 200.
        self.post_outcomes: list = []
        self.post_delay_s = 0.0
        # Cents to take off the balance when a POST is accepted (models Kalshi
        # holding cash for the new order).
        self.debit_cents_per_order = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/portfolio/balance"):
            self.balance_reads += 1
            if self.balance_response is not None:
                return self.balance_response
            return httpx.Response(200, json={"balance": self.balance_cents, "portfolio_value": 0})
        if path.endswith("/portfolio/events/orders") and request.method == "POST":
            self.posts.append(jsonlib.loads(request.content))
            if self.post_delay_s:
                await asyncio.sleep(self.post_delay_s)
            outcome = self.post_outcomes.pop(0) if self.post_outcomes else 200
            if isinstance(outcome, BaseException):
                raise outcome
            if isinstance(outcome, Awaitable):
                await outcome
                outcome = 200
            if outcome == 200 and self.balance_cents is not None:
                self.balance_cents -= self.debit_cents_per_order
            return httpx.Response(outcome, json={"order_id": f"ord_{len(self.posts)}"})
        return httpx.Response(404, json={})


def _server(rsa_private_key, kalshi: _Kalshi, **config_overrides) -> FastMCP:
    config = _config(**config_overrides)
    signer = KalshiSigner(key_id="test-key", private_key=rsa_private_key)
    limiter = KalshiRateLimiter(TierLimits.premier())
    http = httpx.AsyncClient(
        base_url=config.rest_base, transport=httpx.MockTransport(kalshi.handler)
    )
    client = KalshiClient(config=config, signer=signer, rate_limiter=limiter, http_client=http)
    server = FastMCP(name="kalshi-test")
    server._kalshi_client = client  # type: ignore[attr-defined]
    server._kalshi_config = config  # type: ignore[attr-defined]
    server._kalshi_signer = signer  # type: ignore[attr-defined]
    server._kalshi_safety = SafetyController(config)  # type: ignore[attr-defined]
    server._kalshi_rate_limiter = limiter  # type: ignore[attr-defined]
    orders.register(server)
    return server


async def _tools(server: FastMCP):
    prepare = (await server.get_tool("kalshi_prepare_order")).fn
    confirm = (await server.get_tool("kalshi_confirm_order")).fn
    return prepare, confirm


def _spent(server: FastMCP) -> float:
    return server._kalshi_safety._daily.peek()[1]  # type: ignore[attr-defined]


# 40 contracts @ 50c = $20.00 notional + $0.70 fee bound = $20.70 worst case.
_ORDER = {"ticker": "KX-TEST", "action": "buy", "side": "yes", "count": 40, "limit_price_cents": 50}
_ORDER_COST = 20.70


async def test_many_prepares_then_confirms_respect_the_daily_cap(rsa_private_key):
    """HIGH: prepare used to be the only check and spend was recorded only at
    confirm, so 6 prepares then 6 confirms spent 6x against a 2x budget."""
    kalshi = _Kalshi()
    server = _server(rsa_private_key, kalshi, daily_limit_usd=50.0)
    prepare, confirm = await _tools(server)

    tokens = [(await prepare(**_ORDER))["confirmation_id"] for _ in range(6)]
    results = []
    for token in tokens:
        try:
            await confirm(confirmation_id=token)
            results.append("ok")
        except SafetyError as exc:
            assert "daily limit" in str(exc)
            results.append("refused")

    assert results == ["ok", "ok", "refused", "refused", "refused", "refused"]
    assert len(kalshi.posts) == 2
    assert _spent(server) == pytest.approx(2 * _ORDER_COST)
    assert _spent(server) <= 50.0


async def test_concurrent_confirms_cannot_jointly_exceed_the_cap(rsa_private_key):
    kalshi = _Kalshi()
    kalshi.post_delay_s = 0.01  # keep each POST in flight across an await
    server = _server(rsa_private_key, kalshi, daily_limit_usd=50.0)
    prepare, confirm = await _tools(server)

    tokens = [(await prepare(**_ORDER))["confirmation_id"] for _ in range(6)]
    results = await asyncio.gather(
        *(confirm(confirmation_id=t) for t in tokens), return_exceptions=True
    )

    ok = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, SafetyError)]
    assert len(ok) == 2 and len(refused) == 4
    assert len(kalshi.posts) == 2
    assert _spent(server) <= 50.0


def test_reserve_order_is_atomic_check_and_debit():
    ctrl = SafetyController(_config(daily_limit_usd=50.0))
    intent = _intent(count=40, price=50)
    assert ctrl.reserve_order(intent, current_cash_usd=None) == pytest.approx(_ORDER_COST)
    ctrl.reserve_order(intent, current_cash_usd=None)
    with pytest.raises(SafetyError):
        ctrl.reserve_order(intent, current_cash_usd=None)
    # The refused attempt debited nothing.
    assert ctrl._daily.peek()[1] == pytest.approx(2 * _ORDER_COST)
    ctrl.release_order(_ORDER_COST)
    assert ctrl._daily.peek()[1] == pytest.approx(_ORDER_COST)


@pytest.mark.parametrize(
    "tighten",
    [
        {"max_order_size_usd": 10.0},
        {"daily_limit_usd": 5.0},
        {"max_contracts_per_order": 10},
    ],
)
async def test_tightening_between_prepare_and_confirm_blocks_the_order(rsa_private_key, tighten):
    kalshi = _Kalshi()
    server = _server(rsa_private_key, kalshi)
    prepare, confirm = await _tools(server)

    token = (await prepare(**_ORDER))["confirmation_id"]
    await server._kalshi_safety.set_limits(**tighten)  # type: ignore[attr-defined]

    with pytest.raises(SafetyError):
        await confirm(confirmation_id=token)
    assert kalshi.posts == []
    assert _spent(server) == 0.0
    # A policy refusal consumes the token — re-prepare under the new limits.
    with pytest.raises(SafetyError, match="Unknown or expired"):
        await confirm(confirmation_id=token)


# ── cash reserve (MCP_CASH_RESERVE_USD) is enforced at confirm ─────────────


async def test_cash_reserve_refuses_an_order_that_would_breach_the_floor(rsa_private_key):
    kalshi = _Kalshi(balance_cents=2_500)  # $25.00 cash
    server = _server(rsa_private_key, kalshi, cash_reserve_usd=10.0)
    prepare, confirm = await _tools(server)

    token = (await prepare(**_ORDER))["confirmation_id"]
    with pytest.raises(SafetyError, match="reserve floor"):
        await confirm(confirmation_id=token)  # $25 - $20.70 = $4.30 < $10
    assert kalshi.balance_reads == 1
    assert kalshi.posts == []
    assert _spent(server) == 0.0


async def test_cash_reserve_allows_an_order_that_keeps_the_floor(rsa_private_key):
    kalshi = _Kalshi(balance_cents=5_000)  # $50.00 cash
    server = _server(rsa_private_key, kalshi, cash_reserve_usd=10.0)
    prepare, confirm = await _tools(server)

    token = (await prepare(**_ORDER))["confirmation_id"]
    await confirm(confirmation_id=token)  # $50 - $20.70 = $29.30 >= $10
    assert kalshi.balance_reads == 1
    assert len(kalshi.posts) == 1


async def test_concurrent_confirms_cannot_jointly_breach_the_cash_reserve(rsa_private_key):
    """The reserve check is read-balance-then-act. Two confirms reading the same
    $50 concurrently would each leave $29.30 >= $10 and both go out, leaving
    $8.60. Confirms are serialized so the second sees the first's debit."""
    kalshi = _Kalshi(balance_cents=5_000)
    kalshi.debit_cents_per_order = 2_070
    kalshi.post_delay_s = 0.01
    server = _server(rsa_private_key, kalshi, cash_reserve_usd=10.0)
    prepare, confirm = await _tools(server)

    tokens = [(await prepare(**_ORDER))["confirmation_id"] for _ in range(2)]
    results = await asyncio.gather(
        *(confirm(confirmation_id=t) for t in tokens), return_exceptions=True
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, SafetyError) for r in results) == 1
    assert len(kalshi.posts) == 1
    assert kalshi.balance_cents >= 1_000


async def test_runtime_raised_cash_reserve_is_enforced(rsa_private_key):
    kalshi = _Kalshi(balance_cents=5_000)
    server = _server(rsa_private_key, kalshi)  # env reserve $0
    prepare, confirm = await _tools(server)

    token = (await prepare(**_ORDER))["confirmation_id"]
    await server._kalshi_safety.set_limits(cash_reserve_usd=40.0)  # type: ignore[attr-defined]
    with pytest.raises(SafetyError, match="reserve floor"):
        await confirm(confirmation_id=token)
    assert kalshi.posts == []


@pytest.mark.parametrize(
    "balance_response",
    [
        httpx.Response(500, json={"error": {"message": "boom"}}),
        httpx.Response(200, json={"portfolio_value": 0}),  # no `balance`
        httpx.Response(200, json={"balance": "2500"}),  # not an integer
        httpx.Response(200, json={"balance": True}),
    ],
)
async def test_balance_read_failure_fails_closed_when_a_reserve_is_set(
    rsa_private_key, balance_response
):
    kalshi = _Kalshi()
    kalshi.balance_response = balance_response
    server = _server(rsa_private_key, kalshi, cash_reserve_usd=10.0)
    prepare, confirm = await _tools(server)

    token = (await prepare(**_ORDER))["confirmation_id"]
    with pytest.raises(SafetyError, match=r"cash balance|balance response"):
        await confirm(confirmation_id=token)
    assert kalshi.posts == []
    assert _spent(server) == 0.0

    # Nothing was sent, so the token survives: once the balance is readable the
    # same confirmation_id goes through.
    kalshi.balance_response = None
    await confirm(confirmation_id=token)
    assert len(kalshi.posts) == 1


async def test_balance_transport_error_fails_closed(rsa_private_key):
    kalshi = _Kalshi()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/portfolio/balance"):
            raise httpx.ConnectError("down", request=request)
        return await kalshi.handler(request)

    server = _server(rsa_private_key, kalshi, cash_reserve_usd=10.0)
    server._kalshi_client._http._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    prepare, confirm = await _tools(server)
    token = (await prepare(**_ORDER))["confirmation_id"]
    with pytest.raises(SafetyError, match="could not read the cash balance"):
        await confirm(confirmation_id=token)
    assert kalshi.posts == []


async def test_no_balance_read_when_no_reserve_is_configured(rsa_private_key):
    kalshi = _Kalshi()
    server = _server(rsa_private_key, kalshi, cash_reserve_usd=0.0)
    prepare, confirm = await _tools(server)
    await confirm(confirmation_id=(await prepare(**_ORDER))["confirmation_id"])
    assert kalshi.balance_reads == 0
    assert len(kalshi.posts) == 1


def test_reserve_order_refuses_without_cash_when_reserve_active():
    ctrl = SafetyController(_config(cash_reserve_usd=5.0))
    with pytest.raises(SafetyError, match="no current cash"):
        ctrl.reserve_order(_intent(), current_cash_usd=None)
    assert ctrl._daily.peek()[1] == 0.0


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_non_finite_cash_is_refused(bad):
    ctrl = SafetyController(_config(cash_reserve_usd=5.0))
    with pytest.raises(SafetyError, match="finite"):
        ctrl.reserve_order(_intent(), current_cash_usd=bad)


# ── ambiguous confirm outcomes keep the reservation AND the token ──────────


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("reset"),
        503,
        500,
    ],
)
async def test_ambiguous_outcome_keeps_reservation_and_token(rsa_private_key, outcome):
    kalshi = _Kalshi()
    kalshi.post_outcomes = [outcome]
    server = _server(rsa_private_key, kalshi)
    prepare, confirm = await _tools(server)

    prepared = await prepare(**_ORDER)
    token = prepared["confirmation_id"]
    with pytest.raises(KalshiAPIError) as exc:
        await confirm(confirmation_id=token)
    message = exc.value.message
    assert "OUTCOME UNKNOWN" in message
    assert token in message and "kalshi_get_orders" in message
    # The spend stays counted: Kalshi may have placed the order.
    assert _spent(server) == pytest.approx(_ORDER_COST)

    # Retrying the SAME confirmation_id resends the identical request (same
    # client_order_id) and does not reserve a second time.
    await confirm(confirmation_id=token)
    assert len(kalshi.posts) == 2
    assert kalshi.posts[0] == kalshi.posts[1]
    assert kalshi.posts[1]["client_order_id"] == prepared["idempotency_key"]
    assert _spent(server) == pytest.approx(_ORDER_COST)
    # Success consumes the token.
    with pytest.raises(SafetyError, match="Unknown or expired"):
        await confirm(confirmation_id=token)


async def test_cancellation_mid_post_is_ambiguous(rsa_private_key):
    """An MCP client timing the tool call out cancels us mid-POST — the order
    may still land, so the reservation and token must survive."""
    kalshi = _Kalshi()
    kalshi.post_outcomes = [asyncio.Event().wait()]  # never completes
    server = _server(rsa_private_key, kalshi)
    prepare, confirm = await _tools(server)

    token = (await prepare(**_ORDER))["confirmation_id"]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(confirm(confirmation_id=token), timeout=0.05)
    assert _spent(server) == pytest.approx(_ORDER_COST)

    await confirm(confirmation_id=token)  # retry goes through, no double reserve
    assert _spent(server) == pytest.approx(_ORDER_COST)
    assert len(kalshi.posts) == 2


async def test_unambiguous_4xx_releases_reservation_and_consumes_token(rsa_private_key):
    kalshi = _Kalshi()
    kalshi.post_outcomes = [400]
    server = _server(rsa_private_key, kalshi)
    prepare, confirm = await _tools(server)

    token = (await prepare(**_ORDER))["confirmation_id"]
    with pytest.raises(KalshiAPIError) as exc:
        await confirm(confirmation_id=token)
    assert exc.value.status == 400
    assert _spent(server) == 0.0
    with pytest.raises(SafetyError, match="Unknown or expired"):
        await confirm(confirmation_id=token)


async def test_first_send_429_releases_but_keeps_token(rsa_private_key):
    kalshi = _Kalshi()
    kalshi.post_outcomes = [429]
    server = _server(rsa_private_key, kalshi)
    prepare, confirm = await _tools(server)

    token = (await prepare(**_ORDER))["confirmation_id"]
    with pytest.raises(KalshiAPIError):
        await confirm(confirmation_id=token)
    assert _spent(server) == 0.0  # Kalshi throttled it — nothing placed

    await confirm(confirmation_id=token)  # re-checked + reserved exactly once
    assert _spent(server) == pytest.approx(_ORDER_COST)
    assert len(kalshi.posts) == 2


async def test_rejected_retry_after_ambiguity_keeps_the_reservation(rsa_private_key):
    """A 4xx on a RETRY is not proof nothing was placed — Kalshi rejects a
    duplicate client_order_id when the first, ambiguous send succeeded.
    Releasing here would under-reserve."""
    kalshi = _Kalshi()
    kalshi.post_outcomes = [httpx.ReadTimeout("slow"), 409]
    server = _server(rsa_private_key, kalshi)
    prepare, confirm = await _tools(server)

    token = (await prepare(**_ORDER))["confirmation_id"]
    with pytest.raises(KalshiAPIError):
        await confirm(confirmation_id=token)
    with pytest.raises(KalshiAPIError) as exc:
        await confirm(confirmation_id=token)
    assert exc.value.status == 409
    assert "kalshi_get_orders" in exc.value.message
    assert _spent(server) == pytest.approx(_ORDER_COST)


async def test_retry_after_ambiguity_still_honors_tightened_per_order_caps(rsa_private_key):
    kalshi = _Kalshi()
    kalshi.post_outcomes = [httpx.ReadTimeout("slow")]
    server = _server(rsa_private_key, kalshi)
    prepare, confirm = await _tools(server)

    token = (await prepare(**_ORDER))["confirmation_id"]
    with pytest.raises(KalshiAPIError):
        await confirm(confirmation_id=token)
    await server._kalshi_safety.set_limits(max_order_size_usd=10.0)  # type: ignore[attr-defined]
    with pytest.raises(SafetyError, match="UNKNOWN"):
        await confirm(confirmation_id=token)
    assert len(kalshi.posts) == 1
    assert _spent(server) == pytest.approx(_ORDER_COST)  # still reserved


async def test_concurrent_confirms_of_one_token_send_once(rsa_private_key):
    kalshi = _Kalshi()
    kalshi.post_delay_s = 0.01
    server = _server(rsa_private_key, kalshi)
    prepare, confirm = await _tools(server)

    token = (await prepare(**_ORDER))["confirmation_id"]
    results = await asyncio.gather(
        confirm(confirmation_id=token), confirm(confirmation_id=token), return_exceptions=True
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, SafetyError) for r in results) == 1
    assert len(kalshi.posts) == 1
    assert _spent(server) == pytest.approx(_ORDER_COST)


# ── prepare input validation: ticker + expiration_ts ────────────────────────


@pytest.mark.parametrize("bad", ["", "   "])
async def test_prepare_rejects_empty_ticker(rsa_private_key, bad):
    server = _server(rsa_private_key, _Kalshi())
    prepare, _ = await _tools(server)
    with pytest.raises(KalshiAPIError, match="ticker must be a non-empty string"):
        await prepare(**{**_ORDER, "ticker": bad})


async def test_prepare_strips_ticker_whitespace(rsa_private_key):
    kalshi = _Kalshi()
    server = _server(rsa_private_key, kalshi)
    prepare, confirm = await _tools(server)
    prepared = await prepare(**{**_ORDER, "ticker": "  KX-TEST "})
    assert prepared["intent"]["ticker"] == "KX-TEST"
    await confirm(confirmation_id=prepared["confirmation_id"])
    assert kalshi.posts[0]["ticker"] == "KX-TEST"


@pytest.mark.parametrize(
    "bad,match",
    [
        (int(time.time() + 3600) * 1000, "MILLISECONDS"),  # ms, not seconds
        (1_000_000_000, "not in the future"),  # 2001
        (int(time.time()) - 1, "not in the future"),
        (1.8e9, "integer"),
        ("1800000000", "integer"),
        (True, "integer"),
    ],
)
async def test_prepare_rejects_bad_expiration_ts(rsa_private_key, bad, match):
    server = _server(rsa_private_key, _Kalshi())
    prepare, _ = await _tools(server)
    with pytest.raises(SafetyError, match=match):
        await prepare(**_ORDER, expiration_ts=bad)


async def test_prepare_accepts_a_future_expiration_in_seconds(rsa_private_key):
    kalshi = _Kalshi()
    server = _server(rsa_private_key, kalshi)
    prepare, confirm = await _tools(server)
    expiry = int(time.time()) + 3600
    prepared = await prepare(**_ORDER, expiration_ts=expiry)
    await confirm(confirmation_id=prepared["confirmation_id"])
    assert kalshi.posts[0]["expiration_time"] == expiry


# ── the pending map is bounded ──────────────────────────────────────────────


async def test_pending_map_is_capped_and_prunes_expired_first(rsa_private_key, monkeypatch):
    server = _server(rsa_private_key, _Kalshi())
    prepare, _ = await _tools(server)
    tiny = {**_ORDER, "count": 1, "limit_price_cents": 1}  # $0.02 each

    for _ in range(orders._MAX_PENDING):
        await prepare(**tiny)
    with pytest.raises(SafetyError, match="Too many prepared-but-unconfirmed"):
        await prepare(**tiny)

    # Once the earlier ones have expired they are pruned before the cap check.
    later = time.time() + orders._PENDING_TTL_S + 1
    monkeypatch.setattr(orders.time, "time", lambda: later)
    assert "confirmation_id" in await prepare(**tiny)
