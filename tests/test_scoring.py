"""Tests for the optional Jev market-scoring tool.

Two HTTP layers are mocked, never real: the Kalshi `/markets` scan (a
`KalshiClient` over `httpx.MockTransport`) and the outbound Jev call (a
`MockTransport` injected as `jev_transport`). Every fallback path is
exercised — 402/429/timeout/network/malformed/low-confidence/no-key — and
each asserts the market degraded to the deterministic heuristic with the
right `fallback_reason`. Async per the repo convention (asyncio_mode="auto").
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from kalshi_mcp_server.auth import KalshiSigner
from kalshi_mcp_server.client import KalshiClient
from kalshi_mcp_server.config import DEMO_REST_BASE, DEMO_WS_URL, Config
from kalshi_mcp_server.rate_limit import KalshiRateLimiter, TierLimits
from kalshi_mcp_server.tools import scoring
from kalshi_mcp_server.tools.scoring import (
    _EDGE_CRITERIA,
    _MAX_RATE_COOLDOWN_SECONDS,
    _MAX_SCORE_LIMIT,
    _base_url_ok,
    _build_state,
    _clean_probabilities,
    _heuristic_score,
    _JevSettings,
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
    http = httpx.AsyncClient(
        base_url=config.rest_base, transport=httpx.MockTransport(_kalshi_handler)
    )
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


# ── Untrusted-response hardening (side allowlist, probabilities, non-finite) ───


async def test_invalid_side_becomes_none_but_stays_scored(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        # A hostile/garbled host returns an off-menu side.
        return httpx.Response(200, json=_jev_answer(score=3.0, edge_conf=0.9, choice="buy_now"))

    out = await _run(rsa_private_key, handler)
    top = out["markets"][0]
    assert top["jev_scored"] is True
    assert top["side"] is None  # "buy_now" is not in {yes,no,pass}


async def test_probabilities_stripped_of_nonfinite_and_unknown_keys(rsa_private_key):
    # A real host can emit non-compliant JSON (bare NaN/Infinity); httpx.json()
    # parses it back to floats. Build the body as raw text so it reproduces that
    # faithfully — the exact case _clean_probabilities exists to neutralize.
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            '{"answers": {"edge": {"score": 3.0, "confidence": 0.9},'
            ' "side": {"choice": "yes", "confidence": 0.7,'
            ' "probabilities": {"yes": NaN, "no": 0.4, "pass": Infinity, "sideways": 0.9}}}}'
        )
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/json"}
        )

    out = await _run(rsa_private_key, handler)
    probs = out["markets"][0]["probabilities"]
    assert probs == {"no": 0.4}
    # Whole result must be strict-JSON-serializable (no bare NaN/Infinity).
    json.dumps(out, allow_nan=False)


async def test_absent_side_object_still_scored(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": {"edge": {"score": 2.0, "confidence": 0.9}}})

    out = await _run(rsa_private_key, handler)
    top = out["markets"][0]
    assert top["jev_scored"] is True
    assert top["side"] is None
    assert top["side_confidence"] is None
    assert top["probabilities"] is None


async def test_confidence_equal_to_threshold_is_scored(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_jev_answer(score=2.0, edge_conf=0.6))

    # Strict `<` threshold: confidence == threshold counts as scored.
    out = await _run(rsa_private_key, handler, min_confidence=0.6)
    assert out["fallback_reasons"] == {}
    assert all(r["jev_scored"] for r in out["markets"])


def test_fallback_rows_carry_side_confidence_none(rsa_private_key):
    # Uniform schema: side_confidence is present (None) on fallback rows too.
    row = scoring._fallback_market(_MARKETS[0], "credits")
    assert row["side_confidence"] is None
    assert set(
        scoring._scored_market(
            _MARKETS[0],
            {
                "edge01": 0.5,
                "edge_confidence": 0.9,
                "side": "yes",
                "side_confidence": 0.8,
                "probabilities": None,
            },
        )
    ) == set(row)  # scored and fallback rows expose the same keys


# ── Heuristic robustness ───────────────────────────────────────────────────────


def test_heuristic_does_not_crash_on_negative_volume():
    bad = {**_MARKETS[0], "volume_24h_fp": "-5"}  # malformed upstream payload
    # Must not raise ValueError from log10(<=0) — the "never crashes" contract.
    assert _heuristic_score(bad) >= 0.0


# ── jev_status honesty with zero candidates ────────────────────────────────────


async def test_disabled_with_zero_candidates_reports_disabled(rsa_private_key):
    def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"markets": [], "cursor": ""})

    client = _make_client(rsa_private_key, empty_handler)
    out = await _score_markets(
        client,
        limit=10,
        scan_limit=200,
        status="open",
        series_ticker=None,
        min_volume=0.0,
        scan_all=False,
        min_confidence=None,
        jev_transport=None,
    )
    assert out["candidates_scored"] == 0
    assert out["jev_enabled"] is False
    assert out["jev_status"] == "disabled"  # NOT silently "ok"


# ── Runtime limit clamp (paid fan-out budget) ──────────────────────────────────


async def test_limit_clamped_to_max_at_runtime(rsa_private_key):
    # 60-market page + a `.fn` caller passing an over-max limit: only _MAX_SCORE_LIMIT
    # candidates may be scored, no matter the schema bound.
    markets = [
        {**_MARKETS[0], "ticker": f"KX{i:03d}", "volume_24h_fp": str(1000 - i)} for i in range(60)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"markets": markets, "cursor": ""})

    client = _make_client(rsa_private_key, handler)
    out = await _score_markets(
        client,
        limit=10_000,
        scan_limit=1000,
        status="open",
        series_ticker=None,
        min_volume=0.0,
        scan_all=False,
        min_confidence=None,
        jev_transport=None,
    )
    assert out["candidates_scored"] == _MAX_SCORE_LIMIT
    assert len(out["markets"]) == _MAX_SCORE_LIMIT


# ── scan_all forwards scan coverage ────────────────────────────────────────────


async def test_scan_all_forwards_completion(rsa_private_key):
    out = await _run(rsa_private_key, None, api_key=None, scan_all=True)
    assert out["scan_all"] is True
    assert out["scan_limit"] is None
    assert out["complete"] is True


# ── Wall-clock deadline and client-error degrade the whole batch ───────────────


async def test_wall_clock_deadline_falls_back(rsa_private_key, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", _API_KEY)
    monkeypatch.setattr(scoring, "_WALL_CLOCK_SECONDS", 0.05)

    async def slow(jev_client, market, settings):
        await asyncio.sleep(1.0)  # exceeds the tiny budget
        return {
            "edge01": 1.0,
            "edge_confidence": 1.0,
            "side": "yes",
            "side_confidence": 1.0,
            "probabilities": None,
        }, None

    monkeypatch.setattr(scoring, "_score_one", slow)
    out = await _run(rsa_private_key, lambda r: httpx.Response(200, json={}), api_key=None)
    assert out["fallback_reasons"] == {"deadline": 2}
    assert all(r["fallback_reason"] == "deadline" for r in out["markets"])


async def test_client_error_falls_back(rsa_private_key, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", _API_KEY)

    async def boom(jev_client, market, settings):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(scoring, "_score_one", boom)
    out = await _run(rsa_private_key, lambda r: httpx.Response(200, json={}), api_key=None)
    assert out["fallback_reasons"] == {"client_error": 2}
    assert all(r["fallback_reason"] == "client_error" for r in out["markets"])


# ── Base-URL validation and cooldown cap (unit) ────────────────────────────────


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://api.typesafe.ai/v1/systemone", True),
        ("https://api.typesafe.ai:443/v1", True),
        ("http://api.typesafe.ai/v1", False),  # not https
        ("https://user:pass@evil.example/v1", False),  # userinfo -> Basic-Auth replaces Bearer
        ("https://api.typesafe.ai:8080/v1", False),  # non-default port
        ("https:///v1", False),  # no host
        ("not a url", False),
    ],
)
def test_base_url_ok(url, ok):
    assert _base_url_ok(url) is ok


def test_cooldown_is_capped(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", _API_KEY)
    monkeypatch.setenv("MCP_JEV_RATE_COOLDOWN_SECONDS", "999999999")
    settings = _JevSettings(min_confidence=None)
    assert settings.cooldown == _MAX_RATE_COOLDOWN_SECONDS


def test_clean_probabilities_none_on_empty():
    assert _clean_probabilities({"unknown": 1.0}) is None
    assert _clean_probabilities("not a dict") is None
    assert _clean_probabilities({"yes": 0.5, "no": "x"}) == {"yes": 0.5}
