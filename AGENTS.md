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
`kalshi://environment` resource. Non-finite values (`NaN`/`inf`) are
rejected on every path — on a write, on load from the store, and on the
env ceilings themselves — since they would otherwise slip past the
`<`/`>` comparisons and silently disable a cap.

The tool is gated by `MCP_ALLOW_RUNTIME_LIMIT_TUNING` (default on). Set it
to `0` to not register `kalshi_set_safety_limits` at all — useful on a
shared HTTP deploy where allowlisted users shouldn't be able to re-tune
the safety envelope; limits then change only via env var + redeploy.

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

**Combo (parlay) creation has its OWN gate.**
`kalshi_create_combo_market` (`tools/multivariate.py`) POSTs to
`/multivariate_event_collections/{ticker}` to materialize a combo market
ticker. Read the distinction carefully before "tidying" it:

- It **places no order and commits no money.** Kalshi requires a combo to be
  created once before it can be looked up or traded; risk arrives later, via
  the normal order path. So it is gated on `MCP_ALLOW_COMBO_CREATION`
  (default **0**), NOT on `KALSHI_TRADING_ENABLED`. Coupling it to the money
  flag would mean a read-only scout couldn't build parlays at all, which is
  precisely the posture we want that capability in. `ComboCreationDisabledError`
  is a distinct type from `TradingDisabledError` so the two gates can't be
  confused.
- When the flag is off the tool is **not registered at all** (same pattern as
  `kalshi_set_safety_limits` under `MCP_ALLOW_RUNTIME_LIMIT_TUNING=0`), so a
  read-only deploy doesn't advertise the capability to the model.
- The scarce resource is **Kalshi's 5000-creations-per-WEEK account quota**,
  not dollars, so it gets its own counter — `SafetyController._combo_creations`,
  bounded by `MCP_MAX_COMBO_CREATIONS_PER_DAY` (default 100). Do NOT merge it
  into the USD daily counter; they measure different budgets. Like the spend
  counter it is in-process and resets on restart: it bounds a runaway loop
  within a session, it is not a durable ledger.
- The slot is **reserved before the POST and released only on an unambiguous
  4xx** — not counted afterwards. Check-then-await-then-record leaves the whole
  round trip between the check and the increment, so concurrent calls all read
  the same count and every one passes a ceiling only one should have. And a
  timeout or 5xx is ambiguous: Kalshi may well have created the combo, and a
  timeout is precisely what triggers the retry loop this ceiling exists to
  bound, so an ambiguous outcome must cost budget. Don't "simplify" it back to
  record-on-success.

This is the one write tool that deliberately does NOT follow the
"build an `OrderIntent`, call `safety.check_order`, generate an idempotency
key, call `record_order_committed`" contract in "How to add a new tool" —
there is no price, count, or cash flow to check. It follows the analogous
shape (`safety.check_combo_creation()` before the wire call,
`safety.record_combo_creation()` after success) and pre-flights the leg set
against the collection's own `size_min`/`size_max`/`is_all_yes` rules, which
Kalshi otherwise rejects with an opaque 400. That contract still binds every
order-placing tool without exception.

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
- `MCP_JWT_SIGNING_KEY` — stable key for proxy-issued JWTs (generated
  per-process if unset, invalidating tokens on restart; **required** when
  `MCP_REDIS_URL` is set, and also the material the storage-encryption key
  is derived from)
- `MCP_REDIS_URL` — persistent DCR client storage (optional; in-memory
  if unset, requires reconnect after each redeploy)
- `MCP_REDIS_COLLECTION_PREFIX` — collection namespace for this
  deployment (default `kalshi`). Give every server sharing a Redis DB a
  distinct value; changing it is a keyspace move, so treat it like a key
  rotation

Stdio transport ignores all of these. Local stdio clients (Claude
Desktop, Claude Code, Cursor) authenticate trivially — the MCP client
itself is the operator.

**`health_check_interval` on the Redis client is load-bearing — don't
strip it.** On the `Redis.from_url` path, `Connection.__init__` ends up
with `Retry(NoBackoff(), 0)` and `health_check_interval=0` — no liveness
check and zero retries. (`Redis.__init__`'s `retries=10` default does not
apply on this path; check with `pool.make_connection().retry.get_retries()`
rather than reading the signature.) A managed Redis that drops idle TLS
connections — Upstash does — then makes the next pooled use raise
`ConnectionError: Error UNKNOWN while writing to socket. Connection lost.`
Setting the interval is what removes that.

