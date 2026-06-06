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

Four per-order checks (`src/kalshi_mcp_server/safety.py`), all enforced
locally before any request goes to Kalshi:

- `MCP_MAX_ORDER_SIZE_USD` — refuse orders whose worst-case cost exceeds it.
- `MCP_DAILY_LIMIT_USD` — refuse if projected daily spend would exceed it.
- `MCP_MAX_CONTRACTS_PER_ORDER` — refuse orders with more contracts than this.
- `MCP_CASH_RESERVE_USD` — refuse if the order would leave less than this
  in cash.

All are operator-configurable. The defaults are conservative on purpose —
fork-and-adjust to your risk tolerance.

**Runtime adjustment (without a redeploy).** The env vars set the *hard
ceiling*. The `kalshi_set_safety_limits` operator tool can tighten any of
the four limits at runtime — but **never loosen one past its env ceiling**
(`SafetyController` validates direction-aware: the three caps may only go
down, `cash_reserve_usd` may only go up; the env value is the absolute
loosest setting). This is the fail-closed property: a runtime actor or bug
can shrink the risk envelope but never widen it. To raise a ceiling you
must change the env var and redeploy. The limits in force vs. their
ceilings are visible via `kalshi_get_environment` and the
`kalshi://environment` resource.

**Persistence.** Runtime overrides live in a `LimitsStore`
(`safety.py` / `safety_store.py`). The default is in-memory — a restart
reverts to the env ceilings. When `MCP_REDIS_URL` is set (the same Redis
the OAuth proxy can use), a Redis-backed store persists overrides across
restarts/redeploys, so a "fast clamp-down" sticks. Two invariants hold
the safety line: (1) only the *sparse* set of fields that differ from the
ceiling is stored, and on load each is **re-clamped to the current env
ceiling** — so a stale/corrupt stored value can only ever tighten, never
loosen, and raising an env ceiling takes effect for any field not actively
tightened; (2) the in-memory update always succeeds even if the store
write fails (the emergency clamp-down is never blocked by a Redis blip).
Caveat: persistence is single-replica-coherent (write-through + load-on-
boot); if you scale past one instance, a mid-life change won't reach other
replicas until they restart.

A fourth gate fires at startup when HTTP transport is used:
**`http` transport refuses to start without OAuth configured** unless
`MCP_ALLOW_INSECURE_HTTP=1` is explicitly set. An unauthenticated HTTP
trade server is a serious footgun; the policy fails closed.

See [DISCLAIMER.md](DISCLAIMER.md) for the full risk disclosure. The
safety controls reduce blast radius but don't eliminate it.

---

## OAuth proxy (HTTP-transport only)

`src/kalshi_mcp_server/oauth.py` wraps the FastMCP server with a
`GitHubProvider` OAuth proxy when relevant env vars are set:

