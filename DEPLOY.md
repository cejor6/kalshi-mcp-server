# Deployment guide

This server is designed for **Pattern A** deployment: the public repo
holds the code, and your production instance pulls a tagged container
image. PRs from the community land in `main` without affecting your
running instance — only a `git tag v...` does that.

This document covers:

1. The why behind Pattern A.
2. A worked example on [Render](https://render.com) (any image-deploy
   target works the same way: Fly.io, Railway, ECS, Cloud Run, etc.).
3. Required env vars.
4. Operational notes (rotation, health checks, OAuth proxy).

---

## Why Pattern A

The naive setup — "Render auto-deploys from `main`" — is unsafe for a
public repo. A malicious PR could merge code that exfiltrates the
secrets baked into the deployment env. Even with branch protection,
anything in `main` runs in your container with your real Kalshi key.

Pattern A breaks that link:

- CI runs tests on every PR. **CI never touches your deployment.**
- A separate `release.yml` workflow builds and pushes a versioned image
  to GHCR. It runs **only on tag push**.
- Only you (and anyone you explicitly grant push access) can publish
  tags, because `main` is branch-protected and tag pushes require repo
  write access.
- Your production host (Render, etc.) is configured to pull the image,
  NOT to build from source.

PRs in main → no deploy. `git tag v0.1.0 && git push origin v0.1.0` →
new image → Render picks it up.

---

## Cutting a release

```bash
# Ensure main is green and the version in pyproject.toml + __init__.py
# matches the tag you're about to cut.
git checkout main
git pull

# Tag and push. The release workflow does the rest.
git tag v0.1.0
git push origin v0.1.0
```

The `Release` workflow will:

1. Build a multi-arch image (amd64 + arm64).
2. Push it to `ghcr.io/cejor6/kalshi-mcp-server:v0.1.0` and `:latest`.

---

## Render setup (worked example)

1. **Create a new Web Service** → Source: "Deploy an existing image
   from a registry" (NOT "Connect a Git repo").
