# Changelog

All notable changes to muselab. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Math formula rendering** in both contexts:
  - Markdown: KaTeX vendored (~600 KB total: CSS 23 KB, JS 275 KB, 20 woff2 fonts ~300 KB), wired into `mdRender()` via auto-render. Supports `$...$`, `$$...$$`, `\(...\)`, `\[...\]`. Runs after DOMPurify; ignores `<code>` / `<pre>` blocks
  - HTML preview iframe: relaxed `sandbox="allow-scripts"` + CSP allows `https:` scripts / styles / fonts / connect, so HTML reports with embedded MathJax / KaTeX / highlight.js CDN scripts render correctly. iframe still runs in unique opaque origin — cannot read MUSELAB_TOKEN, cannot fetch /api/* (CORS blocks)
- **`MUSELAB_MAX_BUFFER_SIZE` env var** (default 32 MB, was SDK default 1 MB). Prevents "chat hangs forever" when a single tool_use JSON message (Edit / Read on a large file) blew past the SDK's 1 MB stream-json reader limit and silently killed the message reader
- **Per-OS one-shot installers with autostart on boot**:
  - `scripts/install-macos.sh` — user-level LaunchAgent (`~/Library/LaunchAgents/com.muselab.plist`), restarts on crash
  - `scripts/install-linux.sh` — user-level systemd service (`~/.config/systemd/user/muselab.service`), reminds about `loginctl enable-linger`
  - `scripts/install-windows.ps1` — Task Scheduler task triggered at user logon
  - Each prompts for archive dir, generates `.env` with random token, and verifies the service starts. Matching `uninstall-*` scripts remove the autostart entry without touching `.env`/data
  - Per-OS docs in `docs/install-{macos,linux,windows}.md` covering verify / restart / tail logs / expose to LAN / troubleshoot
- **`MUSELAB_HOST` env var** (default `127.0.0.1`) — installers bind to localhost-only by default; set to `0.0.0.0` in `.env` to expose on LAN
- **Muse — AI persona inside muselab**: dual-layer brand (muselab is the platform, Muse is the AI). UI surfaces the persona in chat header, empty states, login subtitle, and tooltips. Slogan: *"Meet Muse — an AI assistant that actually knows you."*
- **Nine-Muses mascot system**: 9 abstract geometric SVG forms mapped to the nine Greek Muses (Calliope/Clio/Erato/Euterpe/Melpomene/Polyhymnia/Terpsichore/Thalia/Urania), one per session — picked by hash of (date + hour), so it stays stable within an hour but rotates throughout the day. Click to cycle, animates on chat-pane re-open, spins during streaming.
- **Dynamic favicon** generated from the current mascot SVG as a `data:` URL; auto-updates when accent color or active muse changes — no static `.ico` file shipped.
- **Bilingual UI (中文 / English)** with in-app toggle (Settings → Language). ~90 string keys in `STRINGS` table, falls back to Chinese for any missing English entry. Auto-detects browser language on first visit; persisted in localStorage.
- **Multi-file tabs** in preview pane (VSCode-style): click files to open in tabs, click to switch, × or middle-click to close
- **Settings modal** (gear icon in chat header):
  - Configure API keys for DeepSeek / 智谱 GLM / MiniMax / Kimi without editing `.env`
  - Default model / permission mode / show-thinking
  - Model params: thinking budget, max tool turns
  - Logout button moved here from file pane
- **Per-session custom system prompt** (🧠 in session bar) — prepended to muselab's default
- **Third-party Anthropic-compatible providers via direct endpoints** (no router needed):
  - DeepSeek (`api.deepseek.com/anthropic`)
  - 智谱 GLM (`open.bigmodel.cn/api/anthropic`)
  - MiniMax (`api.minimax.io/anthropic`)
  - Kimi/Moonshot (`api.moonshot.cn/anthropic`)
- **Brand empty states** for preview and chat panes (large `muse·lab` logo + tagline + quick tips)
- **CodeMirror 5 editor** with 14-language syntax highlighting + line numbers + bracket matching + theme follow
- Editor **status bar**: Ln/Col, selection length, total lines, char count, mode, dirty indicator
- **Right-click context menu** on file tree: preview / @ mention to chat / copy path / download / rename / new file / new subdir / upload here / delete
- **File full-text search** (pure Python, cross-platform — no `grep` dependency)
- **@-mention files** in chat input with dropdown picker
- **Light / Dark theme toggle** (☀/🌙) with system preference detection + persistence
- **Toast notifications** + custom modal (replaces native `alert()` / `confirm()` / `prompt()`)
- **Collapsible + draggable sidebars** with width persistence; left/right toggle in middle pane header
- **Show hidden files** toggle (👁) — for `.git`, `.env`, etc.
- **MCP server support** via `mcp.json` (4 sample servers: filesystem, fetch, memory, git)
- **Session lifecycle**: rename, delete, system_prompt edit; sessions persist in `sessions/<id>.json`
- **Cost tracking** per message + cumulative session total
- **Tool call visualization** (Read/Edit/Bash/Grep blocks rendered with native styling)
- **Cross-platform Docker setup**: multi-stage Dockerfile, docker-compose.yml, .dockerignore; pre-installs `node`/`npx`/`uv`/`uvx`/`git` for MCP support
- **49+ pytest tests** covering auth, security boundaries, file CRUD, sessions, endpoints catalog, settings API
- `Makefile` with `make test` / `make run` shortcuts
- **README** in English + Chinese (`README_zh.md`)
- `SECURITY.md`, `LICENSE` (MIT), `.dockerignore`

### Changed
- **Env vars renamed `PORTAL_*` → `MUSELAB_*`** for consistency with project name
  (legacy `PORTAL_*` still works with a deprecation warning)
- **localStorage keys renamed `portal_*` → `muselab_*`** (auto-migrated on next page load)
- **Multi-provider dispatch**: chat backend uses Claude Agent SDK for everything;
  non-Claude models go through SDK with vendor's Anthropic-compatible endpoint via per-request env override
- **System prompt** now layered: per-session custom prompt (if set) prepended to muselab default; both prepended to auto-loaded CLAUDE.md
- **Pane layout** uses dynamic grid template (matches actual rendered children) instead of fixed 5-column layout
- **CodeMirror** integrates with theme toggle (light: `default`, dark: `material-darker`)

### Removed
- Dead code from the early LiteLLM multi-provider experiment: `backend/providers.py` (-296 lines), `backend/tools.py` (-255 lines), and the `litellm` dependency from `pyproject.toml`. Replaced by the direct Anthropic-compatible-endpoint dispatch in `backend/endpoints.py`, which is both simpler and gives non-Claude providers the full Claude SDK agent loop (Read/Edit/Bash/Glob/Grep/Task/TodoWrite/MCP/Skills/CLAUDE.md) — see [`docs/add-provider.md`](docs/add-provider.md).

### Security
- `safe_resolve()` blocks path traversal outside `MUSELAB_ROOT`
- **Sensitive file names blocked**: `.env*`, `.pem`, `id_rsa`, `id_ed25519`, `credentials*`, `*.key`, etc. — refused even with valid token
- `MUSELAB_ROOT` blocklist: refuses `/`, `/etc`, `/root`, `/home`, `/var`, `/usr`, `/boot`, `$HOME`
- `MUSELAB_TOKEN` minimum 16 chars validation at startup
- **XSS protection**: all markdown rendered through DOMPurify
- **HTML/SVG preview sandboxed**: `iframe sandbox=""` + strict `Content-Security-Policy`
- `Content-Disposition: inline` on `/read` and inline-able `/raw` to prevent unwanted downloads
- RFC 5987 filename encoding for non-ASCII paths (Chinese filenames no longer cause 500)
- Settings API masks existing keys on GET (only first/last 4 chars visible)
- Auto-clears `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` at startup so SDK uses Pro OAuth instead of console billing

### Fixed
- Hidden iframe/img elements no longer trigger phantom `/raw` requests that caused md/txt files to download
- Alpine `Cannot read properties of undefined (reading 'after')` errors from nested `<template x-if>` (refactored to `x-show` with conditional `src`)
- SVG icons invisible on dark background (added global `stroke: currentColor; fill: none`)
- HTML files force-downloaded instead of rendering (now sandboxed iframe preview)
- Highlighting only worked once per preview (changed from `highlightElement` to `highlight()` + `innerHTML`)
- Right resizer turned blue when left sidebar collapsed (fixed dynamic grid template to match child count exactly)
- Sessions: tabs sync on file delete; collapsed → ☀/🌙 stays visible

## Architecture notes

- **Backend** (~1.2 k lines Python): FastAPI + Claude Agent SDK + httpx
- **Frontend** (~3.2 k lines, no build): plain HTML + Alpine.js + marked + DOMPurify + highlight.js + CodeMirror 5 (all vendored)
- **No npm / no webpack / no bundler** — clone and run
- **Single binary install** via `uv` (cross-platform)

[Unreleased]: https://github.com/hesorchen/muselab/commits/main
