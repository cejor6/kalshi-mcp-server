# Agent guide

This file is for **any agent** working on this repo — Claude Code, Cursor,
Codex, Aider, etc. Conventions live here; tool-specific files (like
`CLAUDE.md`) simply point back to this document.

If you're a human reading this for the first time, it's also a perfectly
good architectural overview — start with the "Repo layout" and "How
authentication works" sections.

---

## What this project is

A Model Context Protocol server for [Kalshi](https://kalshi.com),
implemented in Python on top of [FastMCP](https://github.com/jlowin/fastmcp).
It speaks the Kalshi REST API (and eventually the WebSocket API) and
exposes tools + resources that an LLM agent can use to query markets,
inspect a portfolio, and place trades.

The repo is designed to be **forked**. Conventions, safety controls, and
documentation should be useful to anyone who clones the project. Write
for a stranger, not for the current owner.

---

## Repo layout

```
kalshi-mcp-server/
├── src/kalshi_mcp_server/
│   ├── auth.py          RSA-PSS request signer
│   ├── rate_limit.py    Token-bucket limiter (Kalshi's read/write model)
│   ├── config.py        Env loader + prod/trading safety guards
│   ├── safety.py        Order-time controls (size, daily cap, reserve)
│   ├── errors.py        Exception hierarchy
│   ├── cli.py           FastMCP entrypoint
│   ├── tools/           MCP tool implementations
│   └── resources/       MCP resource implementations
├── tests/               pytest suite
├── .github/workflows/   CI (tests on PRs), Release (image build on tag)
├── Dockerfile           Multi-stage build, non-root runtime
├── pyproject.toml       Package metadata + ruff + pytest config
├── server.json / .yaml  MCP server registry manifests
├── AGENTS.md            This file
├── CLAUDE.md            Stub pointing here (Claude Code-specific)
├── CLAUDE.local.md      [GITIGNORED] Personal notes for a single dev
└── DEPLOY.md            Self-deployment guide (Pattern A: image-deploy)
```

---

## How authentication works

Kalshi uses RSA-PSS request signing. The contract is brittle — get any
piece wrong and the API returns `signature_invalid`. See `src/kalshi_mcp_server/auth.py`
for the canonical implementation.

Three things to remember:

1. **Path is signed without the query string.** `?limit=50` is part of
   the request but NOT part of the signed message. The `_path_without_query`
   helper handles this.
2. **Timestamp is in MILLISECONDS.** Not seconds. `time.time() * 1000`.
3. **The body is not signed.** Only `timestamp + METHOD + path`.

Headers on every authenticated request:
- `KALSHI-ACCESS-KEY` — the key ID
- `KALSHI-ACCESS-TIMESTAMP` — ms since epoch
- `KALSHI-ACCESS-SIGNATURE` — base64 of the RSA-PSS signature

WebSocket auth uses the same scheme. Sign `GET /trade-api/ws/v2` and pass
the headers on the upgrade handshake.

**Demo and prod use SEPARATE key pairs.** Cross-using a key produces an
auth failure that's hard to debug.

References:
- https://docs.kalshi.com/getting_started/api_keys
- https://docs.kalshi.com/getting_started/making_your_first_request

---

## How rate limiting works

As of April 2026, Kalshi uses a token-bucket model with **separate read
and write budgets** per account. See `src/kalshi_mcp_server/rate_limit.py`.

Tier defaults (read/write tokens per second):

| Tier      | Read | Write |
|-----------|------|-------|
| Basic     | 200  | 100   |
| Advanced  | 300  | 300   |
| Premier   | 1000 | 1000  |
| Paragon   | 2000 | 2000  |
| Prime     | 4000 | 4000  |

Most endpoints cost 10 tokens. Batch operations bill per item (except
`BatchCancelOrders`, which charges 0.2 per cancel). HTTP 429 is returned
with **no `Retry-After` header** — clients must back off themselves.

The limiter is consulted client-side BEFORE a request goes out, so the
server doesn't spam Kalshi during overload.

Reference: https://docs.kalshi.com/getting_started/rate_limits

---

## Safety model

Two startup guards (`src/kalshi_mcp_server/config.py`):

1. **`KALSHI_ENV=prod` requires `KALSHI_ALLOW_PROD=1`.** Refuses to start
   otherwise. This is intentional — a typo in a shell env shouldn't be
   enough to route real money.
2. **`KALSHI_TRADING_ENABLED=0` is the default.** Order-placement,
   cancellation, and amendment tools refuse to execute. Set the flag to
   `1` to enable writes.

Three per-order checks (`src/kalshi_mcp_server/safety.py`), all enforced
locally before any request goes to Kalshi:

- `MCP_MAX_ORDER_SIZE_USD` — refuse orders whose worst-case cost exceeds it.
- `MCP_DAILY_LIMIT_USD` — refuse if projected daily spend would exceed it.
- `MCP_CASH_RESERVE_USD` — refuse if the order would leave less than this
  in cash.

All three are operator-configurable. The defaults are conservative on
purpose — fork-and-adjust to your risk tolerance.

---

## How to add a new tool

1. Create a module under `src/kalshi_mcp_server/tools/`, e.g.
   `discovery.py`.
2. Define a `register(server: FastMCP) -> None` function that uses
   `@server.tool` to declare each tool.
3. Import + call your `register` from `tools/__init__.py:register_all_tools`.
4. Add a unit test under `tests/` that exercises the happy path with a
   mocked Kalshi response (use `httpx.MockTransport` or similar). **Never
   hit the real Kalshi API in tests.**
5. Update README.md's tool list.

Tool naming convention: `kalshi_<verb>_<noun>`, lowercase, snake_case.
Examples: `kalshi_search_markets`, `kalshi_get_balance`, `kalshi_place_order`.

Write tools (anything that mutates state) MUST:
- Call `safety.assert_trading_enabled()` at the top
- Build an `OrderIntent` and call `safety.check_order(...)`
- Generate a client-side idempotency key
- Call `safety.record_order_committed(...)` after the response succeeds

---

## How to add a new resource

1. Create a module under `src/kalshi_mcp_server/resources/`.
2. Register URI handlers via `@server.resource("kalshi://...")`.
3. Resources should be cheap to read repeatedly — cache where it makes
   sense (e.g. event metadata that rarely changes).
4. Live resources backed by WebSocket data subscribe lazily on first read.

URI scheme: `kalshi://<noun>[/<id>][/<subresource>]`. Examples:
- `kalshi://balance`
- `kalshi://markets/KXFED-26MAR19-B5.25`
- `kalshi://markets/KXFED-26MAR19-B5.25/orderbook`

---

## Testing conventions

- **No real account data in fixtures.** Mock everything. The CI runner
  has no Kalshi credentials and PRs from forks have no secrets exposure.
- **Generate RSA keys at test time**, don't commit a PEM. `conftest.py`
  provides an `rsa_private_key` fixture.
- **Async tests** use the `asyncio_mode = "auto"` setting in
  `pyproject.toml` — just write `async def test_...` and pytest handles
  the rest.
- **Cover the canonical-message contract carefully** — `test_auth.py`
  checks query-string stripping, method casing, and timestamp inclusion
  precisely because these are the parts most likely to drift.

---

## What NOT to commit

- Real or test `.pem` files. Generate keys on demand in fixtures.
- `.env`, `.envrc`, or any file with real values.
- Account-specific data (your subaccount IDs, your portfolio balances).
- Personal thresholds tuned to your risk (use the env-var defaults).
- Recorded API responses with real account IDs (anonymize first).
- Personal notes / strategies / scratchpads — those go in
  `CLAUDE.local.md`, which is gitignored. A template exists at
  `CLAUDE.local.md.example`.
- Tokens or webhook URLs of any kind.

The pre-commit hook (`detect-secrets`) blocks most of this. The CI
secret-scan job (`gitleaks`) is a second line of defense. Both can be
bypassed locally — discipline is the actual safeguard.

---

## What's deliberately NOT in this server

- **FIX protocol.** Kalshi supports it for institutional users. This
  server is for the REST + WS surface only.
- **OAuth for end users.** Authentication is RSA-PSS with the operator's
  own Kalshi key. If you want OAuth in front (e.g. for a remote MCP
  deployment where multiple users connect to one server instance), that
  belongs in a *proxy* in front of this server, not inside it. See
  DEPLOY.md.
- **Trading strategies / signal generation.** This server exposes the
  Kalshi API. The decision of WHAT to trade belongs in a separate program
  that consumes this MCP. Keeping that separation makes the server
  trustable and fork-able.

---

## Useful references

- Kalshi docs index: https://docs.kalshi.com
- LLM-readable docs index: https://docs.kalshi.com/llms.txt
- Auth + first request: https://docs.kalshi.com/getting_started/api_keys
- Rate limits: https://docs.kalshi.com/getting_started/rate_limits
- WebSocket quickstart: https://docs.kalshi.com/getting_started/quick_start_websockets
- Model Context Protocol spec: https://modelcontextprotocol.io
- FastMCP: https://github.com/jlowin/fastmcp
