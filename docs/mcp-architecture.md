# MCP architecture & connector strategy

> [简体中文](mcp-architecture_zh.md)

A pre-release decision record for how muselab handles MCP (Model Context
Protocol) servers: what to preset, what to drop, and how the backend +
frontend should treat servers uniformly.

## TL;DR

- **MCP is for capabilities Claude Code does not already have** — external
  integrations (email, calendar, notes, your own API). It is **not** for
  things the built-in tools cover.
- **The out-of-box default ships zero user MCP servers** (only the in-process
  `muselab` server for `ask_user_question`). Connectors are opt-in.
- **Mirror the connector lifecycle from mature clients (e.g. Claude app):**
  install-level *connection*, optional per-session *enable*, and per-session
  *isolation*. See "Three-layer model" below.
- **Treat every server — preset or user-added — by attributes, not by a fixed
  catalog**: `transport` (`stdio` | `http`/`sse`), a muselab-local `disabled`
  flag, pinned `version`.
- **Ship a small curated connector gallery (default-off) + a registry/custom
  escape hatch**, instead of bundling-and-enabling a pile of servers.

## Three-layer model (Claude app parity)

A useful mental model, borrowed from mature MCP clients, is to separate three
concerns. muselab is single-user self-hosted, so "account-level" collapses to
"install-level", but the layering still applies:

| Layer | Meaning | muselab today |
|---|---|---|
| **1. Connection** | configure a connector once; every session can use it | `mcp.json` + inherited Claude Code configs. **In place.** |
| **2. Per-session enable** | same connector on in one chat, off in another | only a **global** `disabled` flag today; per-session override is **deferred** (see note) |
| **3. Session isolation** | tool results don't leak between conversations | each SDK client is per-session ⇒ **free.** |

> **Why per-session enable is deferred.** It earns its complexity only once a
> connector holds *standing* access worth scoping out of specific chats (a
> brokerage, your inbox). With the default shipping zero connectors, building a
> per-session toggle now is complexity ahead of a problem. It lands alongside
> the first remote connectors (Tier-1), where the mechanism is cheap anyway —
> `mcp_dict` is already built per session in `backend/chat.py`.

**Transport is orthogonal to these layers** but decides how cheaply Layer 1 can
be shared: an `http`/`sse` connector is one long-running endpoint that every
session connects to (shared, no cold start); a `stdio` server is a subprocess
the CLI spawns *per SDK client* (not shareable across sessions; has cold start).

## Background: how the failure modes surfaced

muselab does not spawn MCP servers itself — it drives Claude through the Claude
Agent SDK (`ClaudeSDKClient`), and the spawned `claude` CLI subprocess launches
any configured servers. The shipped default configures **none** (`mcp.json` is
gitignored and seeded by no installer; only `mcp.json.example` — the UI gallery
— is tracked). The three failure modes below were observed when *six* stdio
servers (`filesystem`, `fetch`, `memory`, `git`, `sequential-thinking`, `time`)
were configured and enabled at once — a heavy hand-rolled config, not the
out-of-box state. They are documented because **any** stdio-heavy setup hits
them:

1. **Duplicate processes.** SDK clients are pooled per `(session, model,
   effort)` with an LRU cap of 3 (`backend/chat.py`, `_CLIENT_POOL_CAP`).
   Each pooled client spawns its own stdio servers ⇒ **up to 3× the full server
   set, never shared** across sessions. (Layer-1 sharing is exactly what `http`
   transport buys back.)
2. **Slow cold start.** Unpinned `npx -y` / `uvx` re-resolve packages from the
   registry on each launch. With six such servers, observed startup until all
   tools were available was ~80s.
3. **Mid-turn toolset change → wedged sessions.** Servers connect lazily in the
   background, so the available tool-set changes partway through a turn. When
   extended thinking is on, this collides with the API's thinking-block
   signature validation (a thinking block in the latest assistant message must
   be returned unmodified), producing a permanent `400 ... thinking blocks ...
   cannot be modified` that no further prompt can recover. **Root-fixed** by the
   backend readiness gate (see Frontend/Backend requirements).

## Principle 1 — MCP vs built-in tools

