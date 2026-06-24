# muselab

[![CI](https://github.com/hesorchen/muselab/actions/workflows/ci.yml/badge.svg)](https://github.com/hesorchen/muselab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Self-hosted](https://img.shields.io/badge/deploy-self--hosted-orange.svg)](docs/quickstart.md)
[![Container](https://img.shields.io/badge/ghcr.io-muselab-blue?logo=docker)](https://github.com/hesorchen/muselab/pkgs/container/muselab)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/hesorchen/muselab)
[![中文](https://img.shields.io/badge/lang-中文-red)](README.md)

**muselab is a self-hosted AI personal workspace built on the Claude Agent SDK.**

> Muse comes from the Muses of Greek mythology, goddesses of inspiration, art, and knowledge.

## Core features

| | |
|---|---|
| **Complete user context** | Your personal archive keeps accumulating; the more you use it, the better Muse understands you, creating compounding context |
| **Leading Agent Harness** | Built on the Claude Agent SDK, with agent capabilities such as tool use, Skills, and MCP extensions |
| **Switchable foundation models** | One-click switching across 8 model providers: Claude / DeepSeek / GLM / MiniMax / Kimi / Qwen / MiMo / ERNIE |
| **Cross-domain analysis** | Family information, career planning, health records, and financial data live in one context, so Muse can surface cross-domain insights |
| **Native rendering** | HTML pages and Markdown documents render live as they are written, with no plugins required |
| **Mobile PWA** | Near-native App experience, synced sessions across desktop and phone, and continued work while you are away from your desk |

<p align="center">
  <img src="promo/media/screenshot-desktop.png" width="720"
       alt="muselab desktop three-pane layout: file tree, chat, and live HTML preview in dark mode">
</p>
<p align="center"><em>Desktop three-pane layout — archive area, native preview area, and Muse conversation area (dark mode with live HTML rendering). Click to enlarge.</em></p>

<table align="center">
<tr>
<td><img src="promo/media/screenshot-mobile-files.jpeg" width="200"></td>
<td><img src="promo/media/screenshot-mobile-preview.png" width="200"></td>
<td><img src="promo/media/screenshot-mobile-chat.png" width="200"></td>
</tr>
<tr>
<td align="center">Mobile · files</td>
<td align="center">Mobile · preview</td>
<td align="center">Mobile · chat</td>
</tr>
</table>

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

> "This is my checkup report from this year. Compare it with last year's report and turn the metric changes into a one-page HTML trend report."

Muse finds both PDFs in `health/`, reads the files, extracts the metrics, and writes a single-file HTML report with charts — rendered directly in the preview pane. Then you say:

> "Now check the insurance policies in `money/`. Do these metric changes reveal any coverage gaps?"

Archives from two domains are analyzed in the same session, producing concrete guidance.

🌐 More scene demos on the [muselab promo page](https://hesorchen.github.io/muselab/promo/).

## Why not existing solutions?

| Solution | Limitation | How muselab works |
|---|---|---|
| ChatGPT / Claude.ai | Files are uploaded temporarily; memory is a black box | Archived files stay local, with a transparent memory mechanism |
| Claude Code | Born in the terminal, built for code | The same Agent Harness, aimed at life files, usable on desktop and phone |
| RAG document chat | Chunking + retrieval loses cross-document meaning; better suited for massive document sets | Stores source documents and reads complete files for lossless understanding |

Full comparison (Open WebUI / LobeChat / AnythingLLM / claudecodeui, etc.): [How it compares](docs/comparison.md).

## Practical details

- **Modern file tree** — Modern file operations: drag-and-drop upload, fuzzy search, rename, and trash
- **Multiple modes and themes** — Light / dark / eye-care themes, with your own accent color
- **Bilingual UI** — Switch between English and Chinese in one click, without refreshing the page
- **Message queue** — Keep sending messages while Muse thinks; the queue runs them in order so no idea is lost
- **Scheduled tasks** — Create overnight tasks and check the results when you wake up

## Docs

**[📚 Full documentation index](docs/README.md)**

- **Get started:** [Quick start](docs/quickstart.md) · [Linux install](docs/install-linux.md) · [macOS install](docs/install-macos.md) · [Upgrade](docs/upgrade.md)
- **Usage:** [Personalize CLAUDE.md](docs/personalize-claude-md.md) · [Skills](docs/skills.md) · [Mobile PWA](docs/mobile.md) · [Scheduled tasks](docs/scheduler.md)
- **Models:** [Providers](docs/providers.md) · [Add a provider](docs/add-provider.md) · [Model routing](docs/routing.md)
- **Internals:** [Architecture](docs/architecture.md) · [Sessions](docs/backend-sessions.md) · [Files API](docs/backend-files.md) · [Security model](docs/backend-security.md) · [Frontend](docs/frontend.md) · [Infrastructure](docs/infrastructure.md)
- **Reference:** [Configuration](docs/configuration.md) · [Data & backup](docs/data-and-backup.md) · [Troubleshooting](docs/troubleshooting.md) · [Glossary](docs/glossary.md)
- **Concepts:** [How it compares](docs/comparison.md) · [The nine Muses](docs/muses.md)
- **Project:** [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Third-party licenses](THIRD_PARTY_LICENSES.md)

## Status

v1.1 — first stable enhancement release.

[MIT](LICENSE)
