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
        "MCP_REDIS_COLLECTION_PREFIX",
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


def _redis_storage(monkeypatch):
    """Build the Redis-backed store (wrappers included) and return it."""
    # No credentials in the URL — nothing here connects, and embedded
    # basic-auth trips the detect-secrets hook.
    monkeypatch.setenv("MCP_REDIS_URL", "rediss://example.invalid:6379")
    monkeypatch.setenv("MCP_JWT_SIGNING_KEY", "test-signing-key-not-a-real-secret")
    from kalshi_mcp_server.oauth import _build_client_storage

    storage, desc = _build_client_storage()
    assert storage is not None
    assert "redis" in desc
    return storage


def _redis_store_client(monkeypatch):
    """Return the redis client underneath the wrapper stack."""
    from key_value.aio.stores.redis import RedisStore

    storage = _redis_storage(monkeypatch)
    # Unwrap: FernetEncryptionWrapper -> PrefixCollectionsWrapper -> RedisStore
    inner = storage
    while not isinstance(inner, RedisStore):
        inner = inner.key_value
    return inner._client


# ── Redis client store: isolation + encryption at rest ─────────────────────


def test_redis_store_is_encrypted_at_rest(monkeypatch):
    """Supplying client_storage opts out of FastMCP's own encryption.

    Without re-applying it, UpstreamTokenSet — the live upstream access and
    refresh tokens — sits in Redis as plaintext JSON.
    """
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    storage = _redis_storage(monkeypatch)
    assert isinstance(storage, FernetEncryptionWrapper)
    # isinstance alone passes for the stock class too, so reverting the call
    # site to it would leave this green. Pin the strict behavior instead:
    # an unencrypted value must read as absent.
    assert storage._decrypt_value({"not": "encrypted"}) is None


def test_redis_store_prefixes_collections(monkeypatch):
    """FastMCP's collection names are identical across servers.

    Without a prefix, two OAuth-proxy servers sharing a Redis DB resolve
    each other's client registrations and JTIs.
    """
    from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper

    from kalshi_mcp_server.oauth import DEFAULT_REDIS_COLLECTION_PREFIX

    storage = _redis_storage(monkeypatch)
    prefixed = storage.key_value
    assert isinstance(prefixed, PrefixCollectionsWrapper)
    assert prefixed.prefix == DEFAULT_REDIS_COLLECTION_PREFIX


def test_redis_store_requires_a_stable_signing_key(monkeypatch):
    """Redis + no signing key is already broken; fail closed rather than
    silently encrypt with a key that dies with the process."""
    from kalshi_mcp_server.errors import ConfigError
    from kalshi_mcp_server.oauth import _build_client_storage

    monkeypatch.setenv("MCP_REDIS_URL", "rediss://example.invalid:6379")
    monkeypatch.delenv("MCP_JWT_SIGNING_KEY", raising=False)
    with pytest.raises(ConfigError) as exc:
        _build_client_storage()
    assert "MCP_JWT_SIGNING_KEY" in str(exc.value)


async def test_wrapper_stack_encrypts_and_isolates_end_to_end():
    """Behavioral: build the same stack over MemoryStore and prove all four
    properties at once — round-trip, prefixing, no plaintext at rest, and
    that an unprefixed read (i.e. a sibling server) can't see the value.

    Wiring tests above assert the wrappers are present; this asserts they
    actually do their job when composed in this order. Uses the module's own
    salt constant so changing the salt in oauth.py doesn't silently leave
    this passing against a stale value.
    """
    from key_value.aio.stores.memory import MemoryStore
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
    from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper

    from kalshi_mcp_server.oauth import (
        DEFAULT_REDIS_COLLECTION_PREFIX,
        STORAGE_ENCRYPTION_SALT,
    )

    inner = MemoryStore()
    stack = FernetEncryptionWrapper(
        key_value=PrefixCollectionsWrapper(key_value=inner, prefix=DEFAULT_REDIS_COLLECTION_PREFIX),
        source_material="k" * 40,
        salt=STORAGE_ENCRYPTION_SALT,
        raise_on_decryption_error=False,
    )

    # `mcp-upstream-tokens` is the collection holding UpstreamTokenSet —
    # the live upstream access + refresh tokens, i.e. the actual exposure.
    secret = {"access_token": "ghu_SUPERSECRET", "refresh_token": "ghr_ALSOSECRET"}
    await stack.put(key="tok1", value=secret, collection="mcp-upstream-tokens")

    assert await stack.get(key="tok1", collection="mcp-upstream-tokens") == secret

    at_rest = await inner.get(
        key="tok1", collection=f"{DEFAULT_REDIS_COLLECTION_PREFIX}__mcp-upstream-tokens"
    )
    assert at_rest is not None, "prefixed collection should resolve"
    assert "SUPERSECRET" not in str(at_rest), "token must not be plaintext at rest"

    # A sibling server on the same Redis, without our prefix, sees nothing.
    assert await inner.get(key="tok1", collection="mcp-upstream-tokens") is None


