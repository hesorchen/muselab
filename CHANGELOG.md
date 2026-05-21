# Changelog

All notable changes to muselab. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Docker `docker run` example mounted to `/root/*` instead of `/home/muse/*`.**
  The container's `USER` is `muse` (uid 1000), so bind-mounting host paths
  to `/root` left files unreadable for the running process and OAuth was
  never picked up. Standalone `docker run` example in `docs/quickstart.md`
  + `docs/quickstart_zh.md` now mounts `/data` for the archive and
  `/home/muse/.claude` for credentials, matching `docker-compose.yml`.
- **English users saw Chinese session labels** (`[设置档案]`, `[整理档案]`)
  in their tab strip from `/sessions/profile-intake` and `/sessions/organize`.
  Both endpoints now pick zh / en label via the same `LANG / LC_ALL /
  LC_MESSAGES` probe as the installer scripts. Logic extracted to
  `settings.is_chinese_locale()` so future locale-aware code paths can
  reuse one helper.
- **Stale doc copy.** `docs/architecture{,_zh}.md` Mermaid label backend
  size `~6.8k` → `~7k` (matches current Python LOC). `docs/comparison.md`
  was already `~22 k` — no change.

### Added
- **`THIRD_PARTY_LICENSES.md`** at the repo root attributing every
  vendored frontend library (Alpine.js / marked / DOMPurify /
  highlight.js / KaTeX / CodeMirror) plus every backend dependency, each
  with license + version floor + upstream URL.
- **CHANGELOG `[0.1.0] - 2026-05-22` release entry** cut from `[Unreleased]`
  so the inaugural tag has a structured Keep-a-Changelog summary instead
  of a long squash commit body.
- **Multi-user-not-supported notice** in `README.md` / `README_zh.md`
  plus a "muselab is **not**" section in `docs/comparison{,_zh}.md` so
  prospective users don't deploy expecting per-user isolation.
- **`Self-hosted` README badge** linking to `docs/quickstart.md`.
- **Sessions picker grouping** expanded from `Today / This week / Earlier`
  to `Today / Yesterday / Last 7 days / Last 30 days / Earlier` so a few
  hundred sessions stay scannable.
- **CodeMirror Ctrl/Cmd-S → saveEdit()** via `extraKeys`, defending
  against browsers that swallow the document-level keydown handler.
- **`/api/log/client-error` per-IP rate limit** (30/minute) so a runaway
  browser error loop can't flood journald / docker logs. New
  `test_client_error_rate_limited` locks the budget.
- **`test_i18n_zh_en_key_parity`** locks the i18n key set to be identical
  across the zh and en language blocks in `frontend/i18n/index.js`. We've
  shipped raw `foo.bar` keys to users before when a zh-only addition
  landed without the en mirror.

### Security
- **SECURITY.md** updated to reflect the new client-error rate limit
  alongside upload caps + grep budget.

## [0.1.0] - 2026-05-22

First public-tagged release. Code has been used daily by the author for
a few months; this version cuts a tag so people can pin / fork / point
issues at a stable revision. Treat anything below as released-as-of
this date.

### Onboarding & first-run polish (2026-05-22)
- **Chat-driven CLAUDE.md intake.** Replaces the "edit CLAUDE.md by hand"
  surface with a curated chat: muselab opens a fresh session prompted to
  walk the user section-by-section through `CLAUDE.md`, saves each
  answer via `Edit`, and handles sensitive sections (money / health)
  gently. New endpoint `POST /api/chat/sessions/profile-intake`; top-bar
  👤 button opens it. Side-effect: seeds CLAUDE.md from the
  locale-aware template + creates the archive skeleton if missing.
- **Skills auto-discovery + visualization.** `/api/settings/skills` now
  walks all three Claude Code scopes — project (`./.claude/skills`),
  user (`~/.claude/skills`), and plugin marketplace
  (`~/.claude/plugins/marketplaces/<mp>/plugins/<plug>/skills`). Each
  entry returns its `scope` and (for plugin scope) `source`. The
  Settings UI changed from a text list to a filterable card grid; new
  🧩 toolbar drawer in the chat input lets the user fire a skill any
  time. First-load toast tells the user how many skills were discovered.
