# kalshi-mcp-server

A Model Context Protocol server for [Kalshi](https://kalshi.com)
prediction markets. Native RSA-PSS auth, token-bucket rate limiting,
demo/prod safety controls.

> **Status — alpha.** Auth, rate-limiting, config, and safety scaffolding
> are in place. Tools land in subsequent commits. Designed to be forked
> and deployed.

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
- **Fork-able.** MIT, no personal data, clean Pattern A CI/CD that lets
  others deploy their own instance without touching yours.

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
2. Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
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

## Use with Claude Desktop / Claude Code / Cursor

Claude Desktop (`claude_desktop_config.json`):

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

Claude Code (project `.mcp.json` or `~/.claude/mcp.json`):

```json
{
  "mcpServers": {
    "kalshi": {
      "type": "stdio",
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

## Tools (planned)

Coming in subsequent commits. The intended v0.1 surface:

| Group        | Tools |
|--------------|-------|
| Discovery    | `kalshi_search_markets`, `kalshi_get_market`, `kalshi_get_event`, `kalshi_get_series`, `kalshi_resolve_ticker` |
| Market data  | `kalshi_get_orderbook`, `kalshi_get_candlesticks`, `kalshi_get_trades` |
| Portfolio    | `kalshi_get_balance`, `kalshi_get_positions`, `kalshi_get_fills`, `kalshi_get_settlements` |
| Orders       | `kalshi_prepare_order`, `kalshi_confirm_order`, `kalshi_cancel_order`, `kalshi_amend_order` |
| Account      | `kalshi_get_api_limits`, `kalshi_get_exchange_status` |

## Resources (planned)

| URI | Description |
|---|---|
| `kalshi://environment` | Current env, tier, rate-limit headroom |
| `kalshi://balance` | Cash + portfolio value |
| `kalshi://positions` | Open positions |
| `kalshi://orders/open` | Resting orders |
| `kalshi://markets/{ticker}` | Single market snapshot |
| `kalshi://markets/{ticker}/orderbook` | Live (WS-backed) orderbook |

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

## Self-deployment

This repo uses **Pattern A** (image-deploy) so that public PRs cannot
trigger deployments to the maintainer's instance. See
[DEPLOY.md](DEPLOY.md) for the full setup, with Render as a worked
example.

## Contributing

PRs welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first — there are a
few rules around auth changes, secret hygiene, and test conventions.

## License

[MIT](LICENSE).

## Acknowledgments

- [FastMCP](https://github.com/jlowin/fastmcp) — MCP framework.
- [Kalshi](https://docs.kalshi.com) — the underlying API.
- Prior art that informed the design:
  [yakub268/kalshi-mcp](https://github.com/yakub268/kalshi-mcp),
  [alexandermazza/kalshi-trading-mcp](https://github.com/alexandermazza/kalshi-trading-mcp),
  [joinQuantish/kalshi-mcp](https://github.com/joinQuantish/kalshi-mcp).
