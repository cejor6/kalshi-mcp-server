"""OAuth proxy wiring for remote-MCP deployments.

This module is **only** relevant when running with `--transport http`
and exposing the server publicly (e.g. for use as a claude.ai custom
connector). Local stdio use does not need any of this — the MCP client
itself is the operator and there's no third party in the loop.

When the relevant env vars are set, the FastMCP server is constructed
with a `GitHubProvider`-backed OAuth proxy. The proxy speaks Dynamic
Client Registration + PKCE + JWT issuance, so claude.ai's custom-
connector form (which only supports OAuth) can talk to it.

Three pieces here:

1. `build_auth_provider()` returns a configured `GitHubProvider` when
   `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` / `MCP_BASE_URL` are all
   set, else `None`.
2. `build_user_restriction_middleware()` returns middleware that rejects
   tool calls from GitHub logins not in `MCP_ALLOWED_GITHUB_LOGINS`.
   Without this, anyone with a GitHub account who completes the OAuth
   flow could invoke tools — usually not what you want.
3. `_build_client_storage()` builds an Upstash Redis-backed store for
   the proxy's DCR registrations + OAuth state when `MCP_REDIS_URL` is
   set. Without it, registrations live only in the container's memory
   and a redeploy boots all connected clients out (they'd have to
   reconnect via claude.ai).

The "fail closed when http+missing-config" check lives in `cli.py`, not
here — this module is policy-free and just builds what the env says.
"""

from __future__ import annotations

import os

from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext


def _build_client_storage() -> tuple[object | None, str]:
    """Return `(storage, description)` for the OAuth proxy's DCR client store.

    With `MCP_REDIS_URL` set, builds a Redis-backed store keyed under a
    Kalshi-specific collection name so a shared Redis can host multiple
    MCP servers without colliding. Without the env var, falls back to
    FastMCP's default in-container file store (ephemeral — every restart
    forces clients to reconnect).

    Uses `redis.asyncio.Redis.from_url` because `RedisStore(url=...)` in
    `py-key-value-aio` 0.4.4 ignores TLS on `rediss://` URLs. Upstash
    requires TLS so we construct the client manually.

    `decode_responses=True` is required: `RedisStore._get_managed_entry`
    rejects non-`str` responses and `Redis.from_url` defaults to False.
    """
    url = os.environ.get("MCP_REDIS_URL", "").strip()
    if not url:
        return None, "in-memory file (ephemeral; resets on container restart)"

    # Imports are inside the function so the base install doesn't need
    # redis + py-key-value-aio. Those are part of the [oauth] extras.
    try:
        from key_value.aio.stores.redis import RedisStore
        from redis.asyncio import Redis
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "MCP_REDIS_URL is set but the [oauth] extras aren't installed. "
            "Re-install with `uv sync --extra oauth` (or `pip install "
            "kalshi-mcp-server[oauth]`)."
        ) from exc

    client = Redis.from_url(url, decode_responses=True)
    return (
        RedisStore(client=client, default_collection="kalshi-oauth-proxy"),
        "redis (persistent)",
    )


def build_auth_provider() -> tuple[GitHubProvider | None, str]:
    """Construct the GitHub OAuth provider from env vars, or `(None, '')`.

    Returns `(provider, storage_description)`. The description is logged
    by the CLI on startup so the chosen storage backend (memory vs
    Redis) is visible in Render's log stream.

    Required env vars (all three must be set to enable OAuth):
    - `GITHUB_CLIENT_ID`     — OAuth App client ID
    - `GITHUB_CLIENT_SECRET` — OAuth App client secret
    - `MCP_BASE_URL`         — public URL of this server (no trailing slash)

    Optional:
    - `MCP_JWT_SIGNING_KEY`  — stable signing key for proxy-issued JWTs.
      Without this, a fresh key is generated each process start and
      previously-issued tokens are invalidated on restart.
    - `MCP_REDIS_URL`        — `rediss://...` for persistent DCR client
      storage (Upstash works well for this).
    """
    client_id = os.environ.get("GITHUB_CLIENT_ID")
    client_secret = os.environ.get("GITHUB_CLIENT_SECRET")
    base_url = os.environ.get("MCP_BASE_URL")
    if not (client_id and client_secret and base_url):
        return None, ""

    storage, storage_desc = _build_client_storage()

    return (
        GitHubProvider(
            client_id=client_id,
            client_secret=client_secret,
            base_url=base_url.rstrip("/"),
            jwt_signing_key=os.environ.get("MCP_JWT_SIGNING_KEY") or None,
            client_storage=storage,
        ),
        storage_desc,
    )


def _parse_allowed_logins() -> frozenset[str]:
    raw = os.environ.get("MCP_ALLOWED_GITHUB_LOGINS", "").strip()
    return frozenset(s.strip().lower() for s in raw.split(",") if s.strip())


class RestrictGitHubUsersMiddleware(Middleware):
    """Reject tool calls from GitHub users not in MCP_ALLOWED_GITHUB_LOGINS.

    The OAuth proxy will happily issue a token to any GitHub account
    that completes the flow. Without this middleware, anyone with a
    GitHub account could call your Kalshi tools. The middleware checks
    the `login` claim on the proxy-issued JWT against an env-configured
    allowlist and aborts the call before it reaches the tool body.
    """

    def __init__(self, allowed_logins: frozenset[str]) -> None:
        self._allowed = allowed_logins

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        if not self._allowed:
            raise ToolError("Server misconfigured: MCP_ALLOWED_GITHUB_LOGINS is empty")
        token = get_access_token()
        if token is None:
            raise ToolError("Unauthenticated")
        login = (token.claims.get("login") or "").lower()
        if login not in self._allowed:
            raise ToolError(f"GitHub user '{login or '(unknown)'}' is not authorized")
        return await call_next(context)


def build_user_restriction_middleware() -> RestrictGitHubUsersMiddleware | None:
    """Return the allowlist middleware if configured, else None."""
    allowed = _parse_allowed_logins()
    if not allowed:
        return None
    return RestrictGitHubUsersMiddleware(allowed)
