# Security policy

This project handles authentication keys for a real trading API. The
threat model is "user runs the server on their own machine with their
own Kalshi keys"; security flaws can result in unauthorized trades or
leaked credentials.

## Reporting a vulnerability

**Please do not file public issues for vulnerabilities.**

Use one of:

1. **GitHub private advisory** (preferred): open at
   https://github.com/cejor6/kalshi-mcp-server/security/advisories/new
2. **Email**: cejor6 — subject line: `[security] kalshi-mcp-server`.

Please include:

- Affected version (commit SHA or release tag).
- Steps to reproduce.
- Potential impact (info leak, unauthorized trade, RCE, etc.).
- Whether the issue requires the attacker to have the operator's
  credentials, network access, or just public-PR access.

A response within 7 days is the target. Disclosure timing will be
coordinated with the reporter.

## In scope

- Auth / signing bugs (e.g. signature reuse, replay vulnerabilities).
- Safety-control bypasses (e.g. ways to bypass trading-disabled mode).
- Secret exposure (key leakage in logs, error messages, audit files).
- Supply-chain risks introduced by this repo (e.g. a published package
  with a backdoor).
- CI/CD vulnerabilities (e.g. a PR that can exfiltrate secrets on merge).

## Out of scope

- Vulnerabilities in the Kalshi API itself (report to Kalshi).
- Vulnerabilities in upstream dependencies (`fastmcp`, `cryptography`,
  etc.) — report to those projects. We'll happily bump the version once
  patched.
- Issues that require the attacker to have already compromised the
  operator's machine.

## Known design decisions

These are intentional and not vulnerabilities:

- The server processes requests in the operator's user context with the
  operator's Kalshi keys. There is no multi-tenant isolation.
- Stdio transport has no authentication — that's by design (the MCP
  client is the operator).
- Logs include the request method and path. Signatures and API keys are
  NEVER logged.
