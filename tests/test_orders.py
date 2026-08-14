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
from kalshi_mcp_server.safety import SafetyController
from kalshi_mcp_server.tools import orders


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
async def test_confirm_sends_correct_body_and_idempotency(rsa_private_key):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/portfolio/orders"):
            captured["body"] = jsonlib.loads(request.content)
            return httpx.Response(200, json={"order": {"id": "ord_xyz", "status": "resting"}})
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
    assert response["order"]["id"] == "ord_xyz"

    body = captured["body"]
    assert body["ticker"] == "KX-TEST"
    assert body["action"] == "buy"
    assert body["side"] == "yes"
    assert body["count"] == 10
    assert body["yes_price"] == 25
    assert body["client_order_id"] == prepared["idempotency_key"]
    assert body["client_order_id"].startswith("mcp-")
    assert "no_price" not in body  # side=yes -> yes_price only


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
    assert received_method["path"] == "/trade-api/v2/portfolio/orders/ord_a"


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
