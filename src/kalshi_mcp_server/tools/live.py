"""Live (WebSocket-backed) data tools.

These tools open a transient Kalshi WebSocket, subscribe to a channel,
collect messages for a bounded duration, then close. That's the
simplest useful surface — agents that want sustained streaming should
graduate to a long-lived shared connection (v0.3 work).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from kalshi_mcp_server.errors import KalshiAPIError
from kalshi_mcp_server.ws import KalshiWSClient

if TYPE_CHECKING:
    from fastmcp import FastMCP

MAX_DURATION_S = 30.0


def register(server: FastMCP) -> None:
    """Register live-data tools against the FastMCP server."""
    config = server._kalshi_config  # type: ignore[attr-defined]
    signer = server._kalshi_signer  # type: ignore[attr-defined]

    @server.tool
    async def kalshi_get_live_orderbook(
        ticker: str,
        duration_s: float = 2.0,
    ) -> dict[str, Any]:
        """Open a WebSocket, subscribe to orderbook updates for a market,
        collect messages for `duration_s` seconds, and return the latest
        snapshot plus a count of deltas observed.

        Use this when you want a fresher orderbook than the REST endpoint
        offers, or when you need to gauge how active a market's quoting
        is before placing a maker order.

        Args:
            ticker: Market ticker (e.g. "KXFED-26MAR19-B5.25").
            duration_s: How long to listen, 0.5-30.0 seconds. Default 2.0.

        Returns:
            ticker, snapshot (None if none arrived), deltas_observed,
            messages_observed, duration_s.
        """
        if not 0.5 <= duration_s <= MAX_DURATION_S:
            raise KalshiAPIError(
                status=0,
                message=f"duration_s must be between 0.5 and {MAX_DURATION_S}.",
            )

        ws = KalshiWSClient(config=config, signer=signer)
        snapshot: dict[str, Any] | None = None
        deltas = 0
        total = 0

        async with ws:
            await ws.subscribe(channel="orderbook_delta", market_tickers=[ticker])
            deadline = time.monotonic() + duration_s
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    total += 1
                    mtype = msg.get("type")
                    if mtype == "orderbook_snapshot":
                        snapshot = msg.get("msg", msg)
                    elif mtype == "orderbook_delta":
                        deltas += 1
                    elif mtype == "error":
                        raise KalshiAPIError(
                            status=0,
                            message=f"WS error: {msg.get('msg', msg)}",
                            body=msg,
                        )
            except TimeoutError:
                # Hit the deadline mid-recv — expected end of window.
                pass

        return {
            "ticker": ticker,
            "snapshot": snapshot,
            "deltas_observed": deltas,
            "messages_observed": total,
            "duration_s": duration_s,
        }

    @server.tool
    async def kalshi_sample_trades(
        ticker: str | None = None,
        duration_s: float = 3.0,
    ) -> dict[str, Any]:
        """Listen on the public `trade` channel for a short window and
        return the trades that occurred.

        Args:
            ticker: Restrict to a single market. Omit to sample across
                the whole exchange (firehose — bound `duration_s` low).
            duration_s: How long to listen, 0.5-30.0 seconds. Default 3.0.

        Returns the list of trade messages observed plus a count.
        """
        if not 0.5 <= duration_s <= MAX_DURATION_S:
            raise KalshiAPIError(
                status=0,
                message=f"duration_s must be between 0.5 and {MAX_DURATION_S}.",
            )

        ws = KalshiWSClient(config=config, signer=signer)
        trades: list[dict[str, Any]] = []

        async with ws:
            await ws.subscribe(
                channel="trade",
                market_tickers=[ticker] if ticker else None,
            )
            deadline = time.monotonic() + duration_s
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    if msg.get("type") == "trade":
                        trades.append(msg.get("msg", msg))
                    elif msg.get("type") == "error":
                        raise KalshiAPIError(
                            status=0,
                            message=f"WS error: {msg.get('msg', msg)}",
                            body=msg,
                        )
            except TimeoutError:
                pass

        return {
            "ticker": ticker,
            "trade_count": len(trades),
            "trades": trades,
            "duration_s": duration_s,
        }
