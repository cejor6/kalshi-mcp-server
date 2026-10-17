"""CLI entrypoint — boots the FastMCP server with stdio or http transport.

Tool registration is intentionally minimal at this stage. As tools land
under `kalshi_mcp_server/tools/`, they're registered here.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

from kalshi_mcp_server import __version__
from kalshi_mcp_server.auth import KalshiSigner
from kalshi_mcp_server.client import KalshiClient
from kalshi_mcp_server.config import Config
from kalshi_mcp_server.errors import ConfigError, KalshiMCPError
from kalshi_mcp_server.rate_limit import KalshiRateLimiter, TierLimits
from kalshi_mcp_server.safety import SafetyController

logger = logging.getLogger("kalshi_mcp_server")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )


def _build_signer(config: Config) -> KalshiSigner:
    return KalshiSigner.from_env(
        key_id=config.key_id,
        pem_path=config.private_key_path,
        pem_text=config.private_key_pem,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kalshi-mcp",
        description="Model Context Protocol server for Kalshi prediction markets.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"kalshi-mcp-server {__version__}",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=None,
        help="Transport to use. Defaults to MCP_TRANSPORT env or stdio.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port when running with --transport http. Defaults to PORT env or 8000.",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help=(
            "Host to bind on with --transport http. Defaults to 127.0.0.1 "
            "(safe for local dev). The published Docker image overrides "
            "this to 0.0.0.0 via CMD so containerized deployments work "
            "without any extra config."
        ),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            "Path to a .env file to load before reading config. If omitted, "
            "we still look for a `.env` in the current working directory; "
            "set --env-file explicitly when launching from an MCP client "
            "(Claude Desktop, etc.) so secrets live in a file instead of "
            "being inlined into the MCP config JSON."
        ),
    )
    return parser.parse_args(argv)


def _load_env_file(env_file: Path | None) -> None:
    """Load a .env file before resolving config.

    - If --env-file was given, load exactly that path (error if missing).
    - Otherwise, look for a `.env` in CWD and load it if present (no error
      if absent — running with all env vars already exported is fine).

    Values already in the environment win over .env entries (override=False).
    """
    if env_file is not None:
        if not env_file.exists():
            raise ConfigError(f"--env-file path does not exist: {env_file}")
        load_dotenv(env_file, override=False)
        logger.info("Loaded env from %s", env_file)
        return

    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        load_dotenv(cwd_env, override=False)
        logger.info("Loaded env from %s", cwd_env)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Logging is configured AFTER env load so LOG_LEVEL from .env applies,
    # but we want to capture the "loaded env from X" message — so set up a
    # minimal handler first, then re-configure once we know the level.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True)

    try:
        _load_env_file(args.env_file)
        config = Config.from_env()
    except ConfigError as exc:
        sys.stderr.write(f"\nConfig error: {exc}\n\n")
        return 2

    _setup_logging(config.log_level)
    logger.info(
        "Starting kalshi-mcp-server %s (env=%s, trading_enabled=%s)",
        __version__,
        config.env,
        config.trading_enabled,
    )
    if config.env == "prod":
        logger.warning(
            "PROD MODE — orders will hit real markets. Trading enabled: %s.",
            config.trading_enabled,
        )

    transport = args.transport or config.transport
    port = args.port or config.port

    try:
        signer = _build_signer(config)
        # Runtime safety-limit overrides persist via this store when
        # MCP_REDIS_URL is set; otherwise they're in-memory (reset on
        # restart). Building the store is non-fatal — an unreachable Redis
        # surfaces later, at load/save time.
        from kalshi_mcp_server.safety_store import build_limits_store

        limits_store, limits_store_desc = build_limits_store()
        safety = SafetyController(config, store=limits_store)
        logger.info("Safety-limit override store: %s", limits_store_desc)
        # Start with Basic-tier defaults; the lifespan hook below queries
        # /account/limits at boot and reconfigures the limiter to the
        # real numbers Kalshi reports. Basic is the safer fallback if
        # that call fails — it under-promises (blocks earlier than the
        # real Kalshi ceiling).
        rate_limiter = KalshiRateLimiter(TierLimits.basic())
        client = KalshiClient(
            config=config,
            signer=signer,
            rate_limiter=rate_limiter,
        )
    except KalshiMCPError as exc:
        sys.stderr.write(f"\nStartup failed: {exc}\n\n")
        return 2

    @asynccontextmanager
    async def _kalshi_lifespan(_server):
        """Hydrate the rate limiter from /account/limits at startup.

        Kalshi tier limits depend on the account, so the hardcoded Basic
        defaults are conservative for accounts that have been upgraded.
        Calling /account/limits once at boot replaces the buckets with
        the real numbers. If the call fails (network glitch, brief auth
        hiccup), we keep the Basic defaults — the server still works,
        just on the conservative budget.

        Also restores any persisted runtime safety-limit override (clamped
        to the env ceilings). If the override store is unreachable we boot
        at the env ceilings and warn — availability is not coupled to the
        optional persistence backend, and the ceiling is always the loosest
        allowed state, so this fails safe.
        """
        try:
            effective = await safety.load_persisted()
            if effective != safety.ceilings:
                logger.info(
                    "Restored persisted runtime safety limits (clamped to env ceilings): %s",
                    effective.as_dict(),
                )
        except Exception as exc:
            logger.warning(
                "Could not load persisted safety limits — booting at env ceilings. (%s)",
                exc,
            )

        try:
            limits = await client.get("/account/limits")
            read = limits.get("read", {}) or {}
            write = limits.get("write", {}) or {}
            read_capacity = read.get("bucket_capacity")
            write_capacity = write.get("bucket_capacity")
            if read_capacity and write_capacity:
                tier = TierLimits(
                    read_capacity=float(read_capacity),
                    read_refill=float(read.get("refill_rate") or read_capacity),
                    write_capacity=float(write_capacity),
                    write_refill=float(write.get("refill_rate") or write_capacity),
                )
                rate_limiter.reconfigure(tier)
                logger.info(
                    "Rate limiter hydrated from /account/limits: tier=%s "
                    "read=%g cap @ %g/s, write=%g cap @ %g/s",
                    limits.get("usage_tier", "?"),
                    tier.read_capacity,
                    tier.read_refill,
                    tier.write_capacity,
                    tier.write_refill,
                )
        except Exception as exc:
            logger.warning(
                "Could not hydrate rate limits from /account/limits — "
                "keeping Basic-tier defaults. (%s)",
                exc,
            )
        yield

    # Lazy import so config errors surface before pulling in the framework.
    try:
        from fastmcp import FastMCP
    except ImportError:
        sys.stderr.write("\nfastmcp is not installed. Run `uv sync` to install dependencies.\n\n")
        return 1

    # Build the OAuth proxy if env vars are present, then apply the
    # transport's security policy. Stdio doesn't need auth (the MCP client
    # *is* the operator). HTTP needs OAuth + an identity allowlist, or an
    # explicit MCP_ALLOW_INSECURE_HTTP=1 opt-out — which in turn is confined
    # to a loopback bind and gets DNS-rebinding protection. Every refusal
    # below is a ConfigError, caught here so it prints the same clean
    # startup-failure line as every other config problem instead of a raw
    # traceback.
    try:
        auth_provider, storage_desc, user_restrict, http_middleware = _build_transport_security(
            transport, args.host
        )
    except KalshiMCPError as exc:
        sys.stderr.write(f"\nStartup failed: {exc}\n\n")
        return 2
    if auth_provider is not None:
        logger.info("OAuth: GitHub proxy enabled — DCR client storage: %s", storage_desc)

    server = FastMCP(
        name="kalshi-mcp-server",
        instructions=(
            "Kalshi prediction-markets server. "
            f"Connected to {config.env.upper()}. "
            f"Trading {'enabled' if config.trading_enabled else 'DISABLED (read-only)'}."
        ),
        auth=auth_provider,
        lifespan=_kalshi_lifespan,
    )

    if user_restrict is not None:
        server.add_middleware(user_restrict)
        logger.info(
            "OAuth: all MCP requests restricted to MCP_ALLOWED_GITHUB_IDS / "
            "MCP_ALLOWED_GITHUB_LOGINS"
        )

    # Hold references so they aren't GC'd, and so tool modules can import them.
    server._kalshi_signer = signer  # type: ignore[attr-defined]
    server._kalshi_config = config  # type: ignore[attr-defined]
    server._kalshi_safety = safety  # type: ignore[attr-defined]
    server._kalshi_rate_limiter = rate_limiter  # type: ignore[attr-defined]
    server._kalshi_client = client  # type: ignore[attr-defined]

    from kalshi_mcp_server.resources import register_all_resources
    from kalshi_mcp_server.tools import register_all_tools

    register_all_tools(server)
    register_all_resources(server)

    logger.info("Transport: %s", transport)
    if transport == "stdio":
        server.run(transport="stdio")
    else:
        logger.info("HTTP bind: %s:%s", args.host, port)
        server.run(
            transport="http",
            host=args.host,
            port=port,
            middleware=http_middleware or None,
        )
    return 0


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _build_transport_security(transport: str, bind_host: str):
    """Resolve everything auth-related for this transport, or raise ConfigError.

    Returns `(auth_provider, storage_desc, user_restriction_middleware,
    http_asgi_middleware)`:

    - On HTTP a partial OAuth config refuses to start. On stdio (which never
      uses OAuth) it only warns: `GITHUB_CLIENT_ID` is a generic name and the
      CLI auto-loads `./.env`, so an unrelated value must not break stdio.
    - The GitHub allowlist middleware is installed **only for HTTP + OAuth**.
      On stdio `get_access_token()` is always None, so installing it there
      would refuse every local request whenever the env file happened to
      carry `MCP_ALLOWED_GITHUB_*`.
    - The Host/Origin (DNS-rebinding) guard is installed only for HTTP
      *without* OAuth — see `http_guard.py` for why it's scoped that way.
    """
    from kalshi_mcp_server.oauth import (
        build_auth_provider,
        build_user_restriction_middleware,
    )

    if transport == "stdio":
        auth_provider, storage_desc = build_auth_provider(strict=False)
        return auth_provider, storage_desc, None, []

    auth_provider, storage_desc = build_auth_provider()

    _enforce_http_auth_policy(auth_provider, bind_host=bind_host)

    if auth_provider is not None:
        user_restrict = build_user_restriction_middleware()
        if user_restrict is None:  # pragma: no cover — policy guarantees a list
            raise ConfigError("OAuth is configured but no GitHub allowlist is set.")
        return auth_provider, storage_desc, user_restrict, []

    from starlette.middleware import Middleware as ASGIMiddleware

    from kalshi_mcp_server.http_guard import HostOriginGuard, parse_allowed_hosts_env

    extra_hosts = parse_allowed_hosts_env()
    logger.info(
        "Insecure HTTP: Host/Origin restricted to loopback%s",
        f" + {', '.join(sorted(extra_hosts))}" if extra_hosts else "",
    )
    return (
        None,
        storage_desc,
        None,
        [ASGIMiddleware(HostOriginGuard, allowed_hosts=extra_hosts)],
    )


def _enforce_http_auth_policy(auth_provider, bind_host: str = "127.0.0.1") -> None:
    """Fail closed when --transport http isn't safely configured.

    HTTP exposes the server's tool surface over a network — without OAuth
    + an allowlist, anyone who can reach the port can read the account and
    place trades. This check refuses to start the server unless either:

    - OAuth is configured (GITHUB_CLIENT_ID/SECRET + MCP_BASE_URL) AND at
      least one of MCP_ALLOWED_GITHUB_IDS / MCP_ALLOWED_GITHUB_LOGINS is set
      (malformed ids also refuse — parsed here so the error is at boot), OR
    - The operator explicitly opts out via MCP_ALLOW_INSECURE_HTTP=1 with no
      core OAuth var set, on a loopback bind — or a non-loopback bind with
      the second opt-in MCP_ALLOW_INSECURE_PUBLIC_BIND=1.

    A fully configured OAuth deploy that still carries the insecure flag
    (left over from DEPLOY.md's Phase A) is NOT refused — refusing would turn
    the next redeploy of a working prod service into an outage. OAuth wins,
    the flag is ignored, and a warning says so. A *partial* OAuth set plus
    the flag still refuses (`build_auth_provider` raises before we get here;
    the check below covers direct callers).
    """
    from kalshi_mcp_server.http_guard import is_loopback_bind
    from kalshi_mcp_server.oauth import (
        ALLOWLIST_ENV_VARS,
        OAUTH_CORE_ENV_VARS,
        _parse_allowed_ids,
        _parse_allowed_logins,
    )

    insecure = _env_flag("MCP_ALLOW_INSECURE_HTTP")

    if auth_provider is not None:
        if insecure:
            logger.warning(
                "MCP_ALLOW_INSECURE_HTTP=1 is IGNORED because OAuth is configured — "
                "the server runs with OAuth + the GitHub allowlist. Delete the "
                "variable (DEPLOY.md Phase B) so this config isn't misread later."
            )
        # If OAuth is enabled, an allowlist is also required (otherwise any
        # GitHub user could use the server — see RestrictGitHubUsersMiddleware).
        if not _parse_allowed_ids() and not _parse_allowed_logins():
            raise ConfigError(
                "OAuth is configured but no GitHub allowlist is set. Set "
                "MCP_ALLOWED_GITHUB_IDS (preferred — numeric ids never change; "
                "find yours with `gh api user --jq .id`) and/or "
                "MCP_ALLOWED_GITHUB_LOGINS (comma-separated) to lock the server "
                "to specific users."
            )
        return

    if not insecure:
        raise ConfigError(
            "HTTP transport requires OAuth configuration. Set GITHUB_CLIENT_ID, "
            "GITHUB_CLIENT_SECRET, MCP_BASE_URL, and MCP_ALLOWED_GITHUB_IDS or "
            "MCP_ALLOWED_GITHUB_LOGINS — or set MCP_ALLOW_INSECURE_HTTP=1 if this "
            "is local dev only. See DEPLOY.md for the production deployment guide."
        )

    core_set = [n for n in OAUTH_CORE_ENV_VARS if os.environ.get(n, "").strip()]
    if core_set:
        raise ConfigError(
            "MCP_ALLOW_INSECURE_HTTP=1 is set alongside a partial OAuth "
            f"configuration ({', '.join(core_set)}). Set all of "
            f"{', '.join(OAUTH_CORE_ENV_VARS)} to run with OAuth, or unset "
            "them for a no-auth local/private-network server."
        )
    allowlists_set = [n for n in ALLOWLIST_ENV_VARS if os.environ.get(n, "").strip()]
    if allowlists_set:
        logger.warning(
            "%s set but NOT enforced: OAuth is off (MCP_ALLOW_INSECURE_HTTP=1), "
            "so there is no GitHub identity to check.",
            " / ".join(allowlists_set),
        )

    if not is_loopback_bind(bind_host):
        if not _env_flag("MCP_ALLOW_INSECURE_PUBLIC_BIND"):
            raise ConfigError(
                f"Refusing to serve unauthenticated HTTP on non-loopback host "
                f"{bind_host!r}: anyone who can reach that interface could read "
                "the account and place trades. Bind to 127.0.0.1, configure "
                "OAuth, or — only if something in front of this server (VPN, "
                "authenticating reverse proxy, cloud IAM) controls access — also "
                "set MCP_ALLOW_INSECURE_PUBLIC_BIND=1."
            )
        logger.warning(
            "UNAUTHENTICATED HTTP on non-loopback host %s "
            "(MCP_ALLOW_INSECURE_HTTP=1 + MCP_ALLOW_INSECURE_PUBLIC_BIND=1). "
            "Access control is entirely up to whatever sits in front of this "
            "server.",
            bind_host,
        )
        return

    logger.warning(
        "HTTP transport without OAuth — MCP_ALLOW_INSECURE_HTTP is set. "
        "DO NOT use this configuration for any non-localhost deployment."
    )


if __name__ == "__main__":
    raise SystemExit(main())
