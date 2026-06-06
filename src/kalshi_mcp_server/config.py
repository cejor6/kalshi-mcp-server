"""Environment-driven configuration with safety guards.

The configuration loader enforces two safety properties before the server
will start:

1. **Production opt-in.** Talking to Kalshi production requires both
   KALSHI_ENV=prod AND KALSHI_ALLOW_PROD=1. The second flag exists so
   that a typo or stale shell env can't accidentally route real money.

2. **Read-only default.** Order placement, cancellation, and amendment
   refuse to run unless KALSHI_TRADING_ENABLED=1. New deployments start
   read-only and the operator has to consciously enable trading.

Configurable optional safety limits (max order size, daily cap, etc.) are
held here and consulted by `safety.py` before any write is attempted.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Literal

from kalshi_mcp_server.errors import ConfigError

KalshiEnv = Literal["demo", "prod"]

PROD_REST_BASE = "https://api.elections.kalshi.com/trade-api/v2"
PROD_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

DEMO_REST_BASE = "https://demo-api.kalshi.co/trade-api/v2"
DEMO_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"Env var {name}={raw!r} is not a number.") from exc
    if not math.isfinite(value):
        # nan/inf would defeat the safety-limit comparisons downstream.
        raise ConfigError(f"Env var {name}={raw!r} must be a finite number.")
    return value


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Env var {name}={raw!r} is not an integer.") from exc


@dataclass(frozen=True)
class Config:
    """All resolved runtime configuration."""

    # Auth
    key_id: str
    private_key_path: str | None
    private_key_pem: str | None

    # Environment + safety gates
    env: KalshiEnv
    trading_enabled: bool

    # Endpoints (derived from env)
    rest_base: str
    ws_url: str

    # Safety limits
    max_order_size_usd: float
    daily_limit_usd: float
    max_contracts_per_order: int
    cash_reserve_usd: float

    # Runtime / transport
    transport: Literal["stdio", "http"]
    port: int
    log_level: str

    # When False, the kalshi_set_safety_limits operator tool is not
    # registered — limits can then only be changed via env var + redeploy.
    # Defaults True; set MCP_ALLOW_RUNTIME_LIMIT_TUNING=0 to disable (useful
    # on a shared HTTP deploy where allowlisted users shouldn't re-tune the
    # safety envelope). Has a default so existing constructors stay valid.
    runtime_limit_tuning_enabled: bool = True

    @classmethod
    def from_env(cls) -> Config:
        key_id = os.environ.get("KALSHI_API_KEY_ID", "").strip()
        if not key_id:
            raise ConfigError(
                "KALSHI_API_KEY_ID is required. Generate a key pair at "
                "https://kalshi.com/account/profile (or the demo profile page) "
                "and set the key ID via env var. See .env.example."
            )

        pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip() or None
        pem_text = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "").strip() or None
        if (pem_path is None) == (pem_text is None):
            raise ConfigError(
                "Provide exactly one of KALSHI_PRIVATE_KEY_PATH (filesystem path) "
                "or KALSHI_PRIVATE_KEY_PEM (inline PEM). See .env.example."
            )

        env_raw = os.environ.get("KALSHI_ENV", "demo").strip().lower()
        if env_raw not in {"demo", "prod"}:
            raise ConfigError(f"KALSHI_ENV must be 'demo' or 'prod', got {env_raw!r}.")
        env: KalshiEnv = env_raw  # type: ignore[assignment]

        if env == "prod" and not _get_bool("KALSHI_ALLOW_PROD"):
            raise ConfigError(
                "Refusing to start with KALSHI_ENV=prod unless KALSHI_ALLOW_PROD=1. "
                "This is intentional — set KALSHI_ENV=demo while developing, and only "
                "flip both flags when you genuinely intend to trade real money."
            )

        trading_enabled = _get_bool("KALSHI_TRADING_ENABLED", default=False)

        rest_base = PROD_REST_BASE if env == "prod" else DEMO_REST_BASE
        ws_url = PROD_WS_URL if env == "prod" else DEMO_WS_URL

        return cls(
            key_id=key_id,
            private_key_path=pem_path,
            private_key_pem=pem_text,
            env=env,
            trading_enabled=trading_enabled,
            rest_base=rest_base,
            ws_url=ws_url,
            max_order_size_usd=_get_float("MCP_MAX_ORDER_SIZE_USD", 25.0),
            daily_limit_usd=_get_float("MCP_DAILY_LIMIT_USD", 250.0),
            max_contracts_per_order=_get_int("MCP_MAX_CONTRACTS_PER_ORDER", 100),
            cash_reserve_usd=_get_float("MCP_CASH_RESERVE_USD", 0.0),
            transport=_resolve_transport(),
            port=_get_int("PORT", 8000),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            runtime_limit_tuning_enabled=_get_bool("MCP_ALLOW_RUNTIME_LIMIT_TUNING", default=True),
        )


def _resolve_transport() -> Literal["stdio", "http"]:
    raw = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    if raw not in {"stdio", "http"}:
        raise ConfigError(f"MCP_TRANSPORT must be 'stdio' or 'http', got {raw!r}.")
    return raw  # type: ignore[return-value]
