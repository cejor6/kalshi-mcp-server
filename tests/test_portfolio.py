"""Tests for portfolio read tools — focused on the balance total_value fix.

Issue #81: `portfolio_value` is Kalshi's POSITIONS-only value (0 when flat),
which downstream mis-read as "account drained". `kalshi_get_balance` now adds
a computed `total_value` = cash + positions so no caller has to know the trap.
"""

from __future__ import annotations

import httpx
import pytest
from fastmcp import FastMCP

from kalshi_mcp_server.auth import KalshiSigner
from kalshi_mcp_server.client import KalshiClient
from kalshi_mcp_server.config import DEMO_REST_BASE, DEMO_WS_URL, Config
from kalshi_mcp_server.rate_limit import KalshiRateLimiter, TierLimits
from kalshi_mcp_server.safety import SafetyController
from kalshi_mcp_server.tools import portfolio


def _make_server(rsa_private_key, handler) -> FastMCP:
    config = Config(
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
    signer = KalshiSigner(key_id="test-key", private_key=rsa_private_key)
    limiter = KalshiRateLimiter(TierLimits.basic())
    http = httpx.AsyncClient(base_url=config.rest_base, transport=httpx.MockTransport(handler))
    client = KalshiClient(config=config, signer=signer, rate_limiter=limiter, http_client=http)
    server = FastMCP(name="kalshi-test")
    server._kalshi_client = client  # type: ignore[attr-defined]
    server._kalshi_config = config  # type: ignore[attr-defined]
    server._kalshi_signer = signer  # type: ignore[attr-defined]
    server._kalshi_safety = SafetyController(config)  # type: ignore[attr-defined]
    server._kalshi_rate_limiter = limiter  # type: ignore[attr-defined]
    portfolio.register(server)
    return server


async def _tool_fn(server: FastMCP, name: str):
    tool = await server.get_tool(name)
    assert tool is not None
    return tool.fn


@pytest.mark.asyncio
async def test_balance_adds_total_value_flat_account(rsa_private_key):
    """The false-drain repro (synthetic values): cash present, no positions →
    portfolio_value 0. total_value must equal cash, not 0."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"balance": 12500, "balance_dollars": "125.0000", "portfolio_value": 0},
        )

    server = _make_server(rsa_private_key, handler)
    out = await (await _tool_fn(server, "kalshi_get_balance"))()
    assert out["total_value"] == 12500
    assert out["total_value_dollars"] == "125.0000"
    # The raw Kalshi fields are preserved unchanged.
    assert out["portfolio_value"] == 0
    assert out["balance"] == 12500


@pytest.mark.asyncio
async def test_balance_total_value_sums_cash_and_positions(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"balance": 5000, "portfolio_value": 1234})

    server = _make_server(rsa_private_key, handler)
    out = await (await _tool_fn(server, "kalshi_get_balance"))()
    assert out["total_value"] == 6234
    assert out["total_value_dollars"] == "62.3400"


@pytest.mark.asyncio
async def test_balance_skips_total_when_fields_missing_or_nonint(rsa_private_key):
    """Additive and defensive — never fabricate a wrong total from junk."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"balance": 5000})  # no portfolio_value

    server = _make_server(rsa_private_key, handler)
    out = await (await _tool_fn(server, "kalshi_get_balance"))()
    assert "total_value" not in out
    assert out["balance"] == 5000