Claude Code already ships first-class tools: `Read` (incl. images/PDF), `Edit`,
`Write`, `Grep` (ripgrep), `Glob`, `Bash`, `WebFetch`, and native extended
thinking. **Anything these cover should not be an MCP server.** Adding a second
way to do the same thing dilutes tool-selection accuracy and bloats context
(measured elsewhere: focused tool-sets materially outperform bloated ones), for
zero new capability.

Audit of the old presets against built-ins:

| Preset MCP | Built-in equivalent | Verdict |
|---|---|---|
| `filesystem` | `Read` / `Edit` / `Write` / `Grep` / `Glob` (richer) | **Drop / default-off** |
| `git` | `Bash` (`git ...`) | **Default-off** |
| `time` | injected current date in context + `Bash date` | **Drop** |
| `fetch` | `WebFetch` (HTML→markdown) | **Default-off** (minor edge for auth'd pages) |
| `sequential-thinking` | native extended thinking | **Drop** (also a wedge trigger) |
| `memory` | the file/markdown memory system | **Drop** (redundant; unused in practice) |

> Note on `memory`: the `@modelcontextprotocol/server-memory` server is a
> knowledge-graph store (`create_entities` / `relations` / `search_nodes`),
> entirely separate from the file-based memory system muselab already uses. The
> two share a name but nothing else. The MCP one is redundant.

## Principle 2 — MCP vs Skills

Both extend the agent, but differently:

| | MCP | Skill |
|---|---|---|
| Nature | external program exposing **tools** | a folder: `SKILL.md` (instructions) + optional scripts/assets |
| Adds | a new **capability** the model couldn't reach (send email, query a DB) | a new **procedure / know-how** using tools it already has (report templates, research flow) |
| Runtime | a running process / remote service, with auth (OAuth) | files, loaded on demand; scripts run via `Bash` |
| Context cost | tool schemas stay resident once connected | progressive disclosure — cheap until triggered |
| Standard | open cross-client protocol | Claude/Anthropic construct (markdown + assets) |

**Rule of thumb:** if the hard part is *connecting and authenticating to an
external system*, use MCP. If the hard part is *encoding how to do a task well*
(and the connection is just an API key), use a Skill. Example: web search ships
as a **Skill** (one API key, the value is in how results are used); Gmail ships
as an **MCP** (OAuth, sessions, cross-client reuse).

## Principle 3 — attribute-driven, not a fixed catalog

Users will keep adding servers, so behavior must be generic. Every server —
preset or user-added — is stored in `mcp.json` as one of two shapes:

```
stdio : { command, args, env,   disabled }      # type inferred "stdio"
remote: { type: "http"|"sse", url, headers, disabled }
```

- `type=http`/`sse` → connect to a URL; **naturally shared across sessions, no
  cold start**. Prefer this for anything stateful or shared. (Add path landed
  2026-05-30: the add-server form and `MCPServerSpec` accept either shape; the
  backend passes a `url`-shaped spec straight through to the SDK.)
- `type=stdio` → the CLI spawns a child process per client; fine for local,
  stateless, trusted tools.
- `disabled` → muselab-local boolean (the UI toggle). When true the server is
  omitted from the dict handed to the SDK. This is the **global** Layer-2
  control today; a per-session override is the deferred piece above.
- `version` → pin it in `args` (e.g. `mcp-server-foo==1.2.3`); never
  `npx -y latest`.

Readiness gating, health display, version/security checks then apply uniformly
to **any** server, including ones users add later.

## Decision — preset connector gallery

Ship a small, curated gallery of **external** connectors, **default-off**,
one-click + OAuth + consent. Prefer **first-party / official-registry** servers
over random community `npx` packages (trust + maintenance).

**Tier 1 — featured (broad personal appeal, mature, first-party):**

| Connector | Why |
|---|---|
| Gmail | email is a core personal-life surface |
| Google Calendar | scheduling; pairs with Gmail |
| Notion | notes / PKM; official server; overlaps the notes use-case |
| Google Drive / Docs | personal document library |

**Tier 2 — offered, default-off:** Slack (comms), Linear / Todoist (tasks).

**Tier 3 — niche but on-brand for self-hosters:** Home Assistant
(self-hosted home automation — same audience).

**Do not preset:** `filesystem` / `git` / `time` / `memory` /
`sequential-thinking` (built-in covers them); GitHub MCP (large tool surface,
developer-only — the canonical tool-overload example); database write-access /
trading servers (too risky for an out-of-box default).

**Long tail:** rather than hand-maintaining a large catalog, link the
**official MCP registry** (`registry.modelcontextprotocol.io`) for browse/add,
plus an "add custom server" form. muselab then only owns the maintenance of the
few Tier-1 connectors.

## Security requirements (spec-aligned)

From the MCP spec's "Local MCP Server Compromise" and Streamable HTTP guidance:

- **Consent before running a local server.** When a user adds a stdio server,
  show the **exact command** (untruncated), warn that it runs with the app's
  privileges, highlight dangerous patterns (`sudo`, `rm -rf`, `curl` to home/SSH
  paths), and require explicit approval.
- **Pin versions; verify integrity.** No `npx -y latest` in shipped config.
- **Least privilege.** Keep filesystem-style access scoped to the data dir.
- **Local HTTP servers** must bind `127.0.0.1`, validate the `Origin` header
  (DNS-rebinding), and require a token.
- **Prefer HTTP URL over `npx` command** in the add-server form — fewer supply-
  chain risks, shareable, no cold start.

## Backend requirement — readiness gate (the wedge root-fix)

The mid-turn wedge **cannot** be fixed by the frontend alone. Turn 1 has no SDK
client yet; `_start_turn` blocks on `get_client(...)` and the SSE stream does
not open until that returns, so there is nothing for the frontend to poll
*before* the turn that would prevent the toolset from shifting mid-turn.

The fix lives in the backend (`backend/chat.py`, landed as the C1 gate): after
`client.connect()` returns, **block until every enabled MCP server reaches a
terminal state** (`_await_mcp_ready`, polling `get_mcp_status`) before the
client is committed to the pool and the turn starts. Guarded by
`_has_enabled_external_mcp()` so the zero-connector default skips the round-trip
entirely. This mechanically removes the "toolset changes partway through the
first turn" condition that invalidates the thinking-block signature.

## Frontend requirements

These apply to every enabled server (preset or user-added):

1. **Connecting hint** — while the backend gate is holding turn 1 open, show an
   optimistic "connecting tools…" affordance (a delayed hint avoids flashing on
   warm pooled clients). This is UX only; the *gate* is the backend's job
   (above), not the frontend's.
2. **Live health UI** — replace the static server count in the MCP drawer with
   per-server state (connecting / ok / error), sourced from the live status
   endpoint, not the static config list.
3. **Wedge error CTA** — the `400 ... thinking blocks ... cannot be modified`
   error cannot be fixed by "Compact"; the CTA should be "this session can't
   continue — start fresh / rewind to before the error".
4. **Connector gallery + consent dialog** — the Tier-1 gallery with one-click
   OAuth, and the consent dialog from the security section for custom servers.
5. **Local vs remote add form** — a transport toggle (local stdio command /
   remote http+header connector). Landed 2026-05-30.

## Rollout

| Phase | Scope | Why |
|---|---|---|
| **P0 (pre-release)** ✅ landed | Default ships zero user MCP; Docker pre-bakes zero MCP servers (only the `claude` CLI); **backend readiness gate** + frontend "connecting tools…" hint; **remote (http/sse) add path** (form + `MCPServerSpec` + pass-through) | Kills the "slow start" and "wedged session" pains at the root; with nothing to spawn by default, cold start disappears. Low risk — no deep architecture change. |
| **P1 (post-release)** | Tier-1 connector gallery (Gmail / Calendar / Notion / Drive) with OAuth; add-server consent dialog; wedge-error CTA fix; **per-session enable/disable** (Layer 2); decouple MCP set from `(model, effort)` keying | The real product value (useful external connectors) + security baseline + the deferred per-session control, now that connectors with standing access exist. |
| **P2 (polish)** | Registry browse/add; attribute schema persisted; Tier-2/3 connectors; live health UI | Long-tail extensibility without muselab owning the catalog. |

## Resolved / open questions

- **Resolved:** `memory` MCP is dropped (redundant with the file-based memory
  system; no stateful HTTP daemon is needed for P0 once it's gone).
- **Open:** whether to feature Tier-1 connectors enabled-by-default after a
  successful OAuth, or keep them strictly opt-in. Current stance: opt-in.
