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
namespaced key. `safety.py` re-clamps whatever it reads back to the current
env ceiling on load, so the env vars remain the absolute trust anchor — a
value in Redis can only ever tighten a limit, never loosen one.
"""

from __future__ import annotations

import json
import logging
import os

from kalshi_mcp_server.safety import InMemoryLimitsStore, LimitsStore

logger = logging.getLogger(__name__)

# Single key holding the JSON override map. Namespaced so a shared Redis can
# host this alongside the OAuth proxy's `kalshi-oauth-proxy` collection and
# other apps without colliding.
_REDIS_KEY = "kalshi-mcp:safety-limits"


class RedisLimitsStore:
    """Redis-backed override store. Persists across restarts.

    Stores a JSON object of the overridden fields under a single key. Reads
    are defensive: any non-dict / unparseable payload is treated as "no
    override" (returns None) so a corrupt value degrades to the env ceilings
    rather than raising.
    """

    durable = True

    def __init__(self, client: object) -> None:
        # `client` is a `redis.asyncio.Redis`; typed loosely so this module
        # imports without the optional dependency present.
        self._client = client

    async def load(self) -> dict[str, float] | None:
        raw = await self._client.get(_REDIS_KEY)  # type: ignore[attr-defined]
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
        await self._client.set(_REDIS_KEY, json.dumps(overrides))  # type: ignore[attr-defined]

    async def clear(self) -> None:
        await self._client.delete(_REDIS_KEY)  # type: ignore[attr-defined]


def build_limits_store() -> tuple[LimitsStore, str]:
    """Return `(store, description)` for runtime safety-limit persistence.

    With `MCP_REDIS_URL` set, builds a Redis-backed store (persistent across
    restarts). Without it — and on every stdio client — returns the in-memory
    store (overrides reset to the env ceilings on restart).

    Mirrors `oauth.py:_build_client_storage`: uses `redis.asyncio.Redis.from_url`
    with `decode_responses=True` so `rediss://` TLS (e.g. Upstash) works and
    GET returns `str`. The connection is lazy — an unreachable Redis does not
    fail here; it surfaces at load/save time, where the caller degrades to the
    env ceilings (boot) or reports the change as not-durably-persisted (set).

    The returned description is logged at startup so the chosen backend is
    visible in the host's log stream.
    """
    url = os.environ.get("MCP_REDIS_URL", "").strip()
    if not url:
        return InMemoryLimitsStore(), "in-memory (resets to env ceilings on restart)"

    # Lazy import: the base install doesn't ship redis (it's in [oauth]).
    try:
        from redis.asyncio import Redis
    except ImportError:
        logger.warning(
            "MCP_REDIS_URL is set but the [oauth] extras (redis) aren't "
            "installed — runtime safety-limit overrides will NOT persist "
            "across restarts. Re-install with `uv sync --extra oauth` (or "
            "`pip install kalshi-mcp-server[oauth]`) to enable persistence."
        )
        return InMemoryLimitsStore(), "in-memory (redis extra missing; no persistence)"

    client = Redis.from_url(url, decode_responses=True)
    return RedisLimitsStore(client), "redis (persistent)"
