"""Tests for KalshiWSClient.

We stand up a local WebSocket server that mimics Kalshi's protocol —
validates the signed-auth headers, responds to a subscribe command
with a snapshot, then closes. This proves the handshake auth, subscribe
serialization, and recv parsing all work end-to-end without touching
real Kalshi infrastructure.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import socket

import pytest
import websockets

from kalshi_mcp_server.auth import HEADER_KEY, HEADER_SIG, HEADER_TS, KalshiSigner
from kalshi_mcp_server.config import DEMO_REST_BASE, Config
from kalshi_mcp_server.ws import KalshiWSClient


def _free_port() -> int:
    """Pick an unused TCP port; release immediately so the test can bind."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_config(port: int) -> Config:
    return Config(
        key_id="test-key",
        private_key_path=None,
        private_key_pem="<set-in-test>",
        env="demo",
        trading_enabled=False,
        rest_base=DEMO_REST_BASE,
        ws_url=f"ws://127.0.0.1:{port}/trade-api/ws/v2",
        max_order_size_usd=25.0,
        daily_limit_usd=250.0,
        max_contracts_per_order=100,
        cash_reserve_usd=0.0,
        transport="stdio",
        port=8000,
        log_level="INFO",
    )


async def _start_server(handler, port: int):
    """Start a local websockets server on the given port."""
    return await websockets.serve(handler, "127.0.0.1", port)


@pytest.mark.asyncio
async def test_handshake_carries_signed_headers(rsa_private_key):
    """The WS upgrade must include the three Kalshi auth headers."""
    captured_headers: dict[str, str] = {}
    port = _free_port()

    async def handler(websocket):
        # In websockets v16, request headers are on the request property.
        req = websocket.request
        for h in (HEADER_KEY, HEADER_SIG, HEADER_TS):
            if h in req.headers:
                captured_headers[h] = req.headers[h]
        # Echo a subscribe ack so the test can exit cleanly.
        async for raw in websocket:
            data = jsonlib.loads(raw)
            if data.get("cmd") == "subscribe":
                await websocket.send(jsonlib.dumps({"type": "subscribed", "sid": 1}))
                break

    server = await _start_server(handler, port)
    try:
        config = _make_config(port)
        signer = KalshiSigner(key_id="test-key", private_key=rsa_private_key)
        async with KalshiWSClient(config=config, signer=signer) as ws:
            await ws.subscribe(channel="orderbook_delta", market_tickers=["X"])
            msg = await ws.recv()
            assert msg["type"] == "subscribed"
    finally:
        server.close()
        await server.wait_closed()

    assert captured_headers[HEADER_KEY] == "test-key"
    assert captured_headers[HEADER_TS].isdigit()
    assert len(captured_headers[HEADER_SIG]) > 0


@pytest.mark.asyncio
async def test_subscribe_payload_shape(rsa_private_key):
    """The subscribe message should serialize as the documented JSON shape."""
    received_messages: list[dict] = []
    port = _free_port()

    async def handler(websocket):
        async for raw in websocket:
            received_messages.append(jsonlib.loads(raw))

    server = await _start_server(handler, port)
    try:
        config = _make_config(port)
        signer = KalshiSigner(key_id="test-key", private_key=rsa_private_key)
        async with KalshiWSClient(config=config, signer=signer) as ws:
            sid = await ws.subscribe(
                channel="orderbook_delta",
                market_tickers=["KX-A", "KX-B"],
            )
            assert isinstance(sid, int) and sid > 0
            # Give the server a moment to receive the message.
            await asyncio.sleep(0.1)
    finally:
        server.close()
        await server.wait_closed()

    assert len(received_messages) == 1
    msg = received_messages[0]
    assert msg["cmd"] == "subscribe"
    assert msg["params"]["channels"] == ["orderbook_delta"]
    assert msg["params"]["market_tickers"] == ["KX-A", "KX-B"]
    assert isinstance(msg["id"], int)


@pytest.mark.asyncio
async def test_recv_parses_json(rsa_private_key):
    port = _free_port()

    async def handler(websocket):
        async for raw in websocket:
            data = jsonlib.loads(raw)
            if data.get("cmd") == "subscribe":
                # Send a synthetic snapshot.
                await websocket.send(
                    jsonlib.dumps(
                        {
                            "type": "orderbook_snapshot",
                            "sid": 1,
                            "seq": 1,
                            "msg": {
                                "market_ticker": "KX-A",
                                "yes": [[50, 100], [49, 200]],
                                "no": [[40, 100]],
                            },
                        }
                    )
                )
                break

    server = await _start_server(handler, port)
    try:
        config = _make_config(port)
        signer = KalshiSigner(key_id="test-key", private_key=rsa_private_key)
        async with KalshiWSClient(config=config, signer=signer) as ws:
            await ws.subscribe(channel="orderbook_delta", market_tickers=["KX-A"])
            msg = await ws.recv()
            assert msg["type"] == "orderbook_snapshot"
            assert msg["msg"]["market_ticker"] == "KX-A"
            assert msg["msg"]["yes"] == [[50, 100], [49, 200]]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_unsubscribe_serializes_sids(rsa_private_key):
    received: list[dict] = []
    port = _free_port()

    async def handler(websocket):
        async for raw in websocket:
            received.append(jsonlib.loads(raw))

    server = await _start_server(handler, port)
    try:
        config = _make_config(port)
        signer = KalshiSigner(key_id="test-key", private_key=rsa_private_key)
        async with KalshiWSClient(config=config, signer=signer) as ws:
            await ws.subscribe(channel="ticker", market_tickers=["KX-A"])
            await ws.unsubscribe(1, 2, 3)
            await asyncio.sleep(0.1)
    finally:
        server.close()
        await server.wait_closed()

    unsub = [m for m in received if m.get("cmd") == "unsubscribe"]
    assert len(unsub) == 1
    assert unsub[0]["params"]["sids"] == [1, 2, 3]
