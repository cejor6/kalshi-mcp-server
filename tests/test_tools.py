"""Smoke tests for tool registration.

We don't exercise every tool here — the client's mock-transport tests
already prove the REST layer works, and FastMCP itself is responsible
for tool plumbing. What we verify is:

1. Every tool module imports cleanly.
2. `register_all_tools` runs against a FastMCP server without errors.
3. Expected tool names are present on the server after registration.
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
from kalshi_mcp_server.tools import register_all_tools


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


def _make_server(rsa_private_key) -> FastMCP:
    config = _make_config()
    signer = KalshiSigner(key_id="test-key", private_key=rsa_private_key)
    limiter = KalshiRateLimiter(TierLimits.basic())
    http = httpx.AsyncClient(
        base_url=config.rest_base,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": True})),
    )
    client = KalshiClient(config=config, signer=signer, rate_limiter=limiter, http_client=http)
    server = FastMCP(name="kalshi-test")
    server._kalshi_client = client  # type: ignore[attr-defined]
    server._kalshi_config = config  # type: ignore[attr-defined]
    server._kalshi_signer = signer  # type: ignore[attr-defined]
    server._kalshi_safety = SafetyController(config)  # type: ignore[attr-defined]
    server._kalshi_rate_limiter = limiter  # type: ignore[attr-defined]
    return server


def test_register_all_tools_does_not_raise(rsa_private_key):
    server = _make_server(rsa_private_key)
    register_all_tools(server)


@pytest.mark.asyncio
async def test_expected_tools_are_registered(rsa_private_key):
    server = _make_server(rsa_private_key)
    register_all_tools(server)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    expected = {
        # exchange.py
        "kalshi_get_exchange_status",
        "kalshi_get_exchange_schedule",
        "kalshi_get_api_limits",
        "kalshi_get_environment",
        # discovery.py
        "kalshi_get_markets",
        "kalshi_get_market",
        "kalshi_get_event",
        "kalshi_get_events",
        "kalshi_get_series",
        "kalshi_get_trades",
        # portfolio.py
        "kalshi_get_balance",
        "kalshi_get_positions",
        "kalshi_get_orders",
        "kalshi_get_fills",
        "kalshi_get_settlements",
    }
    missing = expected - names
    assert not missing, f"Missing tools: {missing}"
