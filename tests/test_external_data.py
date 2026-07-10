"""Tests for the external-data fetch tool (allowlist, https-only, caps,
redirect handling). Uses httpx.MockTransport — never hits real hosts.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from kalshi_mcp_server.errors import KalshiAPIError
from kalshi_mcp_server.tools.external_data import (
    _MAX_BYTES_CEILING,
    ALLOWED_HOSTS,
    _fetch_external,
    _validate_external_url,
)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# --- _validate_external_url ------------------------------------------------


def test_validate_accepts_allowlisted_https():
    url = "https://api.weather.gov/stations/KNYC/observations/latest"
    assert _validate_external_url(url) == url


def test_validate_strips_whitespace():
    assert _validate_external_url("  https://api.weather.gov/x ") == "https://api.weather.gov/x"


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


def test_validate_rejects_empty_and_non_string():
    with pytest.raises(KalshiAPIError):
        _validate_external_url("")
    with pytest.raises(KalshiAPIError):
        _validate_external_url(None)  # type: ignore[arg-type]


def test_allowlist_is_lowercase_and_frozen():
    assert all(h == h.lower() for h in ALLOWED_HOSTS)
    assert isinstance(ALLOWED_HOSTS, frozenset)


# --- _fetch_external -------------------------------------------------------


def test_fetch_happy_path_returns_body_and_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "kalshi-mcp-server" in request.headers["user-agent"]
        return httpx.Response(
            200, text='{"ok": true}', headers={"content-type": "application/json"}
        )

    result = asyncio.run(
        _fetch_external(
            "https://gamma-api.polymarket.com/markets?limit=1",
            max_bytes=10_000,
            transport=_transport(handler),
        )
    )
    assert result["status"] == 200
    assert result["body"] == '{"ok": true}'
    assert result["truncated"] is False
    assert result["content_type"] == "application/json"
    assert result["bytes_returned"] == len('{"ok": true}')


def test_fetch_truncates_at_max_bytes():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 50_000)

    result = asyncio.run(
        _fetch_external(
            "https://api.open-meteo.com/v1/forecast",
            max_bytes=2_000,
            transport=_transport(handler),
        )
    )
    assert result["truncated"] is True
    assert result["bytes_returned"] == 2_000
    assert len(result["body"]) == 2_000


def test_fetch_max_bytes_clamped_to_floor_and_ceiling():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="y" * (_MAX_BYTES_CEILING + 10_000))

    # Below-floor request is raised to the 1k floor, not honored at 1 byte.
    result = asyncio.run(
        _fetch_external("https://api.weather.gov/x", max_bytes=1, transport=_transport(handler))
    )
    assert result["bytes_returned"] == 1_000

    # Above-ceiling request is capped at the ceiling.
    result = asyncio.run(
        _fetch_external(
            "https://api.weather.gov/x",
            max_bytes=10_000_000,
            transport=_transport(handler),
        )
    )
    assert result["bytes_returned"] == _MAX_BYTES_CEILING


def test_fetch_does_not_follow_redirects():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/steal"})

    result = asyncio.run(
        _fetch_external(
            "https://www.tennisabstract.com/reports/atp_elo_ratings.html",
            max_bytes=10_000,
            transport=_transport(handler),
        )
    )
    assert result["status"] == 302
    assert result["redirect_location"] == "https://evil.example.com/steal"
    assert result["body"] == ""
    # The redirect target was NOT fetched (handler would have been called
    # again with the evil host and failed the assertion below).


def test_fetch_upstream_error_status_passes_through():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream sad")

    result = asyncio.run(
        _fetch_external(
            "https://ensemble-api.open-meteo.com/v1/ensemble",
            max_bytes=10_000,
            transport=_transport(handler),
        )
    )
    assert result["status"] == 503
    assert result["body"] == "upstream sad"


def test_fetch_rejects_unlisted_host_before_any_request():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal called
        called = True
        return httpx.Response(200)

    with pytest.raises(KalshiAPIError, match="allowlist"):
        asyncio.run(
            _fetch_external(
                "https://internal-metadata.example/latest",
                max_bytes=10_000,
                transport=_transport(handler),
            )
        )
    assert called is False


def test_fetch_decodes_non_utf8_with_replacement():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok\xff\xfebytes")

    result = asyncio.run(
        _fetch_external(
            "https://raw.githubusercontent.com/some/vetted/file.csv",
            max_bytes=10_000,
            transport=_transport(handler),
        )
    )
    assert result["status"] == 200
    assert "ok" in result["body"] and "�" in result["body"]
