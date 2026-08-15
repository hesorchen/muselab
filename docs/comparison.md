# How muselab compares

> [简体中文](comparison_zh.md)

These tables are provided to help you determine quickly whether muselab fits
your use case, or whether one of the alternatives is a better match.

## vs. general chat UIs

|  | muselab | claudecodeui | LobeChat | AnythingLLM | Claude Code CLI |
|---|---|---|---|---|---|
| Primary purpose | Local workspaces + executable Agent | IDE for multi-CLI agents | Multi-model chat + plugin store | RAG over your docs | Terminal coding agent |
| Self-hosted | ✅ | ✅ | ✅ | ✅ | ❌ |
| Browser access | ✅ | ✅ | ✅ | ✅ | ❌ |
| HTML / PDF / image preview | ✅ | ⚠️ | ⚠️ | ⚠️ | ❌ |
| Real PTY terminal in browser | ✅ multi-terminal + profiles | ✅ multi-tab | ❌ | ❌ | n/a (runs in your terminal) |
| **Full agent SDK on every model** | ✅ | ⚠️ Claude-mostly | ⚠️ own agent + MCP | ❌ RAG focus | ✅ Claude only |
| Reuse Claude Pro subscription | ✅ | ✅ | ❌ | ❌ | ✅ |
| Install command count | 1 (curl \| bash) | many | docker compose | docker | brew / npm |

For **IDE breadth**, consider claudecodeui or code-server.
For a **plugin marketplace**, consider LobeChat.
For **chat over crawled documents**, consider AnythingLLM.

muselab terminals share the same working directory as files, previews, and
conversations. Multiple real PTY sessions can stay alive at once, while
profiles run a fixed command whenever a terminal is created. Switching pages
does not stop their processes.

Other names that often come up in the same search:

- [Open WebUI](https://github.com/open-webui/open-webui) — the go-to
  self-hosted chat UI for local models (Ollama) and OpenAI-compatible
  endpoints, with its own RAG and tool system. Choose it when local-model
  chat is the centerpiece; choose muselab when you want the Claude Code
  agent loop (Read / Grep / Edit / Bash, Skills, MCP) over your own files.
- [LibreChat](https://github.com/danny-avila/LibreChat) — multi-provider
  chat with multi-user auth and an agents framework. Choose it for a shared,
  team-facing chat portal; muselab is deliberately single-user
  (see [Scope boundaries](#scope-boundaries)).
- **Obsidian / Logseq AI plugins** — AI inside a note-taking app. They focus on
  a notes vault; muselab's Agent works on registered local workspaces (any file
  type) and can execute multi-step tasks with tools and terminals, not just
  write text.

## vs. other Claude harnesses

|  | muselab | Claude Code CLI | Claude Desktop | claudecodeui | claude-code-router |
|---|---|---|---|---|---|
| Uses official **Claude Agent SDK** | ✅ direct | ✅ (canonical impl) | ✅ | ❌ wraps CLI process | ❌ protocol translator |
| Web UI in browser | ✅ | ❌ TTY | ❌ desktop | ✅ | ❌ |
| Files + previews + real terminal | ✅ integrated | ⚠️ terminal-first | ⚠️ no real terminal | ✅ | ❌ |
| **Same agent loop on non-Claude models** | ✅ via vendor anthropic-compat | ❌ Anthropic only | ❌ Anthropic only | partial | ⚠ via translation, drops features |
| Self-host friendly | ✅ | n/a (you already have it) | ❌ closed binary | ✅ | ✅ |
| Open source | ✅ MIT | ❌ | ❌ | ✅ AGPL-3.0 | ✅ MIT |

muselab puts the Agent loop in a self-hosted local workspace that is accessible
from a browser.

Any authorized local directory can be a workspace. The installer collects no
personal profile and creates no predefined directory structure.

## Scope boundaries

- Single-user, single-token — two people sharing one instance share
  everything; use separate instances or a multi-user product for team sharing
- Not a full IDE — the built-in terminal is useful for commands and
  agent-assisted work in the active workspace, but muselab does not provide
  full code navigation, debugging, or an IDE extension ecosystem. Use
  claudecodeui or Claude Code for heavyweight software development
- Not a RAG service — files are read on demand via Read / Grep, never
  pre-embedded; for crawl-style document chat use
  [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm)
- No plugin marketplace — user-installed and external Claude Code plugin
  skills are auto-discovered, but muselab ships no task-specific presets and
  has no in-app store; use [LobeChat](https://github.com/lobehub/lobe-chat)
  if you need one
