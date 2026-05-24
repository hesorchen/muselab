# muselab

> 自部署的多模型 AI 工作台，多端共享会话。
> 私人档案是它的工作集，Muse 了解你的全貌。

[![CI](https://github.com/hesorchen/muselab/actions/workflows/ci.yml/badge.svg)](https://github.com/hesorchen/muselab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Self-hosted](https://img.shields.io/badge/deploy-self--hosted-orange.svg)](docs/quickstart_zh.md)
[![Container](https://img.shields.io/badge/ghcr.io-muselab-blue?logo=docker)](https://github.com/hesorchen/muselab/pkgs/container/muselab)
[![English](https://img.shields.io/badge/lang-English-red)](README.md)

- 🧠 **归档即工作集**。`MUSELAB_ROOT` 指向用户自有目录——其中的文件是 Muse（muselab 内置的 AI 助手）的一等上下文。
- 🤖 **所有模型共用同一套 agent loop**。MCP / Skills / Subagent / plan 模式在 Claude、DeepSeek、GLM、MiniMax、Kimi、Qwen、小米 MiMo 上行为一致。
- 💸 **复用 Claude Pro / Max 订阅**（OAuth），无按令牌计费。也可填入第三方 API key。
- 🔄 **多端共享一台服务器**。手机 / 平板 / 桌面共用会话；归档数据始终保存在用户自己的主机上。
- 🛠 **无构建步骤**。原生 HTML + Alpine.js + CSS，以静态文件形式直接提供——修改文件后刷新浏览器即可生效。

> **单用户专属：一个 token，一个归档目录，运行在用户自己的机器上。**

## 安装

**一行命令**（Linux + macOS + WSL2）——安装 `uv`，克隆仓库至 `~/muselab`，由平台安装程序自动装 Node LTS 与 Anthropic `claude` CLI，并完成服务注册：

```bash
curl -fsSL https://raw.githubusercontent.com/hesorchen/muselab/main/scripts/quick-install.sh | bash
```

> **Windows 用户：** 请通过 WSL2 安装（参见 [Quick start](docs/quickstart_zh.md#windows-用户走-wsl2)）。

**无人值守**——CI / Docker / 录 demo 用。全部取默认值（随机 token、端口 8765、`~/muselab-archive`），跳过所有交互：

```bash
curl -fsSL https://raw.githubusercontent.com/hesorchen/muselab/main/scripts/quick-install.sh | MUSELAB_NONINTERACTIVE=1 bash
```

**手动安装**——逐步执行每条命令：

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
bash scripts/install-linux.sh    # 或 install-macos.sh
```

访问 `http://localhost:8765`，粘贴 `.env` 中的 token。若安装器末尾提示「claude CLI 已装但未登录」，执行一次 `claude login` 即可激活 Anthropic 模型。

前置准备、Docker、开发模式与各平台详细说明，参见 [快速入门](docs/quickstart_zh.md)。

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

Pre-1.0，作者每日使用中。欢迎提交 PR——参见 [CONTRIBUTING.md](CONTRIBUTING.md)。路线图与已知问题见 [GitHub Issues](https://github.com/hesorchen/muselab/issues)。

[MIT](LICENSE)