- **Resize side panes up to 2/3 viewport with snap-hide.** Left and
  right panes can now be dragged to two-thirds of the window width.
  Below the hide threshold (200px) they auto-collapse; 20px hysteresis
  keeps the drag from flickering at the boundary, and releasing the
  mouse is no longer required to recover from an over-shrink.
- **Welcome card on first chat load.** Numbered three-step orientation
  card above the state cards; dismissable, remembered in localStorage.
- **Bilingual cold-start.** Installer scripts (`install-*.sh / .ps1`,
  `intake.sh / .ps1`) detect locale via `$LANG / $LC_ALL / $LC_MESSAGES`
  on Unix and `Get-Culture` on Windows, then pick `default-CLAUDE.md` vs
  `default-CLAUDE.en.md` and `README.{md,en.md}` per subdir. Patching
  switched from `sed` (broke on `/` in Chinese labels) to whole-line
  awk equality. Installer auto-opens the browser at the end
  (`xdg-open` / `open` / `Start-Process`); skip via `MUSELAB_NO_BROWSER=1`.
  Token is **not** placed in the URL (browser history leak).
- **Six predefined archive subdirs ship with examples.** Each of
  `health / work / money / people / notes / archives` has a bilingual
  `README.md / README.en.md` plus a `_example-*.md` template (checkup
  log / project log / monthly budget) the user can copy-paste.

### Docs & open-source polish (2026-05-21)
- **README rewrite (454 → 50 lines).** Replaced the long-form pitch with
  five bullets + an install one-liner + a docs index. Removed
  "read in an afternoon" overclaim. Detail moved into
  `docs/{quickstart,providers,architecture,mobile,comparison,muses}.md`.
- **Bilingual docs.** Every `.md` doc has a `_zh.md` mirror; both carry
  a switcher link at the top.
- **`THIRD_PARTY_LICENSES.md`** lists every vendored frontend library
  (Alpine, marked, DOMPurify, highlight.js, KaTeX, CodeMirror) plus
  backend dependencies with upstream URLs.
- **Removed accidental personal-context leakage from `README_zh.md`** —
  the initial rewrite included `跳槽材料` ("job-hunting materials")
  as a concrete example of what to drop into `MUSELAB_ROOT`. Replaced
  with neutral category language. Codified a rule for future docs.

### Long-running hardening (2026-05-21)
- **systemd unit hardening.** Resource ceilings added to
  `~/.config/systemd/user/muselab.service` and the installer template:
  `MemoryHigh=2G` / `MemoryMax=4G` / `TasksMax=4096` /
  `LimitNOFILE=8192`, plus restart rate limit
  `StartLimitIntervalSec=300` + `StartLimitBurst=5` so a crash loop
  can't burn CPU and flood the journal. Existing installs need
  `systemctl --user daemon-reload && systemctl --user restart muselab`
  to pick them up.
- **macOS launchd parity.** `com.muselab.plist.tmpl` sets
  `HardResourceLimits.NumberOfFiles=8192` + `NumberOfProcesses=4096`.
- **Docker resource limits mirror systemd.** `docker-compose.yml` adds
  `mem_limit: 4g` / `mem_reservation: 1g` / `pids_limit: 4096`.
- **Cheaper Docker healthcheck.** Both `Dockerfile` `HEALTHCHECK` and
  `docker-compose.yml` healthcheck now hit `/api/health` instead of
  `/` — saves ~2880 full HTML renders per container per day.
- **In-flight turn persistence.** Each turn writes
  `sessions/active_turns/<sid>.json` at start and deletes it on clean
  termination. Anything left over after restart is an interrupted turn,
  surfaced on boot with a toast + jump-to-session action. Does not
  auto-resume — user decides whether the prompt is worth rephrasing.
- **`list_sessions()` TTL cache (2s)** with invalidation on muselab
  writes. Profile on a 270-session archive dropped from 150-480 ms to
  ~0 ms on cached calls.

### Security (2026-05-21)
- **Constant-time token comparison.** `backend/auth.py` switched from
  `==` to `hmac.compare_digest` to close a timing side-channel.
- **Default security headers** via middleware: `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options:
  SAMEORIGIN`. The `Referrer-Policy` matters because auth tokens ride
  in query strings for SSE / file download endpoints; `same-origin`
  strips `Referer` on cross-origin clicks.
