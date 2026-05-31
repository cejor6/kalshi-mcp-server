"""CLI entrypoint — boots the FastMCP server with stdio or http transport.

Tool registration is intentionally minimal at this stage. As tools land
under `kalshi_mcp_server/tools/`, they're registered here.
"""

from __future__ import annotations

import argparse
import logging
import sys

from kalshi_mcp_server import __version__
from kalshi_mcp_server.auth import KalshiSigner
from kalshi_mcp_server.config import Config
from kalshi_mcp_server.errors import ConfigError, KalshiMCPError
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
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
    except KalshiMCPError as exc:
        sys.stderr.write(f"\nStartup failed: {exc}\n\n")
        return 2

    # Lazy import so config errors surface before pulling in the framework.
    try:
        from fastmcp import FastMCP
    except ImportError:
        sys.stderr.write("\nfastmcp is not installed. Run `uv sync` to install dependencies.\n\n")
        return 1

    server = FastMCP(
        name="kalshi-mcp-server",
        instructions=(
            "Kalshi prediction-markets server. "
            f"Connected to {config.env.upper()}. "
            f"Trading {'enabled' if config.trading_enabled else 'DISABLED (read-only)'}."
        ),
    )

    # Hold references so they aren't GC'd, and so tool modules can import them.
    server._kalshi_signer = signer  # type: ignore[attr-defined]
    server._kalshi_config = config  # type: ignore[attr-defined]
    server._kalshi_safety = safety  # type: ignore[attr-defined]

    # Register tools (none yet — landing in subsequent commits).
    from kalshi_mcp_server.tools import register_all_tools

    register_all_tools(server)

    logger.info("Transport: %s", transport)
    if transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport="http", port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
