"""Tests for runtime-configurable safety limits (issue #27).

Covers the three things that matter:

1. A runtime change can TIGHTEN a limit and it takes effect immediately.
2. A runtime change can NEVER loosen a limit past its env ceiling
   (fail-closed), in both directions (caps go down, reserve goes up).
3. Persisted overrides are re-clamped to the env ceiling on load, so a
   stale/corrupt stored value can only ever tighten — never widen.

No real Redis: persistence is exercised through the in-memory store and a
couple of small fakes (recording / failing) that implement `LimitsStore`.
"""

from __future__ import annotations

import asyncio
import math

import httpx
import pytest
from fastmcp import FastMCP

from kalshi_mcp_server.auth import KalshiSigner
from kalshi_mcp_server.client import KalshiClient
from kalshi_mcp_server.config import DEMO_REST_BASE, DEMO_WS_URL, Config
from kalshi_mcp_server.errors import SafetyError
from kalshi_mcp_server.rate_limit import KalshiRateLimiter, TierLimits
from kalshi_mcp_server.safety import (
    InMemoryLimitsStore,
    OrderIntent,
    SafetyController,
)
from kalshi_mcp_server.tools import exchange


def _config(
    *,
    trading_enabled: bool = True,
    max_order_size_usd: float = 25.0,
    daily_limit_usd: float = 250.0,
    max_contracts_per_order: int = 100,
    cash_reserve_usd: float = 0.0,
    runtime_limit_tuning_enabled: bool = True,
) -> Config:
    return Config(
        key_id="test-key",
        private_key_path=None,
        private_key_pem="<set-in-test>",
        env="demo",
        trading_enabled=trading_enabled,
        rest_base=DEMO_REST_BASE,
        ws_url=DEMO_WS_URL,
        max_order_size_usd=max_order_size_usd,
        daily_limit_usd=daily_limit_usd,
        max_contracts_per_order=max_contracts_per_order,
        cash_reserve_usd=cash_reserve_usd,
        transport="stdio",
        port=8000,
        log_level="INFO",
        runtime_limit_tuning_enabled=runtime_limit_tuning_enabled,
    )


def _intent(count: int, price: int, action: str = "buy") -> OrderIntent:
    return OrderIntent(
        ticker="KX-TEST",
        side="yes",
        action=action,
        count=count,
        limit_price_cents=price,
    )


class _RecordingStore:
    """A durable LimitsStore fake that records save/clear calls."""

    durable = True

    def __init__(self, initial: dict[str, float] | None = None) -> None:
        self._value = initial
        self.saved: list[dict[str, float]] = []
        self.cleared = 0

    async def load(self) -> dict[str, float] | None:
        return self._value

    async def save(self, overrides: dict[str, float]) -> None:
        self._value = dict(overrides)
        self.saved.append(dict(overrides))

    async def clear(self) -> None:
        self._value = None
        self.cleared += 1


class _FailingStore:
    """A durable LimitsStore whose every operation raises (Redis down)."""

    durable = True

    async def load(self) -> dict[str, float] | None:
        raise RuntimeError("redis down")

    async def save(self, overrides: dict[str, float]) -> None:
        raise RuntimeError("redis down")

    async def clear(self) -> None:
        raise RuntimeError("redis down")


# --- tighten takes effect immediately -------------------------------------


async def test_tighten_max_order_size_takes_effect_immediately():
    ctrl = SafetyController(_config(max_order_size_usd=25.0))
    # 40 contracts * 50c = $20 — fine under the $25 ceiling.
    ctrl.check_order(_intent(count=40, price=50))

    new_limits, _ = await ctrl.set_limits(max_order_size_usd=10.0)
    assert new_limits.max_order_size_usd == 10.0

    # The exact same order is now refused, with no restart in between.
    with pytest.raises(SafetyError):
        ctrl.check_order(_intent(count=40, price=50))
    # A smaller order still passes under the new, tighter cap.
    ctrl.check_order(_intent(count=10, price=50))  # $5


async def test_tighten_max_contracts_takes_effect_immediately():
    ctrl = SafetyController(_config(max_contracts_per_order=100))
    ctrl.check_order(_intent(count=10, price=10))

    await ctrl.set_limits(max_contracts_per_order=5)
    with pytest.raises(SafetyError):
        ctrl.check_order(_intent(count=10, price=10))
    ctrl.check_order(_intent(count=3, price=10))


