"""Order management tools — prepare / confirm / cancel / amend.

Writes are deliberately friction-ier than reads. Order placement is a
**two-step** flow:

1. `kalshi_prepare_order(...)` runs safety checks locally, then returns
   a `confirmation_id` if everything passes. The order is NOT sent.
2. `kalshi_confirm_order(confirmation_id)` retrieves the stored intent
   and sends it to Kalshi. After Kalshi accepts the order, the daily
   cost counter is updated.

This split exists because LLM-driven trading is unforgiving. The
prepare step gives the model a chance to read back the order details,
the estimated cost, and the remaining daily budget BEFORE committing.
If the model gets confused between prepare and confirm, the failure
mode is a leftover unused token, not a duplicate trade.

Cancellation tools bypass the trading-enabled flag intentionally —
cancelling only reduces exposure, so it's always allowed.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from kalshi_mcp_server.errors import SafetyError
from kalshi_mcp_server.safety import OrderIntent

if TYPE_CHECKING:
    from fastmcp import FastMCP

_PENDING_TTL_S = 300  # 5 minutes


@dataclass
class _PendingOrder:
    intent: OrderIntent
    type: str
    post_only: bool
    expiration_ts: int | None
    idempotency_key: str
    expires_at: float


def _gc_expired(pending: dict[str, _PendingOrder]) -> None:
    now = time.time()
    expired = [tok for tok, p in pending.items() if p.expires_at < now]
    for tok in expired:
        del pending[tok]


def _new_token() -> str:
    return uuid.uuid4().hex


def register(server: FastMCP) -> None:
    """Register order-management tools against the FastMCP server."""
    client = server._kalshi_client  # type: ignore[attr-defined]
    safety = server._kalshi_safety  # type: ignore[attr-defined]

    # In-memory store of prepared-but-not-yet-confirmed orders. Lives
    # for the server process lifetime; intents expire after 5 minutes.
    # Not shared across processes — that's fine for a single-tenant MCP
    # server.
    pending: dict[str, _PendingOrder] = {}

    @server.tool
    async def kalshi_prepare_order(
        ticker: str,
        action: Literal["buy", "sell"],
        side: Literal["yes", "no"],
        count: Annotated[int, Field(ge=1)],
        limit_price_cents: Annotated[int, Field(ge=1, le=99)],
        order_type: Literal["limit", "market"] = "limit",
        post_only: bool = False,
        expiration_ts: int | None = None,
    ) -> dict[str, Any]:
        """Prepare an order for review. Does NOT send it to Kalshi yet.

        Returns a `confirmation_id` you pass to `kalshi_confirm_order`
        to execute. The intent expires in 5 minutes.

        Safety checks (run BEFORE returning a token):
        - Trading must be enabled (`KALSHI_TRADING_ENABLED=1`).
        - count must be positive and below MCP_MAX_CONTRACTS_PER_ORDER.
        - limit_price_cents must be 1-99.
        - Projected cost must be below MCP_MAX_ORDER_SIZE_USD.
        - Today's cumulative cost (after this order) must be below
          MCP_DAILY_LIMIT_USD.

        Args:
            ticker: Market ticker.
            action: "buy" or "sell".
            side: "yes" or "no" (which outcome the contract represents).
            count: Number of contracts.
            limit_price_cents: Limit price in cents, 1-99.
            order_type: "limit" or "market". Default "limit".
            post_only: If true, reject if the order would immediately
                take liquidity (maker-only). Default false.
            expiration_ts: Optional GTT — unix seconds at which the
                order auto-cancels if unfilled. Omit for GTC.

        Returns: a dict with `confirmation_id`, the recorded intent,
        estimated cost, expiration timestamp, and a `safety_status`.
        On safety failure this tool raises rather than returning a
        token — there's no "rejected but here's a token" state.
        """
        # The Literal[...] annotations make the schema reject non-matching
        # values for MCP clients; these checks (with case-normalization,
        # matching action/side) are the authoritative backstop for direct
        # `.fn` callers, who bypass Pydantic.
        action_lc = action.lower()
        side_lc = side.lower()
        order_type_lc = order_type.lower()
        if action_lc not in {"buy", "sell"}:
            raise SafetyError(f"action must be 'buy' or 'sell', got {action!r}")
        if side_lc not in {"yes", "no"}:
            raise SafetyError(f"side must be 'yes' or 'no', got {side!r}")
        if order_type_lc not in {"limit", "market"}:
            raise SafetyError(f"order_type must be 'limit' or 'market', got {order_type!r}")

        intent = OrderIntent(
            ticker=ticker,
            side=side_lc,
            action=action_lc,
            count=count,
            limit_price_cents=limit_price_cents,
        )
        # Raises SafetyError if anything's wrong. We do NOT return a
        # token on failure — caller must fix and re-prepare.
        safety.check_order(intent)

        _gc_expired(pending)
        token = _new_token()
        idempotency_key = f"mcp-{uuid.uuid4().hex}"
        expires_at = time.time() + _PENDING_TTL_S
        pending[token] = _PendingOrder(
            intent=intent,
            type=order_type_lc,
            post_only=post_only,
            expiration_ts=expiration_ts,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
        )

        max_price = limit_price_cents if action_lc == "buy" else 100 - limit_price_cents
        cost_usd = round(count * max_price / 100.0, 2)
        return {
            "confirmation_id": token,
            "expires_at_unix": int(expires_at),
            "intent": {
                "ticker": ticker,
                "action": action_lc,
                "side": side_lc,
                "count": count,
                "limit_price_cents": limit_price_cents,
                "order_type": order_type_lc,
                "post_only": post_only,
                "expiration_ts": expiration_ts,
            },
            "estimated_cost_usd": cost_usd,
            "idempotency_key": idempotency_key,
            "safety_status": "PASS",
            "instructions": (
                "Call kalshi_confirm_order(confirmation_id) within 5 "
                "minutes to execute. Re-read the `intent` before "
                "confirming."
            ),
        }

    @server.tool
    async def kalshi_confirm_order(confirmation_id: str) -> dict[str, Any]:
        """Execute a previously prepared order.

        Args:
            confirmation_id: The token returned by `kalshi_prepare_order`.

        Returns Kalshi's order response (order_id, status, etc.).
        Raises SafetyError if the confirmation_id is unknown or expired.
        """
        _gc_expired(pending)
        prepared = pending.pop(confirmation_id, None)
        if prepared is None:
            raise SafetyError(
                f"Unknown or expired confirmation_id: {confirmation_id!r}. "
                "Run kalshi_prepare_order again."
            )

        intent = prepared.intent
        body: dict[str, Any] = {
            "ticker": intent.ticker,
            "action": intent.action,
            "side": intent.side,
            "type": prepared.type,
            "count": intent.count,
            "client_order_id": prepared.idempotency_key,
        }
        # Kalshi names the price field after the side you're acting on.
        if intent.side == "yes":
            body["yes_price"] = intent.limit_price_cents
        else:
            body["no_price"] = intent.limit_price_cents
        if prepared.post_only:
            body["post_only"] = True
        if prepared.expiration_ts is not None:
            body["expiration_ts"] = prepared.expiration_ts

        response = await client.post("/portfolio/orders", json=body)
        # Only update the daily counter after Kalshi accepts the order.
        safety.record_order_committed(intent)
        return response

    @server.tool
    async def kalshi_cancel_order(order_id: str) -> dict[str, Any]:
        """Cancel a resting order.

        Cancellation is always allowed — even when KALSHI_TRADING_ENABLED
        is off — because cancellation only reduces risk.
        """
        return await client.delete(f"/portfolio/orders/{order_id}")

    @server.tool
    async def kalshi_decrease_order(
        order_id: str,
        reduce_by: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        """Reduce the contract count on a resting order.

        Like cancel, this only reduces exposure — allowed even when
        KALSHI_TRADING_ENABLED is off. Use this instead of cancel+replace
        when you want to keep your queue priority.

        Args:
            order_id: The order to decrease.
            reduce_by: Number of contracts to remove from the order. The
                order is cancelled if this is >= the remaining count.
        """
        if reduce_by <= 0:
            raise SafetyError(f"reduce_by must be positive, got {reduce_by}.")
        body = {"reduce_by": reduce_by}
        return await client.post(
            f"/portfolio/orders/{order_id}/decrease",
            json=body,
        )

    @server.tool
    async def kalshi_get_order(order_id: str) -> dict[str, Any]:
        """Get the current state of a single order by id."""
        return await client.get(f"/portfolio/orders/{order_id}")
