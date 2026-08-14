"""Multivariate (combo / parlay) market tools.

Kalshi's multivariate surface is its parlay product: a *collection* defines
a universe of events and the rules for combining them (how many legs, whether
all legs must be YES, which events are eligible), and a *combo market* is one
concrete selection of legs drawn from that universe.

Everything parlay-shaped lives here — discovery of collections and the combos
already listed against them, resolution of an existing combo into its legs,
and creation of a new combo market. Keeping them in one module is deliberate:
the write tool's gating is only reviewable next to the reads it composes with.

Three things about this API that are easy to get wrong:

1. **Legs live on the MARKET, not on the collection.**
   `GET /multivariate_event_collections/{ticker}` returns the *universe* a
   combo may be built from. It cannot tell you which legs a specific combo
   selected — those are on the market object (`mve_selected_legs`, or the
   `custom_strike` parallel-CSV fallback).
2. **Creation places no order.** `POST /multivariate_event_collections/{t}`
   materializes a market ticker so it can be looked up and traded; Kalshi's
   own docs say it "must be hit at least once before trading or looking up a
   market". Money is only at risk later, via the normal order path.
3. **Creations are a scarce account resource** — 5000 per WEEK. See
   `SafetyController.check_combo_creation` for how that's bounded locally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field

from kalshi_mcp_server.errors import KalshiAPIError, SafetyError
from kalshi_mcp_server.tools.discovery import (
    _event_hint,
    _parse_fields,
    _project_market,
    _validate_ticker,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


# A combo market carries its legs in two places, both on the market object.
# `mve_selected_legs` is structured and authoritative:
#     [{"event_ticker": …, "market_ticker": …, "side": "yes"|"no"}, …]
# `custom_strike` mirrors the same data as three PARALLEL comma-separated
# strings, used as a fallback when the structured field is absent.
#
# Neither carries a human-readable leg title, which is why callers have been
# splitting the combo's own `title`/`yes_sub_title` on commas — lossy, because
# a leg's own title may itself contain a comma. Titles come from a single
# batched `GET /markets?tickers=…` over the leg market tickers instead.
_CUSTOM_STRIKE_MARKETS_KEY = "Associated Markets"
_CUSTOM_STRIKE_SIDES_KEY = "Associated Market Sides"
_CUSTOM_STRIKE_EVENTS_KEY = "Associated Events"

# Hard cap on legs we'll resolve or submit in one call. Kalshi's combos run to
# ~10 legs; this only exists so a pathological market (or a confused caller)
# can't turn one tool call into a huge query string or request body.
_MAX_COMBO_LEGS = 100


def _legs_from_selected(market: dict[str, Any]) -> list[dict[str, Any]]:
    """Legs from the structured `mve_selected_legs` field (preferred)."""
    raw = market.get("mve_selected_legs")
    if not isinstance(raw, list):
        return []
    legs: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        market_ticker = entry.get("market_ticker")
        if not isinstance(market_ticker, str) or not market_ticker.strip():
            continue
        legs.append(
            {
                "market_ticker": market_ticker.strip(),
                "event_ticker": entry.get("event_ticker"),
                "side": entry.get("side"),
            }
        )
    return legs


def _legs_from_custom_strike(market: dict[str, Any]) -> list[dict[str, Any]]:
    """Legs from the three parallel CSV strings in `custom_strike` (fallback).

    Parallel-array data with no key linking the columns, so a length mismatch
    between markets and sides means we cannot say which side goes with which
    leg. Rather than guess (the whole point of this tool), drop `side` to None
    on mismatch and keep the tickers, which are still unambiguous.
    """
    strike = market.get("custom_strike")
    if not isinstance(strike, dict):
        return []
    tickers = _parse_fields(str(strike.get(_CUSTOM_STRIKE_MARKETS_KEY) or ""))
    if not tickers:
        return []
    sides = _parse_fields(str(strike.get(_CUSTOM_STRIKE_SIDES_KEY) or ""))
    events = _parse_fields(str(strike.get(_CUSTOM_STRIKE_EVENTS_KEY) or ""))
    aligned_sides = len(sides) == len(tickers)
    aligned_events = len(events) == len(tickers)
    return [
        {
            "market_ticker": ticker,
            "event_ticker": events[i] if aligned_events else None,
            "side": sides[i] if aligned_sides else None,
        }
        for i, ticker in enumerate(tickers)
    ]


def _resolve_combo_legs(market: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """Return `(legs, source)` for a combo market, `([], None)` if unresolvable.

    `source` is "mve_selected_legs" or "custom_strike" so a caller can tell
    whether it got the structured field or the parsed-CSV fallback.
    """
    legs = _legs_from_selected(market)
    if legs:
        return legs, "mve_selected_legs"
    legs = _legs_from_custom_strike(market)
    if legs:
        return legs, "custom_strike"
    return [], None


class ComboLeg(BaseModel):
    """One leg of a combo to create: which market, which event, which side."""

    market_ticker: str = Field(description="The leg's MARKET ticker (with outcome suffix).")
    event_ticker: str = Field(description="The event that market belongs to.")
    side: Literal["yes", "no"] = Field(description="Which way the combo bets this leg.")


_VALID_SIDES = frozenset({"yes", "no"})


def _normalize_leg_specs(legs: Any) -> list[dict[str, str]]:
    """Coerce + validate the `legs` argument of `kalshi_create_combo_market`.

    The `list[ComboLeg]` annotation makes an MCP client's schema reject bad
    shapes, but per the repo's dual-layer rule that's steering, not
    enforcement: a direct `.fn` caller bypasses Pydantic entirely. This is the
    authoritative runtime guard, and it accepts either `ComboLeg` models or
    plain dicts so both paths behave identically.

    Raises SafetyError (not KalshiAPIError) — this is a local refusal on a
    state-mutating call, matching how the order path reports bad input.
    """
    if not isinstance(legs, (list, tuple)) or not legs:
        raise SafetyError(
            "legs must be a non-empty list of {market_ticker, event_ticker, side} "
            f"entries, got {legs!r}."
        )
    if len(legs) > _MAX_COMBO_LEGS:
        raise SafetyError(
            f"Refusing to submit {len(legs)} legs; the cap is {_MAX_COMBO_LEGS}. "
            "Real Kalshi collections allow far fewer — check the collection's "
            "`size_max` via kalshi_get_combo_collection."
        )

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, leg in enumerate(legs):
        raw = leg.model_dump() if isinstance(leg, BaseModel) else leg
        if not isinstance(raw, dict):
            raise SafetyError(
                f"legs[{index}] must be an object with market_ticker, event_ticker "
                f"and side, got {raw!r}."
            )
        market_ticker = str(raw.get("market_ticker") or "").strip()
        event_ticker = str(raw.get("event_ticker") or "").strip()
        side = str(raw.get("side") or "").strip().lower()
        if not market_ticker:
            raise SafetyError(f"legs[{index}].market_ticker is required and must be non-empty.")
        if not event_ticker:
            raise SafetyError(
                f"legs[{index}].event_ticker is required and must be non-empty "
                f"(for {market_ticker!r}). It is the EVENT the leg market belongs to, "
                "e.g. 'KXMLBGAME-26AUG141840BOSPIT' for "
                "'KXMLBGAME-26AUG141840BOSPIT-BOS'."
            )
        if side not in _VALID_SIDES:
            raise SafetyError(
                f"legs[{index}].side must be 'yes' or 'no', got {raw.get('side')!r} "
                f"(for {market_ticker!r})."
            )
        if market_ticker in seen:
            raise SafetyError(
                f"legs[{index}] repeats market_ticker {market_ticker!r}. A combo takes "
                "each leg market at most once; two sides of the same market cannot "
                "both be legs."
            )
        seen.add(market_ticker)
        normalized.append(
            {"market_ticker": market_ticker, "event_ticker": event_ticker, "side": side}
        )
    return normalized


def _check_against_collection(
    collection: dict[str, Any], legs: list[dict[str, str]]
) -> None:
    """Pre-flight the leg set against the collection's own construction rules.

    Kalshi rejects a malformed selection with an opaque 400. The collection
    object states the rules explicitly (`size_min`/`size_max`/`is_all_yes`),
    so check them locally and name the violation. Anything we can't read is
    skipped rather than guessed — Kalshi stays authoritative.
    """
    size_min = collection.get("size_min")
    size_max = collection.get("size_max")
    if isinstance(size_min, int) and size_min > 0 and len(legs) < size_min:
        raise SafetyError(
            f"This collection requires at least {size_min} legs; got {len(legs)}. "
            "Kalshi rejects an undersized selection with an opaque 400."
        )
    if isinstance(size_max, int) and size_max > 0 and len(legs) > size_max:
        raise SafetyError(
            f"This collection allows at most {size_max} legs; got {len(legs)}. "
            "Kalshi rejects an oversized selection with an opaque 400."
        )
    if collection.get("is_all_yes") is True:
        no_legs = [leg["market_ticker"] for leg in legs if leg["side"] == "no"]
        if no_legs:
            raise SafetyError(
                "This collection is YES-only (`is_all_yes`), but these legs are "
                f"side='no': {', '.join(no_legs)}. Flip them to 'yes', or pick a "
                "collection that permits NO legs."
            )


def register(server: FastMCP) -> None:
    """Register multivariate (combo / parlay) tools against the FastMCP server."""
    client = server._kalshi_client  # type: ignore[attr-defined]
    config = server._kalshi_config  # type: ignore[attr-defined]
    safety = server._kalshi_safety  # type: ignore[attr-defined]

    @server.tool
    async def kalshi_get_combo_collections(
        status: Literal["unopened", "open", "closed"] | None = None,
        series_ticker: str | None = None,
        associated_event_ticker: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List multivariate event COLLECTIONS — the parlay families on offer.

        A collection is the template a combo is built from: which events are
        eligible, how many legs a combo may have (`size_min`/`size_max`), and
        whether every leg must be YES (`is_all_yes`). Start here when you want
        to know what parlays are constructible right now, then use
        `kalshi_get_combo_events` to see what's already listed, or
        `kalshi_create_combo_market` to materialize a new selection.

        Args:
            status: "unopened", "open", or "closed". Omit for all.
            series_ticker: Restrict to collections under one series.
            associated_event_ticker: Restrict to collections that can include
                this event as a leg — the useful filter when you already know
                the game/market you want to build around.
            limit: 1-200. Default 20.
            cursor: Pagination cursor from a previous response. Kalshi
                silently returns an empty list on a malformed cursor.

        Token-cap note: collection objects carry `associated_event_tickers`,
        which can run to hundreds of entries on a broad cross-category
        collection. Keep `limit` low when browsing; fetch one collection in
        full with `kalshi_get_combo_collection` once you know which you want.
        """
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if associated_event_ticker:
            params["associated_event_ticker"] = associated_event_ticker
        if cursor:
            params["cursor"] = cursor
        return await client.get("/multivariate_event_collections", params=params)

    @server.tool
    async def kalshi_get_combo_collection(collection_ticker: str) -> dict[str, Any]:
        """Fetch one multivariate event collection by ticker.

        Returns the collection's construction rules — `size_min` / `size_max`
        (legs per combo), `is_all_yes` (whether NO legs are allowed),
        `is_ordered`, `is_single_market_per_event`, `functional_description`,
        and the full `associated_event_tickers` universe.

        Read this BEFORE `kalshi_create_combo_market`: those fields are
        exactly the rules Kalshi rejects a malformed selection against, and it
        does so with an opaque 400.

        Args:
            collection_ticker: e.g. "KXMVECROSSCATEGORY-SHARD1-R". A combo
                market's own `mve_collection_ticker` field names its parent
                collection.

        Token-cap note: `associated_event_tickers` can be long on a broad
        collection — this is a single-object fetch, so expect a large-ish
        response and don't loop it over many collections.
        """
        collection_ticker = _validate_ticker(collection_ticker, name="collection_ticker")
        return await client.get(f"/multivariate_event_collections/{collection_ticker}")

    @server.tool
    async def kalshi_get_combo_events(
        collection_ticker: str | None = None,
        series_ticker: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
        cursor: str | None = None,
        with_nested_markets: bool = False,
        minimal: bool = True,
    ) -> dict[str, Any]:
        """List multivariate (combo) EVENTS — parlays already listed on the exchange.

        These are the dynamically-created events behind `KXMVE…` markets. Use
        this to survey live parlay supply: what has already been built against
        a collection, and by extension what other traders are quoting.

        Args:
            collection_ticker: Restrict to one collection. CANNOT be combined
                with `series_ticker` — Kalshi rejects that pairing.
            series_ticker: Restrict to one series. Mutually exclusive with
                `collection_ticker` (see above).
            limit: 1-200. Default 20.
            cursor: Pagination cursor. Kalshi silently returns an empty list
                on a malformed cursor.
            with_nested_markets: Include each event's combo markets. Default
                False, and leave it False unless you need the books — combo
                markets are the single largest objects Kalshi returns.
            minimal: Project nested markets to the small triage whitelist
                (see `kalshi_get_markets`). Defaults TRUE here, unlike
                elsewhere, because combo markets are exactly the case the
                whitelist exists for: their bulk lives in `custom_strike`,
                `mve_selected_legs`, and long repeated sub-titles. Set False
                only if you specifically need those raw fields — expect a
                very large response.

        Most combo markets have empty or one-sided books, so treat a listing
        here as supply discovery, not as a tradeable shortlist.
        """
        if collection_ticker and series_ticker:
            raise KalshiAPIError(
                status=0,
                message=(
                    "Pass collection_ticker OR series_ticker, not both — Kalshi "
                    "rejects the combination. Filter by collection to see one "
                    "parlay family, or by series to see all of a series' combos."
                ),
            )
        params: dict[str, Any] = {
            "limit": limit,
            "with_nested_markets": str(with_nested_markets).lower(),
        }
        if collection_ticker:
            params["collection_ticker"] = collection_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        body = await client.get("/events/multivariate", params=params)

        if minimal:
            for event in body.get("events") or []:
                if isinstance(event, dict) and "markets" in event:
                    event["markets"] = [_project_market(m, minimal=True) for m in event["markets"]]
        return body

    # Keep the emitted tool DESCRIPTION (everything before "Args:") under 1024
    # chars — OpenAI rejects longer tool descriptions. Per-field detail belongs
    # in the Args: block, which FastMCP routes to the parameter schema.
    @server.tool
    async def kalshi_get_combo_legs(
        ticker: str,
        with_titles: bool = True,
        include_collection: bool = False,
    ) -> dict[str, Any]:
        """Resolve a multivariate (`KXMVE…`) combo market into its underlying legs.

        Returns one entry per leg — the leg's own MARKET ticker, its event
        ticker, the side the combo takes on it ("yes"/"no"), and (by default)
        its human title. Use this instead of splitting the combo's `title` /
        `yes_sub_title` on commas: that string is a rendering, it drops the
        leg tickers entirely, and it mis-splits whenever a leg's own title
        contains a comma.

        NOT every combo resolves. When Kalshi hasn't published the leg
        breakdown, this returns `resolvable: false` with a `reason` rather
        than guessing from the title — check that field before using `legs`.
        Legs come from the market object (`mve_selected_legs`, or the
        `custom_strike` CSV fallback); the collections endpoint describes the
        *universe* a combo may draw from, not the legs it actually selected,
        so it cannot answer this question and is offered only as context.

        Args:
            ticker: The COMBO market's own ticker, e.g.
                "KXMVECROSSCATEGORY-SHARD1-S2026…-978C8F03297". A plain
                (non-combo) market is a valid call — it just returns
                `resolvable: false, reason: "not_a_combo_market"`.
            with_titles: Resolve each leg's title via ONE extra batched
                markets lookup. Default True. Set False to save a read when
                you only need tickers and sides. Title resolution fails open:
                if the lookup errors, legs still come back with
                `titles_resolved: false`.
            include_collection: Also fetch the parent multivariate event
                collection (`size_min`/`size_max`, `is_all_yes`,
                `associated_event_tickers`, `functional_description`) — the
                rules governing how combos in this family are built. Costs one
                more read and can be sizable, so it defaults False. Fails open.
                `kalshi_get_combo_collection` is the standalone version.

        Returns:
            `resolvable`: True when legs were resolved; False otherwise.
            `legs`: list of `{market_ticker, event_ticker, side, title,
                yes_sub_title}` (the last two only when `with_titles`
                succeeded). Order matches Kalshi's leg order. This list is
                shaped to feed straight back into
                `kalshi_create_combo_market(legs=…)`.
            `leg_count`, `source`: how many legs, and whether they came from
                "mve_selected_legs" (structured, authoritative) or
                "custom_strike" (parsed parallel CSVs — `side` is dropped to
                null if the columns don't line up, rather than guessed).
            `reason` + `message`: only when `resolvable` is False.
                "not_a_combo_market" — the ticker is an ordinary market.
                "legs_not_published" — it IS a combo but Kalshi exposed no
                leg breakdown; the title string is all that exists, and this
                tool deliberately will not parse it for you.
            `collection_ticker`, `event_ticker`, `title`: the combo's own
                identifiers, always present when the market was fetched.
        """
        ticker = _validate_ticker(ticker)
        try:
            body = await client.get(f"/markets/{ticker}")
        except KalshiAPIError as exc:
            if exc.status == 404:
                hint = await _event_hint(client, ticker)
                if hint:
                    raise KalshiAPIError(status=404, message=hint) from exc
            raise

        market = body.get("market")
        if not isinstance(market, dict):
            raise KalshiAPIError(
                status=0,
                message=(
                    f"Kalshi returned no market object for {ticker!r}. "
                    "Double-check the ticker is a full MARKET ticker."
                ),
            )

        collection_ticker = market.get("mve_collection_ticker")
        base: dict[str, Any] = {
            "ticker": market.get("ticker", ticker),
            "event_ticker": market.get("event_ticker"),
            "title": market.get("title"),
            "collection_ticker": collection_ticker,
        }

        legs, source = _resolve_combo_legs(market)
        if not legs:
            # Distinguish "you pointed at a normal market" from "this really is
            # a combo but the legs aren't published" — different caller fixes.
            is_combo = bool(collection_ticker) or str(market.get("strike_type")) == "custom"
            if is_combo:
                reason = "legs_not_published"
                message = (
                    f"{ticker!r} is a multivariate combo market but Kalshi published no "
                    "leg breakdown for it (neither `mve_selected_legs` nor a "
                    "`custom_strike` markets/sides list). Its legs cannot be resolved "
                    "from the API; the title string is a lossy rendering and this tool "
                    "will not parse it. Try kalshi_get_combo_collection for the "
                    "collection's construction rules."
                )
            else:
                reason = "not_a_combo_market"
                message = (
                    f"{ticker!r} is an ordinary (non-multivariate) market — it has no "
                    "legs. Combo tickers carry a `KXMVE…` prefix and an "
                    "`mve_collection_ticker`. Use kalshi_get_market for this one."
                )
            return {**base, "resolvable": False, "reason": reason, "message": message}

        truncated = len(legs) > _MAX_COMBO_LEGS
        legs = legs[:_MAX_COMBO_LEGS]

        titles_resolved = False
        if with_titles:
            leg_tickers = [leg["market_ticker"] for leg in legs]
            try:
                lookup = await client.get(
                    "/markets",
                    params={"tickers": ",".join(leg_tickers), "limit": len(leg_tickers)},
                )
            except KalshiAPIError:
                # Fail open: the legs themselves are the answer; titles are a
                # convenience, and a lookup failure must not lose them.
                lookup = {}
            by_ticker = {
                m.get("ticker"): m
                for m in (lookup.get("markets") or [])
                if isinstance(m, dict) and m.get("ticker")
            }
            if by_ticker:
                titles_resolved = True
                for leg in legs:
                    leg_market = by_ticker.get(leg["market_ticker"])
                    if leg_market is not None:
                        leg["title"] = leg_market.get("title")
                        leg["yes_sub_title"] = leg_market.get("yes_sub_title")

        result: dict[str, Any] = {
            **base,
            "resolvable": True,
            "leg_count": len(legs),
            "source": source,
            "titles_resolved": titles_resolved,
            "legs": legs,
        }
        if truncated:
            result["truncated"] = True
            result["note"] = (
                f"Only the first {_MAX_COMBO_LEGS} legs are shown; this market declared more."
            )

        if include_collection and collection_ticker:
            try:
                collection = await client.get(
                    f"/multivariate_event_collections/{collection_ticker}"
                )
            except KalshiAPIError:
                result["collection_error"] = (
                    f"Could not fetch collection {collection_ticker!r}; legs above are unaffected."
                )
            else:
                result["collection"] = collection.get("multivariate_contract", collection)

        return result

    # ── Write surface ──────────────────────────────────────────────────────
    #
    # Registered only when MCP_ALLOW_COMBO_CREATION=1, mirroring how
    # kalshi_set_safety_limits is withheld under MCP_ALLOW_RUNTIME_LIMIT_TUNING=0.
    # Not registering at all (rather than registering-and-refusing) means a
    # read-only deploy doesn't even advertise the capability to the model.
    if not config.combo_creation_enabled:
        return

    @server.tool
    async def kalshi_create_combo_market(
        collection_ticker: str,
        legs: list[ComboLeg],
        with_market_payload: bool = False,
    ) -> dict[str, Any]:
        """Create (materialize) a combo market for a chosen set of legs.

        This is the parlay-building step. It does NOT place an order and
        commits no money — it turns a leg selection into a real market ticker
        you can then quote, price, or trade through the normal order path.
        Kalshi requires it: a combo market must be created once before it can
        be looked up or traded.

        Two limits apply. Kalshi allows 5000 creations per WEEK per account,
        and this server enforces its own per-day ceiling on top
        (`MCP_MAX_COMBO_CREATIONS_PER_DAY`, default 100) so a retry loop
        cannot burn the weekly quota. Creating the same leg set twice returns
        the same market, but each call still counts — resolve first, create once.

        Args:
            collection_ticker: The parent collection, e.g.
                "KXMVECROSSCATEGORY-SHARD1-R". Get it from
                `kalshi_get_combo_collections`, or from an existing combo's
                `collection_ticker` via `kalshi_get_combo_legs`.
            legs: The selection, as `{market_ticker, event_ticker, side}`
                objects. `event_ticker` is the EVENT the leg market belongs
                to ("KXMLBGAME-26AUG141840BOSPIT" for
                "KXMLBGAME-26AUG141840BOSPIT-BOS"), and `side` is "yes" or
                "no". A leg market may appear at most once. The `legs` list
                returned by `kalshi_get_combo_legs` is already in this shape,
                so you can round-trip an existing combo and vary one leg.
            with_market_payload: Return the full market object alongside the
                tickers. Default False — the payload is a full combo market
                (large). Fetch it with `kalshi_get_market(minimal=True)` if
                you need prices.

        Pre-flight: the collection's own rules (`size_min` / `size_max` /
        `is_all_yes`) are checked locally first, because Kalshi rejects a
        malformed selection with an opaque 400. That check fails OPEN — if the
        collection can't be read, the request proceeds and Kalshi decides.

        Returns `event_ticker` and `market_ticker` for the created combo, plus
        `created_today` / `max_per_day` so you can see your remaining local
        budget. Requires MCP_ALLOW_COMBO_CREATION=1 (this tool is not
        registered otherwise); it is deliberately independent of
        KALSHI_TRADING_ENABLED, which gates money, not ticker creation.
        """
        collection_ticker = _validate_ticker(collection_ticker, name="collection_ticker")
        # Authoritative runtime validation — the `list[ComboLeg]` annotation
        # only steers MCP clients, and a direct `.fn` caller bypasses it.
        normalized = _normalize_leg_specs(legs)
        # Gate + daily ceiling BEFORE any wire call, so a refusal costs nothing.
        safety.check_combo_creation()

        try:
            collection = await client.get(
                f"/multivariate_event_collections/{collection_ticker}"
            )
        except KalshiAPIError:
            # Fail open: Kalshi remains authoritative on whether a selection is
            # valid. We only pre-empt the opaque-400 cases we can see locally.
            pass
        else:
            contract = collection.get("multivariate_contract", collection)
            if isinstance(contract, dict):
                _check_against_collection(contract, normalized)

        body: dict[str, Any] = {"selected_markets": normalized}
        if with_market_payload:
            body["with_market_payload"] = True
        response = await client.post(
            f"/multivariate_event_collections/{collection_ticker}", json=body
        )
        # Only count it after Kalshi accepts — a rejected call consumed no
        # creation quota, so it must not consume local budget either.
        safety.record_combo_creation()

        view = safety.combo_creation_view()
        return {
            **response,
            "collection_ticker": collection_ticker,
            "legs_submitted": normalized,
            "created_today": view["combo_creations_today"],
            "max_per_day": view["max_combo_creations_per_day"],
        }