# --- cannot loosen past ceiling (both directions) -------------------------


async def test_cannot_loosen_cap_past_ceiling():
    ctrl = SafetyController(_config(max_order_size_usd=25.0))
    with pytest.raises(SafetyError):
        await ctrl.set_limits(max_order_size_usd=30.0)
    # Nothing changed.
    assert ctrl.effective_limits().max_order_size_usd == 25.0


async def test_cash_reserve_tightens_upward_only():
    # Ceiling reserve is $10; "tighter" means holding back MORE.
    ctrl = SafetyController(_config(cash_reserve_usd=10.0))

    # Raising the reserve is a tighten — allowed.
    await ctrl.set_limits(cash_reserve_usd=20.0)
    assert ctrl.effective_limits().cash_reserve_usd == 20.0

    # Lowering it below the env value would loosen — rejected.
    with pytest.raises(SafetyError):
        await ctrl.set_limits(cash_reserve_usd=5.0)
    assert ctrl.effective_limits().cash_reserve_usd == 20.0

    # The tightened reserve is enforced: a $5 order leaving $19 < $20 fails.
    with pytest.raises(SafetyError):
        ctrl.check_order(_intent(count=10, price=50), current_cash_usd=24.0)
    # Leaving >= $20 passes.
    ctrl.check_order(_intent(count=10, price=50), current_cash_usd=30.0)


async def test_negative_value_rejected():
    ctrl = SafetyController(_config())
    with pytest.raises(SafetyError):
        await ctrl.set_limits(max_order_size_usd=-1.0)


@pytest.mark.parametrize("bad", [math.nan, math.inf])
async def test_non_finite_value_rejected_on_set(bad):
    # NaN/inf would slip past the < / > comparisons and silently disable the
    # cap (every NaN comparison is False). They must be rejected outright.
    ctrl = SafetyController(_config(max_order_size_usd=25.0))
    with pytest.raises(SafetyError):
        await ctrl.set_limits(max_order_size_usd=bad)
    # Nothing was applied: the original $25 cap is intact and still enforced.
    assert ctrl.effective_limits().max_order_size_usd == 25.0
    ctrl.check_order(_intent(count=40, price=50))  # $20 < $25 still passes
    with pytest.raises(SafetyError):
        ctrl.check_order(_intent(count=60, price=50))  # $30 > $25 still blocked


async def test_inf_cash_reserve_rejected():
    # +inf reserve would make every order fail (unsatisfiable) — reject it.
    ctrl = SafetyController(_config(cash_reserve_usd=0.0))
    with pytest.raises(SafetyError):
        await ctrl.set_limits(cash_reserve_usd=math.inf)


async def test_all_none_call_is_rejected():
    ctrl = SafetyController(_config())
    with pytest.raises(SafetyError):
        await ctrl.set_limits()


# --- partial updates / reset ----------------------------------------------


async def test_partial_update_leaves_other_limits_untouched():
    ctrl = SafetyController(_config())
    await ctrl.set_limits(max_order_size_usd=10.0)
    eff = ctrl.effective_limits()
    assert eff.max_order_size_usd == 10.0
    assert eff.daily_limit_usd == 250.0
    assert eff.max_contracts_per_order == 100
    assert eff.cash_reserve_usd == 0.0


async def test_reset_to_ceiling_clears_persisted_override():
    store = _RecordingStore()
    ctrl = SafetyController(_config(max_order_size_usd=25.0), store=store)

    _, persisted = await ctrl.set_limits(max_order_size_usd=10.0)
    assert persisted is True
    assert store.saved == [{"max_order_size_usd": 10.0}]

    # Resetting back to the ceiling is "no override" — the stored value is
    # cleared so a future env-ceiling change isn't silently capped.
    await ctrl.set_limits(max_order_size_usd=25.0)
    assert store.cleared == 1
    assert store._value is None
    assert ctrl.effective_limits() == ctrl.ceilings


# --- persistence: flag + load/clamp ---------------------------------------


async def test_in_memory_store_is_not_durable():
    ctrl = SafetyController(_config(), store=InMemoryLimitsStore())
    _, persisted = await ctrl.set_limits(max_order_size_usd=10.0)
    assert persisted is False
    assert ctrl.persistence_durable is False


async def test_durable_store_reports_persisted():
    ctrl = SafetyController(_config(), store=_RecordingStore())
    _, persisted = await ctrl.set_limits(max_order_size_usd=10.0)
    assert persisted is True
    assert ctrl.persistence_durable is True


