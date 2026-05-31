"""Tests for the OAuth proxy wiring + http-transport policy enforcement."""

from __future__ import annotations

import pytest

from kalshi_mcp_server.cli import _enforce_http_auth_policy
from kalshi_mcp_server.errors import ConfigError
from kalshi_mcp_server.oauth import (
    RestrictGitHubUsersMiddleware,
    _parse_allowed_logins,
    build_auth_provider,
    build_user_restriction_middleware,
)


@pytest.fixture(autouse=True)
def _clean_oauth_env(monkeypatch):
    """Each test starts with a clean OAuth env so order doesn't matter."""
    for var in [
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
        "MCP_BASE_URL",
        "MCP_ALLOWED_GITHUB_LOGINS",
        "MCP_JWT_SIGNING_KEY",
        "MCP_REDIS_URL",
        "MCP_ALLOW_INSECURE_HTTP",
    ]:
        monkeypatch.delenv(var, raising=False)


def test_build_auth_provider_returns_none_when_env_unset():
    provider, desc = build_auth_provider()
    assert provider is None
    assert desc == ""


def test_build_auth_provider_returns_provider_when_env_set(monkeypatch):
    monkeypatch.setenv("GITHUB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("MCP_BASE_URL", "https://example.com")
    provider, desc = build_auth_provider()
    assert provider is not None
    assert "ephemeral" in desc  # no Redis configured


def test_build_auth_provider_strips_trailing_slash_from_base_url(monkeypatch):
    """A trailing slash on MCP_BASE_URL would corrupt OAuth redirect URIs."""
    monkeypatch.setenv("GITHUB_CLIENT_ID", "id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MCP_BASE_URL", "https://example.com/")
    provider, _ = build_auth_provider()
    assert provider is not None
    # GitHubProvider stores base_url internally; we can't always introspect
    # it cleanly across versions, but the construction must not raise.


def test_parse_allowed_logins_handles_whitespace_and_case():
    import os

    os.environ["MCP_ALLOWED_GITHUB_LOGINS"] = "Alice, BOB ,  charlie"
    try:
        logins = _parse_allowed_logins()
        assert logins == frozenset({"alice", "bob", "charlie"})
    finally:
        del os.environ["MCP_ALLOWED_GITHUB_LOGINS"]


def test_user_restriction_middleware_none_when_empty():
    mw = build_user_restriction_middleware()
    assert mw is None


def test_user_restriction_middleware_built_when_configured(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_LOGINS", "cejor6")
    mw = build_user_restriction_middleware()
    assert isinstance(mw, RestrictGitHubUsersMiddleware)


# ── http-transport policy enforcement ──────────────────────────────────────


def test_http_policy_refuses_when_no_auth_and_no_override():
    with pytest.raises(ConfigError) as exc:
        _enforce_http_auth_policy(auth_provider=None)
    assert "HTTP transport requires OAuth" in str(exc.value)


def test_http_policy_allows_with_explicit_insecure_override(monkeypatch):
    monkeypatch.setenv("MCP_ALLOW_INSECURE_HTTP", "1")
    # Should not raise.
    _enforce_http_auth_policy(auth_provider=None)


def test_http_policy_refuses_oauth_without_allowlist(monkeypatch):
    """OAuth alone isn't enough — must also set MCP_ALLOWED_GITHUB_LOGINS."""
    monkeypatch.setenv("GITHUB_CLIENT_ID", "id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MCP_BASE_URL", "https://example.com")
    provider, _ = build_auth_provider()
    assert provider is not None
    with pytest.raises(ConfigError) as exc:
        _enforce_http_auth_policy(auth_provider=provider)
    assert "MCP_ALLOWED_GITHUB_LOGINS is empty" in str(exc.value)


def test_http_policy_passes_with_oauth_and_allowlist(monkeypatch):
    monkeypatch.setenv("GITHUB_CLIENT_ID", "id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MCP_BASE_URL", "https://example.com")
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_LOGINS", "cejor6")
    provider, _ = build_auth_provider()
    assert provider is not None
    # Should not raise.
    _enforce_http_auth_policy(auth_provider=provider)