- **`noindex` defense-in-depth.** `<meta name="robots"
  content="noindex,nofollow,noarchive">` in `index.html` + a
  `/robots.txt` route serving `Disallow: /`.

### Code health (2026-05-21)
- **Ruff is now a pinned dev dep** (`pyproject.toml` `[dependency-groups].dev`
  + `[tool.ruff]` config). CI lint step is **blocking** instead of
  `|| true`. Cleared 23 pre-existing ruff errors.

### Docs — project positioning rewrite + tone tightening (2026-05-21)
- **Removed accidental personal-context leakage from `README_zh.md`**: the initial rewrite included `跳槽材料` ("job-hunting materials") as a concrete example of what users might put in `MUSELAB_ROOT`, which inadvertently surfaced the project author's current life situation. Replaced with neutral category language ("notes / records / documents") that doesn't presume what the user is going through.
- **Tone tightened to project-author voice**: the first hero rewrite over-corrected toward colloquial / informal phrasing ("by the time you say hi", "like a colleague would", "随手翻阅", "切 vendor 不影响") which read as a chat transcript more than a project README. Replaced with declarative, scannable prose; kept the htmx / 11ty / Levels lineage references (those situate the project in an existing aesthetic, which is the point).
- **README hero + Chinese mirror**: replaced the previous "Three things" framing with four pillars structured around what muselab actually offers users vs what it borrows from the ecosystem.
  - **Lead changed from "Claude Agent SDK harness" to "Your personal context, first-class"** — the SDK is Anthropic's, the personal-archive data model + 6 predefined sub-dirs (`health / work / money / people / notes / archives`) + auto-loaded CLAUDE.md *is* muselab's contribution and should lead.
  - **New pillar**: "Multi-device synced via your own server, no SaaS middleman" — surfaces an architectural property that was implicit (single backend → all browsers see same sessions) but never explicitly called out. Differentiates against Claude.ai / ChatGPT where every uploaded file + message is held on vendor cloud.
  - **Privacy framing corrected**: previous prose said "data stays put"; updated to be precise — archive / sessions / config / credentials stay on the machine, but the messages you send to a model vendor DO leave (that's the whole point of using a hosted model). Reads as more honest, less overclaim.
  - **Pro/Max OAuth removed from headline pillars** — it's a real cost benefit but the OAuth path itself is Anthropic's, not muselab's tech. Kept in `TODO.md` "extras" + still in the bullet list as a feature.
- **`TODO.md` project-positioning section**: aligned to the same four pillars + an explicit "what muselab borrowed vs built" disambiguation, so future README edits stay anchored.
- **Fixed minor inconsistency**: TODO.md previously said the preset sub-dirs included `career`, but the installer / doctor / intake all create `work`. Settled on `work` (matching code).

### Added — long-running hardening + open-source polish (2026-05-21)
- **systemd unit hardening.** Resource ceilings added to `~/.config/systemd/user/muselab.service` and the installer template (`scripts/templates/muselab.service.tmpl`): `MemoryHigh=2G` / `MemoryMax=4G` / `TasksMax=4096` / `LimitNOFILE=8192`, plus restart rate limit `StartLimitIntervalSec=300` + `StartLimitBurst=5` so a crash loop can't burn CPU and flood the journal. New installs get these by default; existing installs need `systemctl --user daemon-reload && systemctl --user restart muselab` to pick them up.
- **macOS launchd parity.** `scripts/templates/com.muselab.plist.tmpl` now sets `HardResourceLimits.NumberOfFiles=8192` + `NumberOfProcesses=4096` so macOS installs get the same process / fd caps as Linux. (Memory ceilings are advisory on macOS — kernel jetsam still kicks in on OOM, but launchd has no cgroup-style enforcement.)
- **Docker resource limits mirror systemd.** `docker-compose.yml` adds `mem_limit: 4g` / `mem_reservation: 1g` / `pids_limit: 4096` so the container path gets the same envelope as a native install.
- **Cheaper Docker healthcheck.** Both `Dockerfile` `HEALTHCHECK` and `docker-compose.yml` healthcheck now hit `/api/health` (returns `{"status":"ok"}`) instead of `/` — saves ~2880 full HTML renders per container per day.
- **In-flight turn persistence.** `_active_turns` (the per-session SSE broadcast registry) is in-memory only; if muselab is restarted / OOM-killed / SIGKILL'd mid-stream, the user's prompt was previously lost silently. New: each turn writes `sessions/active_turns/<sid>.json` via `atomic_write_text` at start and deletes it on clean termination (success / error / 30 min timeout). Anything left over after restart is an interrupted turn. New endpoints `GET /api/chat/interrupted-turns` + `POST /api/chat/interrupted-turns/{sid}/dismiss`; the frontend toasts each interrupted turn on boot with an `[打开]` action that jumps to the session. Does NOT auto-resume — user decides whether the prompt is worth rephrasing.
- **`list_sessions()` TTL cache.** Profile on a 270-session archive showed 150-480 ms per call dominated by `sdk_list_sessions()`. Cache for 2 s with invalidation on muselab writes (`_save_index` calls `invalidate_sessions_cache`). Cached calls now ~0 ms; rapid refresh storms (heartbeat reconnect + scheduler unread + UI refresh) dedupe automatically.

### Security (2026-05-21)
- **Constant-time token comparison.** `backend/auth.py` switched from `==` to `hmac.compare_digest` to close a timing side-channel. A trivial change (no behavioral diff for legitimate clients) that nonetheless removes "guess token char by char via response timing" from the threat surface.
- **Default security headers** added via FastAPI middleware: `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: SAMEORIGIN`. The Referrer-Policy is particularly relevant — auth token rides in query strings for SSE / file download endpoints, and `same-origin` strips `Referer` on cross-origin navigation so the token doesn't leak when clicking a link out to GitHub.
- **`noindex` defense-in-depth.** `<meta name="robots" content="noindex,nofollow,noarchive">` in `index.html` + a `/robots.txt` route serving `Disallow: /`. If a user accidentally exposes their instance (port-forward / Cloudflare tunnel / misconfigured reverse proxy), at least search engines won't slurp it up.

### Changed — code health (2026-05-21)
- **Ruff is now a pinned dev dep** (`pyproject.toml` `[dependency-groups].dev` + `[tool.ruff]` config), CI lint step is **blocking** instead of `|| true`. Cleared 23 pre-existing ruff errors (unused imports, multi-statement lines, imports not at top of file, unused locals).


- **Sidebar resize no longer "jumps" mid-drag.** Root cause: `mousemove` was lost the moment the cursor passed over the HTML preview's sandboxed iframe (iframe ate the event). Fix: overlay a transparent fullscreen `<div>` during drag — events still bubble to `document`, but iframe / video / other embeds can't intercept.
- **Preview auto-refresh after file edits.** When the assistant runs `Edit` / `Write` / `MultiEdit` / `NotebookEdit` on the currently-previewed file (basename match), the preview pane re-fetches text/markdown and bumps a `previewVersion` cache-buster on iframe / image / PDF URLs. No more stale page after the AI updates an HTML you're looking at.
- **Click a chat tab → file tree reveals + scrolls to the open file.** Adds `revealInTree(path)` that calls `expandPath` for every ancestor directory then `scrollIntoView({block:"nearest"})` on the row. `<li>` now carries `:data-path` for unambiguous selection.
- **Tool / thinking blocks align with assistant bubble** (40 px left margin = avatar width + gap). Eliminates the misalignment where tool bubbles ran flush-left while text bubbles were offset.
- **Thinking + tool_result default-collapsed.** Each block shows a one-line summary (first 80 chars / line count); click to expand. The currently-streaming last-rendered block stays expanded automatically. User's explicit toggle persists for that message.
- **Multi-tab strip respects viewport boundary.** Added `min-width: 0; max-width: 100%` to `.chat-tabs` so the flex-shrink:0 tab row overflows internally (horizontal scroll) instead of pushing past the pane edge. Switching tabs now `scrollIntoView` on the new active tab — no more invisible active tab on programmatic activation.
- **Compact UX overhauled.** While `/compact` is running: ctx-meter switches to a shimmering progress bar + pulsing label (replaces the old 120-s toast). Messages typed during compact are queued (`_compactQueue`) and dispatched in order once compact finishes — input never locks. Queue depth shown live in the meter label.
- **"Versions & upgrade" per-row upgrade buttons.** Each row (SDK / system CLI) gets its own inline "升级 / Upgrade" button when an update is available; the bottom "Upgrade all" remains for batch action. Fixes a latent bug where the "all" button's disabled condition referenced a non-existent `cli_upgrade_available` key (should be `system_cli_upgrade_available`).
- **Removed "空会话清理 / Clean up empty sessions" feature** — endpoint, helper, i18n, and settings row all gone.

### Fixed — SDK best-practices audit (2026-05-18)
- **Context meter false 796.6% reading** (`backend/chat.py`). Root cause: the per-session usage snapshot was being filled from `ResultMessage.usage`, which SDK docs (see `ContextUsageResponse.apiUsage` in `types.py:821`) explicitly mark as "Cumulative API usage for the session". `_session_usage` then summed `input + cache_read + cache_creation` as if those were per-turn — after a few cache-hit turns the meter showed e.g. `0.0K / 200K (796.6%)`. Now per-turn numbers come from `AssistantMessage.usage` (raw Anthropic Messages API `usage` dict, recorded per API call — `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` ≤ window). `_session_usage` is overwritten (not accumulated) on each `AssistantMessage`. Two regression tests in `test_regressions.py` lock the new behavior: `test_context_used_prefers_sdk_authoritative_value` and `test_context_used_fallback_when_sdk_value_missing`.
- **Mid-text truncation diagnostic log** — when the FE bubble was observed truncating mid-word ("CSS 变量切" before a tool_use), backend `TextBlock` handler now logs `streamed=N chars, block.text=M chars, emitting tail=K chars` only when M > N, so the next reproduction confirms root cause (CLI/server folded tail tokens into TextBlock vs. SSE buffer cut vs. EventSource framing).

### Changed — SDK-native API migration (2026-05-18)
- **`stop()` uses `client.interrupt()` not `disconnect()`** — new `/api/chat/interrupt` endpoint. Previously stopping a turn destroyed the SDK client, forcing a full CLI subprocess restart (re-loading CLAUDE.md / MCP / system prompt) on the next message. Now the client stays connected and the conversation continues seamlessly.
- **Model switch uses `client.set_model()` when same provider** — `update_model` endpoint detects same provider via `endpoints.lookup().prefix` match. Same-provider switches (e.g. claude-opus → claude-sonnet) swap in-place via SDK; cross-provider (e.g. Claude → DeepSeek) still rebuilds because `env_override` / `base_url` differ.
- **Permission-mode swap uses `client.set_permission_mode()` without rebuild** — `_client_permission` shadows the mode each cached client was created with; `get_client` syncs via SDK rather than full rebuild when the requested mode differs.
- **MCP toggle propagates to live clients via SDK** — `/api/settings/mcp/{name}/toggle` now calls `client.toggle_mcp_server()` against every live client instead of only writing `mcp.json` (which previously took effect only on next client rebuild). New endpoints: `POST /api/settings/mcp/{name}/reconnect` (`client.reconnect_mcp_server()`) and `GET /api/settings/mcp/status` (`client.get_mcp_status()` aggregated per-session).
- **Session list merges SDK truth** — `sessions.list_sessions()` now calls `claude_agent_sdk.list_sessions(directory=ROOT)` for `custom_title` / `last_modified` / `created_at` / `tag`, overlays muselab `index.json` for `model` / `system_prompt` / `auto_named`. Sessions created in muselab but never streamed (no CLI JSONL yet) still appear. `get_session_meta()` symmetrically merges via `sdk_get_session_info()`.
- **`PATCH /api/chat/sessions/{sid}` accepts `tag`** — wires `claude_agent_sdk.tag_session()` for SDK-native session tagging (visible to manual `claude` CLI runs too). 409 if JSONL doesn't exist yet (CLI never invoked).
- **Empty-session cleanup cascades to SDK** — `delete_empty_sessions` now also calls `sdk_delete_session()` to drop the CLI JSONL when present, avoiding orphan transcripts if the user manually claude-CLI'd against the sid.

### Added — multi-tab chat (2026-05-17)
- **VS Code–style chat tab strip** above the message area. Each tab is an open session; switching just swaps the visible bubble stream and **does not stop background streams** (per-tab `tabState[id]` keeps each session's messages / ES / sessionUsage / streaming flag isolated). The active tab's root state aliases into the per-tab slot via `_activateTabState`, so existing Alpine bindings keep working without rewrites.
- **Tab interactions**: single-click switches, double-click renames inline (input + Enter / Esc), middle-click closes, right-click opens a context menu (rename / edit system prompt / close tab / delete session). On mobile, a `⋮` kebab button on every tab and history row replaces right-click. Streaming tabs show a pulsing accent dot.
- **History picker** (📁 button at right end of tab strip): pop-up listing every session. Click a row to promote it into a tab; row × deletes; row context menu mirrors tab menu. Position computed by `getBoundingClientRect` and rendered as `position: fixed` so it escapes the tab strip's `overflow-x: auto` clip.
- **Tab persistence**: `openTabIds`, `currentId`, file-preview `tabs` and `selected` all round-trip through `localStorage` (`muselab_prefs`). Page refresh restores tab order, active session, and the file you were previewing.
- **Chat bubble avatars** — assistant bubbles show the current mascot SVG; user bubbles show a generic glyph (we never asked for the user's name).
- **Browser tab title** mirrors the active session: `<session name> · muselab`, with a ● prefix while streaming.
- **Close-tab undo toast** — closing a chat tab surfaces a 5 s "已关闭 tab — 撤销" toast; click "撤销" to restore the tab at its original index (no-op if the close happened mid-stream).
- **Keyboard shortcuts** — `Ctrl+T` new chat, `Ctrl+W` close, `Ctrl+Tab` / `Ctrl+Shift+Tab` cycle, `Ctrl+1..9` jump to Nth tab, `Esc` closes tab context menu.
- **Drag-and-drop tab reorder** via native HTML5 drag; a 2 px accent bar marks the drop target.
- **Streaming markdown render throttle** — `mdRender` is O(content length); during fast token streams we now coalesce to ≥ 80 ms between renders and force-flush on `done` / `error` / `cancelled` / bubble close. Keeps long replies smooth without losing the final paint.
- **Frontend lint sanity test** (`tests/test_frontend_lint.py`) — fails when `app.js` contains two top-level method definitions with the same name. Guards against another `closeChatTab`-style silent shadow.
- **e2e test scaffold** (`tests/e2e/`) — pytest-playwright spec covering new/switch/close, undo toast, browser title, preview persistence. Default skipped; enable with `RUN_E2E=1`. See `tests/e2e/README.md` for setup.

### Changed — caching & layout (2026-05-17)
- **Versioned static URLs**: `index.html` is regenerated per request with a `?v=<mtime>` query stamp on every `/static/...` URL. Stamped requests get `Cache-Control: public, max-age=31536000, immutable`; unstamped requests fall back to no-cache. Solves "mobile Safari serves last week's app.js" without sacrificing cold-start speed.
- **`100vh` → `100dvh`** for `.layout` / `.pane` so the chat input doesn't get hidden behind mobile Safari's collapsing address bar.
- **`MUSELAB_ROOT` may now be `$HOME`** — the prior block was security theatre, since the agent runs with `bypassPermissions` and already has full FS write access. ROOT still rejects `/`, `/etc`, `/home` (other users), `/var`, `/usr`, `/boot`. The UI's sensitive-name blocklist (`SENSITIVE_NAMES` / `SENSITIVE_SUFFIX`) gains `.bash_history`, `.zsh_history`, `.python_history`, `.viminfo`, `.lesshst`, `.npm-debug.log` to reduce token-leak blast radius.
- **CLI session-id fallback**: when the bundled CLI rejects `--session-id X` with "already in use" (transient lock leak we don't fully understand), `get_client` automatically retries with `--resume X` so the user's message still goes through.

### Fixed — multi-tab edge cases (2026-05-17)
- **`closeTab` name collision** — the file-preview tab strip already had `closeTab(path)`. Naming the chat-tab closer the same silently shadowed it (object-literal: later wins) so every chat × tap was a no-op. Renamed to `closeChatTab(id, ev)`. Added a sanity test that scans `app.js` for duplicate method definitions to prevent recurrence.
- **`<template x-if>` blur race** — inline rename input was inside a `<template x-if>`; the blur that committed the new name unmounted the very element that fired blur, crashing Alpine with `Cannot read properties of null (reading 'node')`. Switched to `x-show` (both nodes stay mounted, only display toggles).
- **`ctxMenu` name collision** with the file-tree context menu. Renamed chat tab menu state to `tabCtxMenu`.
- **File-path links in chat**: relative-path markdown links inside assistant replies now intercept at the `chat-body` level, resolve via `_normalizeArchivePath` (strips ROOT prefix, decodes URL-encoded Unicode filenames), and open in the preview pane via `openByPathToasted` (toasts "not found" instead of silently failing).
- **Tool-call file paths** (Read / Edit / Write / NotebookEdit / MultiEdit) render as clickable `.file-link` chips in the tool bubble.

### Added — Docker / CI
- **GitHub Container Registry images** — every push to `main` and every `v*.*.*`
  tag publishes a multi-arch image (linux/amd64 + linux/arm64) to
  `ghcr.io/hesorchen/muselab`. Tags: `latest` (main HEAD), `1.2.3` (semver),
  `1.2`, `1`, `sha-abc1234` (short SHA per commit).
- **PR docker smoke** — pull-requests still build (single-arch) without
  pushing, to verify the Dockerfile keeps building.

### Added — 2026-05-17 sprint
- **Permission-request UI bridge** (`backend/permission_request.py`) — when `permission_mode` is not `bypassPermissions`, the SDK's `can_use_tool` callback pushes a side-channel event; the frontend renders an Allow / Deny / Always-allow bubble. Per-session always-allow cache avoids re-prompting for the same `(tool, key)` pair. 10-min timeout = deny.
- **Default `CLAUDE.md` starter template** (`scripts/templates/default-CLAUDE.md`) — four personas (health / asset / career / research) with explicit evidence-tier rules and guardrails. Install scripts (linux/macos/windows) interactively offer to drop it into `ARCHIVE_DIR`. Docs at `docs/personalize-claude-md.md`.
- **MCP server management UI** (Settings → MCP) — list / add / remove / enable-disable MCP servers from the browser. Backend: `GET/PUT/PATCH/DELETE /api/settings/mcp/...`. Storage in `mcp.json` with new `disabled` flag honored by the chat loop.
- **MCP preset upgrade**: `mcp.json.example` adds `sequential-thinking` + `time` (with timezone) and a `description` field on every entry. Install scripts now check for `npx`/`uvx` and warn which presets won't run.
- **Skills re-enabled and discovery wired in**: removed `"Skill"` from the disallowed-tools blocklist; `ClaudeAgentOptions.setting_sources=["user","project","local"]` + `skills="all"` so the SDK loads skills from `~/.claude/skills` and `muselab/skills`.
- **7 preset skills** under `skills/`: `web-search`, `markdown-formatter`, `mermaid-helper`, `code-reviewer`, `citation-formatter`, `task-decomposer`, `summary-distiller`. Each follows the SKILL.md frontmatter format with `USE WHEN ...` descriptions.
- **Skill discovery API** — `GET /api/settings/skills` lists discoverable skills (project + user scope). Settings modal shows them read-only with scope tag and description.
- **Dedicated UIs for common tool calls**:
  - **TodoWrite** — checkbox task list with status badges (pending / in-progress / completed) and a "doing" tag
  - **Task (subagent)** — purple-bordered card with subagent_type chip + description + collapsible prompt
  - **ExitPlanMode** — markdown-rendered plan card
- **MCP / Skill tool-call render polish** — `mcp__server__tool` displays as `server · tool` with a 🔌 prefix and cyan border; Skill calls use 🧩 with amber border.
- **Image input (ImageBlock)** — paste / drag / picker → thumbnail preview → POST `/api/chat/upload-image` (10 MB, 10-min TTL) → attached as base64 content to the SDK's user message. New `&image_ids=...` query param on the stream endpoint.
- **Competitive analysis** at `docs/competitive-analysis.md` — 8 reference projects, viral-lift patterns, muselab gaps, README hero A/B, 3-week pre-launch plan.

### Added — earlier
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
  - Configure API keys for DeepSeek / 智谱 GLM / MiniMax without editing `.env`
  - Default model / permission mode / show-thinking
  - Model params: thinking budget, max tool turns
  - Logout button moved here from file pane
- **Per-session custom system prompt** (🧠 in session bar) — prepended to muselab's default
- **Third-party Anthropic-compatible providers via direct endpoints** (no router needed):
  - DeepSeek (`api.deepseek.com/anthropic`)
  - 智谱 GLM (`open.bigmodel.cn/api/anthropic`)
  - MiniMax (`api.minimax.io/anthropic`)
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