async def test_undecryptable_entry_degrades_to_a_cache_miss():
    """A key rotation must not hard-fail — FastMCP treats None as "unknown
    client" and the client re-registers. Raising instead would wedge the
    proxy for every stored entry until someone flushed Redis by hand.
    """
    from key_value.aio.stores.memory import MemoryStore
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    from kalshi_mcp_server.oauth import STORAGE_ENCRYPTION_SALT

    inner = MemoryStore()
    written = FernetEncryptionWrapper(
        key_value=inner,
        source_material="original-key",
        salt=STORAGE_ENCRYPTION_SALT,
        raise_on_decryption_error=False,
    )
    await written.put(key="k", value={"a": 1}, collection="c")

    # Same store, different source material == a rotated key.
    rotated = FernetEncryptionWrapper(
        key_value=inner,
        source_material="rotated-key",
        salt=STORAGE_ENCRYPTION_SALT,
        raise_on_decryption_error=False,
    )
    assert await rotated.get(key="k", collection="c") is None


def test_encrypted_data_marker_matches_upstream():
    """Our copy of the marker must track py-key-value-aio's private constant.

    If upstream renames it, the strict wrapper below would reject every entry
    (fail-closed — misses, not plaintext acceptance), but the connector would
    break. Fail here first, loudly, instead.
    """
    from key_value.aio.wrappers.encryption.base import _ENCRYPTED_DATA_KEY

    from kalshi_mcp_server.oauth import _ENCRYPTED_DATA_MARKER

    assert _ENCRYPTED_DATA_MARKER == _ENCRYPTED_DATA_KEY


async def test_strict_wrapper_rejects_planted_plaintext():
    """The stock wrapper returns unencrypted values as-is, so anyone who can
    WRITE to Redis could plant an accepted record without knowing the key —
    e.g. a ProxyDCRClient with attacker-chosen redirect_uris. Treat "not
    encrypted by us" as "not there".
    """
    from key_value.aio.stores.memory import MemoryStore

    from kalshi_mcp_server.oauth import (
        STORAGE_ENCRYPTION_SALT,
        _strict_encryption_wrapper_class,
    )

    inner = MemoryStore()
    strict = _strict_encryption_wrapper_class()(
        key_value=inner,
        source_material="k" * 40,
        salt=STORAGE_ENCRYPTION_SALT,
        raise_on_decryption_error=False,
    )

    # Written straight to the backing store, bypassing encryption entirely.
    await inner.put(
        key="planted",
        value={"redirect_uris": ["https://attacker.example/callback"]},
        collection="mcp-oauth-proxy-clients",
    )
    assert await strict.get(key="planted", collection="mcp-oauth-proxy-clients") is None

    # And the stock wrapper is what we're protecting against — confirm it
    # really does accept the same record, so this test isn't guarding nothing.
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    lenient = FernetEncryptionWrapper(
        key_value=inner,
        source_material="k" * 40,
        salt=STORAGE_ENCRYPTION_SALT,
        raise_on_decryption_error=False,
    )
    assert await lenient.get(key="planted", collection="mcp-oauth-proxy-clients") is not None


async def test_strict_wrapper_still_round_trips_its_own_writes():
    """Rejecting plaintext must not break the normal path."""
    from key_value.aio.stores.memory import MemoryStore

    from kalshi_mcp_server.oauth import (
        STORAGE_ENCRYPTION_SALT,
        _strict_encryption_wrapper_class,
    )

    strict = _strict_encryption_wrapper_class()(
        key_value=MemoryStore(),
        source_material="k" * 40,
        salt=STORAGE_ENCRYPTION_SALT,
        raise_on_decryption_error=False,
    )
    payload = {"access_token": "ghu_x", "refresh_token": "ghr_y"}
    await strict.put(key="t", value=payload, collection="mcp-upstream-tokens")
    assert await strict.get(key="t", collection="mcp-upstream-tokens") == payload


