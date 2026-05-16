# muselab

### Meet **Muse** — an AI assistant that actually knows you.
*muselab is the open, self-hosted lab where Muse lives — alongside your archive.*

> ⚡ ~4.4 k lines · no npm · no bundler · runs on a 1 GB VPS
> 🚀 One `docker compose up`. Done.
> 🧠 Powered by Anthropic's official **Claude Agent SDK** — the same engine as Claude Code,
> with full MCP / Skills / Subagents / CLAUDE.md support across **all** providers.

[中文 → README_zh.md](README_zh.md) · [Changelog](CHANGELOG.md) · [Add a new provider](docs/add-provider.md) · [Security](SECURITY.md)

<!-- 📸 Demo gif coming soon — see docs/assets/README.md if you want to contribute one -->

---

## Why muselab

| | |
|---|---|
| 🏛 **Built to live** | Not a coding sidebar. muselab sits permanently next to your archive — health records, career notes, finance plans, papers — and helps you read, write, and reason over them. |
| ⚡ **Honestly tiny** | ~1.2 k lines of Python + ~3.2 k lines of vanilla HTML / JS / CSS. No bundler, no npm. You can read every line in an afternoon. |
| 🚀 **Trivial to deploy** | Docker Compose: 3 commands from `git clone` to running on your VPS. Native install (uv) is one more command. |
| 🧠 **Full agent power** | Uses Claude Agent SDK as the only chat backend — so MCP servers, Skills, Subagents, CLAUDE.md auto-load, plan mode, tool use all work out of the box. Multi-provider dispatch via vendor's Anthropic-compatible endpoints, **no protocol-translation router required**. |

## What it is

Three panes in one browser page (~100 MB resident):

- 📁 **Files** — tree of your archive root: list, multi-tab preview, drag-drop upload, full-text search, in-browser editor with syntax highlighting
- 💬 **Chat** — streaming agent with tools (Read / Edit / Bash / Glob / Grep / WebFetch / TodoWrite / Task / MCP servers), session history persisted to disk, switch model mid-conversation
- ⚙ **Settings** — configure providers, themes, accent color, **interface language (中文 / English)**, defaults — all from the UI, no editing `.env`

## Quick start

### One-shot installers — autostart at login, localhost-only by default

Pick your OS. Each script installs deps, generates `.env` (with a random token
and an archive path you choose), and registers an autostart entry so muselab
comes back after every reboot.

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
claude login   # one-time, for Anthropic models; non-Claude providers need just an API key

# macOS  — user-level LaunchAgent
bash scripts/install-macos.sh

# Linux  — user-level systemd service
bash scripts/install-linux.sh

# Windows — Task Scheduler (PowerShell)
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

Then open `http://localhost:8765` and paste the token (in `.env`).

#### After you reboot the laptop?

| OS | Reboot → log back in | Reboot → never log in (lid closed, ssh-only, ...) |
|----|---------------------|----------------------------------------------------|
| **macOS** | ✓ auto-starts | n/a (you always log in on a Mac after reboot) |
| **Linux** | ✓ auto-starts | ✗ needs one-time `sudo loginctl enable-linger $USER` to survive logout / no-login reboots |
| **Windows** | ✓ auto-starts | n/a (Task Scheduler trigger is "At Logon") |

For a personal laptop where you log in after each reboot, **all three just work** —
the installer registers the autostart entry and the OS handles it.

For a desktop / mini-PC you may not actively log into (Linux), enable linger:
```bash
sudo loginctl enable-linger $USER
```
The Linux installer reminds you of this at the end if it's not already enabled.

Detailed per-OS guide (verify / restart / tail logs / expose to LAN / uninstall):
[macOS](docs/install-macos.md) · [Linux](docs/install-linux.md) · [Windows](docs/install-windows.md).

### Advanced — Docker / manual

<details>
<summary>Docker Compose (recommended for VPS / NAS)</summary>

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
cp .env.example .env && $EDITOR .env   # set MUSELAB_TOKEN, ARCHIVE_DIR
claude login                            # host-side; container reuses OAuth
docker compose up -d
```

`docker-compose.yml` ships with `restart: unless-stopped`, so the container
survives host reboots without any extra autostart wiring.

</details>

<details>
<summary>Native (uv, no service)</summary>

For when you want to run muselab manually in a terminal — dev, debug, or
ephemeral use.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cd muselab && uv sync
cp .env.example .env && $EDITOR .env
claude login
uv run python -m backend.main          # binds MUSELAB_HOST:MUSELAB_PORT from .env
```

</details>

## Use any model

muselab uses Claude Agent SDK as the single chat backend, then per-request env override
routes non-Claude models to their vendor's Anthropic-compatible endpoint. **All providers get
the FULL agent loop** — not just chat. No proxy process, no translation losses.

| Provider | How to enable | Tool use | Notes |
|---|---|---|---|
| **Anthropic Claude** (Sonnet/Haiku/Opus) | `claude login` once | ✅ | Reuses Pro/Max OAuth — no API key, no per-token cost |
| **DeepSeek** (V4 Pro / V4 Flash / R1 / Chat) | `DEEPSEEK_API_KEY` in `.env` or Settings UI | ✅ | ~10× cheaper than Claude for chat-heavy tasks |
| **智谱 GLM** (GLM-5 / GLM-4-Plus) | `ZHIPUAI_API_KEY` | ✅ | |
| **MiniMax** (M2.7) | `MINIMAX_API_KEY` | ✅ | |
| **Kimi** (K2.6) | `MOONSHOT_API_KEY` | ✅ | |

Switching model mid-conversation is one dropdown click — history isn't lost.
Adding a new provider takes **3 lines** of `endpoints.py` config — see [docs/add-provider.md](docs/add-provider.md).

