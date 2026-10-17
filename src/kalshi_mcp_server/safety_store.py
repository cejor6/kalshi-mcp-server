"""Persistence backends for runtime safety-limit overrides.

The in-memory store lives in `safety.py` (it's the zero-dependency default).
This module adds the optional **Redis-backed** store so that a runtime
tightening of the safety limits survives a restart/redeploy — the "fast
clamp-down that sticks" use case from issue #27.

Like the OAuth DCR storage (`oauth.py:_build_client_storage`), Redis is only
engaged when `MCP_REDIS_URL` is set, the `redis` dependency ships in the
`[oauth]` extra (not the base install), and the import is lazy so a plain
stdio install never needs it. Unlike the OAuth path — which hard-fails when
the extra is missing because OAuth literally cannot work without it — limit
persistence is a *nice-to-have*: if the extra is absent we log a warning and
fall back to the in-memory store rather than refusing to boot. The server is
fully functional either way; it just won't remember a runtime override across
a restart.

The store only ever holds a *sparse* override map (the fields that differ
from the env ceiling), serialized as a small JSON object under a single
key namespaced by `MCP_REDIS_COLLECTION_PREFIX` (see `_limits_key`).
`safety.py` re-clamps whatever it reads back to the current
env ceiling on load, so the env vars remain the absolute trust anchor — a
value in Redis can only ever tighten a limit, never loosen one.
"""

from __future__ import annotations

import json
import logging
import os

from kalshi_mcp_server.redis_client import (
    DEFAULT_REDIS_COLLECTION_PREFIX,
    build_redis_client,
    collection_prefix,
    validate_redis_url,
)
from kalshi_mcp_server.safety import InMemoryLimitsStore, LimitsStore

logger = logging.getLogger(__name__)

# Key holding the JSON override map. The `kalshi-mcp:` prefix keeps it clear
# of the OAuth proxy's collections (`<prefix>__mcp-*`, see `oauth.py`) and of
# other apps on a shared Redis.
#
# The key is namespaced by `MCP_REDIS_COLLECTION_PREFIX` — the same knob that
# separates the OAuth collections — so two deployments sharing one Redis (a
# demo and a prod service, say) no longer share one override record:
#
#   default prefix (`kalshi`)  -> `kalshi-mcp:safety-limits`   (legacy name)
#   any other prefix `<p>`     -> `kalshi-mcp:<p>:safety-limits`
#
# The default prefix deliberately keeps the LEGACY key name. Before
# namespacing, every deployment used `kalshi-mcp:safety-limits`; renaming it
# for the default would silently drop every active override back to the env
# ceilings on upgrade — a lost tightening IS a loosening. For a non-default
# prefix, `load` falls back to the legacy key while the namespaced key has
# never been written, so a clamp persisted before upgrade still applies. That
# fallback can only tighten (everything loaded is re-clamped to the env
# ceilings in `safety.py`), and it ends the first time this deployment saves
# or resets its limits — `clear` writes an empty `{}` record rather than
# deleting, precisely so a reset isn't undone by the fallback on next boot.
# The legacy key itself is never written or deleted from a namespaced store:
# with a default-prefix deployment on the same Redis, it's that deployment's
# live record.
_LEGACY_REDIS_KEY = "kalshi-mcp:safety-limits"


def _limits_key(prefix: str) -> str:
    if prefix == DEFAULT_REDIS_COLLECTION_PREFIX:
        return _LEGACY_REDIS_KEY
    return f"kalshi-mcp:{prefix}:safety-limits"


class RedisLimitsStore:
    """Redis-backed override store. Persists across restarts.

    Stores a JSON object of the overridden fields under a single key. Reads
    are defensive: any non-dict / unparseable payload is treated as "no
    override" (returns None) so a corrupt value degrades to the env ceilings
    rather than raising.
    """

    durable = True

    def __init__(self, client: object, prefix: str = DEFAULT_REDIS_COLLECTION_PREFIX) -> None:
        # `client` is a `redis.asyncio.Redis`; typed loosely so this module
        # imports without the optional dependency present.
        self._client = client
        self.key = _limits_key(prefix)
        # Read-only fallback for overrides persisted before keys were
        # namespaced. None when this store already uses the legacy key.
        self._legacy_key = None if self.key == _LEGACY_REDIS_KEY else _LEGACY_REDIS_KEY

    async def load(self) -> dict[str, float] | None:
        raw = await self._client.get(self.key)  # type: ignore[attr-defined]
        if raw is None and self._legacy_key is not None:
            # Never written under the namespaced key: honor a clamp persisted
            # before upgrade. Tighten-only — `safety.py` re-clamps on load.
            raw = await self._client.get(self._legacy_key)  # type: ignore[attr-defined]
            if raw:
                logger.info(
                    "No safety-limit record under %s yet; applying the legacy "
                    "%s record (tighten-only) until this deployment saves its own.",
                    self.key,
                    self._legacy_key,
                )
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Persisted safety limits are not valid JSON; ignoring.")
            return None
        if not isinstance(data, dict):
            logger.warning("Persisted safety limits are not an object; ignoring.")
            return None
        return data

    async def save(self, overrides: dict[str, float]) -> None:
        await self._client.set(self.key, json.dumps(overrides))  # type: ignore[attr-defined]

    async def clear(self) -> None:
        if self._legacy_key is None:
            await self._client.delete(self.key)  # type: ignore[attr-defined]
        else:
            # An explicit empty record, not a delete: a missing key would
            # re-enable the legacy fallback in `load` on the next boot and
            # resurrect a clamp the operator just reset.
            await self._client.set(self.key, "{}")  # type: ignore[attr-defined]


def build_limits_store() -> tuple[LimitsStore, str]:
    """Return `(store, description)` for runtime safety-limit persistence.

    With `MCP_REDIS_URL` set, builds a Redis-backed store (persistent across
    restarts). Without it — and on every stdio client — returns the in-memory
    store (overrides reset to the env ceilings on restart).

    Builds its client with `redis_client.build_redis_client` — the same
    hardened client the OAuth store uses (health check, bounded timeouts,
    jittered retry, `decode_responses=True`) — and namespaces its key with
    `MCP_REDIS_COLLECTION_PREFIX` (see `_limits_key`). The connection is
    lazy — an unreachable Redis does not fail here; it surfaces at load/save
    time, where the caller degrades to the env ceilings (boot) or reports the
    change as not-durably-persisted (set).

    Raises `ConfigError` for a cleartext `redis://` URL to a non-loopback
    host unless `MCP_ALLOW_PLAINTEXT_REDIS=1` (`redis_client.validate_redis_url`)
    — checked before the extras probe, so the policy holds either way.

    The returned description is logged at startup so the chosen backend is
    visible in the host's log stream.
    """
    url = os.environ.get("MCP_REDIS_URL", "").strip()
    if not url:
        return InMemoryLimitsStore(), "in-memory (resets to env ceilings on restart)"

    validate_redis_url(url)

    # Lazy import: the base install doesn't ship redis (it's in [oauth]).
    try:
        client = build_redis_client(url)
    except ImportError:
        logger.warning(
            "MCP_REDIS_URL is set but the [oauth] extras (redis) aren't "
            "installed — runtime safety-limit overrides will NOT persist "
            "across restarts. Re-install with `uv sync --extra oauth` (or "
            "`pip install kalshi-mcp-server[oauth]`) to enable persistence."
        )
        return InMemoryLimitsStore(), "in-memory (redis extra missing; no persistence)"

    store = RedisLimitsStore(client, prefix=collection_prefix())
    return store, f"redis (persistent, key {store.key})"
