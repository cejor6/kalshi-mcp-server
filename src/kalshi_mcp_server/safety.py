"""Server-side safety controls for write operations.

These checks run BEFORE we hit Kalshi. The point is to fail locally with a
clear message rather than relying on Kalshi to reject something — and to
enforce policies the API doesn't enforce (e.g. "no more than $250/day").

Every order-placing tool MUST call `SafetyController.check_order(...)`
before sending. The controller raises `SafetyError` on policy violations.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

from kalshi_mcp_server.config import Config
from kalshi_mcp_server.errors import SafetyError, TradingDisabledError


@dataclass
class OrderIntent:
    """Compact description of an order for the safety check."""

    ticker: str
    side: str  # "yes" or "no" (book_side) — surface-level only, not validated here
    action: str  # "buy" or "sell"
    count: int
    limit_price_cents: int  # 1..99 for Kalshi binary contracts


@dataclass
class _DailyCounter:
    """Tracks cumulative cost spent today (UTC). Resets at UTC midnight."""

    day: str = ""
    cost_usd: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _maybe_roll(self) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.cost_usd = 0.0

    def add(self, cost_usd: float) -> float:
        with self._lock:
            self._maybe_roll()
            self.cost_usd += cost_usd
            return self.cost_usd

    def peek(self) -> tuple[str, float]:
        with self._lock:
            self._maybe_roll()
            return self.day, self.cost_usd


class SafetyController:
    """Enforces trading-enabled + size + daily caps. Lives for the server's lifetime."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._daily = _DailyCounter()

    def assert_trading_enabled(self) -> None:
        if not self._config.trading_enabled:
            raise TradingDisabledError(
                "Trading is disabled. The server is in read-only mode. "
                "Set KALSHI_TRADING_ENABLED=1 and restart to enable order "
                "placement, cancellation, and amendment."
            )

    def check_order(self, intent: OrderIntent, *, current_cash_usd: float | None = None) -> None:
        """Raise SafetyError if the order violates any configured policy.

        Pass `current_cash_usd` (from a fresh balance read) to enforce the
        cash-reserve floor. Omit to skip that check (e.g. dry-run mode).
        """
        self.assert_trading_enabled()

        if intent.count <= 0:
            raise SafetyError(f"Order count must be positive, got {intent.count}.")
        if not 1 <= intent.limit_price_cents <= 99:
            raise SafetyError(
                f"limit_price_cents must be in 1..99 for binary contracts, "
                f"got {intent.limit_price_cents}."
            )
        if intent.count > self._config.max_contracts_per_order:
            raise SafetyError(
                f"Order count {intent.count} exceeds MCP_MAX_CONTRACTS_PER_ORDER="
                f"{self._config.max_contracts_per_order}."
            )

        # Cost in USD = (count * price_cents) / 100. For sells, cost is risk
        # to the position (selling YES at 30c is buying NO at 70c), so we
        # treat both sides symmetrically for the size cap.
        max_price_cents = (
            intent.limit_price_cents if intent.action == "buy" else 100 - intent.limit_price_cents
        )
        cost_usd = (intent.count * max_price_cents) / 100.0

        if cost_usd > self._config.max_order_size_usd:
            raise SafetyError(
                f"Order cost ${cost_usd:.2f} exceeds MCP_MAX_ORDER_SIZE_USD="
                f"${self._config.max_order_size_usd:.2f}. Reduce count or price."
            )

        _, today_so_far = self._daily.peek()
        projected = today_so_far + cost_usd
        if projected > self._config.daily_limit_usd:
            raise SafetyError(
                f"Projected daily cost ${projected:.2f} would exceed "
                f"MCP_DAILY_LIMIT_USD=${self._config.daily_limit_usd:.2f}. "
                f"Already spent ${today_so_far:.2f} today. Resets at UTC midnight."
            )

        if current_cash_usd is not None:
            remaining_after = current_cash_usd - cost_usd
            if remaining_after < self._config.cash_reserve_usd:
                raise SafetyError(
                    f"Order would leave ${remaining_after:.2f} in cash, below the "
                    f"reserve floor MCP_CASH_RESERVE_USD=${self._config.cash_reserve_usd:.2f}."
                )

    def record_order_committed(self, intent: OrderIntent) -> None:
        """Call AFTER the order is successfully accepted by Kalshi."""
        max_price_cents = (
            intent.limit_price_cents if intent.action == "buy" else 100 - intent.limit_price_cents
        )
        cost_usd = (intent.count * max_price_cents) / 100.0
        self._daily.add(cost_usd)
