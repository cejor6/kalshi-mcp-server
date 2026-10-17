"""Order management tools — prepare / confirm / cancel / amend.

Writes are deliberately friction-ier than reads. Order placement is a
**two-step** flow:

1. `kalshi_prepare_order(...)` runs safety checks locally, then returns
   a `confirmation_id` if everything passes. The order is NOT sent.
2. `kalshi_confirm_order(confirmation_id)` retrieves the stored intent,
   RE-RUNS every safety check against the limits in force now (plus the
   cash-reserve floor, from a fresh balance read), reserves the order's
   worst-case cost in the daily counter, and only then sends it. The
   reservation is given back only if Kalshi unambiguously refused the order.

This split exists because LLM-driven trading is unforgiving. The
prepare step gives the model a chance to read back the order details,
the estimated cost, and the remaining daily budget BEFORE committing.
If the model gets confused between prepare and confirm, the failure
mode is a leftover unused token, not a duplicate trade.

Cancellation tools bypass the trading-enabled flag intentionally —
cancelling only reduces exposure, so it's always allowed.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from kalshi_mcp_server.errors import KalshiAPIError, RateLimitError, SafetyError
from kalshi_mcp_server.safety import OrderIntent, OrderReservation
from kalshi_mcp_server.tools._validation import _validate_path_segment, _validate_ticker

if TYPE_CHECKING:
    from fastmcp import FastMCP

_PENDING_TTL_S = 300  # 5 minutes
# 4xx statuses that do NOT prove the order was refused: 408 Request Timeout
# and 425 Too Early can be returned after (or while) the server processed it.
_AMBIGUOUS_4XX = frozenset({408, 425})
# Live prepared-but-unconfirmed orders. Each holds no money, but an unbounded
# map is a memory leak an agent loop can drive; 100 is far more than any sane
# workflow keeps open within the 5-minute TTL.
_MAX_PENDING = 100
# 2100-01-01T00:00:00Z in unix SECONDS. A millisecond timestamp for any date
# after 1970-02-17 is larger than this, so the bound catches the classic
# seconds-vs-ms mixup while allowing any plausible GTT expiry.
_MAX_EXPIRATION_TS = 4_102_444_800


@dataclass
class _PendingOrder:
    intent: OrderIntent
    type: str
    post_only: bool
    expiration_ts: int | None
    idempotency_key: str
    expires_at: float
    # Set once a send MAY have reached Kalshi (timeout, transport error, 5xx,
    # cancellation mid-POST). From then on the order's cost stays reserved and
    # a retry resends the identical body (same client_order_id) without
    # re-reserving.
    possibly_sent: bool = False
    # The daily-budget debit for this order (amount + the UTC day it was
    # charged to). Set by reserve_order; cleared only by an unambiguous release.
    reservation: OrderReservation | None = None


def _gc_expired(pending: dict[str, _PendingOrder]) -> None:
    now = time.time()
    expired = [tok for tok, p in pending.items() if p.expires_at < now]
    for tok in expired:
        del pending[tok]


def _validate_expiration_ts(expiration_ts: Any, *, now: float) -> int | None:
    """Authoritative check for a GTT `expiration_ts` (unix SECONDS)."""
    if expiration_ts is None:
        return None
    if isinstance(expiration_ts, bool) or not isinstance(expiration_ts, int):
        raise SafetyError(
            f"expiration_ts must be an integer unix timestamp in SECONDS, got "
            f"{expiration_ts!r}. Omit it for a good-till-cancelled order."
        )
    if expiration_ts > _MAX_EXPIRATION_TS:
        raise SafetyError(
            f"expiration_ts={expiration_ts} is past the year 2100 — it looks like "
            f"MILLISECONDS. Pass unix SECONDS (e.g. {expiration_ts // 1000} if this "
            f"was ms)."
        )
    if expiration_ts <= now:
        raise SafetyError(
            f"expiration_ts={expiration_ts} is not in the future (now is "
            f"{int(now)}). Pass a future unix timestamp in seconds, or omit it for "
            "a good-till-cancelled order."
        )
    return expiration_ts


def _new_token() -> str:
    return uuid.uuid4().hex


# ── Kalshi V2 order model (endpoint migration, issue #83) ───────────────────
#
# Kalshi retired the old `POST /portfolio/orders` create path (migration
# deadline was 2026-05-06) — it now returns `410 Gone: Please switch to the V2
# endpoints`. The replacement is `POST /portfolio/events/orders` with a
# YES-CENTRIC SINGLE-BOOK request shape, which is a materially different body,
# so the conversion below is the load-bearing part of this fix.
#
# The V2 side is `bid`/`ask` on the YES leg ONLY:
#   bid  = buy YES
#   ask  = sell YES
# and `price` is the YES-side price as fixed-point dollars ("0.5600").
# A NO order has no native representation — you express it on the YES side at
# the complementary price, because buying NO at X¢ is economically selling YES
# at (100-X)¢ (and selling NO at X¢ is buying YES at (100-X)¢). Get this
# inversion wrong and a "buy NO" becomes a "buy YES" — a completely different
# real-money bet — so it is unit-tested for all four action-by-side combinations.
#
# The intuitive yes/no + buy/sell + cents model is preserved everywhere the
# operator and the safety layer see it (prepare_order, OrderIntent,
# safety.check_order); this converter runs only at confirm time, translating to
# the wire shape. Safety semantics are unchanged.
#
# NOT YET VALIDATED against the live demo API — this environment has no demo
# credentials to place a test order. Three fields carry residual uncertainty
# and should be confirmed with a demo prepare→confirm before trusting real
# money (see the issue): the `count` string format, the
# `self_trade_prevention_type` default, and the market-order → IOC mapping.
_V2_SELF_TRADE_PREVENTION = "taker_at_cross"  # cancel the incoming order if it would self-cross


def _v2_side_and_price_cents(action: str, side: str, limit_price_cents: int) -> tuple[str, int]:
    """Map our (action, side, cents) onto the V2 (bid/ask, YES-price-cents).

    V2 is YES-centric: bid = buy YES, ask = sell YES, price is the YES price.
      - yes+buy  → bid at the same cents
      - yes+sell → ask at the same cents
      - no+buy   → ask at (100 - cents)   [buy NO @ X = sell YES @ 100-X]
      - no+sell  → bid at (100 - cents)    [sell NO @ X = buy YES @ 100-X]
    """
    if side == "yes":
        return ("bid" if action == "buy" else "ask"), limit_price_cents
    # NO side → convert to the equivalent YES-side order at the complement.
    return ("ask" if action == "buy" else "bid"), 100 - limit_price_cents


def _build_v2_order_body(prepared: _PendingOrder) -> dict[str, Any]:
    """Build the `POST /portfolio/events/orders` (V2) request body."""
    intent = prepared.intent
    v2_side, yes_price_cents = _v2_side_and_price_cents(
        intent.action, intent.side, intent.limit_price_cents
    )
    body: dict[str, Any] = {
        "ticker": intent.ticker,
        "side": v2_side,
        # Fixed-point dollars, always 2 cents of precision (e.g. 56 → "0.5600").
        "price": f"{yes_price_cents / 100:.4f}",
        # FixedPointCount string per the V2 schema.
        "count": str(intent.count),
        "client_order_id": prepared.idempotency_key,
        # A "market" order becomes take-now-or-cancel (the price bound from the
        # limit still caps how far it crosses — the safety cost check used it).
        # A limit order rests until cancelled.
        "time_in_force": (
            "immediate_or_cancel" if prepared.type == "market" else "good_till_canceled"
        ),
        "self_trade_prevention_type": _V2_SELF_TRADE_PREVENTION,
    }
    # `post_only` (maker-only) and `expiration_time` (good-till-time) only make
    # sense on a RESTING order, and Kalshi rejects either combined with a
    # market order's immediate-or-cancel. prepare_order already rejects those
    # combinations, but gate them here too so a directly-built _PendingOrder
    # can never emit a contradictory body.
    is_market = prepared.type == "market"
    if prepared.post_only and not is_market:
        body["post_only"] = True
    if prepared.expiration_ts is not None and not is_market:
        body["expiration_time"] = prepared.expiration_ts
    return body


def register(server: FastMCP) -> None:
    """Register order-management tools against the FastMCP server."""
    client = server._kalshi_client  # type: ignore[attr-defined]
    safety = server._kalshi_safety  # type: ignore[attr-defined]

    # In-memory store of prepared-but-not-yet-confirmed orders. Lives
    # for the server process lifetime; intents expire after 5 minutes.
    # Not shared across processes — that's fine for a single-tenant MCP
    # server.
    pending: dict[str, _PendingOrder] = {}
    confirm_lock = asyncio.Lock()

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

        Safety checks (run here as a fast-fail, and AGAIN at confirm against the
        limits in force then):
        - Trading must be enabled (`KALSHI_TRADING_ENABLED=1`).
        - count must be positive and below MCP_MAX_CONTRACTS_PER_ORDER.
        - limit_price_cents must be 1-99.
        - Worst-case cost (notional + a conservative taker-fee bound) must be
          below MCP_MAX_ORDER_SIZE_USD.
        - Today's cumulative cost (after this order) must be below
          MCP_DAILY_LIMIT_USD.
        - At confirm only: cash left after the order must stay above
          MCP_CASH_RESERVE_USD.

        Args:
            ticker: Market ticker.
            action: "buy" or "sell".
            side: "yes" or "no" (which outcome the contract represents).
            count: Number of contracts.
            limit_price_cents: Limit price in cents, 1-99.
            order_type: "limit" or "market". Default "limit".
            post_only: If true, reject if the order would immediately
                take liquidity (maker-only). Default false.
            expiration_ts: Optional GTT — unix SECONDS (not ms) at which
                the order auto-cancels if unfilled; must be in the future.
                Omit for GTC.

        Returns: a dict with `confirmation_id`, the recorded intent,
        estimated cost, expiration timestamp, and a `safety_status`.
        On safety failure this tool raises rather than returning a
        token — there's no "rejected but here's a token" state.
        """
        # The Literal[...] annotations make the schema reject non-matching
        # values for MCP clients; these checks (with case-normalization,
        # matching action/side) are the authoritative backstop for direct
        # `.fn` callers, who bypass Pydantic.
        ticker = _validate_ticker(ticker)
        action_lc = action.lower()
        side_lc = side.lower()
        order_type_lc = order_type.lower()
        if action_lc not in {"buy", "sell"}:
            raise SafetyError(f"action must be 'buy' or 'sell', got {action!r}")
        if side_lc not in {"yes", "no"}:
            raise SafetyError(f"side must be 'yes' or 'no', got {side!r}")
        if order_type_lc not in {"limit", "market"}:
            raise SafetyError(f"order_type must be 'limit' or 'market', got {order_type!r}")
        # A market order maps to immediate-or-cancel on Kalshi's V2 API, which
        # cannot rest and cannot be maker-only — so `expiration_ts` and
        # `post_only` are contradictory with it and Kalshi 400s the combination.
        # Reject locally with an actionable message (the authoritative
        # order-write guard) instead of letting an agent loop on that 400.
        if order_type_lc == "market":
            if expiration_ts is not None:
                raise SafetyError(
                    "A market order executes immediately (immediate-or-cancel) and "
                    "cannot carry an expiration_ts. Use order_type='limit' to rest an "
                    "order with an expiration."
                )
            if post_only:
                raise SafetyError(
                    "A market order takes liquidity and cannot be post_only (maker-only). "
                    "Drop post_only, or use order_type='limit'."
                )
        expiration_ts = _validate_expiration_ts(expiration_ts, now=time.time())

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
        if len(pending) >= _MAX_PENDING:
            raise SafetyError(
                f"Too many prepared-but-unconfirmed orders ({len(pending)}, max "
                f"{_MAX_PENDING}). Confirm the ones you intend to place, or wait for "
                f"unused ones to expire ({_PENDING_TTL_S // 60} minutes after prepare) "
                "before preparing more."
            )
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

        # The SAME number the safety checks use (notional + fee bound), so the
        # displayed estimate can never understate what is checked/reserved.
        cost_usd = round(intent.worst_case_cost_usd(), 2)
        fee_usd = round(intent.fee_upper_bound_cents() / 100.0, 2)
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
            "estimated_fee_usd": fee_usd,
            "idempotency_key": idempotency_key,
            "safety_status": "PASS",
            "instructions": (
                "Call kalshi_confirm_order(confirmation_id) within 5 "
                "minutes to execute. Re-read the `intent` before "
                "confirming."
            ),
        }

    async def _read_cash_usd(reserve_usd: float) -> float:
        """Fresh available-cash read for the reserve check. Fails CLOSED.

        Same source as `kalshi_get_balance`: `GET /portfolio/balance`, whose
        `balance` is available cash in integer cents. Any failure — transport,
        API error, or a missing/non-integer field — refuses the order, because
        the alternative is placing it with the reserve floor unchecked.

        Kalshi documents `balance` as the amount available for trading, i.e.
        net of cash already held for resting orders; the floor is checked
        against that.
        """
        guidance = (
            f"An active cash reserve (${reserve_usd:.2f}) can't be enforced without "
            "it, so the order was NOT sent by this call. Retry "
            "kalshi_confirm_order with the same confirmation_id."
        )
        try:
            body = await client.get("/portfolio/balance")
        except Exception as exc:
            raise SafetyError(
                f"Refusing the order: could not read the cash balance ({exc}). {guidance}"
            ) from exc
        cents = body.get("balance") if isinstance(body, dict) else None
        if isinstance(cents, bool) or not isinstance(cents, int):
            raise SafetyError(
                f"Refusing the order: the balance response had no integer `balance` "
                f"(got {cents!r}). {guidance}"
            )
        return cents / 100.0

    @server.tool
    async def kalshi_confirm_order(confirmation_id: str) -> dict[str, Any]:
        """Execute a previously prepared order.

        Re-checks every safety limit as it stands NOW (an order prepared before
        a limit was tightened is judged by the tightened one), enforces the
        cash reserve from a fresh balance read, and reserves the cost against
        the daily limit BEFORE sending.

        If the send times out or fails ambiguously, the order MAY have been
        placed: its cost stays reserved and the confirmation_id stays valid.
        Check kalshi_get_orders for its client_order_id, or retry with the SAME
        confirmation_id (it resends the same client_order_id). Never re-prepare
        it — that mints a new id and could duplicate the order.

        Args:
            confirmation_id: The token returned by `kalshi_prepare_order`.

        Returns Kalshi's order response (order_id, status, etc.).
        Raises SafetyError if the confirmation_id is unknown or expired, or if
        the order now violates a limit.
        """
        safety.assert_trading_enabled()
        # Serialize confirms end to end. The cash-reserve check is a
        # read-then-act on a REMOTE balance: two confirms reading the same
        # balance concurrently would each pass a floor only one should. Holding
        # the lock across the POST means each balance read comes after every
        # earlier confirm has finished. Throughput is irrelevant for an agent;
        # cancel/decrease don't take the lock, so an emergency cancel is never
        # queued behind a slow confirm (the client timeout bounds the wait).
        async with confirm_lock:
            return await _confirm_locked(confirmation_id)

    async def _confirm_locked(confirmation_id: str) -> dict[str, Any]:
        _gc_expired(pending)
        prepared = pending.get(confirmation_id)
        if prepared is None:
            raise SafetyError(
                f"Unknown or expired confirmation_id: {confirmation_id!r}. "
                "Run kalshi_prepare_order again."
            )
        intent = prepared.intent
        # V2 order model — see `_build_v2_order_body` and the note above it. The
        # old `POST /portfolio/orders` path is retired (410); this is the
        # `POST /portfolio/events/orders` shape. Built before reserving so a
        # (theoretical) build failure can't strand a reservation.
        body = _build_v2_order_body(prepared)
        is_retry = prepared.possibly_sent
        # Fresh cash, net of OTHER orders whose outcome is unknown (they may
        # land later without showing in the balance yet). Read for both paths:
        # a retry must not breach the floor either. Over-counts an ambiguous
        # order that did land — the safe direction.
        reserve_usd = safety.effective_limits().cash_reserve_usd
        cash_usd = (
            await _read_cash_usd(reserve_usd) - _outstanding_ambiguous_usd(confirmation_id)
            if reserve_usd > 0
            else None
        )

        if is_retry:
            # The order may already exist — resend the identical request.
            try:
                reservation = prepared.reservation
                if reservation is None or reservation.day != safety.daily_spend_day():
                    # The reservation was charged to an earlier UTC day (or is
                    # missing), so today's counter doesn't carry this order.
                    # Charge it to today — full checks incl. the daily cap. The
                    # old day's debit stays where it is (over-reserve, safe).
                    prepared.reservation = safety.reserve_order(intent, current_cash_usd=cash_usd)
                else:
                    # Already counted today; re-run everything except the daily
                    # check (which would double-count it), incl. the cash floor.
                    safety.recheck_reserved_order(intent, current_cash_usd=cash_usd)
            except SafetyError as exc:
                raise SafetyError(
                    f"{exc} This confirmation_id's earlier send had an UNKNOWN "
                    "outcome, so the order may already exist and its cost stays "
                    "reserved. Check kalshi_get_orders for client_order_id "
                    f"{prepared.idempotency_key!r}."
                ) from exc
        else:
            try:
                # Atomic: re-check against the limits in force now + debit the
                # daily counter, with no await in between.
                prepared.reservation = safety.reserve_order(intent, current_cash_usd=cash_usd)
            except SafetyError:
                # A policy refusal is final for this token — same as prepare's
                # "no rejected-but-here's-a-token state". Re-prepare if needed.
                pending.pop(confirmation_id, None)
                raise

        try:
            response = await client.post("/portfolio/events/orders", json=body)
        except KalshiAPIError as exc:
            refused = (
                isinstance(exc, RateLimitError) or 400 <= exc.status < 500
            ) and exc.status not in _AMBIGUOUS_4XX
            if not refused:
                # Timeout / transport error (status 0), 3xx, 408/425 or 5xx:
                # Kalshi may have accepted it. Keep the reservation and token.
                _mark_ambiguous(confirmation_id, prepared)
                raise KalshiAPIError(
                    status=exc.status,
                    message=_ambiguous_message(exc.message, confirmation_id, prepared),
                    body=exc.body,
                ) from exc
            rate_limited = isinstance(exc, RateLimitError) or exc.status == 429
            if not is_retry:
                # First send, unambiguous refusal: nothing was placed, so the
                # reservation goes back. A 429 keeps the token (retry the same
                # id shortly, re-checked from scratch); any other 4xx is a
                # final rejection of this order.
                _release(prepared)
                if not rate_limited:
                    pending.pop(confirmation_id, None)
                raise
            # A RETRY was refused. That does NOT prove nothing was placed: the
            # earlier ambiguous send may have succeeded, and Kalshi rejects a
            # duplicate client_order_id. So the reservation stays either way.
            if rate_limited:
                _mark_ambiguous(confirmation_id, prepared)  # still unknown; refresh TTL
                raise
            pending.pop(confirmation_id, None)
            raise KalshiAPIError(
                status=exc.status,
                message=(
                    f"{exc.message} — this RETRY was rejected, but an earlier send "
                    "of the same order had an unknown outcome and may have placed "
                    "it (a duplicate client_order_id is rejected). Its cost stays "
                    "reserved. Check kalshi_get_orders for client_order_id "
                    f"{prepared.idempotency_key!r} before preparing a replacement."
                ),
                body=exc.body,
            ) from exc
        except BaseException:
            # Cancellation (the MCP client timed the tool call out) or anything
            # unexpected while the POST may be in flight: ambiguous by default.
            _mark_ambiguous(confirmation_id, prepared)
            raise

        # Accepted. The reservation becomes the committed spend.
        pending.pop(confirmation_id, None)
        return response

    def _release(prepared: _PendingOrder) -> None:
        if prepared.reservation is not None:
            safety.release_order(prepared.reservation)
            prepared.reservation = None

    def _outstanding_ambiguous_usd(confirmation_id: str) -> float:
        """Reserved cost of OTHER pending orders whose send outcome is unknown."""
        return sum(
            p.reservation.cost_usd
            for token, p in pending.items()
            if token != confirmation_id and p.possibly_sent and p.reservation is not None
        )

    def _mark_ambiguous(confirmation_id: str, prepared: _PendingOrder) -> None:
        prepared.possibly_sent = True
        # Give the agent a full TTL to retry with the SAME idempotency key, and
        # re-insert in case a concurrent prepare's GC dropped it mid-POST.
        prepared.expires_at = max(prepared.expires_at, time.time() + _PENDING_TTL_S)
        pending[confirmation_id] = prepared

    def _ambiguous_message(detail: str, confirmation_id: str, prepared: _PendingOrder) -> str:
        cost = prepared.reservation.cost_usd if prepared.reservation else None
        cost_note = f"${cost:.2f}" if cost is not None else "Its worst-case cost"
        return (
            f"{detail} — OUTCOME UNKNOWN: Kalshi may have accepted this order. "
            f"{cost_note} stays counted against today's daily limit. Do NOT "
            "prepare a new order for it. First check kalshi_get_orders for "
            f"client_order_id {prepared.idempotency_key!r}; if it isn't there, "
            f"retry kalshi_confirm_order with the SAME confirmation_id "
            f"{confirmation_id!r} — it resends the identical request with the same "
            "client_order_id, so Kalshi can reject it as a duplicate."
        )

    @server.tool
    async def kalshi_cancel_order(order_id: str) -> dict[str, Any]:
        """Cancel a resting order.

        Cancellation is always allowed — even when KALSHI_TRADING_ENABLED
        is off — because cancellation only reduces risk.

        Args:
            order_id: The Kalshi order id to cancel.
        """
        order_id = _validate_path_segment(order_id, name="order_id", kind="identifier")
        # V2 path (issue #83): the order family moved to /portfolio/events/orders.
        return await client.delete(f"/portfolio/events/orders/{order_id}")

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
        order_id = _validate_path_segment(order_id, name="order_id", kind="identifier")
        # V2 path + shape (issue #83): moved to /portfolio/events/orders, and
        # `reduce_by` is now a FixedPointCount STRING, not an integer.
        body = {"reduce_by": str(reduce_by)}
        return await client.post(
            f"/portfolio/events/orders/{order_id}/decrease",
            json=body,
        )

    @server.tool
    async def kalshi_get_order(order_id: str) -> dict[str, Any]:
        """Get the current state of a single order by id.

        Args:
            order_id: The Kalshi order id to fetch.
        """
        order_id = _validate_path_segment(order_id, name="order_id", kind="identifier")
        return await client.get(f"/portfolio/orders/{order_id}")
