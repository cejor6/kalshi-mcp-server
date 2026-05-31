<!--
Thanks for the PR! A couple of things to keep in mind:

1. This server handles trading authentication. PRs that touch auth.py,
   safety.py, or .github/workflows/ get extra scrutiny.
2. The repo has secret-scanning hooks. If your PR fails the scan, do NOT
   force-push a "remove the secret" commit — the secret is already in git
   history and must be considered compromised.
-->

## Summary

<!-- What does this change do? Why? -->

## Type of change

- [ ] Bug fix
- [ ] New tool / resource
- [ ] Refactor / cleanup
- [ ] Documentation
- [ ] CI / build / dependencies
- [ ] Other (describe):

## Touches auth, safety, or CI?

- [ ] **No** — proceed to "Testing" below.
- [ ] **Yes** — describe the change carefully. Include a threat-model note:
      what could go wrong, and how does this PR guard against it?

## Testing

- [ ] `uv run pytest` passes locally
- [ ] `uv run ruff check .` passes
- [ ] `uv run pre-commit run --all-files` passes
- [ ] If this touches a Kalshi endpoint, I tested against the **demo**
      environment (not prod).

## Checklist

- [ ] I read [CONTRIBUTING.md](../CONTRIBUTING.md).
- [ ] No secrets, real API keys, real PEM files, or account-specific data
      are introduced by this PR (including in tests and fixtures).
- [ ] If new dependencies, I checked their license + maintenance status.
