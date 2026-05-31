"""Tests for KalshiClient.

We don't hit the real Kalshi API. Instead, `httpx.MockTransport` lets us
intercept every outbound request and assert on the URL, method, and
headers — proving the signer + rate limiter + URL plumbing are wired up
correctly. Response bodies are returned synthetically.
"""

from __future__ import annotations

import json as jsonlib

import httpx
import pytest

from kalshi_mcp_server.auth import HEADER_KEY, HEADER_SIG, HEADER_TS, KalshiSigner
from kalshi_mcp_server.client import KalshiClient
from kalshi_mcp_server.config import DEMO_REST_BASE, DEMO_WS_URL, Config
from kalshi_mcp_server.errors import KalshiAPIError, RateLimitError
from kalshi_mcp_server.rate_limit import KalshiRateLimiter, TierLimits


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


def _make_client(handler, rsa_private_key) -> KalshiClient:
    config = _make_config()
    signer = KalshiSigner(key_id="test-key", private_key=rsa_private_key)
    limiter = KalshiRateLimiter(TierLimits.basic())
    http = httpx.AsyncClient(
        base_url=config.rest_base,
        transport=httpx.MockTransport(handler),
    )
    return KalshiClient(config=config, signer=signer, rate_limiter=limiter, http_client=http)


@pytest.mark.asyncio
async def test_get_returns_body_on_200(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/trade-api/v2/exchange/status"
        return httpx.Response(200, json={"exchange_active": True})

    client = _make_client(handler, rsa_private_key)
    body = await client.get("/exchange/status")
    assert body == {"exchange_active": True}


@pytest.mark.asyncio
async def test_get_signs_with_full_path_including_base(rsa_private_key):
    """The signed path must include the /trade-api/v2 prefix."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(
            {
                "method": request.method,
                "path": request.url.path,
                HEADER_KEY: request.headers[HEADER_KEY],
                HEADER_SIG: request.headers[HEADER_SIG],
                HEADER_TS: request.headers[HEADER_TS],
            }
        )
        return httpx.Response(200, json={})

    client = _make_client(handler, rsa_private_key)
    await client.get("/portfolio/balance")

    assert captured[HEADER_KEY] == "test-key"
    assert captured[HEADER_TS].isdigit()
    assert captured["path"] == "/trade-api/v2/portfolio/balance"

    # The signer's behavior is fully tested in test_auth.py; here we just
    # confirm a signature header is present and non-empty.
    assert len(captured[HEADER_SIG]) > 0


@pytest.mark.asyncio
async def test_get_with_query_params(rsa_private_key):
    """Query params are sent on the wire but NOT included in the signed path."""
    captured_path = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_path.append(request.url.path)
        # The MockTransport-side URL path does not include the query string.
        assert request.url.params.get("status") == "open"
        return httpx.Response(200, json={"markets": []})

    client = _make_client(handler, rsa_private_key)
    body = await client.get("/markets", params={"status": "open", "limit": 50})
    assert body == {"markets": []}
    # Path on the wire — sanity check, query params are separate
    assert captured_path == ["/trade-api/v2/markets"]


@pytest.mark.asyncio
async def test_429_raises_rate_limit_error(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"code": "rate_limit", "message": "Too many requests"}},
        )

    client = _make_client(handler, rsa_private_key)
    with pytest.raises(RateLimitError) as exc:
        await client.get("/markets")
    assert exc.value.status == 429
    assert "Too many requests" in str(exc.value)


@pytest.mark.asyncio
async def test_4xx_raises_kalshi_api_error_with_body(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": "not_found", "message": "Market not found"}},
        )

    client = _make_client(handler, rsa_private_key)
    with pytest.raises(KalshiAPIError) as exc:
        await client.get("/markets/DOES-NOT-EXIST")
    assert exc.value.status == 404
    assert "Market not found" in exc.value.message
    assert exc.value.body["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_post_includes_json_body_and_uses_write_bucket(rsa_private_key):
    received: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["method"] = request.method
        received["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"order": {"id": "ord_123"}})

    client = _make_client(handler, rsa_private_key)
    # Before the call, write bucket is full.
    assert client._rate_limiter.write.tokens == 100  # basic tier write capacity
    body = await client.post("/portfolio/orders", json={"ticker": "X", "count": 1})
    assert received["method"] == "POST"
    assert received["body"] == {"ticker": "X", "count": 1}
    assert body == {"order": {"id": "ord_123"}}
    # After the call, write bucket should have been debited.
    assert client._rate_limiter.write.tokens < 100


@pytest.mark.asyncio
async def test_delete_uses_write_bucket(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        return httpx.Response(200, json={"order": {"id": "ord_123", "status": "canceled"}})

    client = _make_client(handler, rsa_private_key)
    before = client._rate_limiter.write.tokens
    await client.delete("/portfolio/orders/ord_123")
    assert client._rate_limiter.write.tokens < before


@pytest.mark.asyncio
async def test_unparseable_error_body_still_raises_cleanly(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"<html>upstream is angry</html>")

    client = _make_client(handler, rsa_private_key)
    with pytest.raises(KalshiAPIError) as exc:
        await client.get("/markets")
    assert exc.value.status == 500
    assert "raw" in exc.value.body


@pytest.mark.asyncio
async def test_3xx_redirect_raises_clear_error(rsa_private_key):
    """Empty path parameters cause Kalshi to 301 to the canonical URL.

    The client should NOT silently accept that as success — it should
    raise with a hint about what likely went wrong (malformed param).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            301,
            headers={"location": "/trade-api/v2/markets"},
            content=b'<a href="/trade-api/v2/markets">Moved Permanently</a>',
        )

    client = _make_client(handler, rsa_private_key)
    with pytest.raises(KalshiAPIError) as exc:
        await client.get("/markets/")  # trailing-slash hits the redirect
    assert exc.value.status == 301
    assert "redirect" in exc.value.message.lower()
    assert "path parameter" in exc.value.message.lower()


@pytest.mark.asyncio
async def test_other_3xx_codes_also_raise(rsa_private_key):
    """302 / 303 / 307 / 308 — anything in 3xx is unexpected."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": "/elsewhere"})

    client = _make_client(handler, rsa_private_key)
    with pytest.raises(KalshiAPIError) as exc:
        await client.get("/markets/X")
    assert exc.value.status == 307


def test_make_config_has_correct_demo_base():
    """Sanity check on the demo config used throughout these tests."""
    config = _make_config()
    assert config.rest_base == "https://demo-api.kalshi.co/trade-api/v2"


def test_config_is_frozen():
    """Defensive check — Config must be immutable so tests can't accidentally
    mutate a shared instance and pollute later assertions."""
    from dataclasses import FrozenInstanceError

    config = _make_config()
    with pytest.raises(FrozenInstanceError):
        config.key_id = "mutated"  # type: ignore[misc]
