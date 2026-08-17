"""Tests for the session-auth health view in kalshi_get_environment (issue #80).

The expiry countdown is recovered from the incoming FastMCP session JWT (the
`Authorization: Bearer` token), NOT from the `AccessToken` claims — under
`GitHubProvider` that AccessToken has `expires_at=None` and no `exp`, which is
exactly why the original claims-based read surfaced nothing on prod. `login`
still comes from the AccessToken; the two sources are intentionally different.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import jwt

from kalshi_mcp_server.tools import exchange


def _bearer(claims: dict) -> dict[str, str]:
    """Mint an `Authorization` header carrying a signed JWT with `claims`.

    Signature doesn't matter — `_session_token_expiry` decodes with
    verify_signature=False (the proxy already validated the token upstream)."""
    # 32-byte key: below that, PyJWT emits InsecureKeyLengthWarning, which the
    # suite's filterwarnings=error would turn into an exception.
    token = jwt.encode(claims, "k" * 32, algorithm="HS256")
    return {"authorization": f"Bearer {token}"}


def test_session_auth_view_empty_without_a_token(monkeypatch):
    """On stdio / outside a request context there's no token — empty dict, no
    crash. This is the default in every stdio test and deploy."""

    def _raise():
        raise RuntimeError("no request context")

    monkeypatch.setattr("fastmcp.server.dependencies.get_access_token", _raise, raising=False)
    assert exchange._session_auth_view() == {}


def test_session_auth_view_reports_login_and_expiry(monkeypatch):
    """Integration: login from the AccessToken, expiry from the bearer JWT."""
    exp = int(time.time()) + 28800  # 8h GitHub-App session token
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_access_token",
        lambda: SimpleNamespace(claims={"login": "cejor6"}),
        raising=False,
    )
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda include=None: _bearer({"exp": exp, "login": "cejor6"}),
        raising=False,
    )
    view = exchange._session_auth_view()
    assert view["session_authenticated"] is True
    assert view["session_login"] == "cejor6"
    assert view["session_token_expires_at"] == exp
    assert 0 < view["session_token_expires_in_seconds"] <= 28800


def test_session_auth_view_authenticated_without_bearer(monkeypatch):
    """Token present (authenticated) but no readable bearer → login only, no
    expiry, no crash. Covers the claims=None shape too."""
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_access_token",
        lambda: SimpleNamespace(claims=None),
        raising=False,
    )
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda include=None: {},
        raising=False,
    )
    assert exchange._session_auth_view() == {"session_authenticated": True}


def test_session_token_expiry_from_bearer_jwt(monkeypatch):
    """The core #80 fix: expiry is decoded from the session JWT, not claims."""
    exp = int(time.time()) + 3600
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda include=None: _bearer({"exp": exp}),
        raising=False,
    )
    out = exchange._session_token_expiry()
    assert out["session_token_expires_at"] == exp
    assert 0 < out["session_token_expires_in_seconds"] <= 3600


def test_session_token_expiry_empty_without_bearer(monkeypatch):
    """No auth header (stdio, or header stripped) → empty, never raises."""
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda include=None: {},
        raising=False,
    )
    assert exchange._session_token_expiry() == {}


def test_session_token_expiry_ignores_non_bearer_scheme(monkeypatch):
    """A non-Bearer Authorization value is ignored, not fed to jwt.decode."""
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda include=None: {"authorization": "Basic Zm9vOmJhcg=="},
        raising=False,
    )
    assert exchange._session_token_expiry() == {}


def test_session_token_expiry_ignores_garbage_bearer(monkeypatch):
    """A non-JWT bearer must fail open (jwt.decode raises → {}), not propagate."""
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda include=None: {"authorization": "Bearer not-a-jwt"},
        raising=False,
    )
    assert exchange._session_token_expiry() == {}


def test_session_token_expiry_missing_exp_claim(monkeypatch):
    """A valid JWT with no `exp` → no expiry fields, no crash."""
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda include=None: _bearer({"login": "cejor6"}),
        raising=False,
    )
    assert exchange._session_token_expiry() == {}


def test_session_token_expiry_rejects_non_finite_exp(monkeypatch):
    """NaN/inf `exp` is rejected BEFORE int() — which would raise and take down
    the whole kalshi_get_environment tool (same fail-closed rule as safety.py).

    JSON/JWT can't natively carry NaN/inf, so we inject it at the decode layer
    to prove the guard rather than the (unreachable) real-token path."""
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda include=None: {"authorization": "Bearer x"},
        raising=False,
    )
    monkeypatch.setattr(jwt, "decode", lambda *a, **k: {"exp": float("inf")})
    assert exchange._session_token_expiry() == {}
    monkeypatch.setattr(jwt, "decode", lambda *a, **k: {"exp": float("nan")})
    assert exchange._session_token_expiry() == {}


def test_session_token_expiry_clamps_past_expiry(monkeypatch):
    """An already-expired token clamps remaining-seconds to 0, never negative."""
    exp = int(time.time()) - 500
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda include=None: _bearer({"exp": exp}),
        raising=False,
    )
    out = exchange._session_token_expiry()
    assert out["session_token_expires_at"] == exp
    assert out["session_token_expires_in_seconds"] == 0


def test_session_auth_view_none_token_is_not_authenticated(monkeypatch):
    """get_access_token() returns None on the real no-auth path (it does NOT
    raise). This must return {} — NOT {"session_authenticated": True}. Guards
    the `if token is None` branch so a false auth-health positive can't slip in
    if that check is ever dropped."""
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_access_token",
        lambda: None,
        raising=False,
    )
    assert exchange._session_auth_view() == {}


def test_session_token_expiry_accepts_lowercase_bearer_scheme(monkeypatch):
    """The scheme match is case-insensitive — a lowercase `bearer` decodes."""
    exp = int(time.time()) + 3600
    token = jwt.encode({"exp": exp}, "k" * 32, algorithm="HS256")
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda include=None: {"authorization": f"bearer {token}"},
        raising=False,
    )
    assert exchange._session_token_expiry()["session_token_expires_at"] == exp


def test_session_token_expiry_rejects_string_exp(monkeypatch):
    """A non-numeric `exp` (e.g. a string epoch) is ignored, not int()-ed."""
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda include=None: {"authorization": "Bearer x"},
        raising=False,
    )
    monkeypatch.setattr(jwt, "decode", lambda *a, **k: {"exp": "1700000000"})
    assert exchange._session_token_expiry() == {}


def test_session_token_expiry_rejects_bool_exp(monkeypatch):
    """`exp` as a bool (int subclass) is excluded by the explicit bool guard —
    otherwise `True` would int() to 1 and surface a nonsense 1970 expiry."""
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda include=None: {"authorization": "Bearer x"},
        raising=False,
    )
    monkeypatch.setattr(jwt, "decode", lambda *a, **k: {"exp": True})
    assert exchange._session_token_expiry() == {}
