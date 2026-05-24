# How muselab compares

> [简体中文](comparison_zh.md)

These tables are provided to help you determine quickly whether muselab fits
your use case, or whether one of the alternatives is a better match.

## vs. general chat UIs

|  | muselab | claudecodeui | LobeChat | AnythingLLM | Claude Code CLI |
|---|---|---|---|---|---|
| Primary purpose | Archive + AI chat | IDE for multi-CLI agents | Multi-model chat + plugin store | RAG over your docs | Terminal coding agent |
| Self-hosted | ✅ | ✅ | ✅ | ✅ | ❌ |
| Browser access | ✅ | ✅ | ✅ | ✅ | ❌ |
| HTML / PDF / image preview | ✅ | ⚠️ | ⚠️ | ⚠️ | ❌ |
| **Full agent SDK on every model** | ✅ | ⚠️ Claude-mostly | ❌ chat only | ❌ RAG focus | ✅ Claude only |
| Reuse Claude Pro subscription | ✅ | ✅ | ❌ | ❌ | ✅ |
| Lines of code | ~31 k | tens of k | hundreds of k | ~150 k | closed |
| Install command count | 3 | many | docker compose | docker | brew / npm |

For **IDE breadth**, consider claudecodeui or code-server.
For a **plugin marketplace**, consider LobeChat.
For **chat over crawled documents**, consider AnythingLLM.

## vs. other Claude harnesses

|  | muselab | Claude Code CLI | Claude Desktop | claudecodeui | claude-code-router |
|---|---|---|---|---|---|
| Uses official **Claude Agent SDK** | ✅ direct | ✅ (canonical impl) | ✅ | ❌ wraps CLI process | ❌ protocol translator |
| Web UI in browser | ✅ | ❌ TTY | ❌ desktop | ✅ | ❌ |
| Personal-archive focus | ✅ | ❌ coding | ❌ general | ❌ coding | ❌ |
| **Same agent loop on non-Claude models** | ✅ via vendor anthropic-compat | ❌ Anthropic only | ❌ Anthropic only | partial | ⚠ via translation, drops features |
| Self-host friendly | ✅ | n/a (you already have it) | ❌ closed binary | ✅ | ✅ |
| Open source | ✅ MIT | ❌ | ❌ | ✅ MIT | ✅ MIT |

muselab is to your archive what Claude Code is to your codebase.

## Scope boundaries

- Single-user, single-token — two people sharing one instance share
  everything; for team/family use, deploy one instance per person
- Not an IDE — code can live in the archive but development work belongs
  in [claudecodeui](https://github.com/siteboon/claudecodeui) or
  [Claude Code](https://github.com/anthropics/claude-code)
- Not a RAG service — files are read on demand via Read / Grep, never
  pre-embedded; for crawl-style document chat use
  [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm)
- No plugin marketplace — 11 curated skills ship out of the box and
  external Claude Code plugins are auto-discovered, but there's no
  in-app store; use [LobeChat](https://github.com/lobehub/lobe-chat)
  if you need one
