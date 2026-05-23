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
| HTML / PDF / image preview | ✅ first-class | ⚠️ | ⚠️ | ⚠️ | ❌ |
| **Full agent SDK on every model** | ✅ | ⚠️ Claude-mostly | ❌ chat only | ❌ RAG focus | ✅ Claude only |
| Reuse Claude Pro subscription | ✅ | ✅ | ❌ | ❌ | ✅ |
| Lines of code | ~22 k | tens of k | hundreds of k | ~150 k | closed |
| Install command count | 3 | many | docker compose | docker | brew / npm |

For **IDE breadth**, consider claudecodeui or code-server.
For a **plugin marketplace**, consider LobeChat.
For **chat over crawled documents**, consider AnythingLLM.

muselab's design is the opposite: **a minimal, fully auditable archive and AI
interface that gives every model Claude's full agent capabilities.**

## vs. other Claude harnesses

|  | muselab | Claude Code CLI | Claude Desktop | claudecodeui | claude-code-router |
|---|---|---|---|---|---|
| Uses official **Claude Agent SDK** | ✅ direct | ✅ (canonical impl) | ✅ | ❌ wraps CLI process | ❌ protocol translator |
| Web UI in browser | ✅ | ❌ TTY | ❌ desktop | ✅ | ❌ |
| Personal-archive focus | ✅ | ❌ coding | ❌ general | ❌ coding | ❌ |
| **Same agent loop on non-Claude models** | ✅ via vendor anthropic-compat | ❌ Anthropic only | ❌ Anthropic only | partial | ⚠ via translation, drops features |
| Self-host friendly | ✅ | n/a (you already have it) | ❌ closed binary | ✅ | ✅ |
| Open source | ✅ MIT | ❌ | ❌ | ✅ MIT | ✅ MIT |

"muselab is to your archive what Claude Code is to your codebase" — the
shortest accurate description.

## What muselab is **not**

The following use cases are better served by other tools:

- **Multi-tenant SaaS** — muselab is single-user by design: one token,
  one archive, no per-user isolation. If two people share an instance,
  they share everything. For team or family deployments, run one
  instance per person.
- **A coding IDE** — muselab can read and edit code within the archive,
  but it is not a code workspace. Use [claudecodeui](https://github.com/siteboon/claudecodeui),
  [code-server](https://github.com/coder/code-server), or
  [Claude Code](https://github.com/anthropics/claude-code) directly
  for software development work.
- **A RAG document chatbot** — muselab reads files on demand
  (file tree + grep + read tools) and does not pre-embed the archive
  into a vector store. For chat over a large collection of web pages or
  PDFs, [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm)
  is the appropriate tool.
- **A plugin marketplace** — muselab ships eleven curated skills
  out of the box (code review, diagrams, summaries, web search, PPTX
  generation, data analysis, translation, meeting notes, and more) and
  discovers any Claude Code plugin installed separately, but it does not
  include an in-app marketplace.
  [LobeChat](https://github.com/lobehub/lobe-chat) is the appropriate
  tool for that use case.
