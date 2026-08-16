"""Tests for the external-data fetch tool (allowlist, https-only, caps,
redirect handling, transport-error wrapping). Uses httpx.MockTransport —
never hits real hosts. Async tests per the repo convention
(asyncio_mode = "auto").
"""

from __future__ import annotations

import httpx
import pytest

from kalshi_mcp_server.errors import KalshiAPIError
from kalshi_mcp_server.tools.external_data import (
    _MAX_BYTES_CEILING,
    _UNTRUSTED_CLOSE,
    _UNTRUSTED_OPEN,
    ALLOWED_HOSTS,
    _fetch_external,
    _validate_external_url,
)
from kalshi_mcp_server.tools.external_data import (
    register as register_external_data,
)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _unwrap(body: str) -> str:
    """Strip the untrusted-content delimiters from a returned body."""
    assert body.startswith(_UNTRUSTED_OPEN + "\n")
    assert body.endswith("\n" + _UNTRUSTED_CLOSE)
    return body[len(_UNTRUSTED_OPEN) + 1 : -(len(_UNTRUSTED_CLOSE) + 1)]


# --- _validate_external_url ------------------------------------------------


def test_validate_accepts_allowlisted_https():
    url = "https://api.weather.gov/stations/KNYC/observations/latest"
    assert _validate_external_url(url) == url


def test_validate_strips_whitespace():
    assert _validate_external_url("  https://api.weather.gov/x ") == "https://api.weather.gov/x"


def test_validate_accepts_trailing_dot_fqdn():
    # DNS-equivalent form must not fail the allowlist (availability).
    url = "https://api.weather.gov./stations/KNYC/observations/latest"
    assert _validate_external_url(url) == url


def test_validate_rejects_http():
    with pytest.raises(KalshiAPIError, match="https"):
        _validate_external_url("http://api.weather.gov/x")


def test_validate_rejects_unlisted_host():
    with pytest.raises(KalshiAPIError, match="allowlist"):
        _validate_external_url("https://evil.example.com/x")


def test_validate_rejects_subdomain_of_allowlisted_host():
    # Exact-host allowlist: a lookalike subdomain must NOT pass.
    with pytest.raises(KalshiAPIError, match="allowlist"):
        _validate_external_url("https://api.weather.gov.evil.example/x")


def test_validate_rejects_userinfo_trick():
    with pytest.raises(KalshiAPIError):
        _validate_external_url("https://api.weather.gov@evil.example/x")


def test_validate_rejects_nonstandard_port():
    with pytest.raises(KalshiAPIError, match="port"):
        _validate_external_url("https://api.weather.gov:8443/x")


def test_validate_wraps_malformed_ipv6_url():
    # urlsplit raises ValueError on these — must surface as KalshiAPIError,
    # never a raw ValueError (QA CRITICAL).
    with pytest.raises(KalshiAPIError, match="unparseable"):
        _validate_external_url("https://[::1/x")
    with pytest.raises(KalshiAPIError, match="unparseable"):
        _validate_external_url("https://[")


def test_validate_wraps_junk_port():
    # parts.port raises ValueError for non-numeric / out-of-range ports.
    with pytest.raises(KalshiAPIError, match="unparseable"):
        _validate_external_url("https://api.weather.gov:abc/x")
    with pytest.raises(KalshiAPIError, match="unparseable"):
        _validate_external_url("https://api.weather.gov:99999/x")


def test_validate_rejects_empty_and_non_string():
    with pytest.raises(KalshiAPIError):
        _validate_external_url("")
    with pytest.raises(KalshiAPIError):
        _validate_external_url(None)  # type: ignore[arg-type]


def test_allowlist_is_lowercase_and_frozen():
    assert all(h == h.lower() for h in ALLOWED_HOSTS)
    assert isinstance(ALLOWED_HOSTS, frozenset)


def test_tool_docstring_enumerates_exact_allowlist():
    """Guard the allowlist-vs-docstring drift the review flagged: every
    allowed host must appear in the tool's docstring (the LLM's only view
    of the allowlist)."""

    class _FakeServer:
        def __init__(self):
            self.fns = []

        def tool(self, fn):
            self.fns.append(fn)
            return fn

    server = _FakeServer()
    register_external_data(server)  # type: ignore[arg-type]
    (tool_fn,) = server.fns
    doc = tool_fn.__doc__ or ""
    for host in ALLOWED_HOSTS:
        assert host in doc, f"allowlisted host {host} missing from tool docstring"


# --- _fetch_external -------------------------------------------------------


async def test_fetch_happy_path_returns_wrapped_body_and_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "kalshi-mcp-server" in request.headers["user-agent"]
        return httpx.Response(
            200, text='{"ok": true}', headers={"content-type": "application/json"}
        )

    result = await _fetch_external(
        "https://gamma-api.polymarket.com/markets?limit=1",
        max_bytes=10_000,
        transport=_transport(handler),
    )
    assert result["status"] == 200
    assert _unwrap(result["body"]) == '{"ok": true}'
    assert result["truncated"] is False
    assert result["content_type"] == "application/json"
    assert result["bytes_returned"] == len('{"ok": true}')
    assert result["redirect_location"] == ""


async def test_fetch_truncates_at_max_bytes():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 50_000)

    result = await _fetch_external(
        "https://api.open-meteo.com/v1/forecast",
        max_bytes=2_000,
        transport=_transport(handler),
    )
    assert result["truncated"] is True
    # `bytes_returned` is what we read off the wire (capped at max_bytes)...
    assert result["bytes_returned"] == 2_000
    # ...but the DELIVERED result stays within max_bytes, so the body text is
    # trimmed below 2000 to leave room for the wrapper + metadata (issue #82).
    import json as _json

    assert len(_json.dumps(result)) <= 2_000
    assert len(_unwrap(result["body"])) < 2_000


