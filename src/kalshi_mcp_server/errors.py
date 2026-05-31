"""Error hierarchy for the Kalshi MCP server.

Callers (tool implementations, the CLI) catch these to produce structured
MCP error responses instead of leaking tracebacks to the model.
"""

from __future__ import annotations


class KalshiMCPError(Exception):
    """Base class for all server-raised errors."""


class ConfigError(KalshiMCPError):
    """Raised when env config is missing, invalid, or violates a safety guard."""


class AuthError(KalshiMCPError):
    """Raised when signing or key loading fails."""


class KalshiAPIError(KalshiMCPError):
    """Raised when the Kalshi REST API returns an error response.

    Carries the HTTP status and (best-effort) parsed error body so that
    tool handlers can map specific Kalshi errors into helpful MCP responses.
    """

    def __init__(self, status: int, message: str, body: dict | None = None) -> None:
        super().__init__(f"Kalshi API {status}: {message}")
        self.status = status
        self.message = message
        self.body = body or {}


class RateLimitError(KalshiAPIError):
    """Raised on HTTP 429 from Kalshi or when local token bucket is empty."""


class SafetyError(KalshiMCPError):
    """Raised when a request violates a server-side safety control.

    Distinct from KalshiAPIError because this fires BEFORE we hit the wire,
    on purpose — we refuse the trade locally rather than relying on Kalshi
    to reject it.
    """


class TradingDisabledError(SafetyError):
    """Raised when a write tool is invoked but KALSHI_TRADING_ENABLED != 1."""
