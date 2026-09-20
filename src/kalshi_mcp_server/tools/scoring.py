"""Optional Jev-powered market scoring — a cheap, READ-ONLY first-pass scan.

Why this exists: a trading loop that consumes this server wants to score
*many* markets cheaply and escalate only the promising handful to an
expensive reasoning model. Jev (TypeSafe "System One") is built for exactly
that — fast (70-500ms) typed decisions with calibrated confidence, over a
plain REST endpoint. This tool reuses the liquid-market ranking, builds a
compact plaintext situation per market, asks Jev a *batched* question set
(an ``edge`` score + a ``side`` choice) in ONE call per market, and returns
the results ranked by ``confidence * edge``.

STRICTLY READ-ONLY. This module never places, prepares, decreases, or
cancels an order — it imports nothing from the order/safety write path. The
only Kalshi traffic it makes is the same ``GET /markets`` listing scan that
``kalshi_find_liquid_markets`` already does. The decision of *what* to trade
still belongs in the program that consumes this server; this tool is a
ranking aid that surfaces a first-pass edge estimate and decides nothing.

Two independent gates (both fail safe):

- **Registration gate.** The tool is registered only when
  ``MCP_ALLOW_JEV_SCORING=1`` (default 0), mirroring how
  ``kalshi_create_combo_market`` / ``kalshi_set_safety_limits`` self-gate —
  a default clone never advertises a signal-adjacent capability.
- **Jev gate + graceful fallback.** Even when registered, Jev is an
  *enhancement, never a dependency*. If ``TYPESAFE_API_KEY`` is unset/empty
  the tool ranks by a deterministic liquidity/spread heuristic with
  ``jev_scored=false``. Every Jev call is wrapped in a timeout budget and
  falls back to that heuristic on ANY failure — non-2xx (402 out-of-credits,
  429 rate-limited, other), network error, timeout, malformed/missing
  answer, or confidence below the configured threshold. The whole fan-out is
  also bounded by an aggregate wall-clock budget, so a slow/drip-feeding host
  degrades to the heuristic instead of blocking. The tool never crashes;
  genuine Jev failures are logged ONCE per scan and returned as an aggregate
  ``fallback_reasons`` count.

Boundary note: like ``kalshi_fetch_external_data``, this is a deliberate,
documented exception to the "Kalshi surface only" rule (see AGENTS.md).
The claude.ai cloud-routine environment that consumes this server cannot
egress to ``api.typesafe.ai`` directly; this server (Render, unrestricted
egress, already attached as a connector) is the channel that reaches it. No
new hard dependency is added — the Jev call is plain REST over the ``httpx``
this server already ships (no ``typesafe-sdk``).

Data sent to Jev is PUBLIC Kalshi market data only (title, quoted prices,
volume, close time, resolution-rule snippet). No account data, balances,
positions, or secrets ever enter the ``state`` text, and the API key is
sent only to the operator-configured Jev host.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from kalshi_mcp_server.tools.discovery import (
    _MINIMAL_MARKET_FIELDS,
    _minimal_market,
    _scan_markets_excluding_mve,
    _TopKByVolume,
    _volume_24h,
    _yes_spread,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# ── Jev settings (all read lazily at call time; never a startup dependency) ──
#
# Read from env on every scan so an operator can set the key / retune without
# a redeploy, and an absent key simply disables Jev rather than failing the
# tool. Defaults are conservative.
_DEFAULT_BASE_URL = "https://api.typesafe.ai/v1/systemone"
_DEFAULT_MODEL = "jev-latest"
_DEFAULT_CONFIDENCE_THRESHOLD = 0.6
_DEFAULT_TIMEOUT_SECONDS = 8.0  # per-market Jev request (connect+read budget)
_DEFAULT_RATE_COOLDOWN_SECONDS = 60.0  # global back-off window after a 429
_MAX_RATE_COOLDOWN_SECONDS = 3600.0  # cap so a typo'd env can't disable Jev ~forever

# Hard ceiling on markets scored per scan — each is one PAID Jev call, so this
# is a budget guard, not just paging: it MUST be enforced at runtime, since the
# tool's `limit` schema bound (le=50) is advisory and a direct `.fn` caller
# (or `scan_all=True`) bypasses it. Matches the schema max.
_MAX_SCORE_LIMIT = 50

# Aggregate wall-clock budget for the whole Jev fan-out. The per-request timeout
# bounds ONE call, but limit x waves could still run ~100s, and a drip-feeding
# host resets the per-op read timer indefinitely — so the batch as a whole is
# also deadline-bounded (mirrors external_data's _WALL_CLOCK_SECONDS and
# discovery's _SCAN_ALL_MAX_SECONDS). On expiry the batch degrades to heuristic.
_WALL_CLOCK_SECONDS = 30.0

# Bounds simultaneous outbound Jev calls PROCESS-WIDE (module-global, like
# external_data's fetch semaphore) — not per scan, so N concurrent scans can't
# multiply into N x this against a single upstream.
_MAX_CONCURRENCY = 4
_jev_semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

# The only sides Jev may return; anything else is dropped to None. The response
# is UNTRUSTED (a hostile/misconfigured host could plant arbitrary text into a
# trusted-looking field a trading loop reads), so `side`/`probabilities` are
# allowlisted, never passed through verbatim.
_ALLOWED_SIDES: frozenset[str] = frozenset({"yes", "no", "pass"})

# Whole-scan disabled states that are steady-state config, not faults — a scan
# in one of these does NOT emit a warning (only genuine Jev failures do).
_STEADY_STATE_REASONS: frozenset[str] = frozenset({"no_api_key", "bad_base_url", "no_candidates"})

# Process-global back-off: once Jev returns a 429, every subsequent scan skips
# Jev entirely (heuristic) until this monotonic deadline, rather than hammering
# a rate-limited upstream from concurrent scans. Reset in tests via
# _reset_rate_cooldown(). Single-process only (matches the in-memory counters
# elsewhere in this server).
_rate_limited_until: float = 0.0


def _reset_rate_cooldown() -> None:
    """Test hook: clear the global 429 back-off."""
    global _rate_limited_until
    _rate_limited_until = 0.0


def _trip_rate_cooldown(seconds: float) -> None:
    global _rate_limited_until
    _rate_limited_until = max(_rate_limited_until, time.monotonic() + seconds)


def _in_rate_cooldown() -> bool:
    return time.monotonic() < _rate_limited_until


# The edge Score question's ordered levels (low->high mispricing). Per the Jev
# contract a Score `criteria` is an ARRAY of 2-10 level descriptions, and the
# returned `score` runs 0..(N-1); we normalize to 0..1 by dividing by N-1.
_EDGE_CRITERIA: tuple[str, ...] = (
    "YES price looks fair; no discernible edge.",
    "Slight apparent mispricing.",
    "Moderate apparent mispricing.",
    "Large apparent mispricing.",
    "Extreme apparent mispricing; the price looks clearly wrong.",
)
_EDGE_LEVELS = len(_EDGE_CRITERIA)  # score range 0..(_EDGE_LEVELS - 1)

# Resolution-rule text can run to paragraphs; a short snippet is enough
# context for a first-pass score and keeps the `state` payload compact.
_RULES_SNIPPET_CHARS = 400

# The scan projects to the minimal market view PLUS the resolution rule, which
# the default minimal whitelist drops. The rule feeds `state`; it is stripped
# from the tool's output projection to keep the result small.
_SCORING_SCAN_FIELDS: tuple[str, ...] = (*_MINIMAL_MARKET_FIELDS, "rules_primary")


class _JevSettings:
    """Resolved, per-scan Jev configuration. `enabled` is False when no key."""

    __slots__ = (
        "api_key",
        "base_url",
        "cooldown",
        "disabled_reason",
        "enabled",
        "model",
        "threshold",
        "timeout",
    )

    def __init__(self, *, min_confidence: float | None) -> None:
        self.api_key = (os.environ.get("TYPESAFE_API_KEY") or "").strip()
        self.base_url = (os.environ.get("MCP_JEV_BASE_URL") or "").strip() or _DEFAULT_BASE_URL
        self.model = (os.environ.get("MCP_JEV_MODEL") or "").strip() or _DEFAULT_MODEL
        self.threshold = _resolve_threshold(min_confidence)
        self.timeout = _env_float("MCP_JEV_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS, minimum=0.1)
        # Cap the cooldown so a fat-fingered env value can't disable Jev for a
        # practically unbounded time.
        self.cooldown = min(
            _env_float(
                "MCP_JEV_RATE_COOLDOWN_SECONDS", _DEFAULT_RATE_COOLDOWN_SECONDS, minimum=0.0
            ),
            _MAX_RATE_COOLDOWN_SECONDS,
        )
        self.enabled = True
        self.disabled_reason = ""
        if not self.api_key:
            self.enabled = False
            self.disabled_reason = "no_api_key"
        elif not _base_url_ok(self.base_url):
            # Operator misconfiguration — never ship the bearer token over a
            # non-TLS URL, to another host via userinfo (httpx would turn
            # `user:pass@host` into a Basic-Auth header, silently REPLACING the
            # Bearer), or to a non-default port. Disable rather than leak.
            self.enabled = False
            self.disabled_reason = "bad_base_url"
        elif _in_rate_cooldown():
            # A recent 429 tripped a global back-off; skip Jev for this scan
            # rather than hammering a rate-limited upstream.
            self.enabled = False
            self.disabled_reason = "rate_cooldown"


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value < minimum:
        return default
    return value


def _base_url_ok(url: str) -> bool:
    """True only for an https URL with no userinfo and a default/443 port.

    The Jev base URL is operator-set (trusted config, not model input), but a
    misconfiguration must never leak the bearer token: a non-https scheme sends
    it in the clear, userinfo (`user:pass@host`) makes httpx emit a Basic-Auth
    header that silently REPLACES our Authorization, and a stray port routes it
    somewhere unexpected. Reject rather than repair.
    """
    try:
        parts = urlsplit(url)
        port = parts.port  # may raise ValueError on a junk port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and "@" not in parts.netloc
        and port in (None, 443)
    )


def _resolve_threshold(min_confidence: float | None) -> float:
    """Tool-param override wins; else env; else the default. Clamped to [0, 1]."""
    if min_confidence is not None and math.isfinite(min_confidence):
        return max(0.0, min(1.0, min_confidence))
    env_value = _env_float(
        "MCP_JEV_CONFIDENCE_THRESHOLD", _DEFAULT_CONFIDENCE_THRESHOLD, minimum=0.0
    )
    return max(0.0, min(1.0, env_value))


# ── State building (public data only) ───────────────────────────────────────


def _hours_to_close(market: dict[str, Any]) -> float | None:
    """Whole-hour distance from now to the market's close, or None if unknown."""
    close = market.get("close_time")
    if not isinstance(close, str) or not close:
        return None
    try:
        # Kalshi sends whole-second RFC3339 UTC ("…Z"); normalize for parsing.
        parsed = datetime.fromisoformat(close.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = parsed - datetime.now(UTC)
    return round(delta.total_seconds() / 3600.0, 1)


def _price_str(market: dict[str, Any], key: str) -> str:
    raw = market.get(key)
    try:
        return f"${float(raw):.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _build_state(market: dict[str, Any]) -> str:
    """Compact plaintext situation for one market — the Jev `state`.

    Public fields only: title, quoted YES/NO bid+ask, YES spread, 24h volume,
    time-to-close, and a resolution-rule snippet. Deliberately terse so a
    batched call stays cheap.
    """
    title = str(market.get("title") or market.get("ticker") or "Unknown market")
    sub = market.get("yes_sub_title")
    if isinstance(sub, str) and sub.strip():
        title = f"{title} — {sub.strip()}"

    spread = _yes_spread(market)
    spread_str = f"${spread:.2f}" if spread is not None else "n/a (one-sided/empty book)"

    hours = _hours_to_close(market)
    if hours is None:
        close_str = "unknown"
    elif hours < 0:
        close_str = "already past close"
    elif hours < 48:
        close_str = f"{hours:.1f} hours"
    else:
        close_str = f"{hours / 24:.1f} days"

    rules = market.get("rules_primary")
    rules_str = ""
    if isinstance(rules, str) and rules.strip():
        snippet = rules.strip()[:_RULES_SNIPPET_CHARS]
        rules_str = f"\nResolution rule: {snippet}"

    return (
        f"Kalshi market: {title}\n"
        f"YES bid {_price_str(market, 'yes_bid_dollars')} / "
        f"YES ask {_price_str(market, 'yes_ask_dollars')}; "
        f"NO bid {_price_str(market, 'no_bid_dollars')} / "
        f"NO ask {_price_str(market, 'no_ask_dollars')}\n"
        f"YES bid/ask spread: {spread_str}\n"
        f"24h volume: {_volume_24h(market):.0f} contracts\n"
        f"Time to close: {close_str}"
        f"{rules_str}"
    )


def _jev_questions() -> dict[str, Any]:
    """The batched question set — ONE call scores edge and picks a side."""
    return {
        "edge": {
            "type": "score",
            "instructions": (
                "Judge how MISPRICED the YES price looks versus the situation "
                "described. Higher means the quoted price looks more clearly "
                "wrong (a bigger apparent edge); lower means it looks fair."
            ),
            # Score criteria is an ARRAY ordered low->high (the Jev contract).
            "criteria": list(_EDGE_CRITERIA),
        },
        "side": {
            "type": "choice",
            "instructions": (
                "If there is an edge at the quoted prices, which side is "
                "favorable to BUY? Choose 'pass' if neither side is favorable."
            ),
            "criteria": {
                "yes": "Buying YES at the quoted ask looks favorable.",
                "no": "Buying NO at the quoted ask looks favorable.",
                "pass": "No favorable side; skip this market.",
            },
        },
    }


# ── Heuristic fallback (deterministic liquidity/spread proxy) ────────────────


def _heuristic_score(market: dict[str, Any]) -> float:
    """A deterministic liquidity proxy used when Jev is unavailable.

    This is NOT an edge estimate — it is a proxy that favors higher 24h volume
    and tighter YES spreads, so the fallback ranking still surfaces tradeable
    markets rather than dead ones. A one-sided/empty book (no real spread) is
    penalized as the worst case. Bounded and monotonic; only relative order
    matters for ranking.
    """
    # Clamp volume at 0 before log10: _volume_24h happily floats a negative
    # `volume_24h_fp` from a malformed upstream payload, and log10 of <= 0
    # raises ValueError — which, called for every row, would crash the whole
    # tool and break the "never crashes" contract.
    volume_term = math.log10(max(0.0, _volume_24h(market)) + 1.0)
    spread = _yes_spread(market)
    spread_cents = spread * 100.0 if spread is not None else 100.0
    return volume_term / (1.0 + spread_cents)


def _clean_probabilities(probs: Any) -> dict[str, float] | None:
    """Project an untrusted `probabilities` map to known sides + finite floats.

    The raw value comes straight off the Jev response, so it must not be echoed
    verbatim: unknown keys are arbitrary attacker-/misconfig-controlled text,
    and a NaN/inf float re-serializes to bare `NaN`/`Infinity` — invalid JSON
    that breaks the ENTIRE tool result for a strict client (the same
    fail-closed-on-non-finite rule enforced on edge/confidence above). Keep only
    `yes`/`no`/`pass` keys whose value is a finite float.
    """
    if not isinstance(probs, dict):
        return None
    cleaned: dict[str, float] = {}
    for key, value in probs.items():
        if key not in _ALLOWED_SIDES:
            continue
        try:
            fv = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(fv):
            cleaned[key] = fv
    return cleaned or None


# ── Jev call (single market; returns parsed answer OR a fallback reason) ─────


def _parse_answers(payload: Any) -> dict[str, Any] | None:
    """Extract the fields we need from a Jev response, or None if malformed.

    Tolerant of extra fields and missing optionals, strict about the two we
    rank on (edge score + its confidence). A missing/garbage required field
    means we cannot trust the answer, so the caller falls back.
    """
    if not isinstance(payload, dict):
        return None
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        return None
    edge = answers.get("edge")
    side = answers.get("side")
    if not isinstance(edge, dict):
        return None

    try:
        raw_score = float(edge["score"])
        edge_conf = float(edge["confidence"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(raw_score) and math.isfinite(edge_conf)):
        return None

    # Jev's score runs 0..(N-1); normalize to 0..1 by dividing by N-1.
    edge01 = max(0.0, min(1.0, raw_score / (_EDGE_LEVELS - 1)))
    edge_conf = max(0.0, min(1.0, edge_conf))

    side_choice = None
    side_conf = None
    probabilities = None
    if isinstance(side, dict):
        # UNTRUSTED response: allowlist the choice to the three sides we asked
        # for — never pass an arbitrary string into a field a trading loop feeds
        # to prepare_order(side=...). Anything else becomes None.
        choice = side.get("choice")
        if isinstance(choice, str) and choice in _ALLOWED_SIDES:
            side_choice = choice
        try:
            sc = float(side["confidence"])
            if math.isfinite(sc):
                side_conf = max(0.0, min(1.0, sc))
        except (KeyError, TypeError, ValueError):
            pass
        probabilities = _clean_probabilities(side.get("probabilities"))

    return {
        "edge01": edge01,
        "edge_confidence": edge_conf,
        "side": side_choice,
        "side_confidence": side_conf,
        "probabilities": probabilities,
    }


async def _score_one(
    jev_client: httpx.AsyncClient,
    market: dict[str, Any],
    settings: _JevSettings,
) -> tuple[dict[str, Any] | None, str | None]:
    """POST one market to Jev. Returns (parsed_answer, None) or (None, reason).

    Catches every failure and maps it to a fallback-reason string; it never
    raises, so a single bad market can't take down the whole scan.
    """
    body = {
        "state": _build_state(market),
        "model": settings.model,
        "questions": _jev_questions(),
    }
    try:
        resp = await jev_client.post(settings.base_url, json=body)
    except httpx.TimeoutException:
        return None, "timeout"
    except httpx.RequestError:
        return None, "network"

    if resp.status_code == 402:
        return None, "credits"
    if resp.status_code == 429:
        return None, "rate"
    # Anything non-2xx is a failure. Redirects aren't followed (the Bearer must
    # not walk to another host), so a 3xx has no body to parse — bucket it as an
    # http_error rather than letting it fall through to a misleading "malformed".
    if resp.status_code >= 300:
        return None, "http_error"

    try:
        payload = resp.json()
    except (ValueError, httpx.DecodingError):
        return None, "malformed"

    parsed = _parse_answers(payload)
    if parsed is None:
        return None, "malformed"
    return parsed, None


def _fallback_market(market: dict[str, Any], reason: str) -> dict[str, Any]:
    """Build a result row for a market Jev didn't (or couldn't) score."""
    row = _minimal_market(market)
    heuristic = _heuristic_score(market)
    row.update(
        {
            "jev_scored": False,
            "fallback_reason": reason,
            "edge_score": None,
            "side": None,
            "confidence": None,
            "side_confidence": None,  # keep row schema uniform with scored rows
            "probabilities": None,
            "heuristic_score": round(heuristic, 6),
            "rank_score": round(heuristic, 6),
        }
    )
    return row


def _scored_market(market: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    """Build a result row from a trusted Jev answer above the threshold."""
    row = _minimal_market(market)
    rank = answer["edge01"] * answer["edge_confidence"]
    row.update(
        {
            "jev_scored": True,
            "fallback_reason": None,
            "edge_score": round(answer["edge01"], 6),
            "side": answer["side"],
            "confidence": round(answer["edge_confidence"], 6),
            "probabilities": answer["probabilities"],
            "side_confidence": answer["side_confidence"],
            "heuristic_score": round(_heuristic_score(market), 6),
            "rank_score": round(rank, 6),
        }
    )
    return row


# ── Orchestrator ─────────────────────────────────────────────────────────────


async def _score_markets(
    client: Any,
    *,
    limit: int,
    scan_limit: int,
    status: str,
    series_ticker: str | None,
    min_volume: float,
    scan_all: bool,
    min_confidence: float | None,
    jev_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Scan for liquid markets, score the top `limit` with Jev, rank the result.

    `jev_transport` is injectable for tests (httpx.MockTransport); prod leaves
    it None. Kalshi traffic goes through `client` (the shared, signed Kalshi
    client) — the Jev call uses its own credential-scoped httpx client.
    """
    settings = _JevSettings(min_confidence=min_confidence)

    # Runtime-clamp the number of markets scored — each is one PAID Jev call, so
    # the schema `le=50` bound is NOT sufficient (a direct `.fn` caller, or a
    # `scan_all` sweep with a huge `limit`, would otherwise fan out to thousands
    # of paid calls). This runtime guard is authoritative; the schema is additive.
    limit = max(1, min(limit, _MAX_SCORE_LIMIT))

    # Rank the scanned listing down to the top `limit` candidates, keeping the
    # resolution rule for the `state`. Streaming top-K bounds retention even on
    # a full sweep. Reuses discovery's ranker so behavior matches the rest of
    # the tool surface exactly.
    topk = _TopKByVolume(limit=limit, min_volume=min_volume, fields=",".join(_SCORING_SCAN_FIELDS))
    scan = await _scan_markets_excluding_mve(
        client,
        scan_limit=scan_limit,
        status=status,
        series_ticker=series_ticker,
        scan_all=scan_all,
        on_page=topk,
    )
    candidates = topk.result()

    reasons: Counter[str] = Counter()
    rows: list[dict[str, Any]]

    if not settings.enabled or not candidates:
        # Jev off (no key / misconfigured) or nothing to score → heuristic only.
        if not settings.enabled and candidates:
            reasons[settings.disabled_reason] = len(candidates)
        rows = [
            _fallback_market(m, settings.disabled_reason or "no_candidates") for m in candidates
        ]
        jev_status = "disabled" if not settings.enabled else "ok"
    else:
        rows = await _score_all(candidates, settings, jev_transport, reasons)
        scored = sum(1 for r in rows if r["jev_scored"])
        jev_status = "ok" if scored == len(rows) else ("degraded" if scored else "fallback")

    # Log ONCE per scan (aggregate), and only for GENUINE Jev failures — a scan
    # that's simply unconfigured (no key / bad base url) is steady state, not a
    # fault, and warning on it every call would be noise.
    if reasons and set(reasons) - _STEADY_STATE_REASONS:
        logger.warning(
            "kalshi_score_markets: Jev fallback for %d/%d market(s): %s",
            sum(reasons.values()),
            len(candidates),
            dict(reasons),
        )

    # Scored markets rank by confidence*edge; fallback markets rank by the
    # heuristic. They're on different bases, so scored sort ABOVE fallback,
    # each group ordered by its own rank_score.
    rows.sort(key=lambda r: (r["jev_scored"], r["rank_score"]), reverse=True)

    return {
        "markets": rows,
        "scanned": scan.scanned,
        "requests": scan.requests,
        "candidates_scored": len(candidates),
        "scan_limit": None if scan_all else max(1, min(scan_limit, 1000)),
        "scan_all": scan_all,
        "complete": scan.complete,
        "stopped_by": scan.stopped_by,
        "jev_enabled": settings.enabled,
        "jev_status": jev_status,
        "confidence_threshold": settings.threshold,
        "fallback_reasons": dict(reasons),
    }


async def _score_all(
    candidates: list[dict[str, Any]],
    settings: _JevSettings,
    jev_transport: httpx.AsyncBaseTransport | None,
    reasons: Counter[str],
) -> list[dict[str, Any]]:
    """Score every candidate via Jev with bounded concurrency; build result rows.

    Concurrency is bounded PROCESS-WIDE by the module semaphore, and the whole
    fan-out by an aggregate wall-clock deadline. Any unexpected failure — or the
    deadline — degrades the WHOLE batch to the heuristic; Jev must never be able
    to crash or indefinitely block the tool.
    """

    async def run(
        market: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
        async with _jev_semaphore:
            answer, reason = await _score_one(jev_client, market, settings)
        return market, answer, reason

    try:
        async with httpx.AsyncClient(
            transport=jev_transport,
            trust_env=False,  # never let env proxies / .netrc reroute the key
            follow_redirects=False,
            timeout=settings.timeout,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        ) as jev_client:
            async with asyncio.timeout(_WALL_CLOCK_SECONDS):
                outcomes = await asyncio.gather(*(run(m) for m in candidates))
    except TimeoutError:
        # The aggregate budget blew (a slow/drip-feeding host). Don't block the
        # caller any longer — degrade the whole batch to the heuristic.
        logger.warning(
            "kalshi_score_markets: Jev fan-out exceeded the %.0fs budget; using heuristic",
            _WALL_CLOCK_SECONDS,
        )
        reasons["deadline"] = len(candidates)
        return [_fallback_market(m, "deadline") for m in candidates]
    except Exception:  # Jev must never crash the tool — degrade the whole batch
        logger.warning("kalshi_score_markets: Jev client failed; using heuristic", exc_info=True)
        reasons["client_error"] = len(candidates)
        return [_fallback_market(m, "client_error") for m in candidates]

    rows: list[dict[str, Any]] = []
    for market, answer, reason in outcomes:
        if answer is None:
            reasons[reason or "unknown"] += 1
            rows.append(_fallback_market(market, reason or "unknown"))
        elif answer["edge_confidence"] < settings.threshold:
            reasons["lowconf"] += 1
            rows.append(_fallback_market(market, "lowconf"))
        else:
            rows.append(_scored_market(market, answer))

    # A 429 anywhere in the batch trips a global back-off so the NEXT scan
    # skips Jev entirely for a cooldown window (house rule: don't hammer a
    # rate-limited upstream from every caller).
    if reasons.get("rate") and settings.cooldown > 0:
        _trip_rate_cooldown(settings.cooldown)
    return rows


def register(server: FastMCP) -> None:
    """Register the Jev scoring tool — only when MCP_ALLOW_JEV_SCORING=1.

    Off by default (fail closed): a default clone never advertises this
    signal-adjacent capability. Mirrors the combo-creation / runtime-tuning
    registration gates.
    """
    config = server._kalshi_config  # type: ignore[attr-defined]
    if not getattr(config, "jev_scoring_enabled", False):
        return
    client = server._kalshi_client  # type: ignore[attr-defined]

    @server.tool
    async def kalshi_score_markets(
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        scan_limit: Annotated[int, Field(ge=1, le=1000)] = 200,
        status: str = "open",
        series_ticker: str | None = None,
        min_volume: Annotated[float, Field(ge=0)] = 0.0,
        scan_all: bool = False,
        min_confidence: Annotated[float, Field(ge=0, le=1)] | None = None,
    ) -> dict[str, Any]:
        """Score liquid markets by apparent edge — a cheap READ-ONLY first pass.

        Ranks the most liquid single (non-combo) markets and asks Jev (a fast
        typed-decision model) to score how mispriced each YES price looks and
        pick a favorable side. Built for a trading loop that scores many
        markets cheaply and escalates only the top few to a reasoning model.
        This tool places NO orders and commits no money.

        Jev is OPTIONAL. With no `TYPESAFE_API_KEY` configured (or on any Jev
        failure — out of credits, rate limit, timeout, malformed answer, or
        confidence below the threshold), the affected markets fall back to a
        deterministic liquidity/spread heuristic with `jev_scored=false`. The
        tool never fails because Jev is unavailable.

        Args:
            limit: How many top-by-volume candidates to score and return
                (1-50, default 10). Each is one paid Jev call, so it's hard-
                capped at 50 at runtime even for direct callers.
            scan_limit: How many markets to fetch+rank before taking the top
                `limit` (1-1000, default 200). IGNORED when `scan_all=True`.
            status: Lifecycle filter (default "open"); same values as
                `kalshi_find_liquid_markets` (comma-separated OK).
            series_ticker: Restrict the scan to one series (e.g. "KXMLBGAME").
            min_volume: Drop candidates below this 24h volume before scoring.
            scan_all: Sweep as much of the listing as the internal caps allow
                before ranking, instead of just the first `scan_limit`. Check
                `complete`/`stopped_by` in the result.
            min_confidence: Override the Jev confidence threshold (0-1) below
                which a market falls back to the heuristic. Default from
                `MCP_JEV_CONFIDENCE_THRESHOLD` (0.6).

        Returns:
            `markets`: result rows, ranked. Jev-scored rows (highest
                `confidence * edge`) sort above heuristic-fallback rows (ranked
                by the liquidity proxy) — the two use different bases, so
                always read `jev_scored`. Each row carries the minimal market
                projection plus a uniform set of scoring fields (present on
                every row, null on fallback rows): `jev_scored`, `edge_score`
                (0-1 or null), `side` ("yes"/"no"/"pass" or null — never any
                other value), `confidence`, `side_confidence`, `probabilities`
                (a map over yes/no/pass, or null), `heuristic_score`,
                `rank_score`, `fallback_reason`.
            `jev_enabled`: whether a usable Jev key was configured this scan.
            `jev_status`: "ok" (all scored, or nothing to score), "degraded"
                (some fell back), "fallback" (all Jev calls fell back), or
                "disabled" (no key / not usable this scan).
            `fallback_reasons`: aggregate count of why markets fell back
                (e.g. no_api_key, credits, rate, rate_cooldown, timeout,
                deadline, network, http_error, malformed, lowconf).
            `confidence_threshold`: the threshold in force.
            `scanned`/`requests`/`complete`/`stopped_by`/`scan_all`/
                `scan_limit`: scan coverage, same meaning as
                `kalshi_find_liquid_markets`.
            `candidates_scored`: how many markets were sent to scoring.
        """
        return await _score_markets(
            client,
            limit=limit,
            scan_limit=scan_limit,
            status=status,
            series_ticker=series_ticker,
            min_volume=min_volume,
            scan_all=scan_all,
            min_confidence=min_confidence,
        )