async def test_fetch_delivered_size_stays_within_max_bytes():
    """Issue #82: the live failure was max_bytes=50000 delivering 53267 chars
    (wrapper + JSON-escaping overhead) and getting client-rejected. The
    DELIVERED result must now fit the budget."""

    def handler(request: httpx.Request) -> httpx.Response:
        # A page full of characters that JSON-escaping inflates (quotes,
        # backslashes, newlines) — the worst case for delivered-size overhead.
        return httpx.Response(200, text=('"\\\n' * 20_000))

    result = await _fetch_external(
        "https://api.open-meteo.com/v1/forecast",
        max_bytes=50_000,
        transport=_transport(handler),
    )
    import json as _json

    assert len(_json.dumps(result)) <= 50_000
    assert result["truncated"] is True


async def test_fetch_small_body_not_trimmed():
    """A body comfortably under the budget (even with wrapper + escaping) comes
    back whole and untruncated."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="z" * 500)

    result = await _fetch_external(
        "https://api.open-meteo.com/v1/forecast",
        max_bytes=5_000,
        transport=_transport(handler),
    )
    assert result["bytes_returned"] == 500
    assert result["truncated"] is False
    assert _unwrap(result["body"]) == "z" * 500


async def test_fetch_max_bytes_clamped_to_floor_and_ceiling():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="y" * (_MAX_BYTES_CEILING + 10_000))

    # Below-floor request is raised to the 1k floor, not honored at 1 byte.
    result = await _fetch_external(
        "https://api.weather.gov/x", max_bytes=1, transport=_transport(handler)
    )
    assert result["bytes_returned"] == 1_000

    # Above-ceiling request is capped at the ceiling.
    result = await _fetch_external(
        "https://api.weather.gov/x",
        max_bytes=10_000_000,
        transport=_transport(handler),
    )
    assert result["bytes_returned"] == _MAX_BYTES_CEILING


async def test_fetch_does_not_follow_redirects():
    def handler(request: httpx.Request) -> httpx.Response:
        # If the redirect were followed, this handler would be invoked a
        # second time for evil.example.com; assert single-shot below.
        handler.calls = getattr(handler, "calls", 0) + 1
        return httpx.Response(302, headers={"location": "https://evil.example.com/steal"})

    result = await _fetch_external(
        "https://www.tennisabstract.com/reports/atp_elo_ratings.html",
        max_bytes=10_000,
        transport=_transport(handler),
    )
    assert result["status"] == 302
    assert result["redirect_location"] == "https://evil.example.com/steal"
    assert result["body"] == ""
    assert handler.calls == 1


async def test_fetch_304_is_not_treated_as_redirect():
    """304 is 3xx but carries no Location — it must fall through to the
    body branch, not return a confusing redirect shape (QA bug)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    result = await _fetch_external(
        "https://www.deribit.com/api/v2/public/get_index_price",
        max_bytes=10_000,
        transport=_transport(handler),
    )
    assert result["status"] == 304
    assert result["redirect_location"] == ""
    assert _unwrap(result["body"]) == ""


async def test_fetch_upstream_error_status_passes_through():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream sad")

    result = await _fetch_external(
        "https://ensemble-api.open-meteo.com/v1/ensemble",
        max_bytes=10_000,
        transport=_transport(handler),
    )
    assert result["status"] == 503
    assert _unwrap(result["body"]) == "upstream sad"


async def test_fetch_wraps_transport_errors_as_kalshi_api_error():
    """DNS failure / connection refusal / timeout must surface as the
    project's structured error, never a raw httpx exception (backend BUG)."""

    def raise_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(KalshiAPIError, match="external fetch failed"):
        await _fetch_external(
            "https://api.weather.gov/x",
            max_bytes=10_000,
            transport=_transport(raise_connect),
        )

    def raise_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(KalshiAPIError, match="timed out"):
        await _fetch_external(
            "https://api.weather.gov/x",
            max_bytes=10_000,
            transport=_transport(raise_timeout),
        )


async def test_fetch_rejects_unlisted_host_before_any_request():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal called
        called = True
        return httpx.Response(200)

    with pytest.raises(KalshiAPIError, match="allowlist"):
        await _fetch_external(
            "https://internal-metadata.example/latest",
            max_bytes=10_000,
            transport=_transport(handler),
        )
    assert called is False


async def test_fetch_decodes_non_utf8_with_replacement():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok\xff\xfebytes")

    result = await _fetch_external(
        "https://clob.polymarket.com/markets?limit=1",
        max_bytes=10_000,
        transport=_transport(handler),
    )
    assert result["status"] == 200
    inner = _unwrap(result["body"])
    assert "ok" in inner and "�" in inner


async def test_fetch_response_shape_is_uniform_across_branches():
    """Both redirect and body branches must return the same key set."""

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://api.weather.gov/y"})

    def body_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="hi")

    r1 = await _fetch_external(
        "https://api.weather.gov/x", max_bytes=10_000, transport=_transport(redirect_handler)
    )
    r2 = await _fetch_external(
        "https://api.weather.gov/x", max_bytes=10_000, transport=_transport(body_handler)
    )
    assert set(r1.keys()) == set(r2.keys())
