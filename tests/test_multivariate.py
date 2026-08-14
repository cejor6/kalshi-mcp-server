"""Tests for the multivariate (combo / parlay) tool surface.

Two halves:

1. Pure helpers — leg resolution from `mve_selected_legs` / `custom_strike`,
   and the validation that guards the CREATE path.
2. Wired tools driven through `httpx.MockTransport` — no credentials, no
   network.

The create tool is the only write surface here. Its gating gets first-class
coverage: not registered unless MCP_ALLOW_COMBO_CREATION=1, refuses past the
per-day ceiling, and does not consume local budget on a rejected call.
"""

from __future__ import annotations

import httpx
import pytest
from fastmcp import FastMCP

from kalshi_mcp_server.auth import KalshiSigner
from kalshi_mcp_server.client import KalshiClient
from kalshi_mcp_server.config import DEMO_REST_BASE, DEMO_WS_URL, Config
from kalshi_mcp_server.errors import ComboCreationDisabledError, KalshiAPIError, SafetyError
from kalshi_mcp_server.rate_limit import KalshiRateLimiter, TierLimits
from kalshi_mcp_server.safety import SafetyController
from kalshi_mcp_server.tools import multivariate
from kalshi_mcp_server.tools.multivariate import (
    _check_against_collection,
    _legs_from_custom_strike,
    _legs_from_selected,
    _normalize_leg_specs,
    _resolve_combo_legs,
    ComboLeg,
)


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


def _make_server(rsa_private_key, handler, **config_overrides) -> FastMCP:
    config = _make_config(**config_overrides)
    signer = KalshiSigner(key_id="test-key", private_key=rsa_private_key)
    limiter = KalshiRateLimiter(TierLimits.basic())
    http = httpx.AsyncClient(
        base_url=config.rest_base,
        transport=httpx.MockTransport(handler),
    )
    client = KalshiClient(config=config, signer=signer, rate_limiter=limiter, http_client=http)
    server = FastMCP(name="kalshi-test")
    server._kalshi_client = client  # type: ignore[attr-defined]
    server._kalshi_config = config  # type: ignore[attr-defined]
    server._kalshi_signer = signer  # type: ignore[attr-defined]
    server._kalshi_safety = SafetyController(config)  # type: ignore[attr-defined]
    server._kalshi_rate_limiter = limiter  # type: ignore[attr-defined]
    multivariate.register(server)
    return server


async def _tool_fn(server: FastMCP, name: str):
    tool = await server.get_tool(name)
    assert tool is not None, f"Tool {name!r} not registered"
    return tool.fn


def _mve_market() -> dict:
    """A representative combo market, shaped like a real prod response."""
    return {
        "ticker": "KXMVECROSSCATEGORY-S2026ABC-DEF",
        "event_ticker": "KXMVECROSSCATEGORY-S2026ABC",
        "mve_collection_ticker": "KXMVECROSSCATEGORY-R",
        "mve_selected_legs": [
            {
                "event_ticker": f"KXMLBTOTAL-{i}",
                "market_ticker": f"KXMLBTOTAL-{i}-6",
                "side": "yes",
            }
            for i in range(9)
        ],
    }


# ── leg resolution helpers ─────────────────────────────────────────────────


def test_legs_from_selected_reads_structured_field():
    """`mve_selected_legs` is the authoritative source — ticker, event and
    side come straight out of it, in Kalshi's order."""
    legs = _legs_from_selected(_mve_market())
    assert len(legs) == 9
    assert legs[0] == {
        "market_ticker": "KXMLBTOTAL-0-6",
        "event_ticker": "KXMLBTOTAL-0",
        "side": "yes",
    }


def test_legs_from_selected_skips_malformed_entries():
    market = {
        "mve_selected_legs": [
            "not-a-dict",
            {"event_ticker": "E1"},  # no market_ticker
            {"market_ticker": "   "},  # blank market_ticker
            {"market_ticker": " KX-A ", "event_ticker": "KX", "side": "no"},
        ]
    }
    assert _legs_from_selected(market) == [
        {"market_ticker": "KX-A", "event_ticker": "KX", "side": "no"}
    ]


