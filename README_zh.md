# muselab

> 自托管 Web UI，把 **Claude Agent SDK** 跑在**你自己的文件**之上。

[![CI](https://github.com/hesorchen/muselab/actions/workflows/ci.yml/badge.svg)](https://github.com/hesorchen/muselab/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-179_passing-brightgreen.svg)](tests/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Self-hosted](https://img.shields.io/badge/deploy-self--hosted-orange.svg)](docs/quickstart_zh.md)
[![Container](https://img.shields.io/badge/ghcr.io-muselab-blue?logo=docker)](https://github.com/hesorchen/muselab/pkgs/container/muselab)
[![English](https://img.shields.io/badge/lang-English-red)](README.md)

- 🧠 **你的 archive 就是工作集**。`MUSELAB_ROOT` 指向你自己的目录——
  里面的文件是一等上下文，不是 RAG 召回对象。
- 🤖 **所有模型同一套 agent loop**。MCP / Skills / Subagent / plan 模式
  在 Claude、DeepSeek、GLM、MiniMax 上行为一致。
- 💸 **复用 Claude Pro / Max 订阅**（OAuth），无 token 计费。也可填
  第三方 vendor key。
- 🔄 **多端共享一台 server**。手机 / 平板 / 桌面共用会话；archive
  始终留在你自己的主机上。
- 🛠 **无构建链**。原生 HTML + Alpine.js + CSS，作为静态文件直出——
  编辑文件刷新浏览器即生效。

> **单用户专属**。muselab 设计在你自己的机器上为你一个人服务：一个 token、
> 一个 archive、无多用户隔离。如果你需要多租户部署，这个项目不适合（暂时）。

## 安装

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
bash scripts/install-linux.sh    # 或 install-macos.sh / install-windows.ps1
```

访问 `http://localhost:8765`，粘贴 `.env` 里的 token。

前置准备、Docker、开发模式与各 OS 详细指南，见 [Quick start](docs/quickstart_zh.md)。

## 文档

[Quick start](docs/quickstart_zh.md) ·
[模型](docs/providers_zh.md) ·
[架构](docs/architecture_zh.md) ·
[手机端 PWA](docs/mobile_zh.md) ·
[安全](SECURITY.md) ·
[同类对比](docs/comparison_zh.md) ·
[九位缪斯](docs/muses_zh.md) ·
[第三方授权](THIRD_PARTY_LICENSES.md)

## 状态

Pre-1.0，作者每日使用中。欢迎 PR——见 [CONTRIBUTING.md](CONTRIBUTING.md)。
路线图与已知问题：[TODO.md](TODO.md)。

[MIT](LICENSE)
