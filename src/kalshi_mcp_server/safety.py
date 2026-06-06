"""Server-side safety controls for write operations.

These checks run BEFORE we hit Kalshi. The point is to fail locally with a
clear message rather than relying on Kalshi to reject something — and to
enforce policies the API doesn't enforce (e.g. "no more than $250/day").

Every order-placing tool MUST call `SafetyController.check_order(...)`
before sending. The controller raises `SafetyError` on policy violations.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from kalshi_mcp_server.config import Config
from kalshi_mcp_server.errors import SafetyError, TradingDisabledError

logger = logging.getLogger(__name__)

# The four numeric safety limits, in declaration order. Used for iteration
# when diffing effective-vs-ceiling and when (de)serializing the override set.
_ALL_LIMIT_FIELDS = (
    "max_order_size_usd",
    "daily_limit_usd",
    "max_contracts_per_order",
    "cash_reserve_usd",
)
# "Tighter" is direction-dependent. Three limits tighten DOWNWARD — a smaller
# number is more conservative, so a runtime override may only go <= the env
# ceiling. `cash_reserve_usd` is the odd one out: a LARGER reserve is more
# conservative, so its override may only go >= the env value. The env var is
# the absolute loosest setting either way.
_ASCENDING_LIMITS = frozenset({"cash_reserve_usd"})


@dataclass(frozen=True)
class SafetyLimits:
    """An immutable snapshot of the four numeric safety limits.

    Used three ways: the env-configured *ceiling* (the hard maximum a runtime
    override can never loosen past), the current *effective* limits, and the
    serialization shape for persistence.
    """

    max_order_size_usd: float
    daily_limit_usd: float
    max_contracts_per_order: int
    cash_reserve_usd: float

    @classmethod
    def from_config(cls, config: Config) -> SafetyLimits:
        return cls(
            max_order_size_usd=config.max_order_size_usd,
            daily_limit_usd=config.daily_limit_usd,
            max_contracts_per_order=config.max_contracts_per_order,
            cash_reserve_usd=config.cash_reserve_usd,
        )

    def as_dict(self) -> dict[str, float | int]:
        return {field_name: getattr(self, field_name) for field_name in _ALL_LIMIT_FIELDS}


class LimitsStore(Protocol):
    """Persistence backend for runtime limit *overrides*.

    Only the fields that differ from the env ceiling are stored (a sparse
    map), so that raising an env ceiling on the next redeploy takes effect
    for any field the operator never explicitly tightened. `durable` reports
    whether values survive a process restart — the in-memory store does not,
    the Redis-backed one does.
    """

    durable: bool

    async def load(self) -> dict[str, float] | None: ...

    async def save(self, overrides: dict[str, float]) -> None: ...

    async def clear(self) -> None: ...


class InMemoryLimitsStore:
    """Default store — keeps overrides for the process lifetime only.

    A restart reverts to the env ceilings. This is the behavior for every
    stdio client and for any HTTP deploy without `MCP_REDIS_URL`.
    """

    durable = False

    def __init__(self) -> None:
        self._value: dict[str, float] | None = None

    async def load(self) -> dict[str, float] | None:
        return self._value

    async def save(self, overrides: dict[str, float]) -> None:
        self._value = dict(overrides)

    async def clear(self) -> None:
        self._value = None


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
    """Enforces trading-enabled + size + daily caps. Lives for the server's lifetime.

    The four numeric limits are *runtime-adjustable* (see `set_limits`), but
    only ever to a value at least as tight as the env-configured ceiling —
    the env vars in `Config` are the absolute maximum and can never be
    loosened past at runtime. Adjustments persist via the injected
    `LimitsStore` (in-memory by default; Redis-backed when configured) and
    are re-clamped to the env ceiling on load, so a stale or hostile stored
    value can never widen a limit.
    """

    def __init__(self, config: Config, *, store: LimitsStore | None = None) -> None:
        self._config = config
        self._ceilings = SafetyLimits.from_config(config)
        self._effective = self._ceilings
        self._store: LimitsStore = store if store is not None else InMemoryLimitsStore()
        self._limits_lock = threading.Lock()
        self._daily = _DailyCounter()

    @property
    def ceilings(self) -> SafetyLimits:
        """The env-configured hard maximum. A runtime override never loosens past this."""
        return self._ceilings

    def effective_limits(self) -> SafetyLimits:
        """The limits currently in force (env ceiling, tightened by any runtime override)."""
        with self._limits_lock:
            return self._effective

    @property
    def persistence_durable(self) -> bool:
        """True if runtime changes survive a restart (Redis-backed store)."""
        return self._store.durable

    async def set_limits(
        self,
        *,
        max_order_size_usd: float | None = None,
        daily_limit_usd: float | None = None,
        max_contracts_per_order: int | None = None,
        cash_reserve_usd: float | None = None,
    ) -> tuple[SafetyLimits, bool]:
        """Tighten (or relax, up to the env ceiling) one or more limits at runtime.

        Only the fields you pass are changed; `None` leaves a field as-is.
        Each new value is validated against the env ceiling in the correct
        direction (the three caps may only go <= their env value; the cash
        reserve may only go >= its env value). A value that would loosen
        past the ceiling raises `SafetyError` and nothing changes.

        Returns `(new_effective_limits, durably_persisted)`. Persistence is
        best-effort: the in-memory update always succeeds (so an emergency
        clamp-down takes effect immediately even if the store is down), but
        `durably_persisted` is False if the change won't survive a restart.
        """
        provided: dict[str, float] = {
            "max_order_size_usd": max_order_size_usd,
            "daily_limit_usd": daily_limit_usd,
            "max_contracts_per_order": max_contracts_per_order,
            "cash_reserve_usd": cash_reserve_usd,
        }
        provided = {name: value for name, value in provided.items() if value is not None}
        for name, value in provided.items():
            self._validate_within_ceiling(name, value)

        with self._limits_lock:
            merged = self._effective.as_dict()
            for name, value in provided.items():
                merged[name] = int(value) if name == "max_contracts_per_order" else float(value)
            new_effective = SafetyLimits(**merged)  # type: ignore[arg-type]
            self._effective = new_effective

        persisted = await self._persist(new_effective)
        return new_effective, persisted

    async def load_persisted(self) -> SafetyLimits:
        """Load any persisted override at startup, clamped to the env ceiling.

        The env ceiling always wins: a stored value tighter than the ceiling
        is applied, one looser (or corrupt) is clamped back to the ceiling.
        Returns the resulting effective limits. May raise if the store is
        unreachable — the caller (CLI boot) catches that and keeps the env
        ceilings, logging a warning.
        """
        overrides = await self._store.load()
        if not overrides:
            return self._effective
        merged = self._apply_overrides_clamped(overrides)
        with self._limits_lock:
            self._effective = merged
        return merged

    def _validate_within_ceiling(self, name: str, value: float) -> None:
        if value < 0:
            raise SafetyError(f"{name} must be non-negative, got {value}.")
        ceiling = getattr(self._ceilings, name)
        if name in _ASCENDING_LIMITS:
            if value < ceiling:
                raise SafetyError(
                    f"Refusing to set {name}={value}: that is LOOSER than the env "
                    f"ceiling ({ceiling}). A runtime change may only tighten — for "
                    f"the cash reserve that means a value >= the env setting. The "
                    f"env var is the absolute floor."
                )
        elif value > ceiling:
            raise SafetyError(
                f"Refusing to set {name}={value}: that exceeds the env ceiling "
                f"({ceiling}). A runtime change may only tighten (<= the env "
                f"setting). The env var is the absolute maximum; raise it and "
                f"redeploy to lift the ceiling."
            )

    def _overrides_of(self, effective: SafetyLimits) -> dict[str, float]:
        """The fields where `effective` differs from the env ceiling (sparse)."""
        return {
            name: getattr(effective, name)
            for name in _ALL_LIMIT_FIELDS
            if getattr(effective, name) != getattr(self._ceilings, name)
        }

    def _apply_overrides_clamped(self, overrides: dict[str, float]) -> SafetyLimits:
        merged = self._ceilings.as_dict()
        for name in _ALL_LIMIT_FIELDS:
            if name not in overrides:
                continue
            try:
                raw = overrides[name]
                value = int(raw) if name == "max_contracts_per_order" else float(raw)
            except (TypeError, ValueError):
                continue  # corrupt field → leave at ceiling (fail safe)
            merged[name] = self._clamp(name, value, getattr(self._ceilings, name))
        return SafetyLimits(**merged)  # type: ignore[arg-type]

    @staticmethod
    def _clamp(name: str, value: float, ceiling: float) -> float:
        value = max(value, 0)
        if name in _ASCENDING_LIMITS:
            return max(value, ceiling)  # reserve: at least the env floor
        return min(value, ceiling)  # caps: at most the env ceiling

    async def _persist(self, effective: SafetyLimits) -> bool:
        overrides = self._overrides_of(effective)
        try:
            if overrides:
                await self._store.save(overrides)
            else:
                await self._store.clear()
        except Exception as exc:
            logger.warning(
                "Could not persist runtime safety limits (%s store): %s",
                "durable" if self._store.durable else "in-memory",
                exc,
            )
            return False
        return self._store.durable

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

        # Snapshot the limits in force once. These are the env ceilings unless
        # an operator has tightened them at runtime via `set_limits`.
        limits = self.effective_limits()

        if intent.count <= 0:
            raise SafetyError(f"Order count must be positive, got {intent.count}.")
        if not 1 <= intent.limit_price_cents <= 99:
            raise SafetyError(
                f"limit_price_cents must be in 1..99 for binary contracts, "
                f"got {intent.limit_price_cents}."
            )
        if intent.count > limits.max_contracts_per_order:
            raise SafetyError(
                f"Order count {intent.count} exceeds the active per-order limit "
                f"({limits.max_contracts_per_order})."
            )

        # Cost in USD = (count * price_cents) / 100. For sells, cost is risk
        # to the position (selling YES at 30c is buying NO at 70c), so we
        # treat both sides symmetrically for the size cap.
        max_price_cents = (
            intent.limit_price_cents if intent.action == "buy" else 100 - intent.limit_price_cents
        )
        cost_usd = (intent.count * max_price_cents) / 100.0

        if cost_usd > limits.max_order_size_usd:
            raise SafetyError(
                f"Order cost ${cost_usd:.2f} exceeds the active max order size "
                f"(${limits.max_order_size_usd:.2f}). Reduce count or price."
            )

        _, today_so_far = self._daily.peek()
        projected = today_so_far + cost_usd
        if projected > limits.daily_limit_usd:
            raise SafetyError(
                f"Projected daily cost ${projected:.2f} would exceed the active "
                f"daily limit (${limits.daily_limit_usd:.2f}). Already spent "
                f"${today_so_far:.2f} today. Resets at UTC midnight."
            )

        if current_cash_usd is not None:
            remaining_after = current_cash_usd - cost_usd
            if remaining_after < limits.cash_reserve_usd:
                raise SafetyError(
                    f"Order would leave ${remaining_after:.2f} in cash, below the "
                    f"active reserve floor (${limits.cash_reserve_usd:.2f})."
                )

    def record_order_committed(self, intent: OrderIntent) -> None:
        """Call AFTER the order is successfully accepted by Kalshi."""
        max_price_cents = (
            intent.limit_price_cents if intent.action == "buy" else 100 - intent.limit_price_cents
        )
        cost_usd = (intent.count * max_price_cents) / 100.0
        self._daily.add(cost_usd)