Be precise about the rest, because two reviewers have already misread it:

- `socket_keepalive` and `socket_connect_timeout` match redis-py 8's own
  defaults. They're pinned so the guarantee survives the `redis>=5.0.0`
  floor, not because they change behavior today. `socket_timeout` is the
  one that genuinely matters across that floor — older releases default it
  to `None`, where a blackholed connection blocks `read_response` forever.
- `REDIS_RETRIES` is **1 on purpose.** redis-py applies the retry policy at
  three nested layers (`execute_command`, `connect`, `check_health`), so the
  attempt count multiplies to roughly `(retries + 1) ** 2` TLS connects per
  command. At 8 store ops inside `exchange_refresh_token` (~10 for the whole
  `/token` request), `retries=3` turns a Redis
  outage into a multi-minute hang on an OAuth request. Raising it trades a
  real availability regression for very little.
- The retry covers a **single command** on a fresh connection. It does not
  make rotation atomic — there's no transaction, so a process death or an
  outage longer than the budget still strands the connector.

Why any of it matters: when the upstream issues a refresh token, claude.ai
refreshes periodically and each refresh **rotates** the refresh token with
one-time-use enforcement — the proxy deletes the old refresh JTI *before*
persisting the new refresh metadata. A failure inside that sequence strands
the client with a refresh token the server has no record of, killing the
connector until a human reconnects. For unattended cron/routine consumers
that's a silent outage.

Don't quote a refresh cadence without checking which branch applies: the
upstream's own `expires_in` wins whenever present, and FastMCP's 1h
`DEFAULT_ACCESS_TOKEN_EXPIRY_SECONDS` is only the fallback for an IdP that
omits it. A GitHub App with "Expire user authorization tokens" enabled (the
GitHub config that issues refresh tokens) sends its own `expires_in` — 8h at
time of writing — so rotation is a few times a day. A classic OAuth App, or a
GitHub App with expiration off, returns no refresh token and never rotates.

`tests/test_oauth.py` pins each kwarg and drives `Retry.call_with_retry`
with the real configured policy, proving it survives one transient failure
and gives up after the budget. Read that as covering the *policy*, not the
send/reconnect path — the calls use a hand-written coroutine, and nothing
simulates a blip mid-rotation. That path stays unverified.

**Supplying `client_storage` opts out of two things FastMCP does for you.
Both are re-applied by hand in `_build_client_storage` — don't unwrap them.**
The store is `FernetEncryptionWrapper(PrefixCollectionsWrapper(RedisStore))`:

- **Encryption at rest.** FastMCP wraps storage in `FernetEncryptionWrapper`
  only when `client_storage is None`, so handing it a store silently
  persisted `UpstreamTokenSet` — the live upstream access + refresh tokens —
  as plaintext JSON. We re-apply it via the wrapper's own
  `source_material=`/`salt=` overload, which runs PBKDF2 at 1.2M iterations.
  Use that overload, not `fernet=` with a hand-derived key: the stretching
  is the point, since `MCP_JWT_SIGNING_KEY` is an operator-supplied string
  and a single-hash derivation would let anyone with Redis read access
  brute-force a weak one offline. It also keeps us off FastMCP's unexported
  `jwt_issuer.derive_jwt_key`, which could move under our wide
  `fastmcp>=2.0.0` floor. (FastMCP's own refresh tokens were never the
  exposure: only their SHA-256 is stored, as a key.)
  The wrapper is subclassed via `_strict_encryption_wrapper_class()` to
  **reject values lacking the `__encrypted_data__` marker.** The stock one
  returns them as-is, which would let anyone with Redis *write* access plant
  an accepted plaintext record — an attacker-chosen `ProxyDCRClient` with its
  own `redirect_uris`, say — without ever knowing the key. Don't unwrap that
  back to the plain `FernetEncryptionWrapper`. Collection and key names are
  still plaintext, and encryption remains confidentiality; the subclass is
  what supplies the integrity half.