def test_legs_from_selected_empty_when_absent_or_wrong_type():
    assert _legs_from_selected({}) == []
    assert _legs_from_selected({"mve_selected_legs": "nope"}) == []


def test_legs_from_custom_strike_parses_parallel_csvs():
    """The fallback: three comma-separated columns that line up by index.
    This is the real prod shape (confirmed live)."""
    market = {
        "custom_strike": {
            "Associated Events": "KXATP-A,KXMLB-B",
            "Associated Markets": "KXATP-A-BEL,KXMLB-B-LAA8",
            "Associated Market Sides": "yes,no",
        }
    }
    assert _legs_from_custom_strike(market) == [
        {"market_ticker": "KXATP-A-BEL", "event_ticker": "KXATP-A", "side": "yes"},
        {"market_ticker": "KXMLB-B-LAA8", "event_ticker": "KXMLB-B", "side": "no"},
    ]


def test_legs_from_custom_strike_drops_side_on_column_mismatch():
    """Parallel arrays with no linking key: if the columns don't line up we
    cannot say which side belongs to which leg. Keep the unambiguous tickers,
    null the side — never guess, which is the whole point of this tool."""
    market = {
        "custom_strike": {
            "Associated Markets": "A-1,B-2,C-3",
            "Associated Market Sides": "yes,no",  # short by one
            "Associated Events": "A,B",  # also short
        }
    }
    legs = _legs_from_custom_strike(market)
    assert [leg["market_ticker"] for leg in legs] == ["A-1", "B-2", "C-3"]
    assert all(leg["side"] is None for leg in legs)
    assert all(leg["event_ticker"] is None for leg in legs)


def test_legs_from_custom_strike_empty_without_markets_column():
    assert _legs_from_custom_strike({}) == []
    assert _legs_from_custom_strike({"custom_strike": "nope"}) == []
    assert _legs_from_custom_strike({"custom_strike": {"Associated Market Sides": "yes"}}) == []


def test_resolve_combo_legs_prefers_structured_over_csv():
    market = _mve_market()
    market["custom_strike"] = {"Associated Markets": "SHOULD-NOT-WIN"}
    legs, source = _resolve_combo_legs(market)
    assert source == "mve_selected_legs"
    assert legs[0]["market_ticker"] == "KXMLBTOTAL-0-6"


def test_resolve_combo_legs_falls_back_to_custom_strike():
    market = {
        "custom_strike": {
            "Associated Markets": "A-1",
            "Associated Market Sides": "yes",
            "Associated Events": "A",
        }
    }
    legs, source = _resolve_combo_legs(market)
    assert source == "custom_strike"
    assert legs == [{"market_ticker": "A-1", "event_ticker": "A", "side": "yes"}]


def test_resolve_combo_legs_unresolvable_returns_empty():
    """An ordinary market has neither source — the tool must report
    not-resolvable rather than fall back to parsing the title string."""
    plain = {"ticker": "KXFED-26MAR19-B5.25", "title": "Fed above 5.25%, and other things"}
    assert _resolve_combo_legs(plain) == ([], None)


# ── _normalize_leg_specs (authoritative runtime guard on the write path) ────

_GOOD_LEGS = [
    {"market_ticker": "KXA-E1-X", "event_ticker": "KXA-E1", "side": "yes"},
    {"market_ticker": "KXB-E1-Y", "event_ticker": "KXB-E1", "side": "no"},
]


def test_normalize_leg_specs_accepts_dicts_and_models_identically():
    """The `list[ComboLeg]` annotation only steers MCP clients; a direct `.fn`
    caller passes dicts. Both must normalize to the same thing."""
    from_dicts = _normalize_leg_specs(_GOOD_LEGS)
    from_models = _normalize_leg_specs([ComboLeg(**leg) for leg in _GOOD_LEGS])
    assert from_dicts == from_models == _GOOD_LEGS


