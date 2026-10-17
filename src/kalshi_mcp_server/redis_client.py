"""Shared Redis connection policy for every store that uses `MCP_REDIS_URL`.

Two consumers share one Redis URL: the OAuth proxy's DCR/token store
(`oauth.py:_build_client_storage`) and the runtime safety-limit override
store (`safety_store.py:build_limits_store`). They used to build their
clients separately, and only the OAuth one got the resilience settings
below — so the safety store ran on redis-py's `from_url` defaults (no
health check, zero retries, and on older redis-py no socket timeout). This
module is the single place both now get their client, URL policy and
namespace from.

Three things live here:

1. `validate_redis_url()` — refuses a cleartext `redis://` URL to anything
   but loopback unless `MCP_ALLOW_PLAINTEXT_REDIS=1` is set. Over that
   connection travel the Redis password, the (encrypted) OAuth state and the
   *unencrypted* safety-limit overrides, which an on-path attacker could
   rewrite.
2. `build_redis_client()` — the hardened `redis.asyncio.Redis` (see the
   constants below and `oauth.py:_build_client_storage` for the full story).
3. `collection_prefix()` — the per-deployment namespace
   (`MCP_REDIS_COLLECTION_PREFIX`, default `kalshi`).

`redis` itself is imported lazily: it ships in the `[oauth]` extra, and the
base install must import this module without it.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from urllib.parse import urlsplit

from kalshi_mcp_server.errors import ConfigError

logger = logging.getLogger("kalshi_mcp_server")

# Redis connection-resilience tuning. See `oauth.py:_build_client_storage`
# for what each one actually buys.
REDIS_HEALTH_CHECK_INTERVAL_SECONDS = 30
# 2s, not the redis-py default of 5s. These are the dominant term in the
# worst case: against a *blackholed* (not refused) Redis every leaf attempt
# burns a full timeout, and the nesting below gives ~4 leaf attempts per
# command across the 8 sequential store ops in a token exchange. At 5s that's
# minutes on a hung request; at 2s it's ~1 min. Upstash round-trips are
# single-digit ms, so 2s is still ~100x headroom.
REDIS_CONNECT_TIMEOUT_SECONDS = 2
REDIS_SOCKET_TIMEOUT_SECONDS = 2
# Deliberately 1, not 3. redis-py applies the retry policy at THREE nested
# layers — `execute_command`, `connect`, and `check_health` — so the attempt
# count multiplies to roughly (retries + 1) ** 2 TLS connects per command.
# FastMCP issues 8 sequential store ops inside `exchange_refresh_token`
# (~10 for the whole /token request), so retries=3
# turns a Redis outage into a multi-minute hang on an OAuth request (and
# hammers a provider that bills per connection). `health_check_interval` is
# what actually fixes the stale-socket case; the retry only covers a genuine
# single-command blip, and 1 is enough for that.
REDIS_RETRIES = 1
# Namespaces this server's keys so a single Redis instance can host several
# servers. Env-configurable because the documented deploy model is
# image-deploy from `:latest` — two instances of the *same* image (e.g. a
# demo and a prod service) would otherwise share a prefix and cross-resolve
# each other's state. See `oauth.py:_build_client_storage` and
# `safety_store.py`.
DEFAULT_REDIS_COLLECTION_PREFIX = "kalshi"

ALLOW_PLAINTEXT_REDIS_ENV = "MCP_ALLOW_PLAINTEXT_REDIS"
_TRUTHY = {"1", "true", "yes", "on"}
_warned_plaintext = False


def collection_prefix() -> str:
    """Namespace for this deployment's Redis keys/collections.

    Env-overridable so two instances of the *same* published image can share
    one Redis without colliding — the documented deploy path is image-deploy
    from `:latest`, so a source constant alone would force a fork.
    """
    return (
        os.environ.get("MCP_REDIS_COLLECTION_PREFIX", "").strip() or DEFAULT_REDIS_COLLECTION_PREFIX
    )


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a DNS name other than `localhost`: assume remote


def validate_redis_url(url: str) -> None:
    """Fail closed on a Redis URL that would send traffic in cleartext.

    Accepted without further ado:
    - `rediss://` (TLS) — what Upstash and most managed Redis issue.
    - `redis://` to a loopback host (`localhost`, `127.0.0.0/8`, `::1`) —
      local dev, nothing crosses a network.
    - `unix://` — a local socket, likewise never on the wire.

    Any other `redis://` URL raises `ConfigError` unless the operator sets
    `MCP_ALLOW_PLAINTEXT_REDIS=1`. That opt-out exists for Redis reachable
    only over a private network that the host already isolates — e.g. a
    Render Key Value instance's *internal* `redis://red-…:6379` URL, which
    is plaintext by design and not reachable from the internet. Don't set it
    for anything that crosses the public internet: the connection carries
    the Redis password, the OAuth state and the (unencrypted, rewritable)
    safety-limit overrides.

    The error never echoes the URL — it usually embeds the Redis password.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as exc:
        raise ConfigError("MCP_REDIS_URL is not a valid URL.") from exc

    scheme = parts.scheme.lower()
    if scheme in ("rediss", "unix"):
        return
    if scheme != "redis":
        raise ConfigError(
            f"MCP_REDIS_URL has unsupported scheme {scheme!r}. Use a TLS "
            "`rediss://` URL (or `redis://` to a loopback host for local dev)."
        )
    if _is_loopback(host):
        return
    if os.environ.get(ALLOW_PLAINTEXT_REDIS_ENV, "").strip().lower() in _TRUTHY:
        global _warned_plaintext
        if _warned_plaintext:
            return
        # Once per process: both stores validate the same URL, each twice.
        _warned_plaintext = True
        logger.warning(
            "MCP_REDIS_URL uses cleartext redis:// to %s (allowed by %s). Only "
            "do this on a private network the host isolates — the Redis "
            "password and the safety-limit overrides cross it unencrypted.",
            host,
            ALLOW_PLAINTEXT_REDIS_ENV,
        )
        return
    raise ConfigError(
        f"MCP_REDIS_URL uses cleartext redis:// to a non-loopback host ({host}). "
        "Use the TLS `rediss://` URL (Upstash: the default endpoint). If this "
        "Redis is reachable only over a private network (e.g. Render Key "
        f"Value's internal URL), set {ALLOW_PLAINTEXT_REDIS_ENV}=1 to allow it."
    )


