# Contributing to muselab

Thank you for considering a contribution. muselab is intentionally small
(no npm, no build step) to keep the codebase fully readable.

## Quick start (dev loop)

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
uv sync                                   # install Python deps
cp .env.example .env                      # then fill MUSELAB_TOKEN + MUSELAB_ROOT
uv run uvicorn backend.main:app --reload  # dev server on :8765
uv run pytest tests/                      # all tests should pass
```

The frontend is plain HTML + Alpine.js v3 (vendored). No build step —
edit `frontend/*.html|js|css`, hard-refresh the browser.

## What is welcome

- **Bug fixes** with a regression test
- **New providers** that have an Anthropic-compatible Messages endpoint
  (a single `CATALOG` entry in `backend/endpoints.py`, see [docs/add-provider.md](docs/add-provider.md))
- **MCP / skill presets** under `mcp.json.example` / `skills/`
- **UI translations** (see the `zh` / `en` key tables in `frontend/i18n/index.js`)
- **Documentation improvements** — clearer install instructions, personalization guidance, or FAQ entries
- **Visual / UX polish** — please open an issue first describing the problem

## What will likely be declined

- Adding a build step (webpack / vite / etc.) — this would eliminate the "clone and run" property
- Generic AI chat UI features that fall outside muselab's "personal archive
  assistant" scope (e.g. plugin marketplace, document RAG over crawled content)
- Provider integrations that require an OpenAI-only protocol (muselab routes
  through the Claude Agent SDK, which expects Anthropic-compatible endpoints)
- Anything that requires real personal data to test (all tests must work with
  a throwaway archive directory)

## Pull request checklist

- [ ] `uv run pytest tests/` passes
- [ ] `uv run ruff check backend/ tests/` passes (CI blocks merge on lint failures)
- [ ] `bash scripts/lint.sh` passes (encoding / class-collision + personal-data leak scan)
- [ ] Backend changes: added or updated a test in `tests/`
- [ ] Frontend visual changes: described in the PR with a before/after note
      (visual regression tests are not yet in place)
- [ ] `mcp.json.example` / skills / install scripts: if adding a runtime
      dependency (npx, uvx), install scripts must detect it and emit a warning
- [ ] No secrets in code, commits, or test fixtures
- [ ] No additions to `sessions/` or `.env` (those are gitignored and must
      remain so)

## Code style

- Python: PEP 8, no formatter enforced. Prefer clarity over cleverness.
- Type hints on public functions; not required everywhere.
- JavaScript: no transpiler — write the dialect that Alpine v3 and modern
  browsers understand. Match the semicolon style of neighbouring code.
- CSS: per-component sections with a comment header (see `frontend/styles.css`).
  Use CSS variables (`--c-*`, `--sp-*`) for theming; do not hardcode colors.

## Keeping personal data out (leak scan)

`scripts/lint.sh` runs a personal-data leak scan over tracked files
(constitution P6 — no real personal data in shipped artifacts):

- **Generic PII** (phone / national-ID patterns) — always on, runs in CI.
- **Maintainer identity** — your OS login and home-dir name are derived at
  runtime (nothing private is stored in the script), so a stray `/home/<you>/`
  path or username is caught automatically.
- **Optional private blacklist** — for names a regex can't know (real name,
  employer, target companies), create `scripts/.leak-blacklist` (gitignored,
  one literal per line) or point `MUSELAB_LEAK_BLACKLIST` at a file outside the
  repo. The scan hard-fails if that blacklist ever becomes git-tracked.

## Filing an issue

- **Bug**: include browser and OS, what you did, what happened, and what
  you expected. For server-side issues, attach sanitized logs from
  `journalctl --user -u muselab -n 50` (Linux) or `tail $LOG_DIR/stderr.log`.
- **Feature request**: describe the user problem first, then the proposed
  solution. If the feature requires a new SDK capability, note which one.
- **Provider not working**: use the `Test` button in Settings → Provider
  Keys first; then paste a sanitized excerpt of the vendor response.

## Security

For potential security issues (authentication bypass, RCE, path traversal),
please follow [SECURITY.md](SECURITY.md) — do not open a public issue.
