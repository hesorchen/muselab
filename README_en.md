<h1 align="center">muselab</h1>

<p align="center">
  <a href="https://github.com/hesorchen/muselab/actions/workflows/ci.yml"><img src="https://github.com/hesorchen/muselab/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="docs/quickstart.md"><img src="https://img.shields.io/badge/deploy-self--hosted-orange.svg" alt="Self-hosted"></a>
  <a href="https://github.com/hesorchen/muselab/pkgs/container/muselab"><img src="https://img.shields.io/badge/ghcr.io-muselab-blue?logo=docker" alt="Container"></a>
  <a href="https://deepwiki.com/hesorchen/muselab"><img src="https://deepwiki.com/badge.svg" alt="Ask DeepWiki"></a>
  <a href="README.md"><img src="https://img.shields.io/badge/lang-中文-red" alt="中文"></a>
</p>

<p align="center"><strong>muselab is a self-hosted agent workspace built on the Claude Agent SDK.</strong></p>

<p align="center"><em>Muse comes from the Muses of Greek mythology, goddesses of inspiration, art, and knowledge.</em></p>

<table align="center">
<tr>
<td align="center"><a href="promo/media/screenshot-mobile-preview.png"><img src="promo/media/screenshot-mobile-preview.png" width="210" alt="Mobile light-theme preview"></a></td>
<td align="center"><a href="promo/media/screenshot-mobile.png"><img src="promo/media/screenshot-mobile.png" width="210" alt="Mobile dark-theme preview"></a></td>
</tr>
<tr>
<td align="center">Mobile · light theme</td>
<td align="center">Mobile · dark theme</td>
</tr>
</table>

<p align="center"><sub>Public demo data; click either image to view the original.</sub></p>

## Core features

| | |
|---|---|
| **Reuse existing subscriptions** | Claude supports OAuth; Codex can connect through Gateway, allowing you to reuse existing Claude and ChatGPT subscription services |
| **Integrated Agent workspace** | Browse files, preview reports, interact with an Agent, and run or inspect results in a real terminal—all on the same page |
| **Multiple working directories** | Create and switch among multiple working directories with one click; each directory is a complete Agent context and runtime environment |
| **Real Unix PTY terminals** | Create, rename, and switch among multiple real terminals; each terminal stays bound to the directory where it was created and supports Terminal Profiles |
| **Claude Agent SDK** | Native file operations, tool use, Skills, and MCP support let the Agent execute tasks directly and produce persistent artifacts |
| **Native file preview** | Preview Markdown, text, HTML, images, PDF, XLSX, CSV, TSV, and more |
| **Multiple models and Providers** | Built-in Claude and Codex Gateway Providers, plus support for Anthropic-compatible model services such as Kimi, GLM, and DeepSeek |
| **Self-hosted, multi-device access** | Working directories, session state, and generated files stay local, while desktop browsers and the mobile PWA keep content in sync across devices |

## Quick start

**One-line install** (Linux + macOS + WSL2):

```bash
curl -fsSL https://raw.githubusercontent.com/hesorchen/muselab/main/scripts/quick-install.sh | bash
```

**Manual install**:

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
bash scripts/install-linux.sh    # or install-macos.sh
```

**Verify after installation**:

1. Open `http://localhost:8765` in your browser
2. Paste `MUSELAB_TOKEN` to log in
3. Configure at least one model
4. Send `hello` and confirm Muse responds

Something wrong? Run `bash scripts/doctor.sh` for layered diagnostics and concrete repair suggestions.

> **Windows users:** install through WSL2 (see [Quick start](docs/quickstart.md#windows-via-wsl2)).
>
> **Unattended mode** (CI / Docker / demo recording): `MUSELAB_NONINTERACTIVE=1 bash ...`

## Session practice

> "Inspect the latest changes in this project, find why the tests became slower, fix the cause, run verification, and write the result to `docs/performance-note.md`."

Muse reads the code and Git diff in the active working directory, runs targeted tests in a real terminal, edits the relevant files, and verifies the result. The generated note opens directly in the preview pane. When you switch to another working directory, the file tree, terminals, new sessions, and context switch with it.

Code, research collections, and knowledge bases can all be ordinary workspaces; no fixed directory structure is required.

🌐 More scene demos on the [muselab promo page](https://hesorchen.github.io/muselab/promo/).

## Why not existing solutions?

| Solution | Limitation | How muselab works |
|---|---|---|
| ChatGPT / Claude.ai | Files are usually uploaded; workspace and execution context are not continuous | Local workspaces, generated files, and inspectable context remain available |
| Claude Code | Terminal-first, ideal for direct local CLI work | The same Agent Harness with browser files, previews, real terminals, multiple workspaces, and mobile access |
| RAG document chat | Chunking + retrieval fits large document sets but does not provide a complete execution environment | The Agent reads full files on demand and uses tools and terminals for multi-step work |

Full comparison (Open WebUI / LobeChat / AnythingLLM / claudecodeui, etc.): [How it compares](docs/comparison.md).

## Practical details

- **Modern file tree** — Modern file operations: drag-and-drop upload, fuzzy search, rename, and trash
- **Files and terminals in one preview surface** — Markdown, text, spreadsheets, and HTML restore their reading position; real PTY terminals support multiple instances, profiles, and mobile controls
- **Workspace isolation** — Register multiple local directories and switch files, previews, session tabs, and new-session cwd as one surface
- **Session workspace** — Multi-session tabs, pinning and search, background streaming, persistent queues, context usage, and turn timing
- **Global search** — Search file names, file contents, sessions, message history, and common actions from one place
- **Editing and previewing** — Markdown editing, split preview, find-in-preview, zoom, and retained live HTML page state
- **Self-healing sessions** — After mobile suspension or a silent SSE drop, muselab probes, reconnects, and pulls the final message automatically
- **Multiple modes and themes** — Light / dark / eye-care themes, with your own accent color
- **Bilingual UI** — Switch between English and Chinese in one click, without refreshing the page
- **Message queue** — Keep sending messages while Muse thinks; the queue runs them in order so no idea is lost
- **Tasks and notifications** — Scheduled jobs, background work, the activity center, and Web Push report results in one flow

## Docs

**[📚 Full documentation index](docs/README.md)**

- **Get started:** [Quick start](docs/quickstart.md) · [Linux install](docs/install-linux.md) · [macOS install](docs/install-macos.md) · [Upgrade](docs/upgrade.md)
- **Usage:** [Configure workspace CLAUDE.md](docs/personalize-claude-md.md) · [Skills](docs/skills.md) · [Terminal](docs/terminal.md) · [Mobile PWA](docs/mobile.md) · [Scheduled tasks](docs/scheduler.md)
- **Models:** [Providers](docs/providers.md) · [Codex Gateway](docs/codex-gateway.md) · [Add a provider](docs/add-provider.md) · [Model routing](docs/routing.md)
- **Internals:** [Architecture](docs/architecture.md) · [Sessions](docs/backend-sessions.md) · [Files API](docs/backend-files.md) · [Security model](docs/backend-security.md) · [Frontend](docs/frontend.md) · [Infrastructure](docs/infrastructure.md)
- **Reference:** [Configuration](docs/configuration.md) · [Data & backup](docs/data-and-backup.md) · [Troubleshooting](docs/troubleshooting.md) · [Glossary](docs/glossary.md)
- **Concepts:** [How it compares](docs/comparison.md) · [The nine Muses](docs/muses.md)
- **Project:** [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Third-party licenses](THIRD_PARTY_LICENSES.md)

## Status

v1.2 — multi-workspace support, a real terminal, and richer agent workflows.

[MIT](LICENSE)