- **Collection isolation.** The store's `default_collection` is inert —
  FastMCP passes its own names (`mcp-oauth-proxy-clients`,
  `mcp-refresh-tokens`, …) to each adapter, and those are identical in every
  FastMCP OAuth-proxy server. Without `PrefixCollectionsWrapper`, two
  servers sharing a Redis DB land in one keyspace and resolve each other's
  registrations and JTIs. The prefix is what makes one shared Redis safe —
  give each deployment a distinct `MCP_REDIS_COLLECTION_PREFIX`.

Consequence worth knowing: `MCP_REDIS_URL` now **requires**
`MCP_JWT_SIGNING_KEY`, and `_build_client_storage` raises `ConfigError`
without it. That combination was already broken — an unset signing key is
generated per process, so every restart invalidated all issued tokens and
the persistent store bought nothing — and it leaves no stable secret to
derive a storage key from. Failing closed beats encrypting with a key that
dies with the process.

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

**Bake parameter constraints into the type, not just the body.** When a
param has a fixed accepted-value set or a numeric range, encode it in the
signature so it surfaces in the tool's JSON-Schema (`inputSchema`) and the
client/LLM is steered to valid values at generation time:

- discrete values → `Literal[...]` → emits a JSON-Schema `enum`
  (e.g. `action: Literal["buy", "sell"]`, `period_interval: Literal[1, 60, 1440]`).
  An *optional* enum is `Literal[...] | None` — the `enum` then nests under
  `anyOf` (still honored).
- numeric bounds → `Annotated[int, Field(ge=1, le=1000)]` → emits
  `minimum`/`maximum` (e.g. every `limit`, `limit_price_cents` 1–99).

This is **steering, not enforcement**: per the MCP spec the *server* MUST
validate inputs and `inputSchema` is advisory. The schema steers the model
and lets schema-aware clients reject early, but a direct `.fn` caller (and
our own tests) bypass Pydantic, so where it matters keep a runtime check.
"Where it matters" is the key distinction:

- **Safety-relevant / state-mutating params** (anything on the order write
  path — `count`, `limit_price_cents`, `action`, `side`, …): the runtime
  guard is **mandatory and authoritative**. NEVER let the schema bound be
  the only enforcement — `safety.check_order` / the `_validate_*` helpers
  must still run and must remain the source of truth. The schema bound is
  additive, never substitutive.
- **Benign read / pagination params** passed straight through as Kalshi
  query args (`limit`, `depth`, `scan_limit`, …): the schema steers and
  Kalshi itself is the backstop (it 400s or clamps). A redundant local
  guard is optional here; don't add guard-bloat for read knobs.

Two things that deliberately stay *unconstrained* in the schema, so a future
contributor doesn't "helpfully" add a `Literal`/range and break them:

- **CSV multi-value params** (`status`, `count_filter`) accept
  comma-separated values (`"open,closed"`), which a `Literal` enum can't
  express — leave them free `str` and document the accepted tokens in the
  docstring.
