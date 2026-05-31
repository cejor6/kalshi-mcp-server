# kalshi-mcp-server

A Model Context Protocol server for [Kalshi](https://kalshi.com)
prediction markets. Native RSA-PSS auth, token-bucket rate limiting,
demo/prod safety controls.

> **Status — alpha.** Auth (REST + WS), rate limiting, safety controls,
> 26 tools across REST + live channels, and 4 resources are in place.
> A long-lived multiplexed WebSocket session and `kalshi://markets/{ticker}/orderbook`
> live resource are planned for v0.2.

---

## Why this server

Most existing Kalshi MCPs are thin wrappers around a handful of REST
endpoints. This one aims to be:

- **Native Kalshi.** Real RSA-PSS signer that handles the gotchas
  (path-without-query-string, ms timestamps, separate demo/prod keys).
- **Rate-limit aware.** Client-side token bucket mirrors Kalshi's 2026
  read/write budget model, so the server can't spam the API into a 429.
- **Safe by default.** Refuses to start against prod without an explicit
  opt-in flag. Refuses to write without a separate trading-enabled flag.
  Order-time controls (size cap, daily cap, cash reserve) are all
  operator-configurable.
- **Hosted-deploy friendly.** Accepts the private key as either a file
  path OR an env var with inline PEM, so it works on platforms without
  filesystem mounts.
- **Fork-able.** MIT, no personal data, CI/CD set up so PR contributions
  flow through `main` without ever triggering a production deploy — only
  tagged releases (`v*`) do. Your fork's deployment stays decoupled from
  this repo's, and your fork's contributors can't affect what you run.

## Install

### From source (the only option until v0.1 is published)

```bash
git clone https://github.com/cejor6/kalshi-mcp-server.git
cd kalshi-mcp-server
uv sync
```

### Docker

```bash
docker pull ghcr.io/cejor6/kalshi-mcp-server:latest
```

(Image only exists once a `v*` tag is published. See [DEPLOY.md](DEPLOY.md).)

## Configure

1. Generate a Kalshi API key at https://kalshi.com/account/profile (or
   the demo equivalent at https://demo.kalshi.co/account/profile). Save
   the private key — it is shown ONCE.

2. Put your secrets in **one** `.env` file. A good location for the
   MCP-client use case is `~/.kalshi/.env` (outside any repo). For local
   dev, the repo's own `.env` (gitignored) works too.

```bash
cp .env.example ~/.kalshi/.env
# edit ~/.kalshi/.env
```

3. At minimum, set:

```env
KALSHI_API_KEY_ID=<your-key-id>
KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/your_kalshi_private_key.pem
KALSHI_ENV=demo
```

For prod, also set:

```env
KALSHI_ENV=prod
KALSHI_ALLOW_PROD=1
KALSHI_TRADING_ENABLED=1   # only if you want writes
```

### How env vars are resolved

On startup, the server resolves config in this order (highest wins):

1. **Values already in the process environment** — set in the MCP client
   config's `env:` block, or exported in your shell.
2. **`.env` file** — loaded from `--env-file PATH` if you pass that flag,
   otherwise from `./.env` in the current working directory if it exists.
   Variables already in the environment from step 1 are **not** overridden.

So you can put secrets either inline in the MCP config (`env:`) or in a
file the config points at (`--env-file`). You don't need to do both.

## Use with Claude Desktop / Claude Code / Cursor

The cleanest pattern is to keep all your secrets in one `.env` file
outside any repo and pass `--env-file`:

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "kalshi": {
      "command": "kalshi-mcp",
      "args": ["--env-file", "/Users/you/.kalshi/.env"]
    }
  }
}
```

**Claude Code** (project `.mcp.json` or `~/.claude/mcp.json`):

```json
{
  "mcpServers": {
    "kalshi": {
      "type": "stdio",
      "command": "kalshi-mcp",
      "args": ["--env-file", "/Users/you/.kalshi/.env"]
    }
  }
}
```

If you prefer inline env vars in the MCP config (and don't mind them
sitting in JSON), that works too:

```json
{
  "mcpServers": {
    "kalshi": {
      "command": "kalshi-mcp",
      "env": {
        "KALSHI_API_KEY_ID": "your-key-id",
        "KALSHI_PRIVATE_KEY_PATH": "/path/to/your/private_key.pem",
        "KALSHI_ENV": "demo"
      }
    }
  }
}
```

> **Why not just `.env` in the project dir?** MCP clients spawn the
> server as a subprocess from their own working directory (typically
> your home dir on macOS/Linux, the client's install dir on Windows), so
> a `.env` sitting in this repo wouldn't get found. Hence `--env-file`
> to point at it explicitly. Local development from the project dir
> still works without flags — the server auto-loads `./.env` when
> launched there.

## Tools

| Group | Tools |
|---|---|
| Exchange / account | `kalshi_get_exchange_status`, `kalshi_get_exchange_schedule`, `kalshi_get_api_limits`, `kalshi_get_environment` |
| Discovery | `kalshi_get_markets`, `kalshi_get_market`, `kalshi_get_event`, `kalshi_get_events`, `kalshi_get_series`, `kalshi_get_trades` |
| Market data | `kalshi_get_orderbook`, `kalshi_get_market_candlesticks`, `kalshi_get_event_candlesticks`, `kalshi_get_market_trades` |
| Portfolio | `kalshi_get_balance`, `kalshi_get_positions`, `kalshi_get_orders`, `kalshi_get_fills`, `kalshi_get_settlements` |
| Orders (write) | `kalshi_prepare_order`, `kalshi_confirm_order`, `kalshi_cancel_order`, `kalshi_decrease_order`, `kalshi_get_order` |
| Live (WebSocket) | `kalshi_get_live_orderbook`, `kalshi_sample_trades` |

Write tools require `KALSHI_TRADING_ENABLED=1`. `kalshi_prepare_order` runs
local safety checks and returns a `confirmation_id`; nothing is sent to
Kalshi until you call `kalshi_confirm_order` with that token. Cancel and
decrease bypass the trading-enabled flag — they only reduce exposure.

## Resources

| URI | Description |
|---|---|
| `kalshi://environment` | Current env, safety caps, rate-limit headroom (no API call) |
| `kalshi://balance` | Cash + buying power |
| `kalshi://positions` | Open positions (unsettled) |
| `kalshi://orders` | Resting orders (open / partially filled) |

