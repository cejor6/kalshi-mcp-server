"""Tests for the session-auth health view in kalshi_get_environment (issue #80)."""

from __future__ import annotations

import time
from types import SimpleNamespace

from kalshi_mcp_server.tools import exchange


def test_session_auth_view_empty_without_a_token(monkeypatch):
    """On stdio / outside a request context there's no token — empty dict, no
    crash. This is the default in every stdio test and deploy."""

    # Simulate get_access_token() raising (no request context / no oauth).
    def _raise():
        raise RuntimeError("no request context")

    monkeypatch.setattr("fastmcp.server.dependencies.get_access_token", _raise, raising=False)
    assert exchange._session_auth_view() == {}


def test_session_auth_view_reports_login_and_expiry(monkeypatch):
    exp = int(time.time()) + 3600
    fake_token = SimpleNamespace(claims={"login": "cejor6", "exp": exp})
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_access_token",
        lambda: fake_token,
        raising=False,
    )
    view = exchange._session_auth_view()
    assert view["session_authenticated"] is True
    assert view["session_login"] == "cejor6"
    assert view["session_token_expires_at"] == exp
    # Remaining is ~3600s, always non-negative.
    assert 0 < view["session_token_expires_in_seconds"] <= 3600


def test_session_auth_view_handles_token_without_claims(monkeypatch):
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_access_token",
        lambda: SimpleNamespace(claims=None),
        raising=False,
    )
    # Present token, no claims → authenticated true, no login/exp fields.
    view = exchange._session_auth_view()
    assert view == {"session_authenticated": True}