async def test_load_persisted_applies_overrides():
    store = InMemoryLimitsStore()
    await store.save({"max_order_size_usd": 5, "max_contracts_per_order": 7})
    ctrl = SafetyController(
        _config(max_order_size_usd=25.0, max_contracts_per_order=100), store=store
    )

    eff = await ctrl.load_persisted()
    assert eff.max_order_size_usd == 5
    assert eff.max_contracts_per_order == 7
    # Untouched fields stay at the ceiling.
    assert eff.daily_limit_usd == 250.0
    assert ctrl.effective_limits() == eff


async def test_load_persisted_clamps_stale_value_above_ceiling():
    # A stored value LOOSER than the current env ceiling must be clamped to
    # the ceiling — the env var always wins.
    store = InMemoryLimitsStore()
    await store.save({"max_order_size_usd": 999.0})
    ctrl = SafetyController(_config(max_order_size_usd=25.0), store=store)

    eff = await ctrl.load_persisted()
    assert eff.max_order_size_usd == 25.0


async def test_load_persisted_clamps_reserve_up_to_ceiling():
    # Ascending limit: a stored reserve below the env floor clamps up to it.
    store = InMemoryLimitsStore()
    await store.save({"cash_reserve_usd": 2.0})
    ctrl = SafetyController(_config(cash_reserve_usd=10.0), store=store)

    eff = await ctrl.load_persisted()
    assert eff.cash_reserve_usd == 10.0


async def test_load_persisted_ignores_corrupt_field():
    store = InMemoryLimitsStore()
    await store.save({"max_order_size_usd": "not-a-number"})
    ctrl = SafetyController(_config(max_order_size_usd=25.0), store=store)

    eff = await ctrl.load_persisted()
    assert eff.max_order_size_usd == 25.0  # fell back to ceiling


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
async def test_load_persisted_drops_non_finite_value(bad):
    # json.loads accepts NaN/Infinity (CPython extension), so a corrupt or
    # hostile Redis value could carry one. It must never become an effective
    # limit — fall back to the ceiling.
    store = InMemoryLimitsStore()
    await store.save({"max_order_size_usd": bad})
    ctrl = SafetyController(_config(max_order_size_usd=25.0), store=store)

    eff = await ctrl.load_persisted()
    assert eff.max_order_size_usd == 25.0
    assert math.isfinite(eff.max_order_size_usd)


# --- persistence failures degrade gracefully ------------------------------


async def test_persist_failure_still_applies_in_memory():
    ctrl = SafetyController(_config(max_order_size_usd=25.0), store=_FailingStore())
    new_limits, persisted = await ctrl.set_limits(max_order_size_usd=10.0)
    # Emergency clamp-down took effect even though the store write failed.
    assert new_limits.max_order_size_usd == 10.0
    assert persisted is False
    with pytest.raises(SafetyError):
        ctrl.check_order(_intent(count=40, price=50))  # $20 > $10


async def test_load_persisted_propagates_store_error():
    ctrl = SafetyController(_config(max_order_size_usd=25.0), store=_FailingStore())
    with pytest.raises(RuntimeError):
        await ctrl.load_persisted()
    # Effective stays at the env ceilings — the CLI boot catches this and warns.
    assert ctrl.effective_limits() == ctrl.ceilings


# --- concurrency: writers are serialized, persisted state isn't lost --------


class _SlowStore:
    """Durable store whose save yields control, to surface interleaving."""

    durable = True

    def __init__(self) -> None:
        self._value: dict[str, float] | None = None
        self.saves = 0

    async def load(self):
        return self._value

    async def save(self, overrides):
        # Yield so a concurrent set_limits would interleave here if the
        # critical section didn't span the persist.
        await asyncio.sleep(0)
        self._value = dict(overrides)
        self.saves += 1

    async def clear(self):
        await asyncio.sleep(0)
        self._value = None


async def test_concurrent_set_limits_do_not_lose_an_override():
    store = _SlowStore()
    ctrl = SafetyController(_config(max_order_size_usd=25.0, daily_limit_usd=250.0), store=store)

    # Two concurrent tightenings of DIFFERENT fields. Because set_limits holds
    # the lock across the persist, the second sees the first's result and the
    # final stored state carries BOTH overrides (no last-writer-wins clobber).
    await asyncio.gather(
        ctrl.set_limits(max_order_size_usd=10.0),
        ctrl.set_limits(daily_limit_usd=100.0),
    )

    eff = ctrl.effective_limits()
    assert eff.max_order_size_usd == 10.0
    assert eff.daily_limit_usd == 100.0
    assert store._value == {"max_order_size_usd": 10.0, "daily_limit_usd": 100.0}


