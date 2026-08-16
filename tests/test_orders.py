"""Tests for the prepare/confirm order flow + cancel.

These tests don't hit the real Kalshi API — they intercept the order
POST via MockTransport and verify the body shape, idempotency key,
and safety-controller interactions.
"""

from __future__ import annotations

import json as jsonlib

import httpx
import pytest
from fastmcp import FastMCP

from kalshi_mcp_server.auth import KalshiSigner
from kalshi_mcp_server.client import KalshiClient
from kalshi_mcp_server.config import DEMO_REST_BASE, DEMO_WS_URL, Config
from kalshi_mcp_server.errors import KalshiAPIError, SafetyError, TradingDisabledError
from kalshi_mcp_server.rate_limit import KalshiRateLimiter, TierLimits
from kalshi_mcp_server.safety import OrderIntent, SafetyController
from kalshi_mcp_server.tools import orders
from kalshi_mcp_server.tools.orders import (
    _build_v2_order_body,
    _PendingOrder,
    _v2_side_and_price_cents,
)


def _make_config(*, trading_enabled: bool = True) -> Config:
    return Config(
        key_id="test-key",
        private_key_path=None,
        private_key_pem="<set-in-test>",
        env="demo",
        trading_enabled=trading_enabled,
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


def _make_server(
    rsa_private_key,
    handler,
    *,
    trading_enabled: bool = True,
) -> FastMCP:
    config = _make_config(trading_enabled=trading_enabled)
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
    orders.register(server)
    return server


async def _get_tool_fn(server: FastMCP, name: str):
    """Pull the underlying async function out of a registered tool."""
    tool = await server.get_tool(name)
    assert tool is not None, f"Tool {name!r} not registered"
    return tool.fn


@pytest.mark.asyncio
async def test_prepare_rejects_when_trading_disabled(rsa_private_key):
    server = _make_server(
        rsa_private_key,
        lambda _: httpx.Response(200, json={}),
        trading_enabled=False,
    )
    prepare = await _get_tool_fn(server, "kalshi_prepare_order")
    with pytest.raises(TradingDisabledError):
        await prepare(
            ticker="X",
            action="buy",
            side="yes",
            count=1,
            limit_price_cents=50,
        )


@pytest.mark.asyncio
async def test_prepare_rejects_oversize_order(rsa_private_key):
    """An order whose cost exceeds MCP_MAX_ORDER_SIZE_USD must NOT yield a token."""
    server = _make_server(rsa_private_key, lambda _: httpx.Response(200, json={}))
    prepare = await _get_tool_fn(server, "kalshi_prepare_order")
    # 100 contracts * 50¢ = $50, exceeds max=$25
    with pytest.raises(SafetyError):
        await prepare(
            ticker="X",
            action="buy",
            side="yes",
            count=100,
            limit_price_cents=50,
        )


@pytest.mark.asyncio
async def test_prepare_returns_confirmation_id(rsa_private_key):
    server = _make_server(rsa_private_key, lambda _: httpx.Response(200, json={}))
    prepare = await _get_tool_fn(server, "kalshi_prepare_order")
    result = await prepare(
        ticker="KX-TEST",
        action="buy",
        side="yes",
        count=10,
        limit_price_cents=25,
    )
    assert "confirmation_id" in result
    assert result["safety_status"] == "PASS"
    assert result["estimated_cost_usd"] == 2.50
    assert result["intent"]["ticker"] == "KX-TEST"
    assert result["intent"]["action"] == "buy"
    assert result["intent"]["count"] == 10


@pytest.mark.asyncio
async def test_confirm_sends_v2_body_to_v2_endpoint(rsa_private_key):
    """Issue #83: confirm must hit the V2 create path with the V2 body shape.
    The old `POST /portfolio/orders` + yes/no+cents body is retired (410)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        if request.url.path.endswith("/portfolio/events/orders"):
            captured["body"] = jsonlib.loads(request.content)
            return httpx.Response(200, json={"order_id": "ord_xyz", "remaining_count": "10"})
        return httpx.Response(404)

    server = _make_server(rsa_private_key, handler)
    prepare = await _get_tool_fn(server, "kalshi_prepare_order")
    confirm = await _get_tool_fn(server, "kalshi_confirm_order")

    prepared = await prepare(
        ticker="KX-TEST",
        action="buy",
        side="yes",
        count=10,
        limit_price_cents=25,
    )
    response = await confirm(confirmation_id=prepared["confirmation_id"])
    assert response["order_id"] == "ord_xyz"
    assert captured["path"].endswith("/portfolio/events/orders")  # V2 path, not legacy

    body = captured["body"]
    assert body["ticker"] == "KX-TEST"
    assert body["side"] == "bid"  # buy YES → bid
    assert body["price"] == "0.2500"  # 25¢ → fixed-point YES dollars
    assert body["count"] == "10"  # FixedPointCount STRING
    assert body["time_in_force"] == "good_till_canceled"  # limit rests
    assert body["self_trade_prevention_type"] == "taker_at_cross"
    assert body["client_order_id"] == prepared["idempotency_key"]
    assert body["client_order_id"].startswith("mcp-")
    # The retired v1 fields must be gone.
    for gone in ("action", "yes_price", "no_price", "type"):
        assert gone not in body


@pytest.mark.asyncio
async def test_confirm_with_unknown_token_raises(rsa_private_key):
    server = _make_server(rsa_private_key, lambda _: httpx.Response(200, json={}))
    confirm = await _get_tool_fn(server, "kalshi_confirm_order")
    with pytest.raises(SafetyError):
        await confirm(confirmation_id="nope-not-a-real-token")


@pytest.mark.asyncio
async def test_confirm_consumes_token_so_replay_fails(rsa_private_key):
    """The same confirmation_id can only execute once — prevents duplicate orders."""
    server = _make_server(
        rsa_private_key,
        lambda _: httpx.Response(200, json={"order": {"id": "ord_a"}}),
    )
    prepare = await _get_tool_fn(server, "kalshi_prepare_order")
    confirm = await _get_tool_fn(server, "kalshi_confirm_order")

    prepared = await prepare(
        ticker="X",
        action="buy",
        side="yes",
        count=1,
        limit_price_cents=10,
    )
    await confirm(confirmation_id=prepared["confirmation_id"])
    with pytest.raises(SafetyError):
        await confirm(confirmation_id=prepared["confirmation_id"])


@pytest.mark.asyncio
async def test_cancel_works_even_when_trading_disabled(rsa_private_key):
    """Cancellation must remain available — it only reduces exposure."""
    received_method: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received_method["method"] = request.method
        received_method["path"] = request.url.path
        return httpx.Response(200, json={"order": {"id": "ord_a", "status": "canceled"}})

    server = _make_server(rsa_private_key, handler, trading_enabled=False)
    cancel = await _get_tool_fn(server, "kalshi_cancel_order")
    response = await cancel(order_id="ord_a")
    assert response["order"]["status"] == "canceled"
    assert received_method["method"] == "DELETE"
    # Issue #83: cancel moved to the V2 /portfolio/events/orders path.
    assert received_method["path"] == "/trade-api/v2/portfolio/events/orders/ord_a"


# ── order_id path-segment validation (security hardening) ───────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["kalshi_cancel_order", "kalshi_get_order"])
async def test_order_id_with_query_injection_is_rejected_before_the_wire(
    rsa_private_key, tool_name
):
    """`order_id` is interpolated into the request path, and auth.py strips the
    query BEFORE signing — so `order_id="abc?foo=bar"` would produce a
    signature-VALID request carrying caller-chosen query params. That must be
    rejected locally, before any wire call."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    server = _make_server(rsa_private_key, handler, trading_enabled=True)
    fn = await _get_tool_fn(server, tool_name)

    for bad in ("abc?foo=bar", "../balance", "ord/../../x", "ord a", "."):
        with pytest.raises(KalshiAPIError) as exc:
            await fn(order_id=bad)
        assert "order_id" in exc.value.message
    assert calls == []  # nothing reached Kalshi


@pytest.mark.asyncio
async def test_decrease_order_validates_order_id_before_the_wire(rsa_private_key):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    server = _make_server(rsa_private_key, handler, trading_enabled=True)
    fn = await _get_tool_fn(server, "kalshi_decrease_order")

    with pytest.raises(KalshiAPIError) as exc:
        await fn(order_id="abc?foo=bar", reduce_by=1)
    assert "order_id" in exc.value.message
    assert calls == []


@pytest.mark.asyncio
async def test_order_id_accepts_a_real_uuid(rsa_private_key):
    """The guard must not reject a genuine Kalshi order id (a UUID)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"order": {"id": "ok"}})

    server = _make_server(rsa_private_key, handler, trading_enabled=True)
    fn = await _get_tool_fn(server, "kalshi_get_order")
    out = await fn(order_id="a1b2c3d4-5e6f-7a8b-9c0d-1e2f3a4b5c6d")
    assert out["order"]["id"] == "ok"


# ── V2 order-body conversion (issue #83, real-money-critical) ───────────────


@pytest.mark.parametrize(
    "action,side,cents,exp_side,exp_price_cents",
    [
        # YES leg: bid=buy, ask=sell, same price.
        ("buy", "yes", 25, "bid", 25),
        ("sell", "yes", 25, "ask", 25),
        # NO leg converts to the equivalent YES-side order at the complement:
        #   buy NO @ 30 = sell YES @ 70  → ask, 70
        #   sell NO @ 30 = buy YES @ 70  → bid, 70
        ("buy", "no", 30, "ask", 70),
        ("sell", "no", 30, "bid", 70),
    ],
)
def test_v2_side_and_price_cents_all_four_quadrants(action, side, cents, exp_side, exp_price_cents):
    """The inversion that must be exactly right — getting the NO mapping
    backwards would place a completely different real-money bet."""
    v2_side, price_cents = _v2_side_and_price_cents(action, side, cents)
    assert v2_side == exp_side
    assert price_cents == exp_price_cents


def _pending(action="buy", side="yes", count=2, cents=54, type="limit", post_only=False, exp=None):
    return _PendingOrder(
        intent=OrderIntent(
            ticker="KX-T", side=side, action=action, count=count, limit_price_cents=cents
        ),
        type=type,
        post_only=post_only,
        expiration_ts=exp,
        idempotency_key="mcp-abc",
        expires_at=0.0,
    )


def test_build_v2_body_buy_yes_limit():
    body = _build_v2_order_body(_pending(action="buy", side="yes", count=2, cents=54))
    assert body == {
        "ticker": "KX-T",
        "side": "bid",
        "price": "0.5400",
        "count": "2",
        "client_order_id": "mcp-abc",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
    }


def test_build_v2_body_buy_no_converts_to_yes_ask_at_complement():
    """buy NO @ 47¢ → sell YES @ 53¢ → side=ask price=0.5300. This is the case
    the trading-agent's NYY entry hit; it must not become a YES bet."""
    body = _build_v2_order_body(_pending(action="buy", side="no", cents=47))
    assert body["side"] == "ask"
    assert body["price"] == "0.5300"


def test_build_v2_body_market_order_is_immediate_or_cancel():
    body = _build_v2_order_body(_pending(type="market"))
    assert body["time_in_force"] == "immediate_or_cancel"


def test_build_v2_body_passes_post_only_and_expiration():
    body = _build_v2_order_body(_pending(post_only=True, exp=1_800_000_000))
    assert body["post_only"] is True
    assert body["expiration_time"] == 1_800_000_000


@pytest.mark.asyncio
async def test_decrease_uses_v2_path_and_string_reduce_by(rsa_private_key):
    """Issue #83: decrease moved to the V2 path and `reduce_by` is now a
    FixedPointCount STRING, not an int."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"order_id": "ord_a", "remaining_count": "3"})

    server = _make_server(rsa_private_key, handler, trading_enabled=True)
    decrease = await _get_tool_fn(server, "kalshi_decrease_order")
    await decrease(order_id="ord_a", reduce_by=2)

    assert captured["path"].endswith("/portfolio/events/orders/ord_a/decrease")
    assert captured["body"] == {"reduce_by": "2"}  # string, not int


@pytest.mark.asyncio
async def test_get_order_stays_on_the_legacy_read_path(rsa_private_key):
    """Kalshi kept Get Order on /portfolio/orders/{id} (a read) while the
    write family moved — so this one must NOT be migrated."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"order": {"id": "ord_a"}})

    server = _make_server(rsa_private_key, handler)
    get_order = await _get_tool_fn(server, "kalshi_get_order")
    await get_order(order_id="ord_a")
    assert captured["path"].endswith("/portfolio/orders/ord_a")
    assert "/events/" not in captured["path"]


def test_build_v2_body_market_drops_expiration_and_post_only():
    """REGRESSION: a market order (IOC) must not carry expiration_time or
    post_only — Kalshi 400s that combination. The builder gates them even for a
    directly-constructed pending order."""
    body = _build_v2_order_body(_pending(type="market", post_only=True, exp=1_800_000_000))
    assert body["time_in_force"] == "immediate_or_cancel"
    assert "expiration_time" not in body
    assert "post_only" not in body


@pytest.mark.asyncio
async def test_prepare_rejects_market_order_with_expiration_ts(rsa_private_key):
    server = _make_server(rsa_private_key, lambda _: httpx.Response(200, json={}))
    prepare = await _get_tool_fn(server, "kalshi_prepare_order")
    with pytest.raises(SafetyError) as exc:
        await prepare(
            ticker="X",
            action="buy",
            side="yes",
            count=1,
            limit_price_cents=50,
            order_type="market",
            expiration_ts=1_800_000_000,
        )
    assert "market order" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_prepare_rejects_market_order_post_only(rsa_private_key):
    server = _make_server(rsa_private_key, lambda _: httpx.Response(200, json={}))
    prepare = await _get_tool_fn(server, "kalshi_prepare_order")
    with pytest.raises(SafetyError) as exc:
        await prepare(
            ticker="X",
            action="buy",
            side="yes",
            count=1,
            limit_price_cents=50,
            order_type="market",
            post_only=True,
        )
    assert "post_only" in str(exc.value)