2. **Image URL:** `ghcr.io/<your-github-username>/kalshi-mcp-server:latest`
   (or a pinned tag like `:v0.1.0` if you don't want automatic updates).
   If you're deploying the upstream image without forking, that's
   `ghcr.io/cejor6/kalshi-mcp-server:latest` — but for any serious
   deployment you almost certainly want to fork, audit, and publish your
   own image so you control what code runs.
3. **Plan:** Starter ($7/mo) is fine for stdio-equivalent traffic.
4. **Health check:** TCP/port-listen (no HTTP path) — the MCP `/mcp`
   endpoint returns 401/405 to unauthenticated requests, which Render
   would interpret as unhealthy if you used an HTTP health check.
5. **Environment variables:** see below.
6. **Auto-deploy:** Render polls the registry; set the refresh interval
   in service settings (default is fine).

To deploy a new version: tag the repo, wait for the release workflow
to push to GHCR, and Render will pick up the new `:latest` (or your
pinned tag if you bump it).

---

## Environment variables

### Required

| Key | Purpose |
|---|---|
| `KALSHI_API_KEY_ID` | Kalshi key ID (from profile page) |
| `KALSHI_PRIVATE_KEY_PEM` | Inline PEM contents. Render env vars accept the full PEM as one value. Newlines as `\n` are fine — the code normalizes them. |
| `KALSHI_ENV` | `demo` for testing; `prod` for real money |

If `KALSHI_ENV=prod`, also set:

| Key | Purpose |
|---|---|
| `KALSHI_ALLOW_PROD` | Must be `1` |

To allow writes:

| Key | Purpose |
|---|---|
| `KALSHI_TRADING_ENABLED` | Must be `1` to enable order placement |

### HTTP transport (for remote MCP)

| Key | Purpose |
|---|---|
| `MCP_TRANSPORT` | `http` |
| `PORT` | Render injects this — read by the CLI automatically |

### Optional safety overrides

| Key | Default | Purpose |
|---|---|---|
| `MCP_MAX_ORDER_SIZE_USD` | 25 | Refuse single orders above this cost |
| `MCP_DAILY_LIMIT_USD` | 250 | Refuse if projected daily spend would exceed |
| `MCP_MAX_CONTRACTS_PER_ORDER` | 100 | Hard cap on contracts |
| `MCP_CASH_RESERVE_USD` | 0 | Never spend below this cash floor |

---

## OAuth proxy (for claude.ai remote MCP)

If you're exposing this server as a remote MCP for a claude.ai custom
connector or a Routine, you need an OAuth proxy in front of it —
claude.ai's connector form only supports OAuth, not static bearer
tokens.

**The proxy is now bundled.** Install with the `[oauth]` extras (already
included in the published Docker image) and configure it via env vars.
Local stdio use does not need any of this and ignores the OAuth vars
entirely.

### Required env vars when running with OAuth

| Key | Purpose |
|---|---|
| `MCP_TRANSPORT=http` | Switch from stdio to HTTP |
| `GITHUB_CLIENT_ID` | OAuth App Client ID — github.com → Settings → Developer settings → OAuth Apps |
| `GITHUB_CLIENT_SECRET` | OAuth App Client Secret (shown once at creation) |
| `MCP_BASE_URL` | Public URL of the server (no trailing slash). e.g. `https://kalshi-mcp-XXXX.onrender.com` |
| `MCP_ALLOWED_GITHUB_LOGINS` | Comma-separated GitHub logins permitted to invoke tools (e.g. just `cejor6`) |

### Strongly recommended

| Key | Purpose |
|---|---|
| `MCP_JWT_SIGNING_KEY` | 64-byte URL-safe random — signs proxy-issued JWTs; keep stable across restarts so claude.ai tokens survive redeploys. Generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `MCP_REDIS_URL` | `rediss://default:<password>@<host>.upstash.io:6379` — persistent DCR client store. Without it, every redeploy boots claude.ai out and forces a reconnect. Free Upstash tier is sufficient. |

### Fail-closed behavior

If `MCP_TRANSPORT=http` is set without OAuth configuration, the server
**refuses to start**. This is by design — an unauthenticated trade
server reachable over HTTP is a footgun. The override
`MCP_ALLOW_INSECURE_HTTP=1` exists for local-only dev (e.g. testing
http transport on localhost) but should never be used for a
non-localhost deploy.

### GitHub OAuth App callback URL

When you register the OAuth App at github.com → Settings → Developer
settings → OAuth Apps, set:

- **Homepage URL:** your `MCP_BASE_URL`
- **Authorization callback URL:** `<MCP_BASE_URL>/auth/callback`

### Connecting from claude.ai

claude.ai → Settings → Connectors → **+ Add custom connector**:

- **Name:** any (e.g. `Kalshi MCP (demo)`)
- **Remote MCP server URL:** `<MCP_BASE_URL>/mcp`
- **Advanced settings:** leave OAuth Client ID and Secret **blank** —
  the server advertises Dynamic Client Registration (DCR), so claude.ai
  will self-register a client.

Click Add → GitHub OAuth screen → Authorize → land back in claude.ai →
the connector shows "Connected" with the tool list visible.

---

## Operational notes

### Rotating Kalshi keys

1. Generate a new key pair in Kalshi's account portal.
2. Update `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PEM` in your
   deployment env.
3. Restart the service.
4. Delete the old key in Kalshi's portal once you've confirmed the new
   one is working.

### If a tag was published in error

GHCR images can't be unpublished cheaply, but you can:

1. Push a new tag with the correct content (e.g. `v0.1.1` instead of
   `v0.1.0`).
2. Bump your Render image pin to the corrected tag.

Don't try to "rewrite" an existing tag's content — it confuses anyone
who has already pulled the older version.

### What never appears in logs

- API key IDs (logged with `--redacted--` suffix only)
- Signatures
- Private key material
- Request bodies on writes (logged at DEBUG level only)
