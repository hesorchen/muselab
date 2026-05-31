# muselab

[![CI](https://github.com/hesorchen/muselab/actions/workflows/ci.yml/badge.svg)](https://github.com/hesorchen/muselab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Self-hosted](https://img.shields.io/badge/deploy-self--hosted-orange.svg)](docs/quickstart.md)
[![Container](https://img.shields.io/badge/ghcr.io-muselab-blue?logo=docker)](https://github.com/hesorchen/muselab/pkgs/container/muselab)
[![中文](https://img.shields.io/badge/lang-中文-red)](README_zh.md)

Muse (muselab's built-in assistant) reads across your checkup PDFs, budget spreadsheets,
family info, asset allocations, and career plans — helping you make cross-domain decisions.

- 🧠 **Raw semantics, zero loss.** No vectorizing. PDFs, spreadsheets, Markdown, HTML —
  the original information stays intact. Muse reads them directly. The richer your
  material, the better the outcome.

- 🤖 **Claude Agent SDK × eight models.** MCP tools, Skills, Subagents, plan mode — all
  intact. Not just chat, real delivery. Claude / DeepSeek / GLM / MiniMax / Kimi /
  Qwen / MiMo / ERNIE — switch in one click.

- 🖥️ **Live rendering, multi-device sync.** Muse's HTML reports and Markdown docs render
  live in the preview pane. Sessions sync across desktop and phone, with PWA install
  and push notifications.

<p align="center">
  <img src="promo/media/screenshot-desktop.png" height="340"
       alt="muselab desktop: file tree, chat, and a live-rendered preview pane">
  &nbsp;&nbsp;
  <img src="promo/media/screenshot-mobile.png" height="340"
       alt="muselab on a phone — the same session, continued">
</p>
<p align="center"><em>Rich rendering, real-time multi-device sync — HTML reports, tables and charts render as they're written; one session shared across desktop and phone.</em></p>

## See it in action

🌐 [muselab promo page](https://hesorchen.github.io/muselab/promo/) —
   scene demos, capabilities overview, comparisons & FAQ — a quick look at what muselab can do.

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
  [Mobile (PWA)](docs/mobile.md) ·
  [Scheduled tasks](docs/scheduler.md)
- **Models:** [Providers](docs/providers.md) ·
  [Add a provider](docs/add-provider.md)
- **Reference:** [Configuration](docs/configuration.md) ·
  [Data & backup](docs/data-and-backup.md) ·
  [Troubleshooting](docs/troubleshooting.md) ·
  [Upgrading](docs/upgrade.md)
- **Concepts:** [Architecture](docs/architecture.md) ·
  [How it compares](docs/comparison.md) ·
  [The nine Muses](docs/muses.md)
- **Project:** [Security](SECURITY.md) ·
  [Contributing](CONTRIBUTING.md) ·
  [Third-party licenses](THIRD_PARTY_LICENSES.md)

## Status

v1.0 — first stable release. PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
The roadmap and known issues are tracked on [GitHub Issues](https://github.com/hesorchen/muselab/issues).

[MIT](LICENSE)