- **Direction-relative limits** (`kalshi_set_safety_limits`' USD knobs) are
  validated tighter-only against env ceilings in `SafetyController`, not by a
  fixed range, so `ge`/`le` doesn't fit.

Also: constraints that span *multiple* params (e.g. the candlestick
≤5000-candle window cap, `(end_ts - start_ts) / (period_interval * 60)`)
can't be expressed in JSON-Schema at all — those are runtime-only with an
actionable error message. And don't put strategy/computed values in a
constraint; just the API's own accepted ranges.

Order-placing tools (anything that puts money at risk) MUST:
- Call `safety.assert_trading_enabled()` at the top
- Build an `OrderIntent` and call `safety.check_order(...)`
- Generate a client-side idempotency key
- Call `safety.record_order_committed(...)` after the response succeeds

A state-mutating tool that risks NO money still needs a gate, a
consumable-budget check, and a record-after-success — just not this one.
`kalshi_create_combo_market` is the worked example; see the combo-creation
note in "Safety model" for why it has its own flag and its own counter
rather than reusing the order contract. Adding a second such tool means
following that shape, not weakening the order contract to fit.

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
- **Orderbook levels ascend by price, and BOTH sides are bids.** The best
  level is the LAST element, not the first. `GET /markets/orderbooks` (the
  batch endpoint, up to 100 tickers) has **no `depth` parameter** — unlike
  the single-market one, which takes 0-100 — so `kalshi_get_orderbooks`
  truncates client-side and MUST slice `[-depth:]`. Verified live: a full
  book's last `yes_dollars` entry equals the market's `yes_bid_dollars` /
  `yes_bid_size_fp`, and the single-market endpoint at `depth=3` returns
  exactly the last three entries in the same ascending order. Slicing
  `[:depth]` would hand every scan lens the *worst* levels — dust orders —
  and nothing would visibly break. Don't "fix" it.
- **Batch reads must clamp their local token cost to the bucket capacity.**
  Kalshi bills batch ops per item, so `kalshi_get_orderbooks` debits
  `10 x len(tickers)` — but `TokenBucket.acquire` *rejects* any cost above
  capacity outright, and on the Basic tier 25 items (250) exceeds the
  200-token read budget. Unclamped, every large batch raised `RateLimitError`
  locally and silently degraded to the per-ticker fallback. Relatedly, that
  fallback re-raises `RateLimitError` instead of catching it: answering "you
  are going too fast" with N more requests is how a soft limit becomes a hard
  one.
- **Market objects carry no `series_ticker`.** It exists only as a query
  *filter* (confirmed in the API reference and against a live prod response).
  `kalshi_get_series_summary` therefore DERIVES the series from the ticker
  prefix per Kalshi's `SERIES-EVENTSUFFIX-OUTCOME` convention. That's a
  convention, not a contract — the tool says so in its docstring rather than
  presenting derived series as authoritative.
- **The batch candlestick cap is candles x TICKERS, not candles.**
  `GET /markets/candlesticks` takes up to 100 tickers and returns at most
  10,000 candles TOTAL across all of them, so a window that is perfectly legal
  for one market is 5x over budget for five.
  `market_data.py:_validate_batch_candlestick_window` runs the single-market
  guards first and then this multiplication; don't collapse the two, and keep
  the message that names how many markets the window affords.
- **Forecast percentiles are 0-9999, not 0-100.** The median is 5000. Passing
  50 is *legal* and silently means the 0.5th percentile — a wrong answer, not
  an error — which is why the docstring leads with the scale and the validator
  only enforces the hard bounds. `period_interval=0` (5-second bars) is legal
  on that endpoint and ONLY that one.
- **`custom_strike`'s three columns are PARALLEL ARRAYS — index is the only
  thing linking them.** Two different ways of destroying that have already
  shipped here, both caught in review, so use `_split_csv_positional` and
  nothing else:
  1. **Never de-dupe** (`_parse_fields` does — it's a *field whitelist*
     helper, where a repeat is a caller mistake). A real all-YES combo's sides
     column is `"yes,yes,yes…"`; de-duped it collapsed to one element, failed
     the length-alignment check, and returned every leg with `side: null`
     while still reporting `resolvable: true`.
  2. **Never drop interior empties.** Filtering `if part.strip()` deletes an
     empty field and shifts every later value up an index. If that makes the
     filtered length coincidentally match the ticker column, alignment
     *passes* and legs get the WRONG side — strictly worse than the null a
     mismatch produces, and it round-trips into `kalshi_create_combo_market`
     as a parlay betting the wrong direction. Only TRAILING empties are
     dropped; alignment is decided on positional length.

  The regression tests use REPEATED values and RAGGED columns on purpose — the
  original tests used `"yes,no"`, where both bugs are invisible.
- **Tickers interpolated into a URL path go through `_validate_path_ticker`,
  not just `_validate_ticker`.** Paths are built with f-strings and the signed
  canonical message is rebuilt the same way, so a separator in a ticker sends
  the request somewhere other than what was signed. Two layers, both needed:
  a charset allowlist (blocks `/`, `?`, `#`, whitespace), AND an explicit
  all-dots check — `.` is legal *inside* a ticker (`…-B5.25`), so the charset
  pass alone let a bare `.` or `..` through, and those are dot-SEGMENTS httpx
  resolves away (`.` collapses to the LIST endpoint). Rejecting beats
  escaping.

  **Scope, so nobody assumes blanket coverage:** this is currently applied to
  `collection_ticker` on the multivariate GET/POST only. Other f-string path
  sites still use the looser `_validate_ticker`, and `order_id` in `orders.py`
  has no charset check at all — worth closing, since `auth.py` strips the
  query before signing, so an `order_id` containing `?` yields a
  *signature-valid* request carrying caller-chosen query params.
- **Combo legs live on the MARKET, not on the collection.**
  `/multivariate_event_collections/{ticker}` describes the *universe* a combo
  may be built from (`associated_event_tickers`, `size_min`/`size_max`,
  `is_all_yes`) — it cannot tell you which legs a specific auto-generated
  combo selected. Those come from the market object's `mve_selected_legs`
  (structured, authoritative) or its `custom_strike` parallel-CSV fallback.
  When the CSV columns don't line up, `kalshi_get_combo_legs` nulls `side`
  rather than guessing, and when neither source exists it returns a
  structured `resolvable: false`. It must never fall back to splitting the
  combo's title string — that's the lossy thing the tool exists to replace.
- **Full-sweep paging is budgeted, and the budgets are late-bound.**
  `_scan_markets_excluding_mve(scan_all=True)` is capped by request count,
  wall clock, and total markets; whichever binds first is reported as
  `stopped_by` so `complete` is never a guess. The three defaults resolve
  from the module constants *inside* the function, not in the signature — a
  def-time default would freeze them and silently ignore any override.
- **Candlesticks 400 on two silent footguns** (`market_data.py:_validate_candlestick_window`,
  confirmed live). (1) `period_interval` accepts **only `1` / `60` / `1440`**
  (minute/hour/day) — `5`, `240`, etc. return an opaque `400 bad request`
  that an agent loops on. It's now a `Literal[1, 60, 1440]` enum in the
  schema *and* a runtime check. (2) A window may span at most **5000
  candles** — `(end_ts - start_ts) / (period_interval * 60) <= 5000` — or
  Kalshi 400s; this is cross-param so it's runtime-only, with a message
  telling the caller to widen the interval or narrow the window. Don't
  regress either guard, and don't re-add `5`/`240` to the docstring.

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

## Reviewing changes in this repo

A short checklist for anyone — human or agent — reviewing a diff here. The
author-conflict rule applies: if you wrote the code, the reviewers must be
independent fresh-context agents.

- **Validation is dual-layer — check both layers exist where they should.**
  Tool params with a fixed value set or numeric range carry a JSON-Schema
  `enum`/`minimum`/`maximum` (steering), AND a runtime check where it matters.
  For safety / state-mutating params (the order write path) the runtime guard
  (`safety.check_order`, the `_validate_*` helpers) MUST be authoritative —
  the schema bound is additive, never the sole enforcement. Flag any
  security-relevant bound that lives only in the schema. (See "How to add a
  new tool" for the full rule, including the CSV / direction-relative
  carve-outs.)
- **Candlestick guards** (`market_data.py:_validate_candlestick_window`):
  `period_interval` ∈ {1, 60, 1440}; window ≤ 5000 candles via `ceil` (the
  comment explains why — it's live-verified; don't "simplify" to `floor`/`//`).
  Both must reject *before* any HTTP call — the wiring tests assert
  `calls == []`.
- **No real API calls or account data in tests** — mock everything
  (`httpx.MockTransport`), generate RSA keys at test time.
- **Run it:** `uv run pytest` and `uv run pre-commit run --all-files` must be
  green before the PR, and re-run after any further commit.

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
- **Non-Kalshi data feeds — with ONE documented exception.** The rule
  stands: this server exposes the Kalshi API, and strategy/signal
  concerns live in the programs that consume it. The exception is
  `kalshi_fetch_external_data` (`tools/external_data.py`, added
  2026-07-10): a read-only, GET/https-only, exact-host-allowlisted
  fetch of a handful of public data APIs (Polymarket, NWS, Open-Meteo,
  Tennis Abstract, Deribit public). It exists because the claude.ai
  cloud-routine environment that consumes this server cannot egress to
  those hosts, and this server — already attached to every routine as
  a connector, running with unrestricted egress — is the only channel
  that reaches them. Decision notes: a single generic allowlisted tool
  was chosen over N typed per-feed tools (one operator, tiny call
  volume; the runtime allowlist + GET-only is the load-bearing control
  either way, and N typed tools would 5× the surface to review); no
  wildcard content hosts are allowlisted (a raw.githubusercontent.com
  entry was removed in review); fetched bodies are delimiter-wrapped
  UNTRUSTED data; DNS rebinding is an accepted, documented residual.
  Forks that don't want this surface can delete the module + its
  registration line — nothing else depends on it.
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
