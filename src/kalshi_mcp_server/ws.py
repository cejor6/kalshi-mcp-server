"""Async WebSocket client for the Kalshi real-time API.

Reference:
    https://docs.kalshi.com/getting_started/quick_start_websockets

The handshake auth is identical to the REST signing contract — sign
`GET /trade-api/ws/v2` (path only, no query string) and pass the same
three headers (KALSHI-ACCESS-KEY / SIGNATURE / TIMESTAMP) on the
WebSocket upgrade request.

Once connected, the wire protocol is JSON over text frames:

    Client -> server:
        {"id": <int>, "cmd": "subscribe",
         "params": {"channels": [...], "market_tickers": [...]}}
        {"id": <int>, "cmd": "unsubscribe", "params": {"sids": [...]}}

    Server -> client:
        {"type": "orderbook_snapshot", "sid": <int>, "seq": <int>, "msg": {...}}
        {"type": "orderbook_delta",    "sid": <int>, "seq": <int>, "msg": {...}}
        {"type": "ticker",             "sid": <int>, "seq": <int>, "msg": {...}}
        {"type": "trade",              "sid": <int>, "seq": <int>, "msg": {...}}
        {"type": "fill",               ...}
        {"type": "subscribed", "sid": <int>}
        {"type": "error", "code": <int>, "msg": "..."}

This module exposes a thin `KalshiWSClient` — open the connection,
subscribe, receive messages, close. Higher-level concerns (channel
multiplexing, snapshot+delta merging, reconnection) belong in the
tools that consume it.
"""

from __future__ import annotations

import json as jsonlib
from typing import Any
from urllib.parse import urlsplit

import websockets
from websockets.asyncio.client import ClientConnection

from kalshi_mcp_server.auth import KalshiSigner
from kalshi_mcp_server.config import Config
from kalshi_mcp_server.errors import KalshiAPIError

DEFAULT_OPEN_TIMEOUT_S = 10.0


class KalshiWSClient:
    """Owns one Kalshi WebSocket connection. Single-tenant; not safe to
    share `recv()` calls across multiple coroutines without a queue.

    Typical usage:

        async with KalshiWSClient(config=cfg, signer=signer) as ws:
            await ws.subscribe(channel="orderbook_delta", market_tickers=[ticker])
            while ...:
                msg = await ws.recv()
                ...
    """

    def __init__(self, *, config: Config, signer: KalshiSigner) -> None:
        self._config = config
        self._signer = signer
        # Kalshi signs the absolute path the server reconstructs, which
        # for the WS endpoint is just the path component of ws_url.
        self._ws_path = urlsplit(config.ws_url).path or "/trade-api/ws/v2"
        self._ws: ClientConnection | None = None
        self._next_id = 0

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def connect(self, *, open_timeout: float = DEFAULT_OPEN_TIMEOUT_S) -> None:
        """Open the WebSocket and complete Kalshi's signed handshake."""
        headers = self._signer.sign(method="GET", path=self._ws_path).as_dict()
        self._ws = await websockets.connect(
            self._config.ws_url,
            additional_headers=headers,
            open_timeout=open_timeout,
        )

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def __aenter__(self) -> KalshiWSClient:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    def _ensure_open(self) -> ClientConnection:
        if self._ws is None:
            raise KalshiAPIError(
                status=0,
                message="WebSocket is not connected — call connect() first.",
            )
        return self._ws

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def subscribe(
        self,
        *,
        channel: str,
        market_tickers: list[str] | None = None,
        event_tickers: list[str] | None = None,
    ) -> int:
        """Subscribe to a channel. Returns the client-side message id.

        Args:
            channel: e.g. "orderbook_delta", "ticker", "trade", "fill",
                "market_lifecycle". See Kalshi WS docs for the full list.
            market_tickers: Restrict to specific markets.
            event_tickers: Restrict to specific events.
        """
        ws = self._ensure_open()
        msg_id = self._allocate_id()
        params: dict[str, Any] = {"channels": [channel]}
        if market_tickers is not None:
            params["market_tickers"] = market_tickers
        if event_tickers is not None:
            params["event_tickers"] = event_tickers
        await ws.send(jsonlib.dumps({"id": msg_id, "cmd": "subscribe", "params": params}))
        return msg_id

    async def unsubscribe(self, *sids: int) -> int:
        """Unsubscribe from one or more sid(s) previously confirmed by Kalshi."""
        ws = self._ensure_open()
        msg_id = self._allocate_id()
        await ws.send(
            jsonlib.dumps(
                {
                    "id": msg_id,
                    "cmd": "unsubscribe",
                    "params": {"sids": list(sids)},
                }
            )
        )
        return msg_id

    async def recv(self) -> dict[str, Any]:
        """Receive the next message and return its parsed JSON body."""
        ws = self._ensure_open()
        raw = await ws.recv()
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return jsonlib.loads(text)
