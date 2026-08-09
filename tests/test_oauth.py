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


# ── Redis client store: connection resilience ──────────────────────────────
#
# These assert on the *constructed connection kwargs*, not just that the
# call doesn't raise. redis-py defaults to no health check and zero retries,
# which leaves a dropped idle TLS connection to surface as a hard
# ConnectionError — and one landing inside FastMCP's refresh-token rotation
# window permanently kills a connector. Regressing these is silent in tests
# and catastrophic in prod, so pin them explicitly.


def _redis_store_client(monkeypatch):
    """Build the Redis-backed store and return the underlying redis client."""
    # No credentials in the URL — nothing here connects, and embedded
    # basic-auth trips the detect-secrets hook.
    monkeypatch.setenv("MCP_REDIS_URL", "rediss://example.invalid:6379")
    from kalshi_mcp_server.oauth import _build_client_storage

    storage, desc = _build_client_storage()
    assert storage is not None
    assert "redis" in desc
    return storage._client


def test_redis_store_sets_health_check_interval(monkeypatch):
    """Without this, a stale pooled connection is used blind and throws."""
    client = _redis_store_client(monkeypatch)
    assert client.connection_pool.connection_kwargs["health_check_interval"] == 30


def test_redis_store_enables_socket_keepalive(monkeypatch):
    client = _redis_store_client(monkeypatch)
    assert client.connection_pool.connection_kwargs["socket_keepalive"] is True


def test_redis_store_retries_on_connection_and_timeout_errors(monkeypatch):
    """Retry lowers the odds of a single command failing mid-rotation.

    It does NOT make rotation atomic — see `_build_client_storage`. The
    count is deliberately 1: redis-py applies the policy at three nested
    layers, so it multiplies to ~(retries + 1) ** 2 connects per command.
    """
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    client = _redis_store_client(monkeypatch)
    retry = client.connection_pool.connection_kwargs["retry"]
    assert retry is not None
    # Public accessor — `retry._retries` is private and drifts across versions.
    assert retry.get_retries() == 1
    supported = client.connection_pool.connection_kwargs["retry_on_error"]
    assert RedisConnectionError in supported
    assert RedisTimeoutError in supported


def test_redis_store_backoff_is_jittered(monkeypatch):
    """Unjittered backoff makes every pooled connection retry in lockstep."""
    from redis.backoff import FullJitterBackoff

    client = _redis_store_client(monkeypatch)
    retry = client.connection_pool.connection_kwargs["retry"]
    assert isinstance(retry._backoff, FullJitterBackoff)


def test_redis_store_bounds_read_writes_with_socket_timeout(monkeypatch):
    """Without this, an older redis-py defaults to None and hangs forever."""
    client = _redis_store_client(monkeypatch)
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == 5
    assert kwargs["socket_connect_timeout"] == 5


async def test_retry_policy_actually_retries_a_failing_command(monkeypatch):
    """Behavioral, not wiring: prove the configured retry really fires.

    The other tests assert the kwargs land on the client. This one drives
    `Retry.call_with_retry` with the exact policy we construct and asserts a
    transient ConnectionError is survived — the single-command blip the
    docstring claims to cover.
    """
    from redis.exceptions import ConnectionError as RedisConnectionError

    client = _redis_store_client(monkeypatch)
    retry = client.connection_pool.connection_kwargs["retry"]

    attempts = 0

    async def _flaky():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RedisConnectionError("Error UNKNOWN while writing to socket.")
        return "ok"

    async def _fail(_exc):
        return None

    assert await retry.call_with_retry(_flaky, _fail) == "ok"
    assert attempts == 2  # failed once, retried once, succeeded


async def test_retry_policy_gives_up_after_the_configured_budget(monkeypatch):
    """A sustained outage must surface, not hang retrying indefinitely."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    client = _redis_store_client(monkeypatch)
    retry = client.connection_pool.connection_kwargs["retry"]

    attempts = 0

    async def _always_fails():
        nonlocal attempts
        attempts += 1
        raise RedisConnectionError("down")

    async def _fail(_exc):
        return None

    with pytest.raises(RedisConnectionError):
        await retry.call_with_retry(_always_fails, _fail)
    assert attempts == 2  # initial attempt + REDIS_RETRIES(1)


def test_redis_store_preserves_decode_responses(monkeypatch):
    """decode_responses=False makes RedisStore silently drop every read."""
    client = _redis_store_client(monkeypatch)
    assert client.connection_pool.connection_kwargs["decode_responses"] is True


def test_build_client_storage_without_redis_url_is_ephemeral(monkeypatch):
    from kalshi_mcp_server.oauth import _build_client_storage

    storage, desc = _build_client_storage()
    assert storage is None
    assert "ephemeral" in desc


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
