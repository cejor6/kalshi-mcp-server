"""Async HTTP client for the Kalshi REST API.

Wraps `httpx.AsyncClient` with three Kalshi-specific concerns:

1. **Signing.** Every request is signed via `KalshiSigner` using the path
   Kalshi actually sees (the base-URL path + the per-request path) so
   that the signed canonical message matches the server's reconstruction.
2. **Rate limiting.** Each request acquires tokens from the appropriate
   bucket (read vs write) BEFORE going out, so we don't pile up on 429s.
3. **Error mapping.** Non-2xx responses raise `KalshiAPIError`
   (`RateLimitError` for 429) carrying the parsed error body so tools
   can surface meaningful messages.

Tool implementations consume this via `server._kalshi_client` (set up in
`cli.py` at server start).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from kalshi_mcp_server.auth import KalshiSigner
from kalshi_mcp_server.config import Config
from kalshi_mcp_server.errors import KalshiAPIError, RateLimitError
from kalshi_mcp_server.rate_limit import DEFAULT_ENDPOINT_COST, Bucket, KalshiRateLimiter

DEFAULT_TIMEOUT_S = 30.0

# Methods whose tokens come from the WRITE bucket (mutations). GET/HEAD
# always count as reads. PATCH/PUT/POST/DELETE deduct from writes.
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class KalshiClient:
    """Async client for `trade-api/v2`. One per server instance."""

    def __init__(
        self,
        *,
        config: Config,
        signer: KalshiSigner,
        rate_limiter: KalshiRateLimiter,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._signer = signer
        self._rate_limiter = rate_limiter
        # The path component of the base URL — Kalshi includes this in
        # the canonical message we sign. For prod that's "/trade-api/v2".
        self._base_path = urlsplit(config.rest_base).path or ""
        self._http = http_client or httpx.AsyncClient(
            base_url=config.rest_base,
            timeout=DEFAULT_TIMEOUT_S,
            follow_redirects=False,
        )

    @property
    def config(self) -> Config:
        return self._config

    @property
    def rate_limiter(self) -> KalshiRateLimiter:
        return self._rate_limiter

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- Public verb-shaped helpers ----------------------------------------

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        cost: float = DEFAULT_ENDPOINT_COST,
    ) -> dict[str, Any]:
        return await self._request("GET", path, params=params, cost=cost)

    async def post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        cost: float = DEFAULT_ENDPOINT_COST,
    ) -> dict[str, Any]:
        return await self._request("POST", path, json=json, cost=cost)

    async def put(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        cost: float = DEFAULT_ENDPOINT_COST,
    ) -> dict[str, Any]:
        return await self._request("PUT", path, json=json, cost=cost)

    async def patch(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        cost: float = DEFAULT_ENDPOINT_COST,
    ) -> dict[str, Any]:
        return await self._request("PATCH", path, json=json, cost=cost)

    async def delete(
        self,
        path: str,
        *,
        cost: float = DEFAULT_ENDPOINT_COST,
    ) -> dict[str, Any]:
        return await self._request("DELETE", path, cost=cost)

    # ---- Internal -----------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        cost: float = DEFAULT_ENDPOINT_COST,
    ) -> dict[str, Any]:
        method_up = method.upper()
        bucket = Bucket.WRITE if method_up in _WRITE_METHODS else Bucket.READ
        await self._rate_limiter.acquire(bucket, cost)

        # The signed path is the absolute path Kalshi sees (base path +
        # endpoint path), with query string stripped (the signer handles
        # that part).
        endpoint = path if path.startswith("/") else "/" + path
        sign_path = self._base_path + endpoint

        headers = self._signer.sign(method=method_up, path=sign_path).as_dict()
        headers["Accept"] = "application/json"
        if json is not None:
            headers["Content-Type"] = "application/json"

        try:
            resp = await self._http.request(
                method_up,
                endpoint,
                params=params,
                json=json,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise KalshiAPIError(
                status=0,
                message=f"Timeout calling Kalshi: {exc}",
            ) from exc
        except httpx.RequestError as exc:
            raise KalshiAPIError(
                status=0,
                message=f"Transport error calling Kalshi: {exc}",
            ) from exc

        return self._handle_response(resp)

    @staticmethod
    def _handle_response(resp: httpx.Response) -> dict[str, Any]:
        # Try to parse JSON even on errors — Kalshi returns structured
        # error bodies that are more useful than the raw text.
        body: dict[str, Any]
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {"raw": resp.text}

        if resp.status_code == 429:
            msg = _extract_error_message(body) or "rate limited"
            raise RateLimitError(status=429, message=msg, body=body)

        # 3xx redirects: we don't follow them automatically because they
        # almost always indicate a malformed request (e.g. an empty path
        # parameter that hit `/markets/` → 301 → `/markets`). Treat as
        # error so the agent gets a clear message instead of stray HTML
        # bodies leaking into a "success" path.
        if 300 <= resp.status_code < 400:
            location = resp.headers.get("location", "<unknown>")
            raise KalshiAPIError(
                status=resp.status_code,
                message=(
                    f"Unexpected {resp.status_code} redirect to {location!r}. "
                    "Likely cause: a path parameter (ticker, event_ticker, "
                    "order_id, etc.) is empty or malformed."
                ),
                body=body,
            )

        if 400 <= resp.status_code < 600:
            msg = _extract_error_message(body) or resp.reason_phrase or f"HTTP {resp.status_code}"
            raise KalshiAPIError(status=resp.status_code, message=msg, body=body)

        # 2xx — body is either dict or empty.
        if not isinstance(body, dict):
            return {"data": body}
        return body


def _extract_error_message(body: dict[str, Any]) -> str | None:
    """Pluck Kalshi's error message out of common response shapes.

    Kalshi has returned errors in several shapes historically. This helper
    tries the documented shape first, then a few fallbacks.
    """
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("message") or err.get("code")
    if isinstance(err, str):
        return err
    return body.get("message")