## A typical day with muselab

```
Morning  | Ask Claude to summarize what's new in archives/papers/this-week/
         | Switch to DeepSeek to translate that summary to English
         |
Noon     | Drop a PDF into investment/research/ via drag-and-drop
         | Use @investment/portfolio.md in chat — Claude reads it,
         |   suggests rebalances per your CLAUDE.md investment rules
         |
Evening  | Edit health/training-log.md inline (CodeMirror)
         | Ctrl+S to save; toast confirms
         | Ask Claude to spot trends across last 3 months' entries
```

CLAUDE.md auto-loads from `~/.claude/CLAUDE.md` (your global rules) AND
`<archive-root>/CLAUDE.md` (per-archive rules) — applied to every model uniformly.

## Architecture in 60 seconds

```
┌────────────────────────────────────────────────────────────┐
│ Browser: ~3200 lines vanilla HTML + Alpine.js + CSS        │
│   ┌──────────┬─────────────────┬───────────────────────┐   │
│   │ files    │  preview + tabs │  chat + multi-model    │   │
│   └──────────┴─────────────────┴───────────────────────┘   │
└────────────────────────────────────────────────────────────┘
                              │
                              ▼ HTTP / SSE
┌────────────────────────────────────────────────────────────┐
│ Backend: FastAPI (~1200 lines)                              │
│   ┌──────────────────────┐   ┌─────────────────────────┐   │
│   │ /api/files/*         │   │ /api/chat/*             │   │
│   │   safe path resolve  │   │   ClaudeSDKClient pool  │   │
│   │   read/write/grep    │   │   per (session, model)  │   │
│   │   sensitive blocks   │   │   model prefix dispatch │   │
│   └──────────────────────┘   └─────────────────────────┘   │
└────────────────────────────────────────────────────────────┘
                              │
        claude-*  ┌───────────┴───────────────┐  others (deepseek-/glm-/...)
                  ▼                           ▼ (env override per request)
        api.anthropic.com               api.deepseek.com/anthropic
        (Pro OAuth)                     api.minimax.io/anthropic
                                        open.bigmodel.cn/api/anthropic
                                        api.moonshot.cn/anthropic
```

**No bundler. No build step.** Edit a file, refresh, done.

## How muselab compares

|  | muselab | claudecodeui | code-server + Cline | Obsidian + AI | Claude Code CLI |
|---|---|---|---|---|---|
| Primary purpose | Archive + AI chat | IDE for multi-CLI agents | Web VS Code with AI | Local knowledge base | Terminal coding agent |
| Self-hosted | ✅ | ✅ | ✅ | ❌ local-only | ❌ |
| Browser access | ✅ | ✅ | ✅ | ❌ | ❌ |
| HTML/PDF/image preview | ✅ first-class | ⚠️ | ✅ via plugin | ⚠️ plugin | ❌ |
| **All agent features for ANY model** | ✅ | ⚠️ Claude-mostly | varies | varies | ✅ Claude only |
| Lines of code | ~4.4 k | tens of k | hundreds of k | closed | closed |
| Install command count | 3 | many | docker compose (heavier) | one-click | brew/npm |

If you want IDE breadth, pick claudecodeui or code-server. muselab's pitch is opposite:
**the smallest readable archive + AI surface that gives every model Claude's full agent power.**

## Security

⚠️ **A leaked `MUSELAB_TOKEN` ≈ shell access on `MUSELAB_ROOT`.** Chat sessions run with
`permission_mode="bypassPermissions"` by design — Claude can read/write any file under
that root without per-call confirmation.

What's baked in:

- `MUSELAB_ROOT` blocklist: refuses `/`, `/etc`, `/root`, `/home`, `/var`, `/usr`, `/boot`, `$HOME`
- `MUSELAB_TOKEN` minimum 16 chars
- Path-traversal protection (`safe_resolve`)
- Sensitive-file blocking: `.env*`, `id_rsa`, `*.pem`, `credentials*` etc. — even with valid token
- XSS protection: all markdown rendering goes through DOMPurify
- HTML/SVG preview in `iframe sandbox=""` + strict CSP

What you must do at the ops layer:

- Run as an unprivileged user (not `root`, not your login user)
- Point `MUSELAB_ROOT` at a dedicated directory
- Long random token, rotate on suspicion of leak
- HTTPS + nginx basic auth if reachable beyond your LAN

See [SECURITY.md](SECURITY.md) for the threat model + how to report a vulnerability.

## A note on the name

muselab takes its name from the **nine Muses** of Greek mythology — the goddesses
who inspire art and learning. **Muse** is the AI inside; **muselab** is the open
workshop she lives in.

Each session boots a different muse (hashed from date + hour, so she changes
hourly but stays stable within the hour). Over time you'll meet all nine:

| Muse | Domain | Geometric form |
|---|---|---|
| Calliope | Epic poetry | Hex |
| Clio | History | Stacked bars |
| Erato | Love poetry | Vesica piscis (lens) |
| Euterpe | Music | Sine wave |
| Melpomene | Tragedy | Crescent |
| Polyhymnia | Sacred hymns | Halo |
| Terpsichore | Dance | Trio of dots |
| Thalia | Comedy | Spark |
| Urania | Astronomy | Orbit |

Click the mascot in the chat header to cycle through the rest. The favicon
follows along — your browser tab quietly carries today's muse.

## Status

Pre-1.0, personal project. I use it daily. PRs welcome; the maintainer reserves the
right to reject features that bloat the codebase beyond "readable in an afternoon".

Roadmap and known issues live in [TODO.md](TODO.md).

## License

[MIT](LICENSE)
