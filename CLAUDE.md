# Claude Code guidance

The canonical agent guide for this project is **`AGENTS.md`** — the same
file every other agent (Cursor, Codex, Aider, etc.) reads. Importing it
here so Claude Code picks up the same conventions:

@AGENTS.md

## Claude Code-specific extras

- **Personal scratchpad: `CLAUDE.local.md`** (gitignored). Copy
  `CLAUDE.local.md.example` and customize it for your own machine,
  account, or risk tolerance. Don't put anything machine-specific in
  this committed file.
- **Project-wide Claude Code settings** are in `.claude/settings.json`.
  Personal overrides go in `.claude/settings.local.json` (also
  gitignored — never committed).
- **Run tests:** `uv run pytest`.
- **Run pre-PR checks:** `uv run pre-commit run --all-files` (catches
  lint, formatting, secret-scan issues before CI does).
