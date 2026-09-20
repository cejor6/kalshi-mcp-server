"""Tests for the optional Jev market-scoring tool.

Two HTTP layers are mocked, never real: the Kalshi `/markets` scan (a
`KalshiClient` over `httpx.MockTransport`) and the outbound Jev call (a
`MockTransport` injected as `jev_transport`). Every fallback path is
exercised — 402/429/timeout/network/malformed/low-confidence/no-key — and
each asserts the market degraded to the deterministic heuristic with the
right `fallback_reason`. Async per the repo convention (asyncio_mode="auto").
"""

from __future__ import annotations

import json

import httpx
import pytest

from kalshi_mcp_server.auth import KalshiSigner
from kalshi_mcp_server.client import KalshiClient
from kalshi_mcp_server.config import DEMO_REST_BASE, DEMO_WS_URL, Config
from kalshi_mcp_server.rate_limit import KalshiRateLimiter, TierLimits
from kalshi_mcp_server.tools import scoring
from kalshi_mcp_server.tools.scoring import (
    _build_state,
    _EDGE_CRITERIA,
    _heuristic_score,
    _reset_rate_cooldown,
    _score_markets,
)

_API_KEY = "test-typesafe-key"


@pytest.fixture(autouse=True)
def _clean_jev_env(monkeypatch):
    """Isolate every test from the ambient Jev env and the global cooldown."""
    for var in (
        "TYPESAFE_API_KEY",
        "MCP_JEV_BASE_URL",
        "MCP_JEV_MODEL",
        "MCP_JEV_CONFIDENCE_THRESHOLD",
        "MCP_JEV_TIMEOUT_SECONDS",
        "MCP_JEV_MAX_CONCURRENCY",
        "MCP_JEV_RATE_COOLDOWN_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    _reset_rate_cooldown()
    yield
    _reset_rate_cooldown()


def _make_config(**overrides) -> Config:
    base = {
        "key_id": "test-key",
        "private_key_path": None,
        "private_key_pem": "<set-in-test>",
        "env": "demo",
        "trading_enabled": False,
        "rest_base": DEMO_REST_BASE,
        "ws_url": DEMO_WS_URL,
        "max_order_size_usd": 25.0,
        "daily_limit_usd": 250.0,
        "max_contracts_per_order": 100,
        "cash_reserve_usd": 0.0,
        "transport": "stdio",
        "port": 8000,
        "log_level": "INFO",
    }
    base.update(overrides)
    return Config(**base)


def _make_client(rsa_private_key, handler) -> KalshiClient:
    config = _make_config()
    signer = KalshiSigner(key_id="test-key", private_key=rsa_private_key)
    limiter = KalshiRateLimiter(TierLimits.basic())
    http = httpx.AsyncClient(base_url=config.rest_base, transport=httpx.MockTransport(handler))
    return KalshiClient(config=config, signer=signer, rate_limiter=limiter, http_client=http)


# A two-market, single-page listing. Markets differ in volume and book so the
# heuristic ranking is checkable; tickers appear in `title` so the Jev mock can
# tell them apart from the request body.
_MARKETS = [
    {
        "ticker": "KXAAA",
        "event_ticker": "KXAAA-E",
        "title": "Alpha market KXAAA",
        "status": "open",
        "volume_24h_fp": "500",
        "yes_bid_dollars": "0.4000",
        "yes_ask_dollars": "0.4200",
        "no_bid_dollars": "0.5800",
        "no_ask_dollars": "0.6000",
        "close_time": "2099-01-01T00:00:00Z",
        "rules_primary": "Resolves YES if Alpha happens. " + ("x" * 800),
    },
    {
        "ticker": "KXBBB",
        "event_ticker": "KXBBB-E",
        "title": "Beta market KXBBB",
        "status": "open",
        "volume_24h_fp": "300",
        "yes_bid_dollars": "0.1000",
        "yes_ask_dollars": "0.9000",  # very wide book
        "no_bid_dollars": "0.1000",
        "no_ask_dollars": "0.9000",
        "close_time": "2099-01-01T00:00:00Z",
        "rules_primary": "Resolves YES if Beta happens.",
    },
]


def _kalshi_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path.endswith("/markets")
    # Terminal page (empty cursor) so the scan completes in one request.
    return httpx.Response(200, json={"markets": _MARKETS, "cursor": ""})


def _jev_body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def _which_market(request: httpx.Request) -> str:
    state = _jev_body(request)["state"]
    return "KXAAA" if "KXAAA" in state else "KXBBB"


def _jev_answer(*, score: float, edge_conf: float, choice: str = "yes") -> dict:
    return {
        "model": "jev-latest",
        "answers": {
            "edge": {
                "type": "score",
                "score": score,
                "confidence": edge_conf,
                "probabilities": {"0": 0.0, "1": 0.0, "2": 0.0, "3": 1.0, "4": 0.0},
            },
            "side": {
                "type": "choice",
                "choice": choice,
                "confidence": 0.7,
                "probabilities": {"yes": 0.7, "no": 0.2, "pass": 0.1},
            },
        },
        "usage": {"input_tokens": 100, "output_tokens": 0},
    }


async def _run(rsa_private_key, jev_handler, *, api_key: str | None = _API_KEY, **kwargs):
    """Scan the two-market listing and score via the given Jev handler."""
    client = _make_client(rsa_private_key, _kalshi_handler)
    transport = httpx.MockTransport(jev_handler) if jev_handler is not None else None
    import os

    if api_key is not None:
        os.environ["TYPESAFE_API_KEY"] = api_key
    return await _score_markets(
        client,
        limit=kwargs.pop("limit", 10),
        scan_limit=kwargs.pop("scan_limit", 200),
        status=kwargs.pop("status", "open"),
        series_ticker=kwargs.pop("series_ticker", None),
        min_volume=kwargs.pop("min_volume", 0.0),
        scan_all=kwargs.pop("scan_all", False),
        min_confidence=kwargs.pop("min_confidence", None),
        jev_transport=transport,
    )


# ── Success ──────────────────────────────────────────────────────────────────


async def test_success_scores_and_ranks_by_confidence_times_edge(rsa_private_key, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", _API_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        if _which_market(request) == "KXAAA":
            return httpx.Response(200, json=_jev_answer(score=3.0, edge_conf=0.8))
        return httpx.Response(200, json=_jev_answer(score=1.0, edge_conf=0.9, choice="no"))

    out = await _run(rsa_private_key, handler, api_key=None)

    assert out["jev_enabled"] is True
    assert out["jev_status"] == "ok"
    assert out["fallback_reasons"] == {}
    assert out["candidates_scored"] == 2

    rows = out["markets"]
    assert [r["ticker"] for r in rows] == ["KXAAA", "KXBBB"]  # 0.6 > 0.225

    top = rows[0]
    assert top["jev_scored"] is True
    assert top["fallback_reason"] is None
    assert top["edge_score"] == pytest.approx(0.75)  # score 3 of 0..4
    assert top["confidence"] == pytest.approx(0.8)
    assert top["side"] == "yes"
    assert top["rank_score"] == pytest.approx(0.6)
    assert top["probabilities"] == {"yes": 0.7, "no": 0.2, "pass": 0.1}
    # Output projection drops the verbose resolution rule.
    assert "rules_primary" not in top


# ── Fallback paths (one per failure mode) ──────────────────────────────────────


@pytest.mark.parametrize(
    ("status_code", "reason"),
    [(402, "credits"), (429, "rate"), (500, "http_error")],
)
async def test_non_2xx_falls_back(rsa_private_key, status_code, reason):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "nope"})

    out = await _run(rsa_private_key, handler)

    assert out["jev_status"] == "fallback"
    assert out["fallback_reasons"] == {reason: 2}
    for row in out["markets"]:
        assert row["jev_scored"] is False
        assert row["fallback_reason"] == reason
        assert row["edge_score"] is None
        assert row["rank_score"] == row["heuristic_score"]