def test_normalize_leg_specs_strips_and_lowercases_side():
    out = _normalize_leg_specs(
        [{"market_ticker": " KXA-E1-X ", "event_ticker": " KXA-E1 ", "side": "YES"}]
    )
    assert out == [{"market_ticker": "KXA-E1-X", "event_ticker": "KXA-E1", "side": "yes"}]


@pytest.mark.parametrize(
    "bad,expected",
    [
        ([], "non-empty"),
        ("not-a-list", "non-empty"),
        ([{"event_ticker": "E", "side": "yes"}], "market_ticker"),
        ([{"market_ticker": "M", "side": "yes"}], "event_ticker"),
        ([{"market_ticker": "M", "event_ticker": "E"}], "side"),
        ([{"market_ticker": "M", "event_ticker": "E", "side": "maybe"}], "side"),
        (["not-an-object"], "must be an object"),
    ],
)
def test_normalize_leg_specs_rejects_malformed(bad, expected):
    with pytest.raises(SafetyError) as exc:
        _normalize_leg_specs(bad)
    assert expected in str(exc.value)


def test_normalize_leg_specs_rejects_duplicate_leg_market():
    """A combo takes each leg market once; both sides of one market can't be
    legs of the same parlay, and Kalshi would 400 on it opaquely."""
    with pytest.raises(SafetyError) as exc:
        _normalize_leg_specs(
            [
                {"market_ticker": "KXA-E1-X", "event_ticker": "KXA-E1", "side": "yes"},
                {"market_ticker": "KXA-E1-X", "event_ticker": "KXA-E1", "side": "no"},
            ]
        )
    assert "repeats market_ticker" in str(exc.value)


def test_normalize_leg_specs_rejects_oversized_selection():
    legs = [
        {"market_ticker": f"KX-{i}", "event_ticker": "KX-E", "side": "yes"} for i in range(101)
    ]
    with pytest.raises(SafetyError) as exc:
        _normalize_leg_specs(legs)
    assert "cap is 100" in str(exc.value)


# ── _check_against_collection (pre-flight vs the collection's own rules) ────


def test_check_against_collection_enforces_size_bounds():
    with pytest.raises(SafetyError) as exc:
        _check_against_collection({"size_min": 3, "size_max": 9}, _GOOD_LEGS)
    assert "at least 3" in str(exc.value)

    many = [
        {"market_ticker": f"KX-{i}", "event_ticker": "KX-E", "side": "yes"} for i in range(10)
    ]
    with pytest.raises(SafetyError) as exc:
        _check_against_collection({"size_min": 2, "size_max": 9}, many)
    assert "at most 9" in str(exc.value)


def test_check_against_collection_enforces_all_yes():
    with pytest.raises(SafetyError) as exc:
        _check_against_collection({"is_all_yes": True}, _GOOD_LEGS)
    assert "YES-only" in str(exc.value)
    assert "KXB-E1-Y" in str(exc.value)  # names the offending leg


def test_check_against_collection_skips_unreadable_rules():
    """Anything we can't read is skipped, not guessed — Kalshi stays
    authoritative on validity."""
    _check_against_collection({}, _GOOD_LEGS)
    _check_against_collection({"size_min": None, "size_max": "nine"}, _GOOD_LEGS)
    _check_against_collection({"is_all_yes": False}, _GOOD_LEGS)


# ── kalshi_get_combo_legs (wired) ──────────────────────────────────────────

_COMBO = {
    "ticker": "KXMVECROSS-S1-ABC",
    "event_ticker": "KXMVECROSS-S1",
    "title": "yes Boston,no Milwaukee wins by over 1.5 runs",
    "mve_collection_ticker": "KXMVECROSS-R",
    "strike_type": "custom",
    "mve_selected_legs": [
        {
            "event_ticker": "KXMLBGAME-26AUG141840BOSPIT",
            "market_ticker": "KXMLBGAME-26AUG141840BOSPIT-BOS",
            "side": "yes",
        },
        {
            "event_ticker": "KXMLBSPREAD-26AUG132210MILLAD",
            "market_ticker": "KXMLBSPREAD-26AUG132210MILLAD-MIL2",
            "side": "no",
        },
    ],
}

