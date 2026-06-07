"""Schema-contract tests for the accepted-value / range constraints baked
into tool input schemas.

These assert that the JSON-Schema `enum` / `minimum` / `maximum` keywords are
present on the constrained params, so an MCP client (and the LLM) is steered
to valid arguments at generation time — the first line of defense against the
opaque-400 loops that motivated this pass (e.g. candlestick period_interval).

Per the MCP spec the *server* MUST validate inputs and `inputSchema` is
advisory, so the runtime guards remain the authoritative backstop; those are
covered in the per-tool test modules. Here we only pin the schema contract.
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


def _make_server(rsa_private_key) -> FastMCP:
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
    http = httpx.AsyncClient(
        base_url=config.rest_base,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    )
    client = KalshiClient(config=config, signer=signer, rate_limiter=limiter, http_client=http)
    server = FastMCP(name="kalshi-test")
    server._kalshi_client = client  # type: ignore[attr-defined]
    server._kalshi_config = config  # type: ignore[attr-defined]
    server._kalshi_signer = signer  # type: ignore[attr-defined]
    server._kalshi_safety = SafetyController(config)  # type: ignore[attr-defined]
    server._kalshi_rate_limiter = limiter  # type: ignore[attr-defined]
    register_all_tools(server)
    return server


async def _prop(server: FastMCP, tool_name: str, prop_name: str) -> dict:
    tool = await server.get_tool(tool_name)
    assert tool is not None, f"Tool {tool_name!r} not registered"
    return tool.parameters["properties"][prop_name]


def _enum_of(prop: dict) -> list | None:
    """Return the `enum` list whether it sits at the top level (required
    Literal) or nested under `anyOf` (the shape Pydantic emits for an
    Optional[Literal], e.g. `mve_filter`)."""
    if "enum" in prop:
        return prop["enum"]
    for sub in prop.get("anyOf", []):
        if "enum" in sub:
            return sub["enum"]
    return None


# ── enum constraints ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,prop,expected",
    [
        ("kalshi_prepare_order", "action", ["buy", "sell"]),
        ("kalshi_prepare_order", "side", ["yes", "no"]),
        ("kalshi_prepare_order", "order_type", ["limit", "market"]),
        ("kalshi_get_positions", "settlement_status", ["all", "settled", "unsettled"]),
        ("kalshi_get_markets", "mve_filter", ["exclude", "only"]),
        ("kalshi_get_market_candlesticks", "period_interval", [1, 60, 1440]),
        ("kalshi_get_event_candlesticks", "period_interval", [1, 60, 1440]),
    ],
)
async def test_enum_constraint_in_schema(rsa_private_key, tool, prop, expected):
    server = _make_server(rsa_private_key)
    assert _enum_of(await _prop(server, tool, prop)) == expected


# ── numeric range constraints (min + max) ──────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,prop,lo,hi",
    [
        ("kalshi_get_markets", "limit", 1, 1000),
        ("kalshi_get_events", "limit", 1, 200),
        ("kalshi_get_trades", "limit", 1, 1000),
        ("kalshi_find_liquid_markets", "scan_limit", 1, 1000),
        ("kalshi_find_liquid_markets", "limit", 1, 1000),
        ("kalshi_get_market_trades", "limit", 1, 1000),
        ("kalshi_get_positions", "limit", 1, 1000),
        ("kalshi_get_orders", "limit", 1, 1000),
        ("kalshi_get_fills", "limit", 1, 1000),
        ("kalshi_get_settlements", "limit", 1, 1000),
        ("kalshi_prepare_order", "limit_price_cents", 1, 99),
    ],
)
async def test_range_constraint_in_schema(rsa_private_key, tool, prop, lo, hi):
    server = _make_server(rsa_private_key)
    p = await _prop(server, tool, prop)
    assert p["minimum"] == lo
    assert p["maximum"] == hi


# ── lower-bound-only constraints ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,prop,lo",
    [
        ("kalshi_prepare_order", "count", 1),
        ("kalshi_decrease_order", "reduce_by", 1),
        ("kalshi_get_orderbook", "depth", 0),  # depth=0 means "full book"
        ("kalshi_find_liquid_markets", "min_volume", 0),
    ],
)
async def test_min_only_constraint_in_schema(rsa_private_key, tool, prop, lo):
    server = _make_server(rsa_private_key)
    assert (await _prop(server, tool, prop))["minimum"] == lo
