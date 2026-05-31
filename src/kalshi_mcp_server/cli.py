"""CLI entrypoint — boots the FastMCP server with stdio or http transport.

Tool registration is intentionally minimal at this stage. As tools land
under `kalshi_mcp_server/tools/`, they're registered here.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
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
        safety = SafetyController(config)
        # Default to Basic-tier limits. Once we expose
        # `kalshi_get_api_limits`, the server can query /account/limits at
        # boot and swap to live values — for now Basic is safest because
        # it under-promises (rate limiter blocks before we'd hit Kalshi's
        # real ceiling).
        rate_limiter = KalshiRateLimiter(TierLimits.basic())
        client = KalshiClient(
            config=config,
            signer=signer,
            rate_limiter=rate_limiter,
        )
    except KalshiMCPError as exc:
        sys.stderr.write(f"\nStartup failed: {exc}\n\n")
        return 2

    # Lazy import so config errors surface before pulling in the framework.
    try:
        from fastmcp import FastMCP
    except ImportError:
        sys.stderr.write("\nfastmcp is not installed. Run `uv sync` to install dependencies.\n\n")
        return 1

    # Build the OAuth proxy if env vars are present. Stdio doesn't need
    # auth (the MCP client *is* the operator). HTTP transport without
    # OAuth is allowed locally but refused on a non-localhost bind — see
    # _enforce_http_auth_policy below.
    from kalshi_mcp_server.oauth import (
        build_auth_provider,
        build_user_restriction_middleware,
    )

    auth_provider, storage_desc = build_auth_provider()
    if auth_provider is not None:
        logger.info("OAuth: GitHub proxy enabled — DCR client storage: %s", storage_desc)
    if transport == "http":
        _enforce_http_auth_policy(auth_provider)

    server = FastMCP(
        name="kalshi-mcp-server",
        instructions=(
            "Kalshi prediction-markets server. "
            f"Connected to {config.env.upper()}. "
            f"Trading {'enabled' if config.trading_enabled else 'DISABLED (read-only)'}."
        ),
        auth=auth_provider,
    )

    user_restrict = build_user_restriction_middleware()
    if user_restrict is not None:
        server.add_middleware(user_restrict)
        logger.info("OAuth: tool calls restricted to GitHub logins in MCP_ALLOWED_GITHUB_LOGINS")

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
        server.run(transport="http", port=port)
    return 0


def _enforce_http_auth_policy(auth_provider) -> None:
    """Fail closed when --transport http is used without OAuth configured.

    HTTP exposes the server's tool surface over a network — without OAuth
    + an allowlist, anyone who can reach the port can place trades. This
    check refuses to start the server unless either:

    - OAuth is configured (GITHUB_CLIENT_ID/SECRET + MCP_BASE_URL +
      MCP_ALLOWED_GITHUB_LOGINS), OR
    - The operator explicitly opts out via MCP_ALLOW_INSECURE_HTTP=1
      (use case: local dev where you're hitting localhost yourself).
    """
    if auth_provider is not None:
        # If OAuth is enabled, the allowlist is also required (otherwise
        # any GitHub user could call tools — see RestrictGitHubUsersMiddleware).
        if not os.environ.get("MCP_ALLOWED_GITHUB_LOGINS", "").strip():
            raise ConfigError(
                "OAuth is configured but MCP_ALLOWED_GITHUB_LOGINS is empty. "
                "Set it to your GitHub login (comma-separated for multiple) "
                "to lock tool calls to specific users."
            )
        return

    if os.environ.get("MCP_ALLOW_INSECURE_HTTP", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        logger.warning(
            "HTTP transport without OAuth — MCP_ALLOW_INSECURE_HTTP is set. "
            "DO NOT use this configuration for any non-localhost deployment."
        )
        return

    raise ConfigError(
        "HTTP transport requires OAuth configuration. Set GITHUB_CLIENT_ID, "
        "GITHUB_CLIENT_SECRET, MCP_BASE_URL, and MCP_ALLOWED_GITHUB_LOGINS — "
        "or set MCP_ALLOW_INSECURE_HTTP=1 if this is local dev only. "
        "See DEPLOY.md for the production deployment guide."
    )


if __name__ == "__main__":
    raise SystemExit(main())
