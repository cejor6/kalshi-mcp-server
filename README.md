# kalshi-mcp-server

<!-- mcp-name: io.github.cejor6/kalshi-mcp-server -->

[![CI](https://github.com/cejor6/kalshi-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/cejor6/kalshi-mcp-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

A Model Context Protocol server for [Kalshi](https://kalshi.com)
prediction markets. Native RSA-PSS auth, async token-bucket rate
limiting, two-step prepare/confirm order flow with safety caps,
optional bundled OAuth proxy for remote-MCP deployments, 26 tools +
4 resources across REST and WebSocket. MIT, designed to be forked.

Works with any [MCP](https://modelcontextprotocol.io) client —
locally via stdio (Claude Desktop, Claude Code, Cursor, Zed,
Continue, Cline, Goose, etc.) or remotely as a self-hosted HTTP
server (claude.ai custom connectors today, any OAuth-capable MCP
client in the future).

> ⚠️ **This software lets an LLM place trades. Read [DISCLAIMER.md](DISCLAIMER.md)
> before deploying.** Trading prediction markets involves substantial
> risk of loss. AI agents make mistakes — sometimes confidently. The
> authors are not liable for any losses. Test in demo (`KALSHI_ENV=demo`,
> `KALSHI_TRADING_ENABLED=0`) until you understand the failure modes.

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

## Use with an MCP client (stdio)

Every MCP stdio client uses the same shape: a `command` to launch the
server, optional `args`, optional `env`. The differences are just the
file/UI where you put the config.

Three install patterns work — pick whichever fits your environment.

### Pattern A — `pipx install` (cleanest, recommended once published)

Installs `kalshi-mcp` to a globally-available, isolated environment.
[pipx](https://pipx.pypa.io/) is the modern Python tool for this:

```bash
pipx install kalshi-mcp-server
```

MCP client config then collapses to:

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

Update with `pipx upgrade kalshi-mcp-server` when you want the latest.

### Pattern B — `uv run` against a local clone

Best if you've cloned the repo and have [uv](https://docs.astral.sh/uv/)
installed. Point the MCP client at `uv` with `--directory`:

```json
{
  "mcpServers": {
    "kalshi": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/absolute/path/to/kalshi-mcp-server",
        "kalshi-mcp",
        "--env-file", "/Users/you/.kalshi/.env"
      ]
    }
  }
}
```

`uv run` activates the project's venv automatically. Update with
`git pull` + restart the MCP client. Useful for development /
hacking on the server itself.

### Pattern C — Docker against the public image

Best for users without Python installed, or who prefer container
isolation:

```json
{
  "mcpServers": {
    "kalshi": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-v", "/Users/you/.kalshi/demo.pem:/secrets/demo.pem:ro",
        "-e", "KALSHI_API_KEY_ID=<your-key-id>",
        "-e", "KALSHI_PRIVATE_KEY_PATH=/secrets/demo.pem",
        "-e", "KALSHI_ENV=demo",
        "ghcr.io/cejor6/kalshi-mcp-server:latest"
      ]
    }
  }
}
```

The `-v` mount bind-mounts your PEM file read-only into the
container; `KALSHI_PRIVATE_KEY_PATH` points at that path. Secrets
live in the JSON config — fine for a single-user machine.

### Where to put this config:

| Client | Config location |
|---|---|
| [Claude Desktop](https://claude.ai/download) | `claude_desktop_config.json` (Settings → Developer) |
| [Claude Code](https://claude.com/claude-code) | project `.mcp.json` or `~/.claude/mcp.json` |
| [Cursor](https://cursor.com) | Settings → MCP → Add new MCP Server (UI fills the same JSON) |
| [Zed](https://zed.dev) | `~/.config/zed/settings.json` under `context_servers` |
| [Continue](https://continue.dev) | `~/.continue/config.json` under `experimental.modelContextProtocolServers` |
| [Cline](https://cline.bot) | Cline settings → MCP Servers → Edit JSON |
| [Goose](https://block.github.io/goose/) | `~/.config/goose/config.yaml` under `extensions` |

If you'd rather inline secrets in the MCP config (acceptable for
local dev where the config file is on your own machine):

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
> your home dir on macOS/Linux, the client's install dir on Windows),
> so a `.env` sitting in this repo wouldn't get found. Hence
> `--env-file` to point at it explicitly. Running the server directly
> from the project dir (no client) still works without flags — the
> CLI auto-loads `./.env` when launched there.

## Use as a remote MCP service

For clients that don't speak local stdio — currently the main one
being **claude.ai's custom connector form**, which only supports
OAuth-protected HTTP — host the server somewhere reachable and point
the client at it. The OAuth proxy is bundled with the server; you
just need to configure it.

See [DEPLOY.md](DEPLOY.md) for an end-to-end walkthrough using
Render + GitHub OAuth + Upstash Redis. Other image-deploy hosts
(Fly.io, Cloud Run, ECS, Railway) work the same way — Render is just
the worked example.

## Tools

| Group | Tools |
|---|---|
| Exchange / account | `kalshi_get_exchange_status`, `kalshi_get_exchange_schedule`, `kalshi_get_api_limits`, `kalshi_get_environment` |
| Discovery | `kalshi_get_markets`, `kalshi_find_liquid_markets`, `kalshi_get_market`, `kalshi_get_event`, `kalshi_get_events`, `kalshi_get_series`, `kalshi_get_trades` |
| Market data | `kalshi_get_orderbook`, `kalshi_get_market_candlesticks`, `kalshi_get_event_candlesticks`, `kalshi_get_market_trades` |
| Portfolio | `kalshi_get_balance`, `kalshi_get_positions`, `kalshi_get_orders`, `kalshi_get_fills`, `kalshi_get_settlements` |
| Orders (write) | `kalshi_prepare_order`, `kalshi_confirm_order`, `kalshi_cancel_order`, `kalshi_decrease_order`, `kalshi_get_order` |
| Live (WebSocket) | `kalshi_get_live_orderbook`, `kalshi_sample_trades` |

Write tools require `KALSHI_TRADING_ENABLED=1`. `kalshi_prepare_order` runs
local safety checks and returns a `confirmation_id`; nothing is sent to
Kalshi until you call `kalshi_confirm_order` with that token. Cancel and
decrease bypass the trading-enabled flag — they only reduce exposure.

**Listing markets for an LLM:** `kalshi_get_markets` / `kalshi_get_market`
accept `minimal=true` to project each market down to a small whitelist of
triage fields (ticker, prices, sizes, volume, status, close time). Prefer
this over `compact=true` for scanning — `compact` is a blacklist and barely
shrinks multivariate (`KXMVE…`) combo markets, whose bulk lives in
`custom_strike` / `mve_selected_legs` / long sub-titles. Pass a custom
`fields="ticker,yes_bid_dollars,…"` to override the default whitelist.
View precedence is `fields` > `minimal` > `compact` > full. `kalshi_get_event`
/ `kalshi_get_events` accept the same `minimal` / `fields` for their nested
markets (the event objects themselves only have the `compact` view).

**Don't gate on `liquidity_dollars`:** Kalshi currently returns it as
`0.0000` on every market, even deep books — measure liquidity from the
orderbook (best bid/ask + resting size) plus `volume_24h_fp` /
`open_interest_fp`. It is stripped from `compact` and `minimal` views.

**Finding tradeable markets:** the default open listing is dominated by
multivariate (`KXMVE…`) combo markets with empty/one-sided books. Pass
`mve_filter="exclude"` to `kalshi_get_markets` to drop them server-side, or
use `kalshi_find_liquid_markets` — it excludes combos, ranks by 24h volume,
and returns a short minimal-projection shortlist. (Kalshi has no server-side
sort, so the helper's ranking is over a bounded scan window, reported as
`scanned` in the result.)

**Event ticker vs market ticker:** a *market* ticker carries an outcome
suffix (`…PITHOU-HOU`); an *event* ticker (`…PITHOU`) does not. Passing an
event ticker to `kalshi_get_market` / `kalshi_get_orderbook` / `kalshi_get_markets`
used to fail silently (404, or an empty book/list read as "no liquidity").
These tools now detect that case and raise an actionable hint naming the
real market tickers instead.

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

Use it locally as a stdio server with any MCP client, or run it as a
remote HTTP MCP behind an OAuth proxy.

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

[MIT](LICENSE). See also [DISCLAIMER.md](DISCLAIMER.md) — the MIT
license disclaims warranty; DISCLAIMER.md spells out the trading- and
AI-specific risks you're accepting by using this software.

## Acknowledgments

- [FastMCP](https://github.com/jlowin/fastmcp) — MCP framework.
- [Kalshi](https://docs.kalshi.com) — the underlying API.