_LEG_TITLES = {
    "KXMLBGAME-26AUG141840BOSPIT-BOS": "Will Boston beat Pittsburgh?",
    "KXMLBSPREAD-26AUG132210MILLAD-MIL2": "Will Milwaukee win by over 1.5 runs?",
}


def _combo_handler(market: dict, *, titles_status: int = 200):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path.endswith("/markets") and request.url.params.get("tickers"):
            if titles_status != 200:
                return httpx.Response(titles_status, json={"error": {"message": "nope"}})
            requested = request.url.params["tickers"].split(",")
            return httpx.Response(
                200,
                json={
                    "markets": [
                        {"ticker": t, "title": _LEG_TITLES[t], "yes_sub_title": f"{t} yes"}
                        for t in requested
                        if t in _LEG_TITLES
                    ]
                },
            )
        if "/multivariate_event_collections/" in path:
            return httpx.Response(
                200, json={"multivariate_contract": {"size_min": 2, "size_max": 9}}
            )
        return httpx.Response(200, json={"market": market})

    return handler, calls


@pytest.mark.asyncio
async def test_combo_legs_resolves_structured_legs_with_titles(rsa_private_key):
    handler, calls = _combo_handler(_COMBO)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    out = await fn(ticker="KXMVECROSS-S1-ABC")

    assert out["resolvable"] is True
    assert out["source"] == "mve_selected_legs"
    assert out["leg_count"] == 2
    assert out["titles_resolved"] is True
    assert out["collection_ticker"] == "KXMVECROSS-R"
    assert out["legs"][0] == {
        "market_ticker": "KXMLBGAME-26AUG141840BOSPIT-BOS",
        "event_ticker": "KXMLBGAME-26AUG141840BOSPIT",
        "side": "yes",
        "title": "Will Boston beat Pittsburgh?",
        "yes_sub_title": "KXMLBGAME-26AUG141840BOSPIT-BOS yes",
    }
    assert out["legs"][1]["side"] == "no"
    # Exactly two reads: the combo, then ONE batched title lookup for all legs.
    assert len(calls) == 2
    assert calls[1].url.params["tickers"].count(",") == 1


@pytest.mark.asyncio
async def test_combo_legs_output_round_trips_into_create(rsa_private_key):
    """The `legs` list is documented as feeding straight back into
    kalshi_create_combo_market — so it must survive that validator."""
    handler, _ = _combo_handler(_COMBO)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    out = await fn(ticker="KXMVECROSS-S1-ABC", with_titles=False)

    normalized = _normalize_leg_specs(out["legs"])
    assert [leg["market_ticker"] for leg in normalized] == [
        "KXMLBGAME-26AUG141840BOSPIT-BOS",
        "KXMLBSPREAD-26AUG132210MILLAD-MIL2",
    ]
    assert [leg["side"] for leg in normalized] == ["yes", "no"]


@pytest.mark.asyncio
async def test_combo_legs_skips_title_lookup_when_not_wanted(rsa_private_key):
    handler, calls = _combo_handler(_COMBO)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    out = await fn(ticker="KXMVECROSS-S1-ABC", with_titles=False)

    assert len(calls) == 1
    assert out["titles_resolved"] is False
    assert "title" not in out["legs"][0]


@pytest.mark.asyncio
async def test_combo_legs_title_lookup_fails_open(rsa_private_key):
    """A failed title lookup must not lose the legs — titles are a
    convenience, the legs are the answer."""
    handler, _ = _combo_handler(_COMBO, titles_status=500)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    out = await fn(ticker="KXMVECROSS-S1-ABC")

    assert out["resolvable"] is True
    assert out["leg_count"] == 2
    assert out["titles_resolved"] is False
    assert out["legs"][0]["side"] == "yes"


@pytest.mark.asyncio
async def test_combo_legs_include_collection_attaches_context(rsa_private_key):
    handler, calls = _combo_handler(_COMBO)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    out = await fn(ticker="KXMVECROSS-S1-ABC", with_titles=False, include_collection=True)

    assert out["collection"] == {"size_min": 2, "size_max": 9}
    assert any("multivariate_event_collections" in c.url.path for c in calls)


