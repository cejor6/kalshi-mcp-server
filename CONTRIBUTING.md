# Contributing

Thanks for your interest. This guide covers dev setup, the PR process,
and the non-obvious rules unique to this project.

## Dev setup

Requirements:
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) — `pip install uv` works too
- A Kalshi **demo** API key (https://demo.kalshi.co/account/profile)

```bash
git clone https://github.com/cejor6/kalshi-mcp-server.git
cd kalshi-mcp-server

# Install deps + tooling
uv sync --all-extras --dev

# Install pre-commit hooks (one-time)
uv run pre-commit install

# Copy env template and fill it in (use demo credentials)
cp .env.example .env
# edit .env

# Run tests
uv run pytest

# Run lint + format check
uv run ruff check .
uv run ruff format --check .
```

## Branch / PR workflow

- Branch from `main`. Branch name: `feature/<short-description>` or
  `fix/<short-description>`.
- Keep PRs small and focused. One logical change per PR.
- All CI checks must pass.
- At least one approving review is required before merge.
- Squash merge — keeps history linear and readable.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/) style is
preferred but not enforced:

```
feat(tools): add kalshi_get_orderbook
fix(auth): strip fragment from path before signing
docs: clarify demo-vs-prod key separation
test(rate_limit): cover bucket refill under load
ci: pin gitleaks-action to v2
```

## Code style

- `ruff check` + `ruff format` are both authoritative — match what the
  formatter produces.
- Public functions get short docstrings. Comments explain *why*, not
  *what* (the code already says what).
- Prefer dataclasses + type hints over dict-shaped state.

## Tests

- New tools and resources require unit tests.
- **Never hit the live Kalshi API in tests.** Mock HTTP with
  `httpx.MockTransport` or similar.
- **Never commit a real `.pem`** to fixtures. Generate keys at test
  time — `conftest.py` has an `rsa_private_key` session fixture.
- Async tests use the global `asyncio_mode = "auto"` setting.

## Security rules (read these — they're not negotiable)

1. **No secrets, anywhere.** No real API keys, PEMs, JWTs, OAuth tokens,
   webhook URLs, or account-identifying data — not in code, not in
   tests, not in commit messages, not in screenshots. The pre-commit
   `detect-secrets` hook will block most of this; the CI gitleaks job is
   the second line.

2. **If you accidentally commit a secret, do NOT force-push to "fix"
   it.** GitHub indexes everything; the secret is already public.
   Rotate the credential first, then talk to the maintainer about
   history surgery.

3. **PRs that touch auth, safety, or workflows get extra scrutiny.**
   These files have CODEOWNERS attached. Expect a careful review and
   please describe the threat model in your PR.

4. **Don't add network-fetching CI jobs that need secrets.** Anything
   running on `pull_request` from a fork has no access to secrets and
   must work without them. Anything needing secrets must run on
   `pull_request_target` (carefully) or on push to `main` only.

## How to add a new tool

See the "How to add a new tool" section of [AGENTS.md](AGENTS.md).

## What lands where

- Code: `src/kalshi_mcp_server/`
- Tests: `tests/`
- Architecture / agent guidance: `AGENTS.md`
- User-facing docs: `README.md`
- Deployment: `DEPLOY.md`
- Risk disclosure (trading + AI): `DISCLAIMER.md`
- Personal-only notes: `CLAUDE.local.md` (gitignored)

## Questions

Open a discussion or issue. For sensitive matters (auth bugs, suspected
vulnerabilities), see [SECURITY.md](SECURITY.md).

## Forking this for your own use

When you fork, swap these to your own values before cutting a release or
opening it to outside contributors:

- `LICENSE` — copyright holder name
- `pyproject.toml` — `authors`, `urls.Homepage`, `urls.Issues`, `urls.Source`
- `server.json` / `server.yaml` — `homepage`, docker image path
- `SECURITY.md` — disclosure email and advisory URL
- `CODE_OF_CONDUCT.md` — contact email
- `.github/CODEOWNERS` — replace `@cejor6` with your username
- `.github/ISSUE_TEMPLATE/config.yml` — security advisory URL
- `README.md` — clone/pull URLs in the install section
- `DEPLOY.md` — the Render image URL example

Everything else (CI workflows, source code, tests, agent docs) is
generic by design and does not need editing on fork.

**Do NOT edit out [DISCLAIMER.md](DISCLAIMER.md).** It's generic about
trading and AI risks, not about any specific maintainer, and protects
both you and your downstream users. If you have substantive additions
specific to your deployment context, append them — don't remove the
existing language.
