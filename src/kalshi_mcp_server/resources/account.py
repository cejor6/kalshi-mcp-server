"""Account-state resources.

Each handler returns a JSON string — FastMCP serves it as
`application/json` to the client. We return `str` rather than `dict`
because some MCP clients are stricter about the resource MIME type when
the server declares JSON vs unspecified.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(server: FastMCP) -> None:
    """Register account-state resources against the FastMCP server."""
    client = server._kalshi_client  # type: ignore[attr-defined]
    config = server._kalshi_config  # type: ignore[attr-defined]
    safety = server._kalshi_safety  # type: ignore[attr-defined]
    rate_limiter = server._kalshi_rate_limiter  # type: ignore[attr-defined]

    @server.resource(
        "kalshi://environment",
        name="Kalshi environment",
        description=(
            "Read-only snapshot of which Kalshi environment this MCP server "
            "is connected to (demo vs prod), the trading-enabled flag, the "
            "safety limits currently in force plus their env-configured "
            "ceilings, and the current local rate-limit bucket headroom. "
            "Read this before any write action to confirm you're operating "
            "in the expected environment."
        ),
        mime_type="application/json",
    )
    async def environment() -> str:
        effective = safety.effective_limits()
        ceilings = safety.ceilings
        payload = {
            "env": config.env,
            "trading_enabled": config.trading_enabled,
            "rest_base": config.rest_base,
            "ws_url": config.ws_url,
            "safety_limits": effective.as_dict(),
            "safety_ceilings": ceilings.as_dict(),
            "safety_limits_persist": safety.persistence_durable,
            "rate_limit_headroom": {
                "read_tokens": round(rate_limiter.read.tokens, 2),
                "read_capacity": rate_limiter.read.capacity,
                "write_tokens": round(rate_limiter.write.tokens, 2),
                "write_capacity": rate_limiter.write.capacity,
            },
        }
        return json.dumps(payload, indent=2)

    @server.resource(
        "kalshi://balance",
        name="Kalshi balance",
        description="Current cash balance and buying power for the connected account.",
        mime_type="application/json",
    )
    async def balance() -> str:
        body = await client.get("/portfolio/balance")
        return json.dumps(body, indent=2)

    @server.resource(
        "kalshi://positions",
        name="Kalshi positions",
        description=(
            "All open positions for the connected account. Returns up to "
            "200 entries — use the `kalshi_get_positions` tool for "
            "filtering, pagination, or larger result sets."
        ),
        mime_type="application/json",
    )
    async def positions() -> str:
        body = await client.get(
            "/portfolio/positions",
            params={"limit": 200, "settlement_status": "unsettled"},
        )
        return json.dumps(body, indent=2)

    @server.resource(
        "kalshi://orders",
        name="Kalshi resting orders",
        description=(
            "All currently-resting orders (open or partially filled). "
            "Excludes canceled and fully-executed orders."
        ),
        mime_type="application/json",
    )
    async def orders() -> str:
        body = await client.get(
            "/portfolio/orders",
            params={"limit": 200, "status": "resting"},
        )
        return json.dumps(body, indent=2)