# --- documented operator behaviors (regression guards) ----------------------


async def test_tightening_daily_below_already_spent_blocks_further_orders():
    ctrl = SafetyController(_config(daily_limit_usd=250.0))
    # Spend $20 today.
    ctrl.record_order_committed(_intent(count=40, price=50))
    # Tighten the daily cap below what's already spent.
    await ctrl.set_limits(daily_limit_usd=10.0)
    # Any further order is now refused for the rest of the UTC day.
    with pytest.raises(SafetyError):
        ctrl.check_order(_intent(count=2, price=50))  # even $1 projects > $10


async def test_max_contracts_zero_freezes_all_orders():
    ctrl = SafetyController(_config(max_contracts_per_order=100))
    await ctrl.set_limits(max_contracts_per_order=0)
    with pytest.raises(SafetyError):
        ctrl.check_order(_intent(count=1, price=1))


# --- operator-tool gate -----------------------------------------------------


async def test_tool_absent_when_runtime_tuning_disabled(rsa_private_key):
    server = _make_server(rsa_private_key, config=_config(runtime_limit_tuning_enabled=False))
    names = {t.name for t in await server.list_tools()}
    assert "kalshi_set_safety_limits" not in names
    # The read-only environment tool is still registered.
    assert "kalshi_get_environment" in names


async def test_tool_present_when_runtime_tuning_enabled(rsa_private_key):
    server = _make_server(rsa_private_key, config=_config(runtime_limit_tuning_enabled=True))
    names = {t.name for t in await server.list_tools()}
    assert "kalshi_set_safety_limits" in names


# --- tool wiring ----------------------------------------------------------


def _make_server(rsa_private_key, *, store=None, config=None) -> FastMCP:
    config = config or _config()
    signer = KalshiSigner(key_id="test-key", private_key=rsa_private_key)
    limiter = KalshiRateLimiter(TierLimits.basic())
    http = httpx.AsyncClient(
        base_url=config.rest_base,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": True})),
    )
    client = KalshiClient(config=config, signer=signer, rate_limiter=limiter, http_client=http)
    server = FastMCP(name="kalshi-test")
    server._kalshi_client = client  # type: ignore[attr-defined]
    server._kalshi_config = config  # type: ignore[attr-defined]
    server._kalshi_signer = signer  # type: ignore[attr-defined]
    server._kalshi_safety = SafetyController(config, store=store)  # type: ignore[attr-defined]
    server._kalshi_rate_limiter = limiter  # type: ignore[attr-defined]
    exchange.register(server)
    return server


async def _tool(server: FastMCP, name: str):
    tool = await server.get_tool(name)
    assert tool is not None, f"Tool {name!r} not registered"
    return tool.fn


async def test_set_safety_limits_tool_tightens_and_environment_reflects(rsa_private_key):
    server = _make_server(rsa_private_key)
    set_limits = await _tool(server, "kalshi_set_safety_limits")
    get_env = await _tool(server, "kalshi_get_environment")

    result = await set_limits(max_order_size_usd=10.0)
    assert result["safety_limits"]["max_order_size_usd"] == 10.0
    assert result["safety_ceilings"]["max_order_size_usd"] == 25.0
    assert result["persisted"] is False  # default in-memory store

    env = await get_env()
    assert env["safety_limits"]["max_order_size_usd"] == 10.0
    assert env["safety_ceilings"]["max_order_size_usd"] == 25.0
    assert env["safety_limits_persist"] is False


async def test_set_safety_limits_tool_rejects_loosen(rsa_private_key):
    server = _make_server(rsa_private_key)
    set_limits = await _tool(server, "kalshi_set_safety_limits")
    with pytest.raises(SafetyError):
        await set_limits(max_order_size_usd=100.0)


async def test_set_safety_limits_tool_reports_durable_persistence(rsa_private_key):
    server = _make_server(rsa_private_key, store=_RecordingStore())
    set_limits = await _tool(server, "kalshi_set_safety_limits")
    result = await set_limits(daily_limit_usd=100.0)
    assert result["persisted"] is True
    assert result["safety_limits"]["daily_limit_usd"] == 100.0