A WebSocket-backed live-orderbook resource (`kalshi://markets/{ticker}/orderbook`)
is planned — for now, use the `kalshi_get_live_orderbook` tool which
opens a transient WS, samples the book, and returns the current
snapshot + delta arrival rate.

## Safety model

This server is deliberately conservative for the same reason your bank's
ATM is — small mistakes shouldn't cost large amounts.

- `KALSHI_ENV=prod` **requires** `KALSHI_ALLOW_PROD=1`. The server
  refuses to start without both.
- All write tools require `KALSHI_TRADING_ENABLED=1`. The default is
  read-only.
- Per-order caps (`MCP_MAX_ORDER_SIZE_USD`, `MCP_DAILY_LIMIT_USD`,
  `MCP_MAX_CONTRACTS_PER_ORDER`, `MCP_CASH_RESERVE_USD`) are checked
  before the request reaches Kalshi.

See [AGENTS.md](AGENTS.md) for the full design.

## Deployment

Use it locally as a stdio server (Claude Desktop, Claude Code, Cursor)
or run it as a remote HTTP MCP behind an OAuth proxy.

For remote deployment, the recommended setup is **image-deploy**: a
production host (Render, Fly.io, Cloud Run, ECS, anything that supports
pulling container images) pulls the image that's built and pushed when
you tag a release (`git tag v0.1.0`). This decouples deployments from
PR merges — PRs to `main` only ever run tests, never push a new image —
so a malicious or careless PR cannot affect what's running in your
container.

See [DEPLOY.md](DEPLOY.md) for the rationale and a worked example with
Render.

## Contributing

PRs welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first — there are a
few rules around auth changes, secret hygiene, and test conventions.

## License

[MIT](LICENSE).

## Acknowledgments

- [FastMCP](https://github.com/jlowin/fastmcp) — MCP framework.
- [Kalshi](https://docs.kalshi.com) — the underlying API.
