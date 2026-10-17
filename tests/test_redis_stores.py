"""Tests for the shared Redis policy (`redis_client.py`) and its two consumers.

Covers:

1. Cleartext `redis://` is refused at startup (ConfigError) unless the host
   is loopback or MCP_ALLOW_PLAINTEXT_REDIS=1 — for BOTH the OAuth store and
   the safety-limit store, since they share one URL.
2. The safety-limit store gets the same hardened client as the OAuth store
   (it used to run on bare `from_url` defaults).
3. The safety-limit key is namespaced by MCP_REDIS_COLLECTION_PREFIX, keeps
   the legacy name for the default prefix, and falls back to the legacy
   record (tighten-only) until a namespaced deployment writes its own — so
   upgrading never silently drops a persisted clamp.

No real Redis: nothing here connects. URLs carry no credentials except where
a test checks the password is NOT echoed, and those use obvious
placeholders.
"""

from __future__ import annotations

import pytest

from kalshi_mcp_server.config import DEMO_REST_BASE, DEMO_WS_URL, Config
from kalshi_mcp_server.errors import ConfigError
from kalshi_mcp_server.redis_client import (
    DEFAULT_REDIS_COLLECTION_PREFIX,
    validate_redis_url,
)
from kalshi_mcp_server.safety import InMemoryLimitsStore, SafetyController
from kalshi_mcp_server.safety_store import (
    _LEGACY_REDIS_KEY,
    RedisLimitsStore,
    build_limits_store,
)


@pytest.fixture(autouse=True)
def _clean_redis_env(monkeypatch):
    for var in [
        "MCP_REDIS_URL",
        "MCP_REDIS_COLLECTION_PREFIX",
        "MCP_ALLOW_PLAINTEXT_REDIS",
        "MCP_JWT_SIGNING_KEY",
    ]:
        monkeypatch.delenv(var, raising=False)


# ── URL scheme policy ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "rediss://example.invalid:6379",
        "REDISS://example.invalid:6379",
        "redis://localhost:6379",
        "redis://LOCALHOST:6379/0",
        "redis://127.0.0.1:6379",
        "redis://127.0.0.2:6379",  # all of 127.0.0.0/8 is loopback
        "redis://[::1]:6379",
        "unix:///var/run/redis.sock",
    ],
)
def test_encrypted_or_local_urls_are_accepted(url):
    validate_redis_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "redis://example.invalid:6379",
        "redis://10.0.0.5:6379",  # private ≠ loopback: still on a network
        "redis://red-abc123:6379",  # Render Key Value internal hostname
        "redis://localhost.example.invalid:6379",  # not actually localhost
        "redis://:6379",  # no host at all
    ],
)
def test_cleartext_to_a_network_host_is_refused(url):
    with pytest.raises(ConfigError, match="MCP_ALLOW_PLAINTEXT_REDIS"):
        validate_redis_url(url)


def test_unknown_scheme_is_refused():
    with pytest.raises(ConfigError, match="unsupported scheme"):
        validate_redis_url("http://example.invalid:6379")


def test_refusal_never_echoes_the_password():
    marker = "placeholder-not-a-real-pw"
    url = (
        "redis://default:placeholder-not-a-real-pw@example.invalid:6379"  # pragma: allowlist secret
    )
    with pytest.raises(ConfigError) as exc_info:
        validate_redis_url(url)
    assert marker not in str(exc_info.value)


@pytest.mark.parametrize("flag", ["1", "true", "YES", " on "])
def test_plaintext_opt_out_allows_private_network_redis(monkeypatch, flag):
    monkeypatch.setenv("MCP_ALLOW_PLAINTEXT_REDIS", flag)
    validate_redis_url("redis://red-abc123:6379")


@pytest.mark.parametrize("flag", ["0", "false", "", "no"])
def test_plaintext_opt_out_requires_a_truthy_value(monkeypatch, flag):
    monkeypatch.setenv("MCP_ALLOW_PLAINTEXT_REDIS", flag)
    with pytest.raises(ConfigError):
        validate_redis_url("redis://red-abc123:6379")


def test_oauth_store_refuses_cleartext_redis(monkeypatch):
    """The DEPLOY.md promise ("the proxy rejects redis://") is now true."""
    from kalshi_mcp_server.oauth import _build_client_storage

    monkeypatch.setenv("MCP_REDIS_URL", "redis://example.invalid:6379")
    monkeypatch.setenv("MCP_JWT_SIGNING_KEY", "test-signing-key-not-a-real-secret")
    with pytest.raises(ConfigError, match="cleartext"):
        _build_client_storage()


