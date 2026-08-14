"""Shared input validators for tool modules.

These were originally private helpers in `discovery.py`, but they are used
across `market_data`, `multivariate`, and `orders` — reaching for an
underscore-private name in a sibling tool module contradicts the "module
private" signal and couples the order write path to the discovery tool. They
live here instead, the one place every tool module can import from without
implying a dependency on any particular tool.

`discovery.py` re-exports them for back-compat, so existing
`from …tools.discovery import _validate_ticker` imports (and tests) keep
working; new code should import from here.
"""

from __future__ import annotations

from typing import Literal

from kalshi_mcp_server.errors import KalshiAPIError


def _validate_ticker(ticker: str, *, name: str = "ticker") -> str:
    """Reject empty/whitespace tickers before they reach Kalshi.

    Without this, an empty path parameter hits `/markets/` (trailing slash)
    which Kalshi 301-redirects to the LIST endpoint — that would either
    look like success returning the whole markets list, or surface as a
    confusing redirect error. Catching it here gives a clean message.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        raise KalshiAPIError(
            status=0,
            message=f"{name} must be a non-empty string, got {ticker!r}.",
        )
    return ticker.strip()


# Characters that are safe in a value we interpolate into a URL PATH segment:
# upper/lower alphanumerics plus `-`, `.` and `_`. Covers real Kalshi tickers
# ("KXFED-26MAR19-B5.25", "KXMVECROSSCATEGORY-SHARD1-R") and order-id UUIDs
# ("a1b2c3d4-...") alike. Every character NOT in this set that could appear is
# a URL separator (`/`, `?`, `#`, `&`, whitespace, …).
_PATH_SEGMENT_ALLOWED_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._"
)


def _validate_path_segment(
    value: str, *, name: str = "ticker", kind: Literal["ticker", "identifier"] = "ticker"
) -> str:
    """Validate a value that will be INTERPOLATED INTO A URL PATH segment.

    Tools build paths with f-strings (`/markets/{ticker}/orderbook`,
    `/portfolio/orders/{order_id}`), and the canonical message we sign is
    reconstructed the same way — critically, `auth._path_without_query` strips
    the query string before signing. So a value containing `/`, `?`, `#`, or a
    `..` segment changes which endpoint the request reaches while STILL
    producing a valid signature: `order_id="abc?foo=bar"` signs `/…/abc` but
    sends `/…/abc?foo=bar` with caller-chosen query params. It matters most on
    the state-mutating paths (order cancel/decrease, combo creation), where the
    value is model-supplied, and on any GET where a model steers the ticker.

    Restricting to the charset real tickers / order-ids use is the whole fix:
    every dangerous character is a separator and none are legal in either.
    Rejecting beats escaping — an escaped `/` would just 404 more confusingly.

    `kind` names the value in the error message ("ticker" / "identifier").
    """
    value = _validate_ticker(value, name=name)
    bad = sorted(set(value) - _PATH_SEGMENT_ALLOWED_CHARS)
    if bad:
        example = (
            "'KXFED-26MAR19-B5.25'"
            if kind == "ticker"
            else "an alphanumeric id with '-', '.' or '_'"
        )
        raise KalshiAPIError(
            status=0,
            message=(
                f"{name} contains characters that are not valid in a Kalshi {kind}: "
                f"{''.join(bad)!r}. Allowed: alphanumerics plus '-', '.' and '_' "
                f"(e.g. {example}). Got {value!r}."
            ),
        )
    # `.` is legal inside a value ("…-B5.25"), so the charset check above lets a
    # bare "." or ".." through — and those are dot-SEGMENTS, which httpx
    # resolves away: "." collapses the path to its parent, ".." climbs above it.
    # The signer signs the un-normalized path, so the request would land
    # somewhere other than what was signed. Reject a value that is nothing but
    # dots.
    if set(value) == {"."}:
        raise KalshiAPIError(
            status=0,
            message=(
                f"{name}={value!r} is a path segment, not a {kind} — '.' and '..' "
                "resolve to a different endpoint than the one we sign."
            ),
        )
    return value


def _validate_path_ticker(ticker: str, *, name: str = "ticker") -> str:
    """Back-compat alias: validate a TICKER interpolated into a URL path.

    Thin wrapper over `_validate_path_segment` — see it for the why.
    """
    return _validate_path_segment(ticker, name=name, kind="ticker")