def build_redis_client(url: str) -> object:
    """Return a `redis.asyncio.Redis` for `url` with the shared policy applied.

    Validates the URL first (`validate_redis_url`), so a cleartext URL never
    gets a client built. Raises `ImportError` when the `[oauth]` extra
    (`redis`) isn't installed; each caller decides whether that is fatal
    (OAuth) or a degrade-to-in-memory (safety limits).

    `decode_responses=True` is required by both consumers: `RedisStore`
    rejects non-`str` responses, and the safety store JSON-decodes a `str`.
    The connection is lazy — an unreachable Redis does not fail here.
    """
    validate_redis_url(url)

    from redis.asyncio import Redis
    from redis.asyncio.retry import Retry

    # FullJitterBackoff over ExponentialBackoff so pooled connections
    # don't retry in lockstep — a small win at these delays (~0-16ms vs
    # a flat 16ms), but free. Not ExponentialWithJitterBackoff: it
    # landed in redis-py 6.0 and the `redis>=5.0.0` floor allows older
    # releases, where importing it would surface as an ImportError that
    # callers report as "extras aren't installed".
    from redis.backoff import FullJitterBackoff
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    return Redis.from_url(
        url,
        decode_responses=True,
        health_check_interval=REDIS_HEALTH_CHECK_INTERVAL_SECONDS,
        socket_keepalive=True,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
        retry=Retry(FullJitterBackoff(), retries=REDIS_RETRIES),
        # `Connection.__init__` *appends* to this list when `retry_on_timeout`
        # is truthy, and the pool hands the same list to every connection it
        # makes — so what keeps it from growing unboundedly is leaving
        # `retry_on_timeout` at its default False, not the fresh list here.
        # If you ever set it, copy the list per connection.
        retry_on_error=[RedisConnectionError, RedisTimeoutError],
    )
