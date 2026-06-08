"""Exchange-level and account-level read tools.

These wrap endpoints that don't fit "markets / portfolio / orders" —
they're system info that's useful for an agent to ground itself
before doing anything else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(server: FastMCP) -> None:
    """Register exchange + account tools against the FastMCP server."""
    client = server._kalshi_client  # type: ignore[attr-defined]
    config = server._kalshi_config  # type: ignore[attr-defined]
    safety = server._kalshi_safety  # type: ignore[attr-defined]

    @server.tool
    async def kalshi_get_exchange_status() -> dict[str, Any]:
        """Get current Kalshi exchange status (open/closed, trading hours).

        Use this as a sanity check before placing orders — Kalshi closes
        outside of market hours and during scheduled maintenance windows.
        """
        return await client.get("/exchange/status")

    @server.tool
    async def kalshi_get_exchange_schedule() -> dict[str, Any]:
        """Get the daily/weekly trading hours schedule for the exchange."""
        return await client.get("/exchange/schedule")

    @server.tool
    async def kalshi_get_api_limits() -> dict[str, Any]:
        """Get your account's current API rate-limit tier and remaining headroom.

        Returns Kalshi's view of your tier name, read/write bucket
        capacity, and refill rate. The MCP server uses Basic-tier defaults
        until this is called — invoking this tool periodically (or once
        at session start) lets the rate limiter be tuned to your real
        budget.
        """
        return await client.get("/account/limits")

    @server.tool
    async def kalshi_get_environment() -> dict[str, Any]:
        """Show which Kalshi environment this MCP server is connected to.

        Returns: env (`demo` or `prod`), trading_enabled flag, the REST base
        URL, and the safety limits.

        `safety_limits` are the limits **currently in force**. `safety_ceilings`
        are the env-configured hard maximums — a runtime change (see
        `kalshi_set_safety_limits`) may tighten a limit below its ceiling but
        can never loosen past it. When the two differ, an operator has
        tightened a limit at runtime. Useful for the agent to confirm whether
        a trade would hit real money — and under what caps — before placing one.
        """
        return {
            "env": config.env,
            "trading_enabled": config.trading_enabled,
            "rest_base": config.rest_base,
            "ws_url": config.ws_url,
            **safety.environment_view(),
        }

    # Operator control to tighten the safety envelope at runtime. Gated by
    # MCP_ALLOW_RUNTIME_LIMIT_TUNING (default on): a forker running a shared
    # HTTP deploy can drop the tool entirely so allowlisted users can't
    # re-tune limits. When disabled, limits change only via env + redeploy.
    if not config.runtime_limit_tuning_enabled:
        return

    # Keep the emitted tool DESCRIPTION (everything before "Args:") under 1024
    # chars — OpenAI rejects longer tool descriptions. Per-field detail belongs
    # in the Args: block, which FastMCP routes to the parameter schema.
    @server.tool
    async def kalshi_set_safety_limits(
        max_order_size_usd: float | None = None,
        daily_limit_usd: float | None = None,
        max_contracts_per_order: int | None = None,
        cash_reserve_usd: float | None = None,
    ) -> dict[str, Any]:
        """Tighten the numeric safety limits at runtime — no redeploy needed.

        This is an OPERATOR control. It can only ever make limits **tighter**
        (more conservative) than the env-configured ceilings; it can never
        loosen one past its ceiling. To raise a ceiling you must change the env
        var and redeploy — intentional, so a runtime actor (or a bug) can't
        widen your risk envelope. A value that would loosen past the ceiling is
        rejected and nothing changes; tightening takes effect immediately for
        the next order check. Pass only the fields you want to change.

        Persistence: when `MCP_REDIS_URL` is configured the change survives a
        restart/redeploy; otherwise it is in-memory and reverts to the env
        ceilings on restart (the returned `persisted` flag tells you which). A
        limit reset back to its ceiling clears the override, so a later
        env-ceiling change takes effect. Returns the new `safety_limits`, the
        `safety_ceilings` for reference, `persisted`, and a human `note`.

        Args:
            max_order_size_usd: Cap on a single order's worst-case cost (USD).
                May only be set <= its env ceiling (smaller is tighter). Omit
                to leave unchanged.
            daily_limit_usd: Cap on projected cumulative daily spend (USD). May
                only be set <= its env ceiling. Omit to leave unchanged.
            max_contracts_per_order: Cap on contracts in one order. May only be
                set <= its env ceiling. Omit to leave unchanged.
            cash_reserve_usd: Minimum cash (USD) to keep un-committed. May only
                be set >= its env floor (holding back more is tighter). Omit to
                leave unchanged.
        """
        new_limits, persisted = await safety.set_limits(
            max_order_size_usd=max_order_size_usd,
            daily_limit_usd=daily_limit_usd,
            max_contracts_per_order=max_contracts_per_order,
            cash_reserve_usd=cash_reserve_usd,
        )
        if persisted:
            note = "Saved to the persistent store — survives restart/redeploy."
        elif safety.persistence_durable:
            note = (
                "Applied in memory, but the persistent store could not be "
                "written — the change is active now but may not survive a "
                "restart. Check the server logs."
            )
        else:
            note = (
                "Applied in memory only — reverts to the env ceilings on "
                "restart. Set MCP_REDIS_URL to persist runtime changes."
            )
        return {
            "safety_limits": new_limits.as_dict(),
            "safety_ceilings": safety.ceilings.as_dict(),
            "persisted": persisted,
            "note": note,
        }
