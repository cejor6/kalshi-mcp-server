# Claude Code guidance

See **[AGENTS.md](AGENTS.md)** for the canonical agent guide. All
conventions, architecture, auth model, and safety rules live there and
apply equally to Claude Code, Cursor, Codex, and any other agent.

## Claude Code-specific notes

- Personal scratchpad lives in **`CLAUDE.local.md`** (gitignored). Copy
  `CLAUDE.local.md.example` and customize it for your own use. Don't put
  anything in this committed file that's specific to your machine,
  account, or risk tolerance.
- Project-wide Claude Code settings are in `.claude/settings.json`. Your
  personal overrides go in `.claude/settings.local.json` (also gitignored).
- Default to running tests with `uv run pytest`. The pre-commit hook
  also enforces lint via ruff — `uv run pre-commit run --all-files`
  before opening a PR.