async def test_timeout_falls_back(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    out = await _run(rsa_private_key, handler)
    assert out["fallback_reasons"] == {"timeout": 2}
    assert all(r["fallback_reason"] == "timeout" for r in out["markets"])


async def test_network_error_falls_back(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    out = await _run(rsa_private_key, handler)
    assert out["fallback_reasons"] == {"network": 2}


async def test_malformed_answer_falls_back(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        # 200 but no usable `answers.edge.score`.
        return httpx.Response(200, json={"answers": {"edge": {"nonsense": True}}})

    out = await _run(rsa_private_key, handler)
    assert out["fallback_reasons"] == {"malformed": 2}
    assert all(r["fallback_reason"] == "malformed" for r in out["markets"])


async def test_non_json_body_falls_back(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    out = await _run(rsa_private_key, handler)
    assert out["fallback_reasons"] == {"malformed": 2}


async def test_low_confidence_falls_back(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_jev_answer(score=3.0, edge_conf=0.30))

    out = await _run(rsa_private_key, handler, min_confidence=0.6)
    assert out["fallback_reasons"] == {"lowconf": 2}
    assert out["confidence_threshold"] == pytest.approx(0.6)
    assert all(r["fallback_reason"] == "lowconf" for r in out["markets"])


async def test_mixed_scored_and_fallback_ranks_scored_first(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        if _which_market(request) == "KXAAA":
            return httpx.Response(200, json=_jev_answer(score=2.0, edge_conf=0.7))
        return httpx.Response(402, json={"error": "credits"})

    out = await _run(rsa_private_key, handler)
    assert out["jev_status"] == "degraded"
    assert out["fallback_reasons"] == {"credits": 1}
    rows = out["markets"]
    # The scored market sorts above the fallback one regardless of heuristic.
    assert rows[0]["ticker"] == "KXAAA"
    assert rows[0]["jev_scored"] is True
    assert rows[1]["jev_scored"] is False


# ── No key → deterministic heuristic ───────────────────────────────────────────


async def test_no_api_key_uses_heuristic(rsa_private_key):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(request)
        return httpx.Response(200, json=_jev_answer(score=4.0, edge_conf=1.0))

    out = await _run(rsa_private_key, handler, api_key=None)

    assert out["jev_enabled"] is False
    assert out["jev_status"] == "disabled"
    assert out["fallback_reasons"] == {"no_api_key": 2}
    assert calls == []  # Jev never contacted without a key
    rows = out["markets"]
    assert all(r["jev_scored"] is False for r in rows)
    # KXAAA (higher volume, tighter book) outranks KXBBB on the heuristic.
    assert [r["ticker"] for r in rows] == ["KXAAA", "KXBBB"]
    assert rows[0]["rank_score"] == pytest.approx(_heuristic_score(_MARKETS[0]))


async def test_bad_base_url_disables_jev(rsa_private_key, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", _API_KEY)
    monkeypatch.setenv("MCP_JEV_BASE_URL", "http://insecure.example/v1")  # not https

    out = await _run(rsa_private_key, None, api_key=None)
    assert out["jev_enabled"] is False
    assert out["fallback_reasons"] == {"bad_base_url": 2}


# ── 429 trips a global cooldown for subsequent scans ───────────────────────────


async def test_429_trips_global_cooldown(rsa_private_key, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", _API_KEY)
    monkeypatch.setenv("MCP_JEV_RATE_COOLDOWN_SECONDS", "300")

    def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    ok_calls: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        ok_calls.append(request)
        return httpx.Response(200, json=_jev_answer(score=4.0, edge_conf=1.0))

    first = await _run(rsa_private_key, rate_limited, api_key=None)
    assert first["fallback_reasons"] == {"rate": 2}

    # The cooldown is now armed: the next scan must skip Jev entirely even
    # though this handler would succeed.
    second = await _run(rsa_private_key, ok, api_key=None)
    assert second["jev_enabled"] is False
    assert second["fallback_reasons"] == {"rate_cooldown": 2}
    assert ok_calls == []


# ── State building: public data only, correct payload shape ────────────────────


def test_build_state_contains_public_fields_only():
    state = _build_state(_MARKETS[0])
    assert "Alpha market KXAAA" in state
    assert "$0.42" in state  # yes ask
    assert "500 contracts" in state
    assert "Resolution rule:" in state
    # No account/secret leakage.
    for forbidden in ("balance", "position", "api_key", "Bearer", "TYPESAFE"):
        assert forbidden.lower() not in state.lower()


def test_rules_snippet_is_truncated():
    state = _build_state(_MARKETS[0])  # rules_primary is 800+ chars
    # The whole state stays compact; the rule is snippetted, not dumped whole.
    assert len(state) < 900


async def test_request_shape_and_auth_header(rsa_private_key, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", _API_KEY)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_jev_answer(score=2.0, edge_conf=0.9))

    await _run(rsa_private_key, handler, api_key=None)

    assert captured, "Jev was not called"
    req = captured[0]
    assert req.headers["authorization"] == f"Bearer {_API_KEY}"
    assert req.url.host == "api.typesafe.ai"  # key only ever sent to the Jev host
    body = json.loads(req.content)
    assert body["model"] == "jev-latest"
    assert set(body["questions"]) == {"edge", "side"}
    # Score criteria MUST be an ordered array (the Jev contract), not a map.
    assert body["questions"]["edge"]["type"] == "score"
    assert body["questions"]["edge"]["criteria"] == list(_EDGE_CRITERIA)
    assert body["questions"]["side"]["type"] == "choice"
    assert isinstance(body["questions"]["side"]["criteria"], dict)


# ── Registration gate (off by default) ─────────────────────────────────────────


def _make_server(rsa_private_key, *, enabled: bool):
    from fastmcp import FastMCP

    from kalshi_mcp_server.safety import SafetyController

    config = _make_config(jev_scoring_enabled=enabled)
    signer = KalshiSigner(key_id="test-key", private_key=rsa_private_key)
    limiter = KalshiRateLimiter(TierLimits.basic())
    http = httpx.AsyncClient(base_url=config.rest_base, transport=httpx.MockTransport(_kalshi_handler))
    client = KalshiClient(config=config, signer=signer, rate_limiter=limiter, http_client=http)
    server = FastMCP(name="kalshi-test")
    server._kalshi_client = client  # type: ignore[attr-defined]
    server._kalshi_config = config  # type: ignore[attr-defined]
    server._kalshi_safety = SafetyController(config)  # type: ignore[attr-defined]
    scoring.register(server)
    return server


async def test_tool_not_registered_by_default(rsa_private_key):
    server = _make_server(rsa_private_key, enabled=False)
    assert await server.get_tool("kalshi_score_markets") is None


async def test_tool_registered_when_flag_on(rsa_private_key):
    server = _make_server(rsa_private_key, enabled=True)
    assert await server.get_tool("kalshi_score_markets") is not None
