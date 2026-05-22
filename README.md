# muselab

> A self-hosted web UI that runs the **Claude Agent SDK** on top of *your own files*.

[![CI](https://github.com/hesorchen/muselab/actions/workflows/ci.yml/badge.svg)](https://github.com/hesorchen/muselab/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-186_passing-brightgreen.svg)](tests/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Self-hosted](https://img.shields.io/badge/deploy-self--hosted-orange.svg)](docs/quickstart.md)
[![Container](https://img.shields.io/badge/ghcr.io-muselab-blue?logo=docker)](https://github.com/hesorchen/muselab/pkgs/container/muselab)
[![中文](https://img.shields.io/badge/lang-中文-red)](README_zh.md)

- 🧠 **Your archive is the working set.** `MUSELAB_ROOT` is a directory
  you own — files there are first-class context, not RAG documents.
- 🤖 **Full agent loop on every model.** MCP / Skills / Subagents / plan
  mode work the same on Claude, DeepSeek, GLM, MiniMax.
- 💸 **Reuse your Claude Pro / Max subscription** via OAuth — no
  per-token bill. Or bring a vendor API key.
- 🔄 **Multi-device, one server.** Phone / tablet / desktop share
  sessions; archive never leaves your host.
- 🛠 **No build step.** Vanilla HTML + Alpine.js + CSS, served as static
  files — edit, refresh, done.

> **Single-user only.** muselab is meant to run on your own machine for
> *you*. There's one token, one archive, and no per-user isolation. If
> you need a multi-tenant deployment, this isn't it (yet).

## Install

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
bash scripts/install-linux.sh    # or install-macos.sh / install-windows.ps1
```

Open `http://localhost:8765`, paste the token from `.env`.

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

Pre-1.0, personal project, used daily by the author. PRs welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md). Roadmap and known issues in
[TODO.md](TODO.md).

[MIT](LICENSE)
