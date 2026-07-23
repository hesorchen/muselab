# muselab Constitution

> [简体中文](constitution_zh.md)
>
> The single source of truth for **how muselab is built and may change**.
> Specs and AI-generated code derive from this document; when code and this
> document disagree, this document wins and the code is the bug.
>
> Scope: this governs *engineering invariants*, not feature wishlists. Feature
> intent lives in per-change specs; product roadmap and known issues live on
> [GitHub Issues](https://github.com/hesorchen/muselab/issues).

- **Version:** 2.0.0
- **Ratified:** 2026-05-31
- **Last amended:** 2026-07-23
- **Derived from:** `docs/architecture.md`,
  `CONTRIBUTING.md`, `SECURITY.md`, `pyproject.toml`, and the backend/frontend
  source as of 2026-07-23.

Normative keywords **MUST / MUST NOT / SHOULD / MAY** follow RFC 2119.

---

## 1. Core Principles

### P1 — Readable over clever, small over featureful
muselab is intentionally small so the whole codebase stays human-readable.
Contributions MUST prefer clarity over cleverness. Any change that materially
increases conceptual surface area MUST justify itself against this principle.

### P2 — Clone-and-run: no build step, ever
The frontend MUST remain runnable by editing a file and refreshing the browser.
- No bundler / transpiler / npm install step MAY be introduced (webpack, vite,
  esbuild, tsc, etc.).
- Third-party browser libs MUST be vendored under `frontend/vendor/` with their
  license recorded in `THIRD_PARTY_LICENSES.md`.
- Frontend code MUST be written in the dialect Alpine.js v3 + modern browsers
  understand directly — no compile step is allowed to bridge the gap.

### P3 — SDK over raw API
muselab drives Claude through the **Claude Agent SDK** (the same engine as
Claude Code), never the raw Messages API directly. This is what makes MCP,
Skills, Subagents, plan mode, and `CLAUDE.md` auto-load behave uniformly across
every provider. New capabilities MUST be expressed through SDK-native mechanisms
rather than bypassing the SDK.

### P4 — Workspaces belong to the user; the repo never touches them
The install root and workspace roots remain separate (see §2). Code MUST NOT
write its own state into a user workspace except under the default workspace's
reserved `<MUSELAB_ROOT>/.muselab/` path and each workspace's recoverable
trash. Installation, upgrade, and migration MUST NOT depend on user files.

### P5 — Whole-file as the unit of input
The assistant reaches user files on demand via Read / Grep / Edit. muselab MUST
NOT pre-embed, pre-index, or RAG-chunk user workspaces. Context comes from the
SDK-discovered `CLAUDE.md` hierarchy for the active cwd plus on-demand reads.

### P6 — Personal data is radioactive in shipped artifacts
This is an open-source repo. No real personal data MAY appear in code, docs,
commits, test fixtures, examples, or README copy. Tests MUST run against a
throwaway archive directory.

---

## 2. Architecture Invariants

These are non-negotiable structural facts. A change that violates one is an
architecture change and MUST amend this constitution (see §8) before landing.

### A1 — Install root and workspace roots stay separate
| Root | What | Backups |
|---|---|---|
| **repo** (`muselab/`) | code + per-install state (`.env`, `sessions/`) | with the install |
| **default workspace** (`MUSELAB_ROOT`) | user files + `.muselab/` application state | independently, without touching the install |
| **other registered workspaces** | additional user files and workspace trash | separately |

`backend/settings.py` owns `ROOT` (the default workspace), while
`backend/workspaces.py` manages other registered roots. Each session stores its
cwd and the SDK discovers `CLAUDE.md` from the corresponding hierarchy.

### A2 — Layered backend, one router per concern
The backend is FastAPI, mounted in `backend/main.py`. Each domain is one module
exposing an `APIRouter`. New surface area MUST follow this one-router-per-concern
shape rather than growing a god-module:

| Module | Owns |
|---|---|
| `main.py` | app factory, uvicorn entry, route mounting, static frontend, log token-redaction, asset-version stamping |
| `auth.py` | `X-Auth-Token` guard (header or `?token=`) |
| `chat.py` | `/api/chat/*` — SDK client pool + SSE turn loop |
| `endpoints.py` | provider `CATALOG` + per-request env wiring |
| `files.py` | `/api/files/*` — safe-resolve read/write/grep + dustbin |
| `workspaces.py` | workspace registration, selection, and directory boundaries |
| `sessions.py` | session index, cwd, sidecars, queue |
| `terminal.py` / `terminal_worker.py` | `/api/terminals/*`, profiles, WebSocket tickets, and PTY workers |
| `scheduler.py` / `api_scheduler.py` | asyncio cron loop + its API |
| `push.py` / `api_push.py` | Web Push / VAPID + its API |
| `activity.py` / `activity_api.py` | activity-center state + its API |
| `transcript_index.py` | transcript index |
| `api_settings.py` | `/api/settings` — hot-rewrite `.env` + `os.environ` |
| `prompts.py` | short starter messages for built-in workflows |
| `ask_user_question.py` | in-process `muselab` MCP server |
| `permission_request.py` | tool-permission round-trip |
| `settings.py` | `ROOT` / `PORT` / `HOST`, `atomic_write_text`, `env_int` |

### A3 — Per-session env override with config isolation
Third-party providers are wired by setting, per request,
`ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY` + an **isolated** `CLAUDE_CONFIG_DIR`.
The isolated config dir is mandatory: it blocks the CLI from silently falling
back to Pro OAuth and billing third-party traffic to the user's Anthropic
account. Any new provider path MUST preserve all three.

### A4 — Client pool keyed by `(session_id, model, effort)`
`chat.py` pools `ClaudeSDKClient` instances on exactly this key. The default LRU
cap is 3 and may be adjusted through supported runtime configuration. Each
assistant message stores its own `model` so badges stay accurate after reload.
Changing the pool key is an architecture change (it interacts with MCP
spawning — see A6).

### A5 — The UI protects model continuity by default
The first real turn establishes the session model. For a non-empty session, the
frontend MUST fork when switching to an incompatible model, avoiding
cross-vendor thinking-block signatures that can produce unrecoverable `400`
errors. The management API MAY explicitly update a session model, but callers
own transcript-compatibility risk. Sessions created before a provider existed
self-heal to a configured model on first send.

### A6 — MCP: attribute-driven, gated, default-zero
- The shipped default configures **zero** user MCP servers; connectors are
  opt-in. Only the in-process `muselab` server (for `ask_user_question`) is
  always present.
- Every server (preset or user-added) is stored in `mcp.json` by **attributes**
  (`transport`, `disabled`, pinned `version`), never a hard-coded catalog.
- Versions MUST be pinned. Shipped config MUST NOT use `npx -y latest` / unpinned
  `uvx`.
- The backend **readiness gate** (`_await_mcp_ready` in `chat.py`) MUST hold
  turn 1 open until every enabled external MCP server reaches a terminal state,
  to prevent mid-turn toolset changes from wedging the session. The gate MUST be
  skipped when no external MCP is enabled (`_has_enabled_external_mcp`).
- MCP is for **capabilities the built-in tools lack** (external auth'd systems).
  Anything `Read`/`Edit`/`Grep`/`Glob`/`Bash`/`WebFetch` already covers MUST NOT
  become an MCP server.

### A7 — MCP vs Skill boundary
If the hard part is *connecting & authenticating to an external system* → MCP.
If the hard part is *encoding how to do a task well* (connection is just an API
key) → Skill (a folder with `SKILL.md` + optional assets, progressive
disclosure). New extensions MUST be classified by this rule.

### A8 — Transcripts are owned by the CLI, not muselab
Conversation transcripts are owned by the Claude CLI. Claude sessions normally
use its default configuration root, while third-party-provider sessions may use
isolated configuration roots. muselab's `sessions/` holds only the sidecar
metadata layered on top (name, cwd, per-message model badge, cost, attachments).
muselab MUST NOT duplicate or fork ownership of the transcript itself.

### A9 — Native instruction ownership
muselab MUST NOT inject a global or per-session custom system prompt. Persistent
identity, response preferences, and personal context belong in the
SDK-discovered `CLAUDE.md` hierarchy. Reusable task workflows belong in Skills,
and tool-specific behavior belongs in tool descriptions and enforced permission
configuration. UI starter prompts MAY invoke those native capabilities without
creating another instruction layer.

---

## 3. Technology Stack & Constraints

- **Language/runtime:** Python `>=3.12`. Dependency + venv management via `uv`.
- **Web:** FastAPI on `starlette>=1.3.1`, served by `uvicorn[standard]`.
- **Agent:** `claude-agent-sdk>=0.2.120,<0.3` — the only path to the model. No direct
  `anthropic` SDK / raw HTTP to model endpoints in app code.
- **Frontend:** vanilla HTML + Alpine.js v3 + CSS. No framework, no build (P2).
- **Persistence:** flat files (JSON sidecars, JSONL transcripts owned by CLI).
  No database dependency MAY be introduced for core function.
- **Adding a dependency** MUST be justified (new capability, not convenience),
  pinned with a floor version, and — if it pulls a runtime binary (`npx`/`uvx`)
  — detected by the install scripts with a warning (CONTRIBUTING checklist).
- **Provider integrations** MUST expose an Anthropic-compatible Messages
  endpoint and land as a single `CATALOG` entry in `endpoints.py`. OpenAI-only
  protocols are out (the SDK expects Anthropic-compatible).

---

## 4. Code Conventions

### Python
- PEP 8. **No formatter is enforced** — do not introduce `black`/auto-format
  that would churn the existing terse style.
- Lint: `ruff` with `select = ["E","F","W"]`, `ignore = ["E501"]`. New code MUST
  pass `ruff check backend/ tests/`. Opinionated families (B, I, N, UP) MUST NOT
  be enabled — they'd churn working code for marginal gain.
- Type hints on **public** functions; not required everywhere.
- Writes to disk MUST go through `atomic_write_text` (or an equally atomic path)
  — never a bare truncating write to a user-visible file.

### JavaScript / CSS
- No transpiler. Write the dialect Alpine v3 + modern browsers run directly.
  Match the semicolon style of neighbouring code.
- CSS: per-component sections with a comment header. Theme via CSS variables
  (`--c-*`, `--sp-*`); colors MUST NOT be hardcoded.
- Editing `app.js` / `styles.css` / `index.html` / `i18n/index.js` /
  `data/constants.js` participates in asset-version stamping — keep them in the
  candidate list when adding split-out modules so clients refetch.

### Internationalization
User-facing UI strings MUST exist in both `en` and `zh` tables in
`frontend/i18n/index.js`. Docs that ship a translation use the `_zh.md` sibling
convention.

---

## 5. Security Requirements

(Authoritative detail in `SECURITY.md`; these are the constitution-level
invariants that code reviews and specs MUST enforce.)

- **Auth on every request.** Normal APIs use `X-Auth-Token`. Browser real-time
  connections MUST first obtain a short-lived, single-use ticket through an
  authenticated request; compatibility query tokens are reserved for resource
  requests that cannot send headers. New endpoints MUST NOT bypass the
  corresponding authentication dependency.
- **Files API path traversal is closed.** Every browser file path MUST resolve
  through the safe-resolve logic in `files.py` and stay within the selected
  registered workspace. Write/upload/rename/copy MUST refuse the
  `.muselab-dustbin/` path (`_guard_not_trash`); deletes are soft (move to
  dustbin), with restore/purge as separate endpoints.
- **No secret leakage in logs.** The uvicorn access-log `token=` redaction
  filter (`_TokenFilter` in `main.py`) MUST stay in place; new log surfaces that
  could carry tokens/keys MUST be scrubbed the same way.
- **Local MCP consent.** Adding a stdio server MUST show the exact untruncated
  command, warn it runs with app privileges, flag dangerous patterns
  (`sudo`, `rm -rf`, `curl` to home/SSH paths), and require explicit approval.
- **Local HTTP servers** MUST bind `127.0.0.1`, validate `Origin`
  (DNS-rebinding), and require a token. Prefer remote HTTP connectors over `npx`
  commands (fewer supply-chain risks).
- **Permission boundaries MUST be described accurately.** Files API access is
  restricted to the selected registered workspace. Agent tools and real
  terminals run with service-user permissions and MUST NOT be described as the
  same path sandbox. Terminals MUST use same-origin checks, single-use tickets,
  and a safe environment allowlist.
- **No secrets** in code, commits, or test fixtures. `.env` and `sessions/`
  remain gitignored and MUST NOT be added.

---

## 6. Testing & Quality Gates

A change is not done until these pass (CONTRIBUTING checklist is the enforced
form):

- [ ] `uv run pytest tests/` green.
- [ ] `uv run ruff check backend/ tests/` clean (CI blocks merge on lint fail).
- [ ] `bash scripts/lint.sh` clean (encoding / BOM / class-collision).
- [ ] **Every backend change adds or updates a test** in `tests/`. Bug fixes
      MUST ship a regression test.
- [ ] Security-relevant changes (auth, path resolution, MCP) extend
      `test_security.py` / `test_files.py` / `test_mcp_gate.py` as applicable.
- [ ] Frontend visual changes documented with a before/after note in the PR
      (no visual-regression harness yet).
- [ ] No secrets; no additions to `.env` / `sessions/`.
- [ ] Tests MUST pass against a throwaway archive — never real personal data.

---

## 7. Scope Boundaries (Non-Goals)

muselab is a **personal-workspace assistant**, not a generic AI platform. The
following MUST be declined absent an explicit constitution amendment:

- A build step of any kind (violates P2).
- A document-RAG / crawled-content pipeline over user workspaces (violates P5).
- Generic chat-UI features outside the personal-workspace scope (plugin
  marketplace, etc.).
- OpenAI-only protocol providers (violates §3 / A3).
- Presetting heavy/developer-only or write/trade-capable MCP servers by default
  (violates A6 — e.g. GitHub MCP, DB-write, brokerage).
- Any feature that requires real personal data to test (violates P6 / §6).

---

## 8. Governance

- **This document outranks code, comments, and habit.** A PR that contradicts an
  invariant here is wrong until either the PR changes or this document is amended
  in the same PR with reviewer sign-off.
- **Specs derive from this constitution.** Each feature/refactor SHOULD carry a
  short spec (what changes, boundaries, acceptance criteria in EARS form: "When
  <trigger>, the system shall <behavior>"). Specs MUST NOT restate invariants —
  they reference them.
- **Amending the constitution:** bump the version (semver — MAJOR for removing/
  redefining an invariant, MINOR for adding one, PATCH for clarifications),
  update *Last amended*, and note the rationale. Architecture changes (§2) MUST
  amend before merge, not after.
- **Drift check:** when reverse-engineered understanding contradicts this
  document, treat the contradiction as a finding — fix the code, or amend the
  doc with rationale. Never silently let them diverge.

---

## 9. Amendment history

- **2.0.0 (2026-07-23):** Replaced the single-archive model with registered
  workspaces; added the real terminal, short-lived real-time tickets, and
  Activity modules; defined model continuity as frontend fork-by-default with
  an explicit management-API override; and fixed native instruction ownership
  across CLAUDE.md, Skills, and tool permissions.

---

*A spec tells the AI what to build next. This constitution tells it what must
stay true no matter what it builds.*