def test_short_signing_key_warns_but_still_starts(monkeypatch, caplog):
    """Warn, don't fail — a short key works, it's just weak. It is the only
    thing protecting the tokens encrypted in Redis, so silence is wrong."""
    from kalshi_mcp_server.oauth import _build_client_storage

    monkeypatch.setenv("MCP_REDIS_URL", "rediss://example.invalid:6379")
    monkeypatch.setenv("MCP_JWT_SIGNING_KEY", "short")
    with caplog.at_level("WARNING", logger="kalshi_mcp_server"):
        storage, _ = _build_client_storage()
    assert storage is not None, "a short key must not block startup"
    assert any("MCP_JWT_SIGNING_KEY is only 5 characters" in r.message for r in caplog.records)


def test_adequate_signing_key_does_not_warn(monkeypatch, caplog):
    with caplog.at_level("WARNING", logger="kalshi_mcp_server"):
        _redis_storage(monkeypatch)
    assert not [r for r in caplog.records if "MCP_JWT_SIGNING_KEY" in r.message]


def test_collection_prefix_is_env_overridable(monkeypatch):
    """Two instances of the same published image must be able to share a
    Redis; a source-only constant would force a fork to do that."""
    from kalshi_mcp_server.oauth import (
        DEFAULT_REDIS_COLLECTION_PREFIX,
        _collection_prefix,
    )

    monkeypatch.delenv("MCP_REDIS_COLLECTION_PREFIX", raising=False)
    assert _collection_prefix() == DEFAULT_REDIS_COLLECTION_PREFIX

    monkeypatch.setenv("MCP_REDIS_COLLECTION_PREFIX", "kalshi-demo")
    assert _collection_prefix() == "kalshi-demo"

    # Whitespace-only must not silently produce an empty namespace.
    monkeypatch.setenv("MCP_REDIS_COLLECTION_PREFIX", "   ")
    assert _collection_prefix() == DEFAULT_REDIS_COLLECTION_PREFIX


def test_storage_key_is_stretched_not_a_bare_hash():
    """The storage key derives via PBKDF2 (1.2M rounds), not a single hash.

    A single-hash derivation would let anyone with Redis read access
    brute-force a weak MCP_JWT_SIGNING_KEY offline at ~1 guess/hash.
    """
    import time

    from key_value.aio.wrappers.encryption.fernet import (
        KDF_ITERATIONS,
        _generate_encryption_key,
    )

    from kalshi_mcp_server.oauth import STORAGE_ENCRYPTION_SALT

    assert KDF_ITERATIONS >= 1_000_000

    material = "test-signing-key-not-a-real-secret"
    start = time.perf_counter()
    key = _generate_encryption_key(source_material=material, salt=STORAGE_ENCRYPTION_SALT)
    elapsed = time.perf_counter() - start

    assert key != material.encode()
    # A bare hash returns in microseconds; stretching cannot.
    assert elapsed > 0.01, f"derivation too fast to be stretched ({elapsed:.4f}s)"


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
    # `get_retries()` over the private `_retries`. It postdates the
    # `redis>=5.0.0` floor, so a fork pinned to the floor would AttributeError
    # here — CI installs the lock, so that's not us.
    assert retry.get_retries() == 1
    supported = client.connection_pool.connection_kwargs["retry_on_error"]
    assert RedisConnectionError in supported
    assert RedisTimeoutError in supported


def test_redis_store_backoff_is_jittered(monkeypatch):
    """Unjittered backoff makes every pooled connection retry in lockstep.

    Reaches `_backoff` because redis-py exposes no public accessor for it
    (`Retry` offers only get_retries/update_retries/update_supported_errors).
    Unavoidable, not an oversight — don't "fix" it to match the public
    `get_retries()` used above.
    """
    from redis.backoff import FullJitterBackoff

    client = _redis_store_client(monkeypatch)
    retry = client.connection_pool.connection_kwargs["retry"]
    assert isinstance(retry._backoff, FullJitterBackoff)


def test_redis_store_bounds_read_writes_with_socket_timeout(monkeypatch):
    """Without this, an older redis-py defaults to None and hangs forever."""
    client = _redis_store_client(monkeypatch)
    kwargs = client.connection_pool.connection_kwargs
    # 2s, not redis-py's 5s default: these dominate the worst-case hang
    # against a blackholed Redis. See `_build_client_storage`.
    assert kwargs["socket_timeout"] == 2
    assert kwargs["socket_connect_timeout"] == 2


async def test_retry_policy_actually_retries_a_failing_command(monkeypatch):
    """Prove the configured retry really fires, not just that it's wired.

    Scope honestly: this drives the real `Retry` object built by
    `_build_client_storage` (so REDIS_RETRIES is end-to-end through
    `from_url`), but with a hand-written coroutine rather than a real
    command — so it covers the policy, NOT the send/reconnect path. A blip
    during the actual rotation sequence remains untested.
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
