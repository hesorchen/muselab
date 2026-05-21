# Contributing to muselab

Thanks for considering a contribution! muselab is intentionally small
(no npm, no build step) so it stays grokkable.

## Quick start (dev loop)

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
uv sync                                   # install Python deps
cp .env.example .env                      # then fill MUSELAB_TOKEN + MUSELAB_ROOT
uv run uvicorn backend.main:app --reload  # dev server on :8765
uv run pytest tests/                      # all tests (~182 passing)
```

The frontend is plain HTML + Alpine.js v3 (vendored). No build step —
edit `frontend/*.html|js|css`, hard-refresh the browser.

## What we welcome

- **Bug fixes** with a regression test
- **New providers** that have an Anthropic-compatible Messages endpoint
  (3 lines in `backend/endpoints.py`, see [docs/add-provider.md](docs/add-provider.md))
- **MCP / skill presets** under `mcp.json.example` / `skills/`
- **Translations** of the UI (see `frontend/app.js` `STRINGS` table)
- **Doc improvements** — clearer install / personalization / FAQ
- **Visual / UX polish** — open an issue first describing what's off

## What we'd push back on

- Adding a build step (webpack / vite / etc.) — kills the "clone & run" pitch
- Generic "AI chat UI" features that don't fit muselab's "personal archive
  assistant" focus (e.g. plugin marketplace, document RAG over crawls)
- Provider integrations behind an OpenAI-only protocol (we route through
  the Claude Agent SDK, which expects Anthropic-compatible endpoints)
- Anything that requires personal user data to test (must work with a
  throwaway archive directory)

## Pull request checklist

- [ ] `uv run pytest tests/` passes
- [ ] `uv run ruff check backend/ tests/` passes (CI blocks merge on lint)
- [ ] `bash scripts/lint.sh` passes (catches encoding / BOM / class-collision bugs)
- [ ] Backend changes: added or updated a test in `tests/`
- [ ] Frontend visual changes: described in PR with a before/after note
      (we don't have visual regression tests yet)
- [ ] `mcp.json.example` / skills / install scripts: if adding a runtime
      dependency (npx, uvx), install scripts must detect it and warn
- [ ] No secrets in code / commits / test fixtures
- [ ] No additions to `sessions/` or `.env` (those are gitignored — and
      should stay that way)
- [ ] CHANGELOG.md entry under `[Unreleased]`

## Code style

- Python: PEP 8, no formatter enforced. Prefer clarity > cleverness.
- Type hints on public functions, not required everywhere.
- JS: no transpiler — write the dialect Alpine v3 + modern browsers
  understand. No semicolons-vs-not war; just match neighbouring code.
- CSS: per-component sections with comment header (see `frontend/styles.css`).
  CSS variables (`--c-*`, `--sp-*`) for theming — don't hardcode colors.

## Filing an issue

- **Bug**: include browser + OS, what you did, what happened, what you
  expected. If the issue is server-side, attach (sanitized) logs from
  `journalctl --user -u muselab -n 50` (Linux) or `tail $LOG_DIR/stderr.log`.
- **Feature request**: describe the user problem first, then the proposed
  feature. If the feature requires a new SDK capability, note which.
- **Provider not working**: use the `Test` button in Settings → Provider
  Keys first; paste the (sanitized) vendor response excerpt.

## Security

For potential security issues (auth bypass, RCE, path traversal), please
follow [SECURITY.md](SECURITY.md) — do not open a public issue.
