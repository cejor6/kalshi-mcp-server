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

## OAuth proxy (for remote MCP deployment)

If you're exposing the server as a remote MCP for a Claude.ai connector
or similar, you need an OAuth proxy in front of it — claude.ai's custom
connector form only supports OAuth, not static bearer tokens.

This is **deliberately not bundled** into the server itself. Patterns
that work:

- **FastMCP `OAuthProxy` + `GitHubProvider`** running in the same
  container, restricting tool calls by GitHub login. This is what the
  Alpaca MCP setup uses; see that repo's `DEPLOY.md` for reference.
- **A separate reverse proxy** (Caddy, nginx) doing OAuth2 in front of
  this server.

If you go the FastMCP `OAuthProxy` route, your wrapper repo can wrap
this image and add the proxy module — no need to fork the upstream.

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