def test_oauth_store_accepts_cleartext_with_opt_out(monkeypatch):
    from kalshi_mcp_server.oauth import _build_client_storage

    monkeypatch.setenv("MCP_REDIS_URL", "redis://red-abc123:6379")
    monkeypatch.setenv("MCP_ALLOW_PLAINTEXT_REDIS", "1")
    monkeypatch.setenv("MCP_JWT_SIGNING_KEY", "test-signing-key-not-a-real-secret")
    storage, desc = _build_client_storage()
    assert storage is not None
    assert "redis" in desc


def test_safety_store_refuses_cleartext_redis(monkeypatch):
    """Fatal, not a degrade-to-memory: the URL is shared with OAuth, and a
    silent fallback would hide a misconfig the operator needs to fix."""
    monkeypatch.setenv("MCP_REDIS_URL", "redis://example.invalid:6379")
    with pytest.raises(ConfigError, match="cleartext"):
        build_limits_store()


# ── Safety store: same hardened client as the OAuth store ─────────────────


def _safety_store(monkeypatch, prefix: str | None = None) -> RedisLimitsStore:
    monkeypatch.setenv("MCP_REDIS_URL", "rediss://example.invalid:6379")
    if prefix is not None:
        monkeypatch.setenv("MCP_REDIS_COLLECTION_PREFIX", prefix)
    store, desc = build_limits_store()
    assert isinstance(store, RedisLimitsStore)
    assert "redis" in desc
    return store


def test_safety_store_client_has_resilience_settings(monkeypatch):
    from redis.backoff import FullJitterBackoff
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    kwargs = _safety_store(monkeypatch)._client.connection_pool.connection_kwargs
    assert kwargs["health_check_interval"] == 30
    assert kwargs["socket_keepalive"] is True
    assert kwargs["socket_timeout"] == 2
    assert kwargs["socket_connect_timeout"] == 2
    assert kwargs["decode_responses"] is True
    assert kwargs["retry"].get_retries() == 1
    assert isinstance(kwargs["retry"]._backoff, FullJitterBackoff)
    assert RedisConnectionError in kwargs["retry_on_error"]
    assert RedisTimeoutError in kwargs["retry_on_error"]


def test_safety_store_without_redis_url_is_in_memory():
    store, desc = build_limits_store()
    assert isinstance(store, InMemoryLimitsStore)
    assert "in-memory" in desc


# ── Safety store: key namespacing + legacy fallback ───────────────────────


def test_default_prefix_keeps_the_legacy_key(monkeypatch):
    """Renaming the default key would drop every live clamp on upgrade."""
    assert _safety_store(monkeypatch).key == _LEGACY_REDIS_KEY == "kalshi-mcp:safety-limits"


def test_blank_prefix_means_default(monkeypatch):
    assert _safety_store(monkeypatch, prefix="   ").key == _LEGACY_REDIS_KEY


def test_custom_prefix_namespaces_the_key(monkeypatch):
    assert _safety_store(monkeypatch, prefix="kalshi-demo").key == (
        "kalshi-mcp:kalshi-demo:safety-limits"
    )


class _FakeRedis:
    """Just the three async commands the store uses, over a shared dict."""

    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = data if data is not None else {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)


async def test_default_store_round_trips_and_clears_by_delete():
    redis = _FakeRedis()
    store = RedisLimitsStore(redis)
    await store.save({"max_order_size_usd": 5.0})
    assert await store.load() == {"max_order_size_usd": 5.0}
    await store.clear()
    assert _LEGACY_REDIS_KEY not in redis.data
    assert await store.load() is None


async def test_namespaced_store_falls_back_to_legacy_until_it_writes():
    redis = _FakeRedis({_LEGACY_REDIS_KEY: '{"max_order_size_usd": 5.0}'})
    store = RedisLimitsStore(redis, prefix="kalshi-demo")
    # Pre-upgrade clamp still applies — losing it would be a loosening.
    assert await store.load() == {"max_order_size_usd": 5.0}

    await store.save({"max_order_size_usd": 7.0})
    assert await store.load() == {"max_order_size_usd": 7.0}
    # The legacy key may be another deployment's live record: never touched.
    assert redis.data[_LEGACY_REDIS_KEY] == '{"max_order_size_usd": 5.0}'


