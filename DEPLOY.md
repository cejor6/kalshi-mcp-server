# Deployment guide

This guide walks you end-to-end through deploying `kalshi-mcp-server`
as a remote, OAuth-protected MCP service that any MCP client
supporting [Streamable HTTP](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http)
+ OAuth 2.1 can connect to.

Estimated time start-to-finish: **~20 minutes** of clicks + waits.

## Pick your path before reading further

Three legitimate ways to run this server, with very different setup
requirements:

| You want to... | Path | Setup time |
|---|---|---|
| Use it from your **own machine** with a local MCP client (Claude Desktop, Cursor, Zed, Continue, Cline, Goose, etc.) | **Local stdio** — see [README.md](README.md). No hosting, no OAuth, no public URL needed. Just `kalshi-mcp --env-file ~/.kalshi/.env`. | ~3 min |
| Use it from **claude.ai** (custom connector, Routines) — or any future client that needs remote OAuth MCP | **Remote with OAuth** — the rest of this guide | ~20 min |
| Run a remote instance for **yourself only** without OAuth (a private VPN like Tailscale, a personal Cloud Run with IAM, or `MCP_ALLOW_INSECURE_HTTP=1` behind your own ingress auth) | **Remote without OAuth** — covered briefly in [Operational notes](#operational-notes) | Varies |

> ⚠️ Don't deploy this server with `MCP_ALLOW_INSECURE_HTTP=1` on a
> public URL. The flag exists for local dev + properly-isolated
> private networks. An unauthenticated MCP server with trading
> credentials reachable on the public internet is a footgun the
> server explicitly refuses unless you opt in.

The walkthrough below assumes you picked the **remote with OAuth**
path. The example client is claude.ai because that's the major MCP
client today that requires OAuth-protected HTTP — the same setup
works for any future client that adopts the same transport + auth.

---

## What you'll have when you're done

```
                 OAuth 2.1 + PKCE
   claude.ai ────────────────────────► https://<your-host>.onrender.com/mcp
   (or any                                          │
    OAuth-MCP                                       │  HTTPS + RSA-PSS signed
    client)                                         ▼
                                            api.kalshi.com or
                                            demo-api.kalshi.co
```

A single Render web service running the published Docker image, with:

- **OAuth proxy** (GitHub provider) in front of the MCP endpoint —
  required for any MCP client that can't speak stdio and needs
  OAuth-protected HTTP (claude.ai is the primary one today).
- **`MCP_ALLOWED_GITHUB_LOGINS` allowlist** — only specific GitHub
  accounts can invoke tools, even after a successful OAuth.
- **Upstash Redis** (free tier) for Dynamic Client Registration
  persistence, so redeploys don't boot connected clients out.
- **Image-deploy** (Pattern A) — Render pulls a tagged image from GHCR;
  PRs against `main` cannot trigger a production deploy.

---

## Prerequisites

You need accounts for these services (all free to start):

| Service | Why | Free tier OK? |
|---|---|---|
| **[Kalshi](https://kalshi.com)** or **[Kalshi demo](https://demo.kalshi.co)** | The exchange whose API the MCP server wraps | Yes (demo) |
| **[GitHub](https://github.com)** | OAuth App + (if forking) image hosting via GHCR | Yes |
| **[Render](https://render.com)** | Container host. Any image-deploy target works (Fly.io, Cloud Run, ECS, Railway) — Render is the worked example below | Starter tier $7/mo, free tier doesn't support custom image deploy |
| **[Upstash](https://upstash.com)** *(optional but recommended)* | Persistent storage for OAuth proxy DCR state | Yes |

Tools on your machine:

- Python 3.11+ (for generating the Kalshi RSA keypair)
- `pip install cryptography` (or `uv`, or the project's venv)
- A browser

---

## Architecture — why Pattern A

The naive setup is "Render auto-deploys from `main`." For a public OSS
repo whose maintainer also runs a deployment, that's unsafe. A
malicious or careless PR could merge code that exfiltrates the Kalshi
keys baked into the deployment's environment. Even branch protection
doesn't help — anything in `main` runs in your container with your
real secrets.

**Pattern A** breaks the link:

- **CI** runs tests on every PR. CI never touches your deployment.
- **`release.yml`** builds and pushes a versioned image to GHCR. It
  runs **only on tag push** (`git tag v0.1.0 && git push origin v0.1.0`).
- **Tag protection** on `v*` means only repo admins can publish tags.
- **Render** is configured to pull the image, NOT to build from source.

Net effect: PRs flow through `main` normally; production deploys only
happen when a maintainer cuts a tag. You can review and merge with
confidence that nothing reaches prod until you explicitly ship.

To deploy a new version yourself (as the maintainer):

```bash
git tag v0.1.4
git push origin v0.1.4
```

The release workflow:

1. Builds + pushes `ghcr.io/<your-owner>/kalshi-mcp-server:v0.1.4` and
   `:latest` to GHCR (multi-arch: amd64 + arm64).
2. **POSTs to your Render deploy hook URL** (stored as the
   `RENDER_DEPLOY_HOOK_URL` GitHub Actions secret) to trigger an
   immediate redeploy. Render's image-deploy services do NOT auto-poll
   GHCR on a useful cadence — without the deploy hook, you'd have to
   click "Manual Deploy" in Render's UI after every tag push.

If `RENDER_DEPLOY_HOOK_URL` isn't set as a secret, step 2 skips cleanly
(forkers running on Fly.io / Cloud Run / etc. don't see workflow
failures). See [Deploy-hook setup](#deploy-hook-setup) below for how
to wire this up.

---

## Quick start — end-to-end checklist

These 9 steps take you from zero to a working remote MCP service —
configured to connect from claude.ai (the demonstration client) but
usable from any future MCP client that supports OAuth-protected
Streamable HTTP. Some of the earliest steps (Kalshi key, GitHub
OAuth App) can be done in parallel if you want, but the order below
is the dependency order.

### 1. Generate a Kalshi RSA keypair on your machine

The most secure way to create a Kalshi API key is to generate the
keypair locally — that way the private key never leaves your machine.
Kalshi only ever sees the public half.

```bash
mkdir -p ~/.kalshi
python -c "
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pathlib import Path
import os

home = Path.home() / '.kalshi'
home.mkdir(exist_ok=True)
priv_path = home / 'demo.pem'
pub_path = home / 'demo.pub'

if priv_path.exists():
    raise SystemExit(f'Refusing to overwrite {priv_path}')

key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
priv = key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption(),
)
pub = key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
)
priv_path.write_bytes(priv)
pub_path.write_bytes(pub)
try:
    os.chmod(priv_path, 0o600)
except Exception:
    pass

print('Private:', priv_path)
print('Public: ', pub_path)
print()
print(pub.decode().strip())
"
```

You'll have:

- `~/.kalshi/demo.pem` — **private key**, never share this
- `~/.kalshi/demo.pub` — public key, paste this into Kalshi in the next step

For prod (later), repeat with `prod.pem` / `prod.pub`. Use separate
keys per environment — Kalshi requires it, and it prevents "I thought
I was in demo" accidents.

### 2. Create the Kalshi API key

In your browser:

1. **Demo:** https://demo.kalshi.co/account/profile → API Keys → **Create New API Key**
   **Prod (only when you're sure):** https://kalshi.com/account/profile → same flow
2. Nickname: anything memorable (e.g. `kalshi-mcp-demo`)
3. **RSA public key:** paste the contents of `~/.kalshi/demo.pub` (the
   `-----BEGIN PUBLIC KEY-----` block from step 1)
4. **Read/Write** scope. The MCP server's `KALSHI_TRADING_ENABLED=0`
   default already blocks writes locally; Kalshi's scope is the outer
   layer of defense-in-depth. Read/Write at Kalshi lets you exercise
   the full prepare/confirm flow when you later flip the inner gate on.
5. Click **Create**. Save the **Key ID** Kalshi shows you (it's a UUID).

The Key ID is **not** a secret — it's an identifier paired with the
private key, like an AWS Access Key ID. Without the private key (which
Kalshi never had), no one can authenticate as you.

### 3. (Forking only) Publish the image and make the package public

If you're using the upstream image directly
(`ghcr.io/cejor6/kalshi-mcp-server:latest`), skip this step.

If you've forked the repo, you need to publish your own image:

1. Cut a tag in your fork: `git tag v0.1.0 && git push origin v0.1.0`
2. Wait ~4 min for the release workflow to build + push to GHCR
3. Go to `https://github.com/<you>/kalshi-mcp-server/pkgs/container/kalshi-mcp-server`
4. Right column → **Package settings**
5. **Danger Zone** → **Change visibility** → **Public** → confirm

The repo itself can stay private — only the *image* visibility
matters for Render to pull without credentials. See
[Image visibility](#image-visibility) for the security analysis.

### 4. Create the Render service — Phase A (get the URL)

Render's URL isn't known until the service exists, but the GitHub
OAuth App needs that URL as its callback. The chicken-and-egg is
resolved by deploying first in **insecure-HTTP mode** (no OAuth),
then adding OAuth once we know the URL.

> **Is insecure-HTTP mode safe for a few minutes?** Yes, for a demo
> deployment with `$0` balance and `KALSHI_TRADING_ENABLED=0`. The
> only thing exposed is rate-limit consumption and the existence of
> the URL. For a prod deployment with funds, use a placeholder Render
> URL pattern guess (described in [Pre-known URL trick](#pre-known-url-trick)
> below) to skip this phase.

On Render:

1. **New** → **Web Service**
2. **Source:** "Deploy an existing image from a registry"
3. **Image URL:** `ghcr.io/cejor6/kalshi-mcp-server:latest` (or your
   fork's path)
4. **Name:** something with a random hex suffix — e.g.
   `kalshi-mcp-<10-random-hex>`. The suffix makes the URL harder to
   guess (mild obscurity during Phase A, free thereafter):
   ```bash
   python -c "import secrets; print(f'kalshi-mcp-{secrets.token_hex(5)}')"
   ```
5. **Region:** anything you like. Same region as your Upstash Redis
   keeps OAuth state lookups fast.
6. **Instance Type:** Starter ($7/mo) is plenty for personal use.
   Free tier doesn't support custom Docker image deploys.
7. **Advanced** → **Health Check Path:** leave **blank**. Render
   defaults to a TCP/port-listen check. An HTTP path check would fail —
   `/mcp` returns 401 to unauthenticated requests, which Render reads
   as unhealthy and refuses to deploy.
8. **Advanced** → **Docker Command:** leave blank. The image's `CMD`
   already passes `--host 0.0.0.0` to bind on all interfaces.
9. **Environment Variables** (just these five for Phase A):

   | Key | Value |
   |---|---|
   | `KALSHI_API_KEY_ID` | The UUID from step 2 |
   | `KALSHI_PRIVATE_KEY_PEM` | Contents of `~/.kalshi/demo.pem` (multi-line; Render accepts it) |
   | `KALSHI_ENV` | `demo` |
   | `MCP_TRANSPORT` | `http` |
   | `MCP_ALLOW_INSECURE_HTTP` | `1` *(temporary — we delete this in Phase B)* |

10. **Deploy.** First boot takes ~2 min (image pull + container start).

When Render shows **Live**, note the URL (looks like
`https://kalshi-mcp-XXXX.onrender.com`). You'll need it for the OAuth
App in the next step.

Smoke test from your terminal:

```bash
curl -i https://kalshi-mcp-XXXX.onrender.com/mcp
# Expected: HTTP/1.1 200 OK or 406 Not Acceptable
#   (this is the MCP endpoint responding to a bare HTTP GET; we want
#    anything other than 502)
```

If you get 502, jump to [Troubleshooting → 502 Bad Gateway](#502-bad-gateway).

### 5. Create the GitHub OAuth App

In your browser:

1. https://github.com/settings/developers → **OAuth Apps** → **New OAuth App**
2. **Application name:** something memorable (`Kalshi MCP (demo)`)
3. **Homepage URL:** `https://kalshi-mcp-XXXX.onrender.com` (your Render URL)
4. **Authorization callback URL:**
   `https://kalshi-mcp-XXXX.onrender.com/auth/callback`
5. **Enable Device Flow:** leave unchecked
6. **Register application**
7. On the next page, save the **Client ID**
8. Click **Generate a new client secret**. **Save the Client Secret
   immediately** — GitHub shows it exactly once.

### 6. (Recommended) Create an Upstash Redis instance

This is optional but strongly recommended. Without persistent storage
for the OAuth proxy's Dynamic Client Registration entries, every Render
redeploy will force connected clients to reconnect via claude.ai
(annoying but not broken).

In your browser:

1. https://console.upstash.com → **Create Database**
2. **Type:** Redis
3. **Name:** anything (e.g. `mcp-oauth-dcr` — generic name lets you share
   across multiple MCP server deployments)
4. **Type:** Regional (free tier)
5. **Region:** same metro as your Render service (matches the Render
   region you picked in step 4)
6. **TLS:** enabled (default)
7. Create. On the database page, copy the **Endpoint** URL — it looks
   like `rediss://default:<password>@<host>.upstash.io:6379`.

The `rediss://` prefix (double-s) is TLS Redis. Required — the OAuth
proxy will reject `redis://` (cleartext) URLs.

### 7. Add OAuth env vars to Render, redeploy — Phase B

Generate a stable JWT signing key (run this on your machine):

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Save the output — you'll only paste it once. Without a stable key, the
server generates a fresh one on every restart and previously-issued
tokens become invalid.

Back in Render → service → **Environment** tab → add these:

| Key | Value |
|---|---|
| `GITHUB_CLIENT_ID` | from step 5 |
| `GITHUB_CLIENT_SECRET` | from step 5 |
| `MCP_BASE_URL` | `https://kalshi-mcp-XXXX.onrender.com` (no trailing slash) |
| `MCP_ALLOWED_GITHUB_LOGINS` | your GitHub username (comma-separated for multiple users) |
| `MCP_JWT_SIGNING_KEY` | output of `secrets.token_urlsafe(64)` above |
| `MCP_REDIS_URL` | the `rediss://...` URL from step 6 *(skip if you skipped step 6)* |

**Delete** the `MCP_ALLOW_INSECURE_HTTP` variable — it's no longer
needed and the server's fail-closed check will refuse to start with it
alongside OAuth on.

**Save** with the **"Save, rebuild, and deploy"** option (the dropdown
next to "Save only"). Render rebuilds + redeploys. Watch the logs for:

```
INFO:kalshi_mcp_server:Starting kalshi-mcp-server X.Y.Z (env=demo, trading_enabled=False)
INFO:kalshi_mcp_server:OAuth: GitHub proxy enabled — DCR client storage: redis (persistent)
INFO:kalshi_mcp_server:OAuth: tool calls restricted to GitHub logins in MCP_ALLOWED_GITHUB_LOGINS
INFO:kalshi_mcp_server:Transport: http
INFO:kalshi_mcp_server:HTTP bind: 0.0.0.0:10000
INFO:     Uvicorn running on http://0.0.0.0:10000
```

That's the success signature. Verify from your terminal:

```bash
curl -i https://kalshi-mcp-XXXX.onrender.com/mcp
# Expected: HTTP/1.1 401 Unauthorized
#   (401 means the server is up AND requiring auth — exactly right)

curl -s https://kalshi-mcp-XXXX.onrender.com/.well-known/oauth-authorization-server | python -m json.tool
# Expected: OAuth metadata JSON with issuer, endpoints, DCR support
```

### 8. Connect from a remote MCP client

Walkthrough below uses **claude.ai** as the concrete example. Any
MCP client supporting OAuth-protected Streamable HTTP follows the
same pattern — point it at `<MCP_BASE_URL>/mcp`, leave Client ID +
Secret blank (Dynamic Client Registration handles them), complete
the GitHub OAuth flow.

**claude.ai:**

1. https://claude.ai → **Settings** → **Connectors**
2. **+ Add custom connector**
3. **Name:** anything (e.g. `Kalshi MCP`)
4. **Remote MCP server URL:** `https://kalshi-mcp-XXXX.onrender.com/mcp`
5. Leave **OAuth Client ID** and **OAuth Client Secret** **blank** —
   the server advertises Dynamic Client Registration, so the client
   self-registers.
6. **Add**

The client opens a tab to GitHub: *"Authorize Kalshi MCP (demo)"*
(your OAuth App name). Click **Authorize** as the GitHub user in your
allowlist.

GitHub redirects to `https://kalshi-mcp-XXXX.onrender.com/auth/callback`,
the proxy completes the token exchange, hands a JWT back to the
client. The connector page shows **Connected** with the full tool
list (26 tools).

### 9. Verify with a few tool calls

Open a new chat with the connector enabled and try:

1. **No-API smoke test:**
   > Use the kalshi_get_environment tool

   Returns server config (env=demo, trading_enabled=false, safety
   limits). Proves the connector + tool invocation path.

2. **First signed Kalshi call:**
   > What's the current Kalshi exchange status?

   Claude calls `kalshi_get_exchange_status` → signed REST call to
   Kalshi → returns `{exchange_active: true, ...}`.

3. **Through the user-restriction middleware:**
   > What's my Kalshi balance?

   Returns `$0.00` for a fresh demo account. Proves the allowlist
   middleware admitted the call.

4. **Verify the write gate (should refuse):**
   > Buy 1 contract of <any ticker> at 50 cents on YES

   Claude attempts `kalshi_prepare_order`. Should return
   `Trading is disabled. The server is in read-only mode.` This is
   expected — `KALSHI_TRADING_ENABLED=0` is the safe default. The
   error confirms server-side safety controls are active.

If all four pass, you're done. The server is ready for use.

---

## Per-resource details

### Render

**Why image-deploy specifically.** Render also supports "deploy from
git repo" which builds the Dockerfile on every push to a watched branch.
We deliberately avoid that — see [Architecture](#architecture--why-pattern-a)
above.

**Redeploys: deploy hook, not auto-polling.** Despite what the older
docs in this repo used to claim, Render does NOT auto-poll image
registries on a cadence that's useful for CI/CD — in practice
redeploys must be triggered explicitly. We use Render's per-service
**deploy hook** (a unique POST URL) that our `release.yml` calls
after the image push. See [Deploy-hook setup](#deploy-hook-setup) for
the one-time configuration. Without the hook, you can still redeploy
manually via Render → service → "Manual Deploy" → "Clear build cache
& deploy".

**Pinning a specific version.** The image URL in Render service
settings defaults to `:latest`. To freeze a version, set it to
`:v0.1.5` (or whatever) — Render then ignores the deploy hook for new
`:latest` pushes and only redeploys when you bump the pin manually.
Useful for staging environments or production rollback windows.

**Health check.** Leave the path blank. Render falls back to a TCP
port-listen check, which succeeds the moment the container binds to
`PORT`. An HTTP path check against `/mcp` would always return 401
(unauthenticated) which Render reads as unhealthy.

**Plan.** Starter ($7/mo) is plenty. The server is mostly idle and
spikes briefly during MCP requests. If you need 24/7 uptime for
Routines, Starter is the floor (the free tier sleeps on idle, which
breaks scheduled agents).

**Region.** Same as your Upstash Redis if you have one. Latency on
every authenticated request matters less than you'd think (the JWT
verifies locally) but DCR reads on connect/reconnect benefit from
proximity.

### GitHub OAuth App

**The Client ID is not a secret.** It identifies your App, but GitHub
enforces that the `redirect_uri` parameter on every OAuth flow matches
the App's registered callback URL exactly. An attacker who got your
Client ID couldn't redirect users to a malicious URL — GitHub would
refuse. The Client *Secret* is the actual secret; treat it like a password.

**Callback URL changes.** If you change the Render service's URL
(unlikely unless you delete + recreate), update the Authorization
callback URL on GitHub immediately. Mismatches break the OAuth flow
with no graceful degradation.

**Rotation.** If the Client Secret leaks, generate a new one on the
OAuth App page and update `GITHUB_CLIENT_SECRET` in Render. The old
secret stays valid until you delete it on GitHub — you can have two
active secrets briefly during rotation.

### Upstash Redis

**What gets stored.** Only OAuth proxy state — DCR client registrations
(claude.ai's self-registered client metadata), authorization codes,
and JWT JTI mappings. **No Kalshi keys, no user PII, nothing
account-identifying** beyond GitHub logins. Stored with a short TTL.

**Why `rediss://` (TLS).** OAuth state in transit between Render and
Upstash should be encrypted. Upstash issues `rediss://` (TLS) URLs by
default; if your dashboard shows a `redis://` URL, click "Show TLS" or
copy the TLS endpoint specifically.

**Collection scoping.** Multiple MCP servers can share one Upstash
instance. Each server scopes its writes under a distinct collection
prefix (this server uses `kalshi-oauth-proxy`). No key collisions
between e.g. a Kalshi server and an Alpaca server on the same Upstash.

**Free tier limits.** Upstash free tier (256MB, 500 commands/sec) is
more than enough for the OAuth proxy workload — DCR entries are tiny
and writes happen only on connect/reconnect.

### claude.ai connector

**"Always allow" vs per-call confirmation.** Render shows tool
permissions defaulting to "Always allow" for the whole server. For
read-only tools this is fine. For the write tools
(`kalshi_prepare_order`, `kalshi_confirm_order`, `kalshi_cancel_order`,
`kalshi_decrease_order`), consider switching to per-call confirmation
once you enable trading server-side.

**Disconnecting.** claude.ai's connector page has a Disconnect button.
Disconnecting from claude.ai doesn't revoke the OAuth grant on GitHub
— do that separately at https://github.com/settings/applications if
you want to fully revoke.

**Multiple Claude users.** Add their GitHub logins to
`MCP_ALLOWED_GITHUB_LOGINS` (comma-separated). Each connects via their
own claude.ai → GitHub OAuth flow.

---

## Local stdio use (not Render)

You don't need Render at all if you only want to use the server
locally with an MCP stdio client (Claude Desktop, Claude Code,
Cursor, Zed, Continue, Cline, Goose, or any other). Local stdio
doesn't need OAuth, doesn't need a public URL, doesn't need Upstash.
See [README.md](README.md) for the local config — short version:

```json
{
  "mcpServers": {
    "kalshi": {
      "command": "kalshi-mcp",
      "args": ["--env-file", "/home/you/.kalshi/.env"]
    }
  }
}
```

You can also run *both* local stdio and a remote Render deployment.
They don't interfere — different processes, different transports.
Same Kalshi key works for both if you want.

---

## Operational notes

### Deploy-hook setup

One-time setup to make `git tag v*` auto-redeploy to Render. Skip if
you're deploying somewhere other than Render — the workflow is
already designed to handle a missing hook gracefully.

1. **In Render** → service settings → scroll to **Deploy Hook** →
   click **Generate Hook URL**. Copy the URL. It looks like
   `https://api.render.com/deploy/srv-xxxxxxxxxxxx?key=...`. The key
   in the URL is the secret part — anyone with the URL can trigger a
   redeploy of *that specific service*.

2. **In GitHub** → your repo → Settings → Secrets and variables →
   Actions → **New repository secret**:
   - Name: `RENDER_DEPLOY_HOOK_URL`
   - Value: paste the URL from step 1

3. Done. The next time you push a `v*` tag, `release.yml` will:
   - Build + push the image to GHCR
   - POST to the deploy hook URL → Render redeploys within seconds

**Security notes**:
- Repository secrets on GitHub are write-only. Once saved, the value
  is never retrievable through the UI (even by repo admins) — only
  workflow runs you trigger can use it.
- The URL is passed to the workflow only as an env var, never
  interpolated into a shell command line. GitHub Actions also
  auto-masks any string matching a known secret value with `***` in
  workflow logs.
- The `release.yml` workflow triggers only on `push: tags: ['v*']`,
  which requires repo write access. Fork PRs cannot trigger it and
  therefore cannot access this secret.
- If the URL leaks anyway: regenerate it in Render's UI (the old one
  immediately stops working). Update the GitHub secret with the new
  value.

**Skipping**: if `RENDER_DEPLOY_HOOK_URL` isn't set, the deploy step
just prints a message and exits 0. Useful for forkers running on
Fly.io / Cloud Run / a different Render account / etc. — your release
pipeline doesn't break just because you don't have this configured.

### Remote hosting without OAuth (advanced)

OAuth is the canonical path for remote MCP because it's what claude.ai
and any other modern OAuth-MCP client expects. But there are legitimate
"remote without OAuth" setups for personal use:

- **Private network only** — host the server on Tailscale, WireGuard,
  ZeroTier, or a Cloudflare Tunnel with Access. The MCP endpoint has
  no public URL; only your devices on the VPN can reach it. Run with
  `MCP_TRANSPORT=http` and `MCP_ALLOW_INSECURE_HTTP=1`. Safe because
  network-layer auth handles access control.
- **Reverse proxy with separate auth** — Caddy with `basic_auth`,
  nginx with mTLS, a custom auth proxy. The MCP server itself runs
  without auth (`MCP_ALLOW_INSECURE_HTTP=1`) on a localhost port; the
  proxy handles the public auth layer. Useful if you have an existing
  SSO setup you'd rather reuse.
- **Cloud-IAM-protected service** — Cloud Run with IAM, ECS behind an
  ALB with Cognito, etc. Same idea: cloud provider handles auth; the
  MCP server is unauthenticated behind it.

> ⚠️ **Don't combine `MCP_ALLOW_INSECURE_HTTP=1` with a directly
> public URL.** The fail-closed startup check exists exactly to
> prevent this. The flag is there for the configurations above where
> something else handles access — never as a substitute.

For a personal, claude.ai-only use case, none of this is worth the
effort. Stick with the documented OAuth + Render path above.

### Pre-known URL trick

If you want to skip Phase A (insecure-HTTP) and never expose an
unauthenticated server even briefly, you can pre-pick the Render
service name and use that as `MCP_BASE_URL` from the start:

1. Pick a unique name (e.g. `kalshi-mcp-myname-prod`)
2. Create the GitHub OAuth App with `https://kalshi-mcp-myname-prod.onrender.com`
   as Homepage + callback URL
3. Set up Upstash, get the URL
4. Create the Render service with the same name AND all 9 OAuth env
   vars already set (no `MCP_ALLOW_INSECURE_HTTP`)
5. Deploy — boots straight into OAuth-on mode

The downside is that if Render rejects your chosen name (collision
with another service), you have to update the OAuth App after. For
personal use the Phase A workaround is easier.

### Rotating Kalshi keys

1. Generate a new keypair (step 1 of the quick start)
2. Register the new public key on Kalshi (step 2)
3. Update `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PEM` in Render
4. Save + redeploy
5. After confirming the new key works, delete the old API key on Kalshi

### Rotating the OAuth Client Secret

1. On the GitHub OAuth App page, **Generate a new client secret**
2. Update `GITHUB_CLIENT_SECRET` in Render
3. Save + redeploy
4. Old client secret remains valid until you click **Delete** on it in
   GitHub — gives you a zero-downtime rotation window

### Rotating the JWT signing key

Less important than the others — only affects sessions in flight at
the time of rotation. Generate a new key, update `MCP_JWT_SIGNING_KEY`
in Render, save + redeploy. Connected claude.ai clients will see a
single "reconnect" prompt then be back.

### Tracking what version is deployed

```bash
curl -s https://kalshi-mcp-XXXX.onrender.com/.well-known/oauth-authorization-server
# Doesn't include version directly — check the Render service's logs
# for the "Starting kalshi-mcp-server X.Y.Z" line at boot
```

Or in Render's UI: service → Events tab shows the image digest /
deployed version on each deploy.

### Image visibility

For Render to pull without credentials, the GHCR **package** must be
public. The **repo** can stay private — those are independent settings.

What's in the public image:

- Source code (Python files, Dockerfile, all dependencies)
- The exact tag's commit metadata

What's NOT in the public image:

- Any `.env` file (gitignored, not in the image)
- Any PEM file (you generate locally, never committed)
- Any of the env vars you set in Render (those exist only on the host
  at runtime)

So making the package public exposes nothing beyond what an
open-source release would expose. Same trust level.

### What never appears in logs

The server is deliberately careful with sensitive material:

- **API key IDs** — logged with last-4-truncation
- **Signatures** — never logged
- **Private key material** — never logged
- **OAuth tokens / secrets** — never logged
- **Request bodies on writes** — DEBUG level only (don't run prod at DEBUG)

If you find anything that does leak into logs, that's a bug — see
[SECURITY.md](SECURITY.md).

---

## Troubleshooting

### 502 Bad Gateway

The container is up but Render's gateway can't reach it. Almost always
a host-binding issue.

**Check the Render logs for:**

```
INFO     Starting MCP server 'kalshi-mcp-server' with transport 'http' on
         http://127.0.0.1:10000/mcp
==> No open ports detected on 0.0.0.0, continuing to scan...
```

That's the signature. The fix is to ensure the image you're running is
**v0.1.1 or newer** (the Dockerfile passes `--host 0.0.0.0` via CMD).
If you've pinned a specific older version in the Render image URL,
bump it. If you're on `:latest`, force a redeploy via Manual Deploy →
Clear build cache & deploy.

### `ModuleNotFoundError: No module named 'redis'`

Image was built without the `[oauth]` extras. The fix is to use a
**v0.1.2 or newer** image. If you've forked and built your own, your
Dockerfile needs `RUN uv pip install --no-cache ".[oauth]"` (not just
`.`).

### Stuck "Connecting" / OAuth fails in claude.ai

99% of the time this is a `redirect_uri` mismatch. Check:

- The GitHub OAuth App's **Authorization callback URL** is exactly
  `<MCP_BASE_URL>/auth/callback` — including the `/auth/callback` suffix
- `MCP_BASE_URL` in Render env matches your actual Render URL with no
  trailing slash and the same protocol (https://)

The mismatch is silent — GitHub just refuses to redirect and claude.ai
gets a generic error. Fix the URLs to match and click "Connect" again.

### Tools return "Unauthorized" after OAuth completes

`MCP_ALLOWED_GITHUB_LOGINS` doesn't include your GitHub login. The
comparison is case-insensitive but exact-match — `cejor` won't match if
you registered as `cejor6`. Check the spelling, save, redeploy.

### "Trading is disabled" on every order attempt

Expected — by design. `KALSHI_TRADING_ENABLED=0` is the safe default.

> ⚠️ **Before flipping `KALSHI_TRADING_ENABLED=1`,** read
> [DISCLAIMER.md](DISCLAIMER.md). You are about to let an LLM place
> orders on your behalf. Even in demo, this is the moment where AI
> mistakes start having effects. Make sure your `MCP_MAX_ORDER_SIZE_USD`,
> `MCP_DAILY_LIMIT_USD`, and `MCP_CASH_RESERVE_USD` are tuned to a
> blast radius you can absorb, and that you've test-run the
> prepare/confirm flow at small sizes in demo first.

Set it to `1` in Render env vars and save + redeploy. The server will
log:

```
WARNING  PROD MODE — orders will hit real markets. Trading enabled: true.
```

(If you're in demo, "real markets" is misleading; ignore the warning
text. The flag controls server behavior, not which markets it hits —
that's `KALSHI_ENV`.)

### Demo orderbook prices come back as null

Not a bug — Kalshi's demo environment doesn't replicate live order
flow from prod. You get real market structure (tickers, expirations,
events) but no order book or trade history. For realistic prices you
need prod (`KALSHI_ENV=prod` + `KALSHI_ALLOW_PROD=1`).

### Render didn't pick up the new image

Render image-deploy services don't auto-poll GHCR on a useful cadence
— redeploys are triggered explicitly. Two paths:

- **Set up the deploy hook** (recommended) — see
  [Deploy-hook setup](#deploy-hook-setup). After that, every tag push
  triggers a redeploy automatically.
- **Manual redeploy** — Render service page → **Manual Deploy** (top
  right) → **Clear build cache & deploy**. Forces a fresh pull
  immediately.

If you have the hook set up and a tag-push redeploy still didn't
happen, check the release-workflow logs for the "Trigger Render
deploy" step. The hook URL might be stale (regenerate in Render
settings) or the action might have hit a transient error.

### Claude.ai disconnected after a Render redeploy

`MCP_REDIS_URL` isn't set, so DCR registrations were in-memory. They
were wiped when the container restarted. Either:

- Set up Upstash and add `MCP_REDIS_URL` (step 6 of the quick start), or
- Accept the reconnect-after-redeploy cost — click "Reconnect" in
  claude.ai's connector page once per deploy

### Render service is "Live" but `/mcp` returns 502 still

Sometimes Render's status lags reality. Wait 30s, retry. If still 502
after 2 minutes, check the Render logs — most likely the container is
in a CrashLoopBackoff state. Look for a Python traceback at the top of
the most recent log block.

### "Refusing to start with KALSHI_ENV=prod unless KALSHI_ALLOW_PROD=1"

You set `KALSHI_ENV=prod` but didn't also set `KALSHI_ALLOW_PROD=1`.
This is intentional — `prod` requires an explicit acknowledgement
beyond just typing the env var. Set both and redeploy.

### "HTTP transport requires OAuth configuration"

You set `MCP_TRANSPORT=http` without configuring OAuth (or without
setting `MCP_ALLOW_INSECURE_HTTP=1`). For Phase A of the quick start,
add `MCP_ALLOW_INSECURE_HTTP=1`. For production, finish setting all
OAuth env vars (`GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`,
`MCP_BASE_URL`, `MCP_ALLOWED_GITHUB_LOGINS`).

---

## Getting help

- **Server bugs / questions about this guide:** open an issue at
  https://github.com/cejor6/kalshi-mcp-server/issues
- **Security issues:** see [SECURITY.md](SECURITY.md) — do not file
  publicly
- **Kalshi API questions:** Kalshi's Discord (#dev channel) or
  https://docs.kalshi.com
- **FastMCP / OAuth proxy internals:** https://github.com/jlowin/fastmcp
- **MCP protocol:** https://modelcontextprotocol.io
