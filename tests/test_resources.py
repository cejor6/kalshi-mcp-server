"""Smoke tests for resource registration.

Mirrors test_tools.py but for the resource side of the server.
"""

from __future__ import annotations

import httpx
import pytest
from fastmcp import FastMCP

from kalshi_mcp_server.auth import KalshiSigner
from kalshi_mcp_server.client import KalshiClient
from kalshi_mcp_server.config import DEMO_REST_BASE, DEMO_WS_URL, Config
from kalshi_mcp_server.rate_limit import KalshiRateLimiter, TierLimits
from kalshi_mcp_server.resources import register_all_resources
from kalshi_mcp_server.safety import SafetyController


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


def test_register_all_resources_does_not_raise(rsa_private_key):
    server = _make_server(rsa_private_key)
    register_all_resources(server)


@pytest.mark.asyncio
async def test_expected_resources_are_registered(rsa_private_key):
    server = _make_server(rsa_private_key)
    register_all_resources(server)
    resources = await server.list_resources()
    uris = {str(r.uri) for r in resources}
    expected = {
        "kalshi://environment",
        "kalshi://balance",
        "kalshi://positions",
        "kalshi://orders",
    }
    missing = expected - uris
    assert not missing, f"Missing resources: {missing}"