@pytest.mark.asyncio
async def test_combo_legs_unresolvable_combo_returns_structured_error(rsa_private_key):
    """A real combo with no published leg breakdown must say so — and must
    NOT fall back to splitting the title string."""
    opaque = {
        "ticker": "KXMVEOPAQUE-S1-XYZ",
        "event_ticker": "KXMVEOPAQUE-S1",
        "title": "yes Boston,no Milwaukee",
        "mve_collection_ticker": "KXMVEOPAQUE-R",
    }
    handler, _ = _combo_handler(opaque)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    out = await fn(ticker="KXMVEOPAQUE-S1-XYZ")

    assert out["resolvable"] is False
    assert out["reason"] == "legs_not_published"
    assert "legs" not in out
    assert out["collection_ticker"] == "KXMVEOPAQUE-R"
    assert "will not parse it" in out["message"]


@pytest.mark.asyncio
async def test_combo_legs_on_a_plain_market_says_not_a_combo(rsa_private_key):
    plain = {
        "ticker": "KXFED-26MAR19-B5.25",
        "event_ticker": "KXFED-26MAR19",
        "title": "Fed funds above 5.25%",
    }
    handler, _ = _combo_handler(plain)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    out = await fn(ticker="KXFED-26MAR19-B5.25")

    assert out["resolvable"] is False
    assert out["reason"] == "not_a_combo_market"
    assert "kalshi_get_market" in out["message"]


@pytest.mark.asyncio
async def test_combo_legs_falls_back_to_custom_strike(rsa_private_key):
    csv_only = {
        "ticker": "KXMVECSV-S1-ABC",
        "event_ticker": "KXMVECSV-S1",
        "mve_collection_ticker": "KXMVECSV-R",
        "custom_strike": {
            "Associated Markets": "KXMLBGAME-26AUG141840BOSPIT-BOS",
            "Associated Market Sides": "yes",
            "Associated Events": "KXMLBGAME-26AUG141840BOSPIT",
        },
    }
    handler, _ = _combo_handler(csv_only)
    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    out = await fn(ticker="KXMVECSV-S1-ABC", with_titles=False)

    assert out["resolvable"] is True
    assert out["source"] == "custom_strike"
    assert out["legs"] == [
        {
            "market_ticker": "KXMLBGAME-26AUG141840BOSPIT-BOS",
            "event_ticker": "KXMLBGAME-26AUG141840BOSPIT",
            "side": "yes",
        }
    ]


@pytest.mark.asyncio
async def test_combo_legs_rejects_blank_ticker_before_any_call(rsa_private_key):
    calls: list[str] = []
    server = _make_server(
        rsa_private_key,
        lambda r: (calls.append(r.url.path), httpx.Response(200, json={}))[1],
    )
    fn = await _tool_fn(server, "kalshi_get_combo_legs")

    with pytest.raises(KalshiAPIError):
        await fn(ticker="  ")
    assert calls == []


# ── collection / event discovery (wired) ───────────────────────────────────


@pytest.mark.asyncio
async def test_get_combo_collections_passes_filters(rsa_private_key):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"multivariate_contracts": [], "cursor": ""})

    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_collections")

    await fn(status="open", series_ticker="KXMVECROSSCATEGORY", limit=5)

    assert seen[0].url.path.endswith("/multivariate_event_collections")
    assert seen[0].url.params["status"] == "open"
    assert seen[0].url.params["series_ticker"] == "KXMVECROSSCATEGORY"
    assert seen[0].url.params["limit"] == "5"


@pytest.mark.asyncio
async def test_get_combo_events_rejects_mutually_exclusive_filters(rsa_private_key):
    """Kalshi rejects collection_ticker + series_ticker together; catch it
    locally so the caller gets a message instead of an opaque 400."""
    calls: list[str] = []
    server = _make_server(
        rsa_private_key,
        lambda r: (calls.append(r.url.path), httpx.Response(200, json={}))[1],
    )
    fn = await _tool_fn(server, "kalshi_get_combo_events")

    with pytest.raises(KalshiAPIError) as exc:
        await fn(collection_ticker="KX-R", series_ticker="KX")
    assert "not both" in exc.value.message
    assert calls == []


