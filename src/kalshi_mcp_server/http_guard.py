"""Network-perimeter guards for the *unauthenticated* HTTP mode.

Only relevant when `--transport http` runs WITHOUT the OAuth proxy, i.e. with
`MCP_ALLOW_INSECURE_HTTP=1`. In that mode nothing at the MCP layer checks who
is calling, so two cheap checks keep the blast radius to what the operator
intended:

1. `is_loopback_bind(host)` — `cli.py` refuses a non-loopback `--host` unless
   `MCP_ALLOW_INSECURE_PUBLIC_BIND=1` is also set. One flag shouldn't be
   enough to put an unauthenticated trade server on every interface.
2. `HostOriginGuard` — DNS-rebinding protection. A server bound to
   `127.0.0.1` is still reachable from a malicious web page: the page's
   hostname is re-pointed at 127.0.0.1 and the browser happily sends it
   requests (same-origin from its point of view). Those requests carry the
   attacker's hostname in `Host` (and `Origin`), so allowing only loopback
   names — plus whatever the operator lists in `MCP_ALLOWED_HOSTS` — stops it.

Why only in no-auth mode: with OAuth on, every MCP request needs a bearer
token that a rebinding page has no way to obtain, so the attack buys nothing;
and Render-style proxies forward the public hostname as `Host`, which this
guard would otherwise have to be taught about. The MCP SDK ships an
equivalent (`mcp.server.transport_security`), but FastMCP's `http_app` does
not expose its settings, so this is a ~40-line ASGI equivalent passed through
`server.run(..., middleware=[...])`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from urllib.parse import urlsplit

logger = logging.getLogger("kalshi_mcp_server")

LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def is_loopback_bind(host: str) -> bool:
    """True iff binding `host` exposes the server only on loopback.

    Fails closed: anything that isn't `localhost` or a loopback IP literal
    (`0.0.0.0`, `::`, a LAN address, an arbitrary hostname that *might*
    resolve to loopback) counts as non-loopback.
    """
    value = host.strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _has_control_chars(value: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)


def _is_loopback_ipv4_literal(hostname: str) -> bool:
    """Any 127.0.0.0/8 literal — matches what `is_loopback_bind` accepts,
    so binding 127.0.0.2 doesn't 421 every request addressed to it."""
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return addr.version == 4 and addr.is_loopback


def _hostname_from_host_header(value: str) -> str | None:
    """`example.com:8000` → `example.com`; `[::1]:8000` → `::1`.

    Returns None for anything urlsplit can't make sense of (including a
    value carrying userinfo, a path, or control characters — not a legal
    Host header; urlsplit would otherwise silently strip tabs/newlines).
    """
    if _has_control_chars(value):
        return None
    value = value.strip()
    if not value or any(c in value for c in "/@?#\\ "):
        return None
    try:
        parsed = urlsplit(f"//{value}")
        # Accessing .port validates it (raises ValueError if malformed).
        _ = parsed.port
    except ValueError:
        return None
    return parsed.hostname  # lowercased by urlsplit


def _hostname_from_origin(value: str) -> str | None:
    if _has_control_chars(value):
        return None
    value = value.strip()
    if not value or value == "null":
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    return parsed.hostname


def parse_allowed_hosts_env() -> frozenset[str]:
    """Extra hostnames from `MCP_ALLOWED_HOSTS` (comma-separated).

    Each entry may be a bare hostname or `host:port`; the port is ignored
    (rebinding is about the *name*, and the bind port is fixed anyway).
    """
    raw = os.environ.get("MCP_ALLOWED_HOSTS", "")
    hosts: set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name = _hostname_from_host_header(part)
        if name:
            hosts.add(name)
    return frozenset(hosts)


class HostOriginGuard:
    """Pure-ASGI middleware: refuse requests whose Host/Origin isn't allowed.

    - `Host` must be present and name an allowed host (a loopback name, any
      127.0.0.0/8 literal, or an `MCP_ALLOWED_HOSTS` entry) → else 421.
    - `Origin`, when present, must name an allowed host → else 403.
      (Absent `Origin` is normal for non-browser clients.)

    Matches status codes with the MCP SDK's `TransportSecurityMiddleware`.
    Applies to `http` and `websocket` scopes; `lifespan` passes through.
    """

    def __init__(self, app, allowed_hosts: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.allowed = LOOPBACK_HOSTNAMES | frozenset(h.lower() for h in allowed_hosts)

    def _is_allowed(self, hostname: str | None) -> bool:
        if hostname is None:
            return False
        return hostname in self.allowed or _is_loopback_ipv4_literal(hostname)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        headers: dict[str, str] = {}
        for key, value in scope.get("headers") or []:
            name = key.decode("latin-1").lower()
            if name in {"host", "origin"}:
                if name in headers:
                    # Duplicate Host/Origin: ambiguous which one a downstream
                    # component would honor. Refuse rather than guess.
                    await self._reject(scope, send, 400, "Duplicate Host or Origin header")
                    return
                headers[name] = value.decode("latin-1")

        host = _hostname_from_host_header(headers.get("host", ""))
        if not self._is_allowed(host):
            logger.warning("Refused request with Host header %r", headers.get("host"))
            await self._reject(scope, send, 421, "Invalid Host header")
            return

        if "origin" in headers:
            origin_host = _hostname_from_origin(headers["origin"])
            if not self._is_allowed(origin_host):
                logger.warning("Refused request with Origin header %r", headers["origin"])
                await self._reject(scope, send, 403, "Invalid Origin header")
                return

        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope, send, status: int, message: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = message.encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
