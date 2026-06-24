# muselab docs

> [简体中文](README_zh.md) · [← back to project README](../README_en.md)

## Install & run

- [Quick start](quickstart.md) — prerequisites, Docker, dev mode, per-OS notes
- [Install on Linux](install-linux.md)
- [Install on macOS](install-macos.md)
- [Upgrading](upgrade.md) — bump the SDK + CLI without losing data

## Use

- [Personalize your CLAUDE.md](personalize-claude-md.md) — teach Muse about you
- [Skills](skills.md) — what ships out of the box, and how to add your own
- [Mobile (PWA)](mobile.md) — install to home screen, push notifications, HTTPS
- [Scheduled tasks](scheduler.md) — run a saved prompt on a cadence

## Models

- [Providers](providers.md) — the built-in catalog (Claude, DeepSeek, GLM,
  MiniMax, Kimi, Qwen, Xiaomi MiMo, Baidu ERNIE)
- [Add a provider](add-provider.md) — wire any Anthropic-compatible endpoint
- [Model routing & the chat loop](routing.md) — how a model is chosen, pooled,
  and billed to the right account

## Architecture & internals

Source-linked deep dives — start at [Architecture](architecture.md) for the map.

- [Architecture](architecture.md) — directory map + how a request flows
- [Session internals](backend-sessions.md) — index, sidecars, queue, fork,
  restart recovery
- [Files API](backend-files.md) — every `/api/files/*` endpoint + `safe_resolve`
- [Security model](backend-security.md) — auth, billing isolation, honest
  limitations
- [Frontend internals](frontend.md) — no-build SPA, rendering pipeline, SSE
  client, service worker
- [MCP architecture](mcp-architecture.md) — connector strategy and the
  three-layer model
- [Infrastructure](infrastructure.md) — scripts, services, Docker, tests, CI/CD

## Reference

- [Configuration](configuration.md) — every `.env` variable + defaults
- [Data & backup](data-and-backup.md) — what to back up, how to restore
- [Troubleshooting](troubleshooting.md) — common failures and fixes
- [Glossary](glossary.md) — muselab's terms of art, defined once

## Concepts

- [How it compares](comparison.md) — muselab vs other self-hosted AI workspaces
- [The nine Muses](muses.md) — the lore behind the name

## Project

- [Security policy](../SECURITY.md)
- [Contributing](../CONTRIBUTING.md)
- [Third-party licenses](../THIRD_PARTY_LICENSES.md)
