# muselab

[![CI](https://github.com/hesorchen/muselab/actions/workflows/ci.yml/badge.svg)](https://github.com/hesorchen/muselab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Self-hosted](https://img.shields.io/badge/deploy-self--hosted-orange.svg)](docs/quickstart.md)
[![Container](https://img.shields.io/badge/ghcr.io-muselab-blue?logo=docker)](https://github.com/hesorchen/muselab/pkgs/container/muselab)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/hesorchen/muselab)
[![中文](https://img.shields.io/badge/lang-中文-red)](README_zh.md)

**muselab is to your life's files what Claude Code is to your codebase.**

Models get replaced every month. Your context doesn't — it compounds.
The files you'd never hand to a SaaS — checkup PDFs, budget spreadsheets,
papers you've read, half-written notes — are exactly what AI should be
reading. muselab is a self-hosted AI workspace: the archive stays on your
own disk, and Muse — built on the same agent loop that powers Claude Code —
works on it directly. The only thing that ever leaves your machine is the
request sent to the model you picked.

- 🔐 **Private, so you hold nothing back.** No SaaS account, no cloud copy —
  which is why health, money and work can finally live in one archive.
  Muse reads across all of it at once, and gives the kind of advice no
  single-domain view can produce.

- 📈 **Your context compounds.** Whole files enter the context exactly as
  written — no vectorizing, no chunking, no retrieval index. Every new model
  generation is a free upgrade to *your* assistant, because the archive it
  lands on is already there, and growing. Eight providers, one click apart:
  Claude / DeepSeek / GLM / MiniMax / Kimi / Qwen / MiMo / ERNIE — swap the
  engine anytime, the asset stays yours. Reuse the Claude subscription you
  already pay for, or run on dirt-cheap API models.

- 📄 **Deliverables, not chat bubbles.** Muse writes HTML reports and
  Markdown docs that render live in the preview pane as it types — no
  plugins, no setup. A paper becomes an annotated reading page; a folder
  of statements becomes a charted report.

- 📱 **An agent in your pocket.** Claude Code lives in a terminal. A muselab
  task started at your desk can be steered from your phone on the way out —
  install as a PWA, get a push when a long run finishes.

<p align="center">
  <img src="promo/media/screenshot-desktop.png" height="340"
       alt="muselab desktop: file tree, chat, and a live-rendered preview pane">
  &nbsp;&nbsp;
  <img src="promo/media/screenshot-mobile.png" height="340"
       alt="muselab on a phone — the same session, continued">
</p>
<p align="center"><em>The desktop three-pane layout — archive tree, conversation with Muse, live preview — and the same session picked up on a phone.</em></p>

## What a session looks like

> "Compare this new checkup PDF with last year's, and turn the changes
> into a one-page HTML trend report."

Muse greps `health/` for both PDFs, reads them whole, extracts the numbers,
and writes a single-file HTML report with charts — rendered live in the
preview pane. Then you follow up:

> "Now check the insurance policies in `money/` — do any of these changes
> leave a gap?"

That's the crossing: two domains in one context is what turns answers into
actions. And on your way out, open the same session on your phone and keep
going.

🌐 More scene demos on the [muselab promo page](https://hesorchen.github.io/muselab/promo/).

## Why not just ChatGPT?

| What you use today | Where it stops | muselab |
|---|---|---|
| ChatGPT / Claude.ai | Files re-uploaded per chat, memory is a black box, sensitive archives stay out | The archive lives on your disk, readable in full |
| Claude Code | The strongest agent loop — but born in a terminal, built for code | The same loop, pointed at your life's files, in a browser and on your phone |
| RAG document chat | Chunk-and-retrieve loses meaning across documents | Whole files in context, zero loss |

Full comparison (Open WebUI, LobeChat, AnythingLLM, claudecodeui …):
[How it compares](docs/comparison.md).

## Small things, done right

- **Queue without losing a word** — keep typing while Muse works; a
  server-side FIFO queue runs each message in turn
- **Scheduled tasks** — daily / weekly / monthly / once; missed runs catch
  up after downtime; results land in a bell drawer and push to your phone
- **Session forking** — branch from any message, rewrite and re-run
- **Restart recovery** — sessions and queued messages come back exactly as
  they were
- **A real file tree** — drag-and-drop upload, search, inline rename,
  drag-to-trash
- **Three themes × accent picker** — light / dark / eye-care, with your
  choice of accent color
- **Bilingual UI** — English / 中文, one click
- **No build step** — edit a frontend file, refresh the browser

## Install

> Prerequisites: `git`, `curl` (bundled on Linux / macOS; on WSL2 run `sudo apt install git curl`).

**One-line (Linux + macOS + WSL2)** — installs `uv`, clones into `~/muselab`,
then runs the platform installer (which auto-installs Node LTS + the Anthropic
`claude` CLI and registers the service):

```bash
curl -fsSL https://raw.githubusercontent.com/hesorchen/muselab/main/scripts/quick-install.sh | bash
```

> **Windows users:** install via WSL2 (see [Quick start](docs/quickstart.md#windows-via-wsl2)).

**Unattended** — for CI / Docker / demo recording. Takes every default
(random token, port 8765, `~/muselab-archive`) and skips every prompt:

```bash
curl -fsSL https://raw.githubusercontent.com/hesorchen/muselab/main/scripts/quick-install.sh | MUSELAB_NONINTERACTIVE=1 bash
```

**Manual** — if you'd rather see every step:

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
bash scripts/install-linux.sh    # or install-macos.sh
```

Open `http://localhost:8765`, paste the token from `.env`. If the installer
reported "claude CLI is installed but not logged in", run `claude login`
once to enable Anthropic models.

For prerequisites, Docker, dev mode and per-OS detail, see
[Quick start](docs/quickstart.md).

## Docs

**[📚 Full documentation index](docs/README.md)**

- **Get going:** [Quick start](docs/quickstart.md) ·
  [Personalize CLAUDE.md](docs/personalize-claude-md.md) ·
  [Skills](docs/skills.md) ·
  [Mobile (PWA)](docs/mobile.md) ·
  [Scheduled tasks](docs/scheduler.md)
- **Models:** [Providers](docs/providers.md) ·
  [Add a provider](docs/add-provider.md) ·
  [Model routing](docs/routing.md)
- **Internals:** [Architecture](docs/architecture.md) ·
  [Sessions](docs/backend-sessions.md) ·
  [Files API](docs/backend-files.md) ·
  [Security model](docs/backend-security.md) ·
  [Frontend](docs/frontend.md) ·
  [Infrastructure](docs/infrastructure.md)
- **Reference:** [Configuration](docs/configuration.md) ·
  [Data & backup](docs/data-and-backup.md) ·
  [Troubleshooting](docs/troubleshooting.md) ·
  [Upgrading](docs/upgrade.md) ·
  [Glossary](docs/glossary.md)
- **Concepts:** [How it compares](docs/comparison.md) ·
  [The nine Muses](docs/muses.md)
- **Project:** [Security](SECURITY.md) ·
  [Contributing](CONTRIBUTING.md) ·
  [Third-party licenses](THIRD_PARTY_LICENSES.md)

## Status

v1.0 — first stable release. If compounding context resonates with you,
a ⭐ helps more people find it — and the best day to start your archive
is today. PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
The roadmap and known issues are tracked on
[GitHub Issues](https://github.com/hesorchen/muselab/issues).

[MIT](LICENSE)
