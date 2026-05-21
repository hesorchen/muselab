# How muselab compares

> [简体中文](comparison_zh.md)

These tables exist so you can decide quickly whether muselab fits your
use case — or whether one of the alternatives is a better match. The
author's own positioning notes (more candid, undated) live in
[competitive-analysis.md](competitive-analysis.md).

## vs. general chat UIs

|  | muselab | claudecodeui | LobeChat | AnythingLLM | Claude Code CLI |
|---|---|---|---|---|---|
| Primary purpose | Archive + AI chat | IDE for multi-CLI agents | Multi-model chat + plugin store | RAG over your docs | Terminal coding agent |
| Self-hosted | ✅ | ✅ | ✅ | ✅ | ❌ |
| Browser access | ✅ | ✅ | ✅ | ✅ | ❌ |
| HTML / PDF / image preview | ✅ first-class | ⚠️ | ⚠️ | ⚠️ | ❌ |
| **Full agent SDK on every model** | ✅ | ⚠️ Claude-mostly | ❌ chat only | ❌ RAG focus | ✅ Claude only |
| Reuse Claude Pro subscription | ✅ | ✅ | ❌ | ❌ | ✅ |
| Lines of code | ~22 k | tens of k | hundreds of k | ~150 k | closed |
| Install command count | 3 | many | docker compose | docker | brew / npm |

If you want **IDE breadth**, pick claudecodeui or code-server.
If you want a **plugin marketplace**, LobeChat.
If you want **chat over crawled docs**, AnythingLLM.

muselab's pitch is opposite: **the smallest readable archive + AI surface
that gives every model Claude's full agent power.**

## vs. other Claude harnesses

|  | muselab | Claude Code CLI | Claude Desktop | claudecodeui | claude-code-router |
|---|---|---|---|---|---|
| Uses official **Claude Agent SDK** | ✅ direct | ✅ (canonical impl) | ✅ | ❌ wraps CLI process | ❌ protocol translator |
| Web UI in browser | ✅ | ❌ TTY | ❌ desktop | ✅ | ❌ |
| Personal-archive focus | ✅ | ❌ coding | ❌ general | ❌ coding | ❌ |
| **Same agent loop on non-Claude models** | ✅ via vendor anthropic-compat | ❌ Anthropic only | ❌ Anthropic only | partial | ⚠ via translation, drops features |
| Self-host friendly | ✅ | n/a (you already have it) | ❌ closed binary | ✅ | ✅ |
| Open source | ✅ MIT | ❌ | ❌ | ✅ MIT | ✅ MIT |

"muselab is to your archive what Claude Code is to your codebase" is the
shortest accurate one-liner.

## What muselab is **not**

A few cases where another tool is the right answer:

- **Multi-tenant SaaS** — muselab is single-user by design: one token,
  one archive, no per-user isolation. If two people share an install,
  they share everything. For team / family deployments, run one
  instance per person.
- **A coding IDE** — muselab can read and edit code in your archive,
  but it's not a code workspace. Use [claudecodeui](https://github.com/siteboon/claudecodeui),
  [code-server](https://github.com/coder/code-server), or
  [Claude Code](https://github.com/anthropics/claude-code) directly
  for serious software work.
- **A RAG document chatbot** — muselab reads your files on demand
  (file tree + grep + read tools), it does not pre-embed your archive
  into a vector store. For chat over a large crawl of web pages or
  PDFs, [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm)
  is the right tool.
- **A plugin marketplace** — muselab carries seven curated skills out
  of the box and discovers any Claude Code plugin you install
  separately, but it does not run an in-app marketplace.
  [LobeChat](https://github.com/lobehub/lobe-chat) is the right tool
  if that's what you want.
