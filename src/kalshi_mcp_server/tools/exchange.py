"""Exchange-level and account-level read tools.

These wrap endpoints that don't fit "markets / portfolio / orders" —
they're system info that's useful for an agent to ground itself
before doing anything else.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _session_token_expiry() -> dict[str, Any]:
    """Recover the CURRENT session token's expiry countdown (issue #80).

    The expiry that matters for "how long is this session good for" lives on the
    short-lived FastMCP reference JWT that claude.ai presents in the
    `Authorization` header — issued with an `exp` claim (`jwt_issuer.py`), whose
    TTL tracks the upstream GitHub-App session (~8h). It is NOT on the
    `AccessToken` that `get_access_token()` returns: under `GitHubProvider` the
    verifier hardcodes `expires_at=None` (GitHub OAuth tokens don't expire) and
    puts no `exp` in `claims`, so reading from there surfaces nothing on prod
    (the original #80 miss). The proxy verifies this JWT and reads its `exp`,
    then discards it — we recover it from the raw bearer.

    Decoded WITHOUT signature verification on purpose: the proxy middleware has
    already cryptographically validated this token before the request reaches
    any tool, this field is a non-authoritative health signal (not an
    authorization decision), and verifying here would couple us to FastMCP's
    unexported JWT key derivation. Fails open (returns {}) on anything
    unexpected — stdio (no HTTP request), a non-JWT bearer, a missing/garbage
    `exp` — because it must never break `kalshi_get_environment`. Non-finite
    `exp` (NaN/inf) is rejected before `int()`, which would otherwise raise and
    take the whole tool down (same fail-closed rule as `safety.py`).
    """
    try:
        import jwt
        from fastmcp.server.dependencies import get_http_headers

        # get_http_headers strips `authorization` by default; opt it back in.
        auth = get_http_headers(include={"authorization"}).get("authorization", "")
        scheme, _, raw = auth.partition(" ")
        if scheme.lower() != "bearer" or not raw:
            return {}
        exp = jwt.decode(raw, options={"verify_signature": False}).get("exp")
    except Exception:
        return {}
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
        return {}
    return {
        "session_token_expires_at": int(exp),
        "session_token_expires_in_seconds": max(0, int(exp - time.time())),
    }


def _session_auth_view() -> dict[str, Any]:
    """Auth health for the CURRENT request's session token (issue #80).

    Returns the authenticated GitHub `login` and the token's expiry countdown
    when the server is running under the OAuth proxy (HTTP transport) and this
    call is inside a request context. Empty on stdio, or when there's no token.
    (`login` comes from the validated `AccessToken`; the expiry is recovered
    from the raw session JWT — see `_session_token_expiry` for why the two have
    different sources.)

    Deliberately labeled "session" — this is the short-lived access token the
    connector refreshes automatically, NOT the connector's long-lived
    authorization. A healthy value here does NOT guarantee the connector won't
    later need a human re-auth (a rotation-stranding outage blinds the session
    entirely — it can't even reach this tool). Use it as an "am I
    authenticated right now, and as which login" health check that a scheduled
    run can journal, not as a multi-day expiry predictor.
    """
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
    except Exception:
        # No OAuth (stdio), or not in a request context — nothing to report.
        return {}
    if token is None:
        return {}
    claims = getattr(token, "claims", None) or {}
    view: dict[str, Any] = {"session_authenticated": True}
    login = claims.get("login")
    if login:
        view["session_login"] = login
    view.update(_session_token_expiry())
    return view


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

        Returns env (`demo`/`prod`), `trading_enabled`, the REST/WS base URLs,
        and the fields below. Read it before any write to confirm you're where
        (and who) you expect.

        `safety_limits` are in force; `safety_ceilings` are the env hard
        maximums (a runtime `kalshi_set_safety_limits` may tighten below a
        ceiling, never loosen past it). `combo_creation_enabled` +
        `combo_creations_today` / `max_combo_creations_per_day` report the
        separate `kalshi_create_combo_market` gate and today's remaining local
        creation budget.

        On OAuth/HTTP deploys only (absent on stdio), `session_authenticated` /
        `session_login` / `session_token_expires_in_seconds` are an auth health
        check. They describe the auto-refreshed SESSION token, NOT the
        connector's long-lived authorization, so a healthy value does not
        guarantee the connector won't later need a human re-auth (issue #80).
        """
        return {
            "env": config.env,
            "trading_enabled": config.trading_enabled,
            "rest_base": config.rest_base,
            "ws_url": config.ws_url,
            **safety.environment_view(),
            **safety.combo_creation_view(),
            # Session-token auth health (HTTP/OAuth only; empty on stdio). See
            # `_session_auth_view` — it's a "who am I / is auth live" signal,
            # not the connector's long-lived credential.
            **_session_auth_view(),
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