@pytest.mark.asyncio
async def test_get_combo_events_projects_nested_markets_minimally(rsa_private_key):
    """Combo markets are the largest objects Kalshi returns, so this tool
    defaults `minimal=True` — the nested bulk must actually be stripped."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "events": [
                    {
                        "event_ticker": "KXMVE-S1",
                        "markets": [
                            {
                                "ticker": "KXMVE-S1-A",
                                "yes_bid_dollars": "0.10",
                                "custom_strike": {"Associated Markets": "a,b,c"},
                                "mve_selected_legs": [{"market_ticker": "x"}],
                                "rules_secondary": "long text",
                            }
                        ],
                    }
                ],
                "cursor": "",
            },
        )

    server = _make_server(rsa_private_key, handler)
    fn = await _tool_fn(server, "kalshi_get_combo_events")

    out = await fn(with_nested_markets=True)

    market = out["events"][0]["markets"][0]
    assert market["ticker"] == "KXMVE-S1-A"
    for dropped in ("custom_strike", "mve_selected_legs", "rules_secondary"):
        assert dropped not in market


# ── kalshi_create_combo_market (the write surface) ─────────────────────────


def _create_handler(*, collection: dict | None = None, create_status: int = 200):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            if create_status != 200:
                return httpx.Response(create_status, json={"error": {"message": "rejected"}})
            return httpx.Response(
                200,
                json={"event_ticker": "KXMVE-S1", "market_ticker": "KXMVE-S1-NEW"},
            )
        if collection is None:
            return httpx.Response(404, json={"error": {"message": "no collection"}})
        return httpx.Response(200, json={"multivariate_contract": collection})

    return handler, calls


@pytest.mark.asyncio
async def test_create_combo_market_not_registered_when_disabled(rsa_private_key):
    """Fail closed AND stay invisible: a read-only deploy must not even
    advertise the capability to the model."""
    handler, _ = _create_handler()
    server = _make_server(rsa_private_key, handler)  # combo_creation_enabled defaults False
    assert await server.get_tool("kalshi_create_combo_market") is None
    # The read tools are still there.
    assert await server.get_tool("kalshi_get_combo_legs") is not None


@pytest.mark.asyncio
async def test_create_combo_market_happy_path(rsa_private_key):
    handler, calls = _create_handler(collection={"size_min": 2, "size_max": 9})
    server = _make_server(rsa_private_key, handler, combo_creation_enabled=True)
    fn = await _tool_fn(server, "kalshi_create_combo_market")

    out = await fn(collection_ticker="KXMVE-R", legs=_GOOD_LEGS)

    assert out["market_ticker"] == "KXMVE-S1-NEW"
    assert out["collection_ticker"] == "KXMVE-R"
    assert out["legs_submitted"] == _GOOD_LEGS
    assert out["created_today"] == 1
    assert out["max_per_day"] == 100
    post = next(c for c in calls if c.method == "POST")
    assert post.url.path.endswith("/multivariate_event_collections/KXMVE-R")


@pytest.mark.asyncio
async def test_create_combo_market_preflights_collection_rules(rsa_private_key):
    """The collection's own rules are checked BEFORE the POST, so a violation
    costs no creation quota and returns a message, not an opaque 400."""
    handler, calls = _create_handler(collection={"is_all_yes": True})
    server = _make_server(rsa_private_key, handler, combo_creation_enabled=True)
    fn = await _tool_fn(server, "kalshi_create_combo_market")

    with pytest.raises(SafetyError) as exc:
        await fn(collection_ticker="KXMVE-R", legs=_GOOD_LEGS)

    assert "YES-only" in str(exc.value)
    assert not [c for c in calls if c.method == "POST"]  # never reached the wire


@pytest.mark.asyncio
async def test_create_combo_market_preflight_fails_open(rsa_private_key):
    """If the collection can't be read we proceed and let Kalshi decide —
    a lookup blip must not block a legitimate creation."""
    handler, calls = _create_handler(collection=None)  # collection GET 404s
    server = _make_server(rsa_private_key, handler, combo_creation_enabled=True)
    fn = await _tool_fn(server, "kalshi_create_combo_market")

    out = await fn(collection_ticker="KXMVE-R", legs=_GOOD_LEGS)

    assert out["market_ticker"] == "KXMVE-S1-NEW"
    assert [c for c in calls if c.method == "POST"]


@pytest.mark.asyncio
async def test_create_combo_market_enforces_daily_ceiling(rsa_private_key):
    handler, calls = _create_handler(collection={"size_min": 1, "size_max": 9})
    server = _make_server(
        rsa_private_key, handler, combo_creation_enabled=True, max_combo_creations_per_day=2
    )
    fn = await _tool_fn(server, "kalshi_create_combo_market")

    await fn(collection_ticker="KXMVE-R", legs=_GOOD_LEGS)
    second = await fn(collection_ticker="KXMVE-R", legs=_GOOD_LEGS)
    assert second["created_today"] == 2

    with pytest.raises(SafetyError) as exc:
        await fn(collection_ticker="KXMVE-R", legs=_GOOD_LEGS)
    assert "per-day ceiling of 2" in str(exc.value)
    # The refused call never hit the wire.
    assert len([c for c in calls if c.method == "POST"]) == 2


@pytest.mark.asyncio
async def test_create_combo_market_does_not_bill_a_rejected_creation(rsa_private_key):
    """Kalshi consumed no creation quota on a rejection, so neither may the
    local counter — otherwise a flaky endpoint eats the daily budget."""
    handler, _ = _create_handler(collection={"size_min": 1, "size_max": 9}, create_status=400)
    server = _make_server(rsa_private_key, handler, combo_creation_enabled=True)
    fn = await _tool_fn(server, "kalshi_create_combo_market")

    with pytest.raises(KalshiAPIError):
        await fn(collection_ticker="KXMVE-R", legs=_GOOD_LEGS)

    assert server._kalshi_safety.combo_creation_view()["combo_creations_today"] == 0


@pytest.mark.asyncio
async def test_create_combo_market_validates_legs_before_any_call(rsa_private_key):
    calls: list[str] = []
    handler, _ = _create_handler(collection={"size_min": 1, "size_max": 9})

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return handler(request)

    server = _make_server(rsa_private_key, counting, combo_creation_enabled=True)
    fn = await _tool_fn(server, "kalshi_create_combo_market")

    with pytest.raises(SafetyError):
        await fn(collection_ticker="KXMVE-R", legs=[{"market_ticker": "M", "side": "yes"}])
    assert calls == []


@pytest.mark.asyncio
async def test_create_combo_market_is_independent_of_trading_enabled(rsa_private_key):
    """The whole point of the separate gate: creation works with trading OFF,
    because materializing a ticker commits no money."""
    handler, _ = _create_handler(collection={"size_min": 1, "size_max": 9})
    server = _make_server(
        rsa_private_key, handler, combo_creation_enabled=True, trading_enabled=False
    )
    fn = await _tool_fn(server, "kalshi_create_combo_market")

    out = await fn(collection_ticker="KXMVE-R", legs=_GOOD_LEGS)
    assert out["market_ticker"] == "KXMVE-S1-NEW"


def test_combo_creation_gate_raises_its_own_error_type():
    """Distinct from TradingDisabledError so a caller can tell the two gates
    apart — and so flipping one is never mistaken for flipping the other."""
    safety = SafetyController(_make_config(combo_creation_enabled=False))
    with pytest.raises(ComboCreationDisabledError) as exc:
        safety.check_combo_creation()
    assert "MCP_ALLOW_COMBO_CREATION" in str(exc.value)
    assert "KALSHI_TRADING_ENABLED" in str(exc.value)  # explains the distinction
