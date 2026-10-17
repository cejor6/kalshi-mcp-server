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
   set, `None` when none are, and raises `ConfigError` for a partial set.
2. `build_user_restriction_middleware()` returns middleware that rejects
   EVERY MCP request (tool calls, resource reads, listings) from GitHub
   users matching neither `MCP_ALLOWED_GITHUB_IDS` nor
   `MCP_ALLOWED_GITHUB_LOGINS`. Without this, anyone with a GitHub
   account who completes the OAuth flow could read your balance and
   positions and invoke tools.
3. `_build_client_storage()` builds an Upstash Redis-backed store for
   the proxy's DCR registrations + OAuth state when `MCP_REDIS_URL` is
   set. Without it, registrations live only in the container's memory
   and a redeploy boots all connected clients out (they'd have to
   reconnect via claude.ai).

The "fail closed when http+missing-config" check lives in `cli.py`, not
here — this module just builds what the env says. It does refuse env it
can't build correctly (a partial OAuth set, a malformed id list, Redis
without a signing key), since "build nothing" would mean "no auth".
"""

from __future__ import annotations

import functools
import logging
import os

from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from kalshi_mcp_server.errors import ConfigError
from kalshi_mcp_server.redis_client import (
    DEFAULT_REDIS_COLLECTION_PREFIX as DEFAULT_REDIS_COLLECTION_PREFIX,
)
from kalshi_mcp_server.redis_client import build_redis_client, validate_redis_url
from kalshi_mcp_server.redis_client import collection_prefix as _collection_prefix

logger = logging.getLogger("kalshi_mcp_server")

# Redis connection policy (resilience kwargs, URL scheme policy, collection
# prefix) is shared with the safety-limit store and lives in
# `redis_client.py`. `DEFAULT_REDIS_COLLECTION_PREFIX` is re-exported here for
# existing importers.

# Domain-separates the storage-encryption key from the JWT signing key.
# Changing this makes every existing stored entry undecryptable (they degrade
# to cache misses and clients re-register) — treat it as a key rotation.
STORAGE_ENCRYPTION_SALT = "kalshi-mcp-storage-encryption-key"
# Below this we warn (never fail — a short key still works). The salt above is
# a fixed public constant, identical across forks, so precomputation amortizes
# across deployments; length is what actually makes that infeasible.
MIN_SIGNING_KEY_LENGTH = 32


# The marker py-key-value-aio writes alongside every encrypted payload. Kept
# as our own literal rather than imported: the upstream name is private, and a
# rename should surface as a loud test failure (see
# `test_encrypted_data_marker_matches_upstream`) rather than an import error
# buried in the [oauth]-extras handler. If it ever did drift unnoticed, the
# behavior stays fail-closed — every read becomes a miss and clients
# re-register, rather than plaintext being silently accepted.
_ENCRYPTED_DATA_MARKER = "__encrypted_data__"


@functools.cache
def _strict_encryption_wrapper_class() -> type:
    """Return a `FernetEncryptionWrapper` that refuses unencrypted values.

    The stock wrapper returns any value lacking the `__encrypted_data__`
    marker **as-is** (`wrappers/encryption/base.py:_decrypt_value`). Fernet
    itself is authenticated, so a reader reasonably assumes tampered data is
    rejected — but that only covers payloads that went *through* Fernet. A
    party who can write to Redis can skip it entirely and plant a plaintext
    record, and the proxy will accept it: an attacker-chosen `ProxyDCRClient`
    with its own `redirect_uris`, for instance.

    Encryption gives us confidentiality; this closes the integrity half by
    treating "not encrypted by us" as "not there". All four read paths
    (`get` / `get_many` / `ttl` / `ttl_many`) funnel through `_decrypt_value`,
    so overriding it covers everything.

    The DCR-client store is the obvious example but not the sharpest one —
    DCR is open anyway, and `get_client` re-imposes
    `allowed_redirect_uri_patterns` from config on read. The authorization-code,
    JTI-mapping and transaction stores are where a forged entry buys more.

    Side benefit that matters during the upgrade: pre-existing *plaintext*
    entries — the ones written before encryption was applied — become misses
    too, so they can't be used even before they're purged.

    Precisely: this rejects anything not encrypted under *this server's*
    signing key and salt. A sibling deployment sharing both could still
    produce entries we accept — which is exactly why co-deployed servers get
    distinct collection prefixes.

    Built by a factory rather than declared at module scope because the base
    class lives behind the lazily-imported `[oauth]` extras. Cached so the
    class has a stable identity across calls.
    """
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    class StrictFernetEncryptionWrapper(FernetEncryptionWrapper):  # type: ignore[misc]
        # Instance-level latch: a store full of legacy plaintext would
        # otherwise log once per read, unbounded and unactionable.
        _warned_unencrypted = False

        def _decrypt_value(self, value):  # type: ignore[no-untyped-def]
            if value is not None and _ENCRYPTED_DATA_MARKER not in value:
                if not self._warned_unencrypted:
                    self._warned_unencrypted = True
                    logger.warning(
                        "Discarding unencrypted entries from the OAuth store "
                        "(logged once). Either they predate encryption — purge "
                        "them, see DEPLOY.md — or something else is writing to "
                        "this Redis."
                    )
                return None
            return super()._decrypt_value(value)

    return StrictFernetEncryptionWrapper


def _build_client_storage() -> tuple[object | None, str]:
    """Return `(storage, description)` for the OAuth proxy's DCR client store.

    With `MCP_REDIS_URL` set, builds a Redis-backed store. Without the env
    var, falls back to FastMCP's default in-container file store (ephemeral
    — every restart forces clients to reconnect).

    The returned store is `RedisStore` wrapped twice, and both wrappers
    exist because supplying `client_storage` at all opts out of what
    FastMCP would otherwise have done for you:

    1. `PrefixCollectionsWrapper` — the store's own `default_collection` is
       inert. FastMCP constructs each `PydanticAdapter` with its *own*
       collection name (`mcp-oauth-proxy-clients`, `mcp-refresh-tokens`, …)
       and the adapter resolves `collection or self._default_collection`
       against that, never consulting the store's. Those names are
       identical across every FastMCP OAuth-proxy server, so without a
       prefix two of them sharing a Redis DB land in one keyspace and can
       resolve each other's registrations and JTIs. The prefix is what
       makes a single shared Redis instance safe.
    2. `FernetEncryptionWrapper` — FastMCP applies its own only when
       `client_storage is None`, so a supplied store silently persisted
       `UpstreamTokenSet` (the live upstream access + refresh tokens) as
       plaintext JSON. We re-apply it via the wrapper's own
       `source_material=`/`salt=` overload, keyed off `MCP_JWT_SIGNING_KEY`.
       (FastMCP's *own* refresh tokens were never the exposure — only
       their SHA-256 is stored, as a key. The upstream ones were.)
       This buys **confidentiality only**: there's no integrity check, and
       collection and key names stay plaintext.

    Uses `redis.asyncio.Redis.from_url` because `RedisStore(url=...)` in
    `py-key-value-aio` 0.4.4 ignores TLS on `rediss://` URLs. Upstash
    requires TLS so we construct the client manually.

    `decode_responses=True` is required: `RedisStore._get_managed_entry`
    rejects non-`str` responses and `Redis.from_url` defaults to False.

    **`health_check_interval` is the load-bearing one.** Going through
    `Redis.from_url` → `ConnectionPool.from_url` → `Connection.__init__`
    with no `retry`/`retry_on_error` yields `Retry(NoBackoff(), 0)` and
    `health_check_interval=0` — no liveness check and zero retries. (The
    `retries=10` default on `Redis.__init__` never applies on this path;
    verify with `pool.make_connection().retry.get_retries()` before
    "simplifying" any of this away.) A managed Redis such as Upstash drops
    idle TLS connections, so the next pooled use of a dead socket raises
    `ConnectionError: Error UNKNOWN while writing to socket. Connection
    lost.` from `send_packed_command`. Setting the interval makes redis-py
    ping a connection that has been idle longer than it before handing it
    out, which is what actually removes the error.

    Why it's worth caring about: when the upstream issues a refresh token,
    claude.ai periodically refreshes, and each refresh rotates the refresh
    token with one-time-use enforcement — the proxy deletes the *old*
    refresh JTI before persisting the new refresh metadata. A failure
    inside that sequence can leave the client holding a refresh token the
    server has no record of: a dead connector that only a manual reconnect
    fixes, which for an unattended cron/routine consumer is a silent
    outage.

    Don't quote a refresh cadence without checking which branch applies.
    The upstream's own `expires_in` wins whenever it is present; FastMCP's
    1h `DEFAULT_ACCESS_TOKEN_EXPIRY_SECONDS` is only the fallback for an
    IdP that omits it. A GitHub App with "Expire user authorization tokens"
    enabled — the GitHub config that issues refresh tokens — sends its own
    `expires_in` (8h at time of writing), so rotation there is a few times a
    day, not hourly. A classic OAuth App, or a GitHub App with expiration
    turned off, returns no refresh token, takes the no-refresh branch, and
    never rotates.

    Be precise about what the retry does and doesn't buy: it retries a
    *single* Redis command on a fresh connection. It does NOT make the
    delete-then-write rotation atomic — there is no transaction, so a
    process death or an outage longer than the retry budget still strands
    the connector. It lowers the odds of one command failing, nothing more.

    `socket_timeout` is set explicitly rather than inherited: redis-py 8
    defaults it to 5s, but the `redis>=5.0.0` floor permits versions that
    default to `None`, where a blackholed connection blocks `read_response`
    forever and `TimeoutError` never fires. `socket_keepalive` and
    `socket_connect_timeout` likewise match redis-py 8's own defaults —
    they're pinned so the guarantee doesn't drift across that floor, not
    because they change behavior today.
    """
    url = os.environ.get("MCP_REDIS_URL", "").strip()
    if not url:
        return None, "in-memory file (ephemeral; resets on container restart)"

    # Scheme policy first: a cleartext URL is a config error whether or not
    # the extras happen to be installed. See `redis_client.validate_redis_url`.
    validate_redis_url(url)

    # Imports are inside the function so the base install doesn't need
    # redis + py-key-value-aio. Those are part of the [oauth] extras.
    try:
        from key_value.aio.stores.redis import RedisStore
        from key_value.aio.wrappers.prefix_collections import (
            PrefixCollectionsWrapper,
        )
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "MCP_REDIS_URL is set but the [oauth] extras aren't installed. "
            "Re-install with `uv sync --extra oauth` (or `pip install "
            "kalshi-mcp-server[oauth]`)."
        ) from exc

    # Check config BEFORE constructing the client, so a misconfig doesn't
    # build a pool it immediately discards.
    signing_key = os.environ.get("MCP_JWT_SIGNING_KEY", "").strip()
    if not signing_key:
        raise ConfigError(
            "MCP_REDIS_URL is set but MCP_JWT_SIGNING_KEY is not. That "
            "combination is already broken before encryption enters the "
            "picture: without a stable signing key FastMCP generates one per "
            "process, so every restart invalidates all issued tokens and the "
            "persistent store buys you nothing. It also leaves no stable "
            "secret to derive a storage-encryption key from. Set "
            'MCP_JWT_SIGNING_KEY (e.g. `python -c "import secrets; '
            'print(secrets.token_urlsafe(64))"`) or unset MCP_REDIS_URL.'
        )

    # Warn, don't fail — a short key still works, it's just weak. This string
    # is now the *only* thing protecting the upstream OAuth tokens at rest, so
    # a low-entropy value undoes the PBKDF2 stretching below. FastMCP warns
    # below 12; the recommended value is token_urlsafe(64) ≈ 86 chars.
    if len(signing_key) < MIN_SIGNING_KEY_LENGTH:
        logger.warning(
            "MCP_JWT_SIGNING_KEY is only %d characters. It is the sole "
            "protection for the OAuth tokens encrypted in Redis — a "
            "low-entropy value is brute-forceable offline by anyone who can "
            "read that Redis. Use at least %d (recommended: "
            "`secrets.token_urlsafe(64)`).",
            len(signing_key),
            MIN_SIGNING_KEY_LENGTH,
        )

    # Shared hardened client (health check, timeouts, jittered retry) — see
    # `redis_client.build_redis_client` and the docstring above.
    client = build_redis_client(url)
    # Encrypt at rest, and namespace the collections. See the docstring for
    # why both are needed — supplying `client_storage` opts out of the
    # equivalents FastMCP would have applied itself.
    #
    # `source_material=` (not the `fernet=` overload) so the library derives
    # the key itself: it runs PBKDF2-HMAC-SHA256 at 1,200,000 iterations
    # (`wrappers/encryption/fernet.py:_generate_encryption_key`). That
    # stretching is the point — MCP_JWT_SIGNING_KEY is an operator-supplied
    # string, and deriving straight from it with a single hash would leave a
    # weak key's storage encryption brute-forceable offline by anyone with
    # Redis read access. Using the library's own overload also keeps us off
    # FastMCP's internal `jwt_issuer.derive_jwt_key`, which is unexported and
    # could move on any minor release above our `fastmcp>=3.2.4` floor.
    #
    # `raise_on_decryption_error=False` makes a key rotation degrade to a
    # cache miss (clients re-register) instead of a hard failure — matching
    # both FastMCP's own behavior and DEPLOY.md's documented rotation story
    # ("clients see a single reconnect prompt then are back").
    return (
        _strict_encryption_wrapper_class()(
            key_value=PrefixCollectionsWrapper(
                key_value=RedisStore(client=client),
                prefix=_collection_prefix(),
            ),
            source_material=signing_key,
            salt=STORAGE_ENCRYPTION_SALT,
            raise_on_decryption_error=False,
        ),
        "redis (persistent, encrypted, prefixed)",
    )


# Env vars that together enable the OAuth proxy. All three or none: a
# partial set is an operator mistake, and the old "silently return None"
# behavior turned it into an unauthenticated server (cli.py then fell into
# the insecure-HTTP branch, or refused with a misleading message).
OAUTH_CORE_ENV_VARS = ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "MCP_BASE_URL")
# Identity allowlists. Only meaningful once OAuth is on.
ALLOWLIST_ENV_VARS = ("MCP_ALLOWED_GITHUB_LOGINS", "MCP_ALLOWED_GITHUB_IDS")
OAUTH_ENV_VARS = OAUTH_CORE_ENV_VARS + ALLOWLIST_ENV_VARS

# JSON-RPC error code for a refused (non-tool) request. JSON-RPC reserves
# -32000..-32099 for implementation-defined server errors; the MCP SDK uses
# -32000 (connection closed) and -32042 (URL elicitation), so -32003 is free.
# Tool calls are refused with a ToolError instead (see the middleware).
FORBIDDEN_ERROR_CODE = -32003


def _env_set(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def build_auth_provider(*, strict: bool = True) -> tuple[GitHubProvider | None, str]:
    """Construct the GitHub OAuth provider from env vars, or `(None, '')`.

    Returns `(provider, storage_description)`. The description is logged
    by the CLI on startup so the chosen storage backend (memory vs
    Redis) is visible in Render's log stream.

    Required env vars (all three must be set to enable OAuth):
    - `GITHUB_CLIENT_ID`     — OAuth App client ID
    - `GITHUB_CLIENT_SECRET` — OAuth App client secret
    - `MCP_BASE_URL`         — public URL of this server (no trailing slash)

    Setting one or two of them raises `ConfigError` (fail closed): a
    half-configured proxy is a mistake, not a request for no auth. With
    `strict=False` (stdio, which never uses OAuth) a partial set only logs
    a warning and returns `(None, '')` — `GITHUB_CLIENT_ID` is a generic
    name, and the CLI auto-loads `./.env`, so an unrelated value in a local
    env file must not take the stdio server down.

    Optional:
    - `MCP_JWT_SIGNING_KEY`  — stable signing key for proxy-issued JWTs.
      Without this, a fresh key is generated each process start and
      previously-issued tokens are invalidated on restart. **Required**
      (not optional) when `MCP_REDIS_URL` is set — see
      `_build_client_storage`, which raises `ConfigError` without it.
    - `MCP_REDIS_URL`        — `rediss://...` for persistent DCR client
      storage (Upstash works well for this).
    - `MCP_REDIS_COLLECTION_PREFIX` — namespace for this deployment's
      collections when several servers share one Redis (default `kalshi`).
    """
    present = [name for name in OAUTH_CORE_ENV_VARS if _env_set(name)]
    if not present:
        return None, ""
    if len(present) != len(OAUTH_CORE_ENV_VARS):
        missing = [name for name in OAUTH_CORE_ENV_VARS if name not in present]
        detail = (
            f"Partial OAuth configuration: {', '.join(present)} set but "
            f"{', '.join(missing)} missing. Set all of {', '.join(OAUTH_CORE_ENV_VARS)} "
            "to enable the GitHub OAuth proxy, or none of them."
        )
        if not strict:
            # Count only, no var names: CodeQL's sensitive-name heuristic
            # treats the literal "GITHUB_CLIENT_SECRET" as secret data.
            logger.warning(
                "Partial OAuth configuration (%d of %d GitHub OAuth vars set). "
                "Ignored: OAuth is not used on this transport.",
                len(present),
                len(OAUTH_CORE_ENV_VARS),
            )
            return None, ""
        raise ConfigError(
            f"{detail} Refusing to start rather than silently running without authentication."
        )

    client_id = os.environ["GITHUB_CLIENT_ID"].strip()
    client_secret = os.environ["GITHUB_CLIENT_SECRET"].strip()
    base_url = os.environ["MCP_BASE_URL"].strip()

    storage, storage_desc = _build_client_storage()

    return (
        GitHubProvider(
            client_id=client_id,
            client_secret=client_secret,
            base_url=base_url.rstrip("/"),
            # Stripped, matching `_build_client_storage` — otherwise stray
            # whitespace in the env var derives the storage key from a
            # different string than the JWT key.
            jwt_signing_key=os.environ.get("MCP_JWT_SIGNING_KEY", "").strip() or None,
            client_storage=storage,
        ),
        storage_desc,
    )


def _parse_allowed_logins() -> frozenset[str]:
    raw = os.environ.get("MCP_ALLOWED_GITHUB_LOGINS", "").strip()
    return frozenset(s.strip().lower() for s in raw.split(",") if s.strip())


def _parse_allowed_ids() -> frozenset[str]:
    """Parse `MCP_ALLOWED_GITHUB_IDS` — comma-separated GitHub numeric user ids.

    Kept as strings because that's how they arrive in the token: FastMCP's
    `GitHubTokenVerifier` sets `claims["sub"] = str(user["id"])` from the
    GitHub `/user` response. A non-numeric entry raises `ConfigError` rather
    than being skipped — someone who pasted a login into the ID list should
    find out at boot, not when they're locked out (or, worse, when a
    lookalike entry quietly matches nothing).
    """
    raw = os.environ.get("MCP_ALLOWED_GITHUB_IDS", "").strip()
    ids: set[str] = set()
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        if not (value.isascii() and value.isdigit()):
            raise ConfigError(
                f"MCP_ALLOWED_GITHUB_IDS entry {value!r} is not a numeric GitHub "
                "user id. Find yours with `gh api user --jq .id` (or "
                "https://api.github.com/users/<login>); put logins in "
                "MCP_ALLOWED_GITHUB_LOGINS instead."
            )
        # Normalize away leading zeros so "0123" and "123" agree — GitHub
        # never emits them, so this can only widen a typo into the id the
        # operator evidently meant.
        ids.add(str(int(value)))
    return frozenset(ids)


class RestrictGitHubUsersMiddleware(Middleware):
    """Reject every MCP request from GitHub users outside the allowlist.

    The OAuth proxy will happily issue a token to any GitHub account that
    completes the flow. This middleware is the authorization half: a caller
    passes if their GitHub numeric id (the token's `sub` claim) is in
    `MCP_ALLOWED_GITHUB_IDS` **or** their login (the `login` claim) is in
    `MCP_ALLOWED_GITHUB_LOGINS`. Ids are preferred — a login can be renamed
    and the freed name re-registered by someone else, an id never changes.

    It hooks `on_request`, not just `on_call_tool`. A tool-only check let any
    GitHub user `resources/read` `kalshi://balance` / `positions` / `orders`
    / `environment` and enumerate the tool + resource surface. `on_request`
    wraps every client request FastMCP routes through middleware
    (`initialize`, `tools/*`, `resources/*`, `prompts/*`), so a new resource
    or request type is covered by default instead of needing its own hook.
    (`ping` isn't routed through middleware; it carries no data.)

    Refusals: a `tools/call` raises `ToolError`, which reaches the model as an
    ordinary tool error; everything else raises `McpError`, the JSON-RPC
    error type FastMCP passes through verbatim (including on `initialize`).

    Only install this on HTTP transport. On stdio there's no access token at
    all (the MCP client *is* the operator), so it would refuse everything.
    """

    def __init__(
        self,
        allowed_logins: frozenset[str],
        allowed_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._allowed_logins = frozenset(login.lower() for login in allowed_logins)
        self._allowed_ids = frozenset(allowed_ids)

    def _refusal_reason(self) -> str | None:
        """Return why the current caller is refused, or None if allowed."""
        if not self._allowed_logins and not self._allowed_ids:
            # Fail closed. cli.py refuses to start in this state; this guards
            # a direct construction that skipped that check.
            return (
                "Server misconfigured: neither MCP_ALLOWED_GITHUB_IDS nor "
                "MCP_ALLOWED_GITHUB_LOGINS is set"
            )
        token = get_access_token()
        if token is None:
            return "Unauthenticated"
        claims = token.claims or {}
        user_id = str(claims.get("sub") or "").strip()
        login = str(claims.get("login") or "").strip().lower()
        if user_id and user_id in self._allowed_ids:
            return None
        if login and login in self._allowed_logins:
            return None
        who = login or "(unknown)"
        if user_id:
            who = f"{who} (id {user_id})"
        return f"GitHub user {who} is not authorized"

    async def on_request(self, context: MiddlewareContext, call_next):
        reason = self._refusal_reason()
        if reason is None:
            return await call_next(context)
        logger.warning("Refused MCP %s: %s", context.method or "request", reason)
        if context.method == "tools/call":
            raise ToolError(reason)
        raise McpError(ErrorData(code=FORBIDDEN_ERROR_CODE, message=reason))


def build_user_restriction_middleware() -> RestrictGitHubUsersMiddleware | None:
    """Return the allowlist middleware if either allowlist is set, else None.

    Raises `ConfigError` for a malformed `MCP_ALLOWED_GITHUB_IDS`.
    """
    logins = _parse_allowed_logins()
    ids = _parse_allowed_ids()
    if not logins and not ids:
        return None
    return RestrictGitHubUsersMiddleware(logins, ids)
