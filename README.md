# muselab

> A self-hosted, multi-model AI workspace. Sessions sync across devices.
> Your archive is the working set — Muse knows the full picture.

[![CI](https://github.com/hesorchen/muselab/actions/workflows/ci.yml/badge.svg)](https://github.com/hesorchen/muselab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Self-hosted](https://img.shields.io/badge/deploy-self--hosted-orange.svg)](docs/quickstart.md)
[![Container](https://img.shields.io/badge/ghcr.io-muselab-blue?logo=docker)](https://github.com/hesorchen/muselab/pkgs/container/muselab)
[![中文](https://img.shields.io/badge/lang-中文-red)](README_zh.md)

- 🧠 **Your archive is the working set.** `MUSELAB_ROOT` is a directory
  you own — files there are first-class context for Muse (muselab's
  built-in AI assistant).
- 🤖 **Full agent loop on every model.** MCP / Skills / Subagents / plan
  mode work the same on Claude, DeepSeek, GLM, MiniMax, Kimi, Qwen,
  Xiaomi MiMo.
- 💸 **Reuse your Claude Pro / Max subscription** via OAuth — no
  per-token billing. Or bring a vendor API key.
- 🔄 **Multi-device, one server.** Phone / tablet / desktop share
  sessions; archive never leaves your host.
- 🛠 **No build step.** Vanilla HTML + Alpine.js + CSS, served as static
  files — edit, refresh, done.

> **Single-user: one token, one archive, runs on your own machine.**

## Install

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

[Quick start](docs/quickstart.md) ·
[Providers](docs/providers.md) ·
[Architecture](docs/architecture.md) ·
[Mobile (PWA)](docs/mobile.md) ·
[Security](SECURITY.md) ·
[How it compares](docs/comparison.md) ·
[The nine Muses](docs/muses.md) ·
[Third-party licenses](THIRD_PARTY_LICENSES.md)

## Status

Pre-1.0, personal project, used daily by the author. PRs are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md). The roadmap and known issues are tracked on
[GitHub Issues](https://github.com/hesorchen/muselab/issues).

[MIT](LICENSE)