- `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `MCP_BASE_URL` — required
  to enable the proxy
- `MCP_ALLOWED_GITHUB_LOGINS` — required for HTTP transport (defense in
  depth — the proxy lets anyone authenticate; the middleware rejects
  tool calls from logins outside this list)
- `MCP_JWT_SIGNING_KEY` — stable key for proxy-issued JWTs (optional;
  generated per-process if unset, invalidates tokens on restart)
- `MCP_REDIS_URL` — persistent DCR client storage (optional; in-memory
  if unset, requires reconnect after each redeploy)

Stdio transport ignores all of these. Local stdio clients (Claude
Desktop, Claude Code, Cursor) authenticate trivially — the MCP client
itself is the operator.

---

## Deployment contracts

A few invariants the deployed image needs to honor. If you change any
of these in a refactor, check that they still hold.

- **HTTP must bind to `0.0.0.0`** when containerized. The CLI defaults
  to `127.0.0.1` (safe for local dev), so the **Dockerfile must
  override via `CMD ["--host", "0.0.0.0"]`**. Without this, hosted
  deploys return 502 — the gateway can't reach a localhost-only bind.
- **The published image must include `[oauth]` extras.** The Dockerfile
  installs with `uv pip install ".[oauth]"`, not `.`. Without the
  extras, the OAuth proxy crashes on import when `MCP_REDIS_URL` is set.
- **The Dockerfile's `ENTRYPOINT` runs as a non-root user** (uid 10001).
  Don't add steps that require root after the `USER app` directive.
- **The release workflow tags both `:vX.Y.Z` and `:latest`** so image-
  deploy hosts on `:latest` pick up new versions automatically.

These contracts are part of the test surface only indirectly (the
Render deploy is the integration test). When in doubt, smoke-test
against Render before tagging a release.

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

## Discovery / market-listing gotchas

Hard-won lessons from running scan agents against prod. The discovery
tools encode these; don't regress them.

- **Two projection axes on `kalshi_get_markets`.** `compact` is a
  *blacklist* (`_VERBOSE_MARKET_FIELDS`) — preserves forward-compat as
  Kalshi adds fields. `minimal` is a *whitelist* (`_MINIMAL_MARKET_FIELDS`)
  — bounds worst-case payload size for `KXMVE…` combo markets, whose bulk
  lives outside the blacklist (`custom_strike`, `mve_selected_legs`, long
  `*_sub_title`s). Prefer `minimal` for listing/scanning. The same
  projection applies to nested markets on the event tools. Precedence:
  `fields` > `minimal` > `compact` > full.
- **Combos dominate the default listing.** A bare `status=open` page is
  mostly multivariate combos with empty/one-sided books. Kalshi has **no
  server-side sort**, but it does have `mve_filter` (`exclude`/`only`).
  `kalshi_get_markets(mve_filter="exclude")` de-noises server-side;
  `kalshi_find_liquid_markets` layers a windowed, volume-ranked shortlist
  on top (be honest about the window — it reports `scanned`).
- **`liquidity_dollars` is always `0.0000`** (even on deep books). Don't
  reintroduce it into curated views or gate on it; rank/assess via the
  orderbook + `volume_24h_fp` / `open_interest_fp`.
- **Event ticker ≠ market ticker.** An event ticker passed where a market
  ticker is expected fails silently (404, empty book, empty list). The
  tools call `_event_hint` on the failed path to raise an actionable error
  naming the real market tickers. `_event_hint` must **fail open** (return
  None on any error) so it never masks the caller's original problem.

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

## Git author identity (recommended, not enforced)

The maintainer uses a **GitHub noreply email** for commits in this
repo, to keep real email out of public commit metadata. Contributors
are welcome to follow the same convention but are not required to:

```bash
# Derive your noreply email from your GitHub numeric ID (no UI hunting):
NOREPLY=$(gh api user --jq '"\(.id)+\(.login)@users.noreply.github.com"')

# Then, in your fork or local clone, scope it to this repo only:
git config user.email "$NOREPLY"
git config user.name  "$(gh api user --jq .login)"
```

The format is always `<numeric-id>+<username>@users.noreply.github.com`.
You can also enable **"Keep my email addresses private"** at
https://github.com/settings/emails — that toggles broader privacy
behavior on your account and unlocks the related
**"Block command line pushes that expose my email"** option, which
GitHub will refuse pushes whose most-recent commit author matches
your real email. The UI on that page doesn't display the constructed
noreply directly, so use the `gh api` snippet above.

Whatever email you commit under will appear in the public commit log
— keep that in mind.

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
- **Trading strategies / signal generation.** This server exposes the
  Kalshi API. The decision of *what* to trade belongs in a separate
  program that consumes this MCP. Keeping that separation makes the
  server trustable and fork-able.
- **Multi-user tenant isolation.** The server's identity is the
  operator's Kalshi key — there is one trading account per running
  process. `MCP_ALLOWED_GITHUB_LOGINS` controls *who can invoke tools*,
  not *which Kalshi account they hit*. Adding multi-user support
  (different Kalshi keys per logged-in GitHub user) would require a
  significant architectural change.

---

## Useful references

- Kalshi docs index: https://docs.kalshi.com
- LLM-readable docs index: https://docs.kalshi.com/llms.txt
- Auth + first request: https://docs.kalshi.com/getting_started/api_keys
- Rate limits: https://docs.kalshi.com/getting_started/rate_limits
- WebSocket quickstart: https://docs.kalshi.com/getting_started/quick_start_websockets
- Model Context Protocol spec: https://modelcontextprotocol.io
- FastMCP: https://github.com/jlowin/fastmcp