async def test_inherited_legacy_clamp_survives_a_later_legacy_reset():
    """Adopt-on-load: once inherited, the clamp is ours. A default-prefix
    deployment resetting (deleting) the shared key must not loosen us."""
    redis = _FakeRedis({_LEGACY_REDIS_KEY: '{"max_order_size_usd": 5.0}'})
    store = RedisLimitsStore(redis, prefix="kalshi-demo")
    assert await store.load() == {"max_order_size_usd": 5.0}
    assert redis.data[store.key] == '{"max_order_size_usd": 5.0}'

    await RedisLimitsStore(redis).clear()  # the other deployment resets
    assert _LEGACY_REDIS_KEY not in redis.data
    assert await store.load() == {"max_order_size_usd": 5.0}


async def test_adoption_never_overwrites_an_existing_own_record():
    redis = _FakeRedis({_LEGACY_REDIS_KEY: '{"max_order_size_usd": 5.0}'})
    store = RedisLimitsStore(redis, prefix="kalshi-demo")
    await store._adopt_legacy({"max_order_size_usd": 5.0})
    redis.data[store.key] = '{"max_order_size_usd": 2.0}'
    await store._adopt_legacy({"max_order_size_usd": 5.0})
    assert redis.data[store.key] == '{"max_order_size_usd": 2.0}'


async def test_corrupt_legacy_record_is_not_adopted():
    redis = _FakeRedis({_LEGACY_REDIS_KEY: "not json"})
    store = RedisLimitsStore(redis, prefix="kalshi-demo")
    assert await store.load() is None
    assert store.key not in redis.data


async def test_adoption_failure_never_blocks_the_load():
    class _ReadOnlyRedis(_FakeRedis):
        async def set(self, key, value, nx=False):
            raise ConnectionError("read-only replica")

    redis = _ReadOnlyRedis({_LEGACY_REDIS_KEY: '{"max_order_size_usd": 5.0}'})
    store = RedisLimitsStore(redis, prefix="kalshi-demo")
    assert await store.load() == {"max_order_size_usd": 5.0}


async def test_namespaced_reset_is_not_undone_by_the_legacy_fallback():
    redis = _FakeRedis({_LEGACY_REDIS_KEY: '{"max_order_size_usd": 5.0}'})
    store = RedisLimitsStore(redis, prefix="kalshi-demo")
    await store.clear()
    assert redis.data[_LEGACY_REDIS_KEY] == '{"max_order_size_usd": 5.0}'
    assert redis.data[store.key] == "{}"
    assert not await store.load()


def _config(max_order_size_usd: float = 25.0) -> Config:
    return Config(
        key_id="test-key",
        private_key_path=None,
        private_key_pem="<set-in-test>",
        env="demo",
        trading_enabled=True,
        rest_base=DEMO_REST_BASE,
        ws_url=DEMO_WS_URL,
        max_order_size_usd=max_order_size_usd,
        daily_limit_usd=250.0,
        max_contracts_per_order=100,
        cash_reserve_usd=0.0,
        transport="stdio",
        port=8000,
        log_level="INFO",
        runtime_limit_tuning_enabled=True,
    )


async def test_two_deployments_on_one_redis_no_longer_share_overrides():
    """The bug: demo + prod on one Redis loaded each other's clamps."""
    redis = _FakeRedis()
    prod = SafetyController(
        _config(), store=RedisLimitsStore(redis, DEFAULT_REDIS_COLLECTION_PREFIX)
    )
    demo = SafetyController(_config(), store=RedisLimitsStore(redis, "kalshi-demo"))

    await demo.set_limits(max_order_size_usd=1.0)  # demo clamps hard
    await prod.set_limits(max_order_size_usd=10.0)

    # "Restart" both against the same Redis.
    prod2 = SafetyController(
        _config(), store=RedisLimitsStore(redis, DEFAULT_REDIS_COLLECTION_PREFIX)
    )
    demo2 = SafetyController(_config(), store=RedisLimitsStore(redis, "kalshi-demo"))
    assert (await prod2.load_persisted()).max_order_size_usd == 10.0
    assert (await demo2.load_persisted()).max_order_size_usd == 1.0


async def test_legacy_fallback_is_still_clamped_to_the_env_ceiling():
    """A legacy record looser than this deployment's ceiling can't widen it."""
    redis = _FakeRedis({_LEGACY_REDIS_KEY: '{"max_order_size_usd": 500.0}'})
    ctrl = SafetyController(_config(max_order_size_usd=25.0), store=RedisLimitsStore(redis, "p2"))
    assert (await ctrl.load_persisted()).max_order_size_usd == 25.0
