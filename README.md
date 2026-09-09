<h1 align="center">muselab</h1>

<p align="center">
  <a href="https://github.com/hesorchen/muselab/actions/workflows/ci.yml"><img src="https://github.com/hesorchen/muselab/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="docs/quickstart_zh.md"><img src="https://img.shields.io/badge/deploy-self--hosted-orange.svg" alt="Self-hosted"></a>
  <a href="https://github.com/hesorchen/muselab/pkgs/container/muselab"><img src="https://img.shields.io/badge/ghcr.io-muselab-blue?logo=docker" alt="Container"></a>
  <a href="https://deepwiki.com/hesorchen/muselab"><img src="https://deepwiki.com/badge.svg" alt="Ask DeepWiki"></a>
  <a href="README_en.md"><img src="https://img.shields.io/badge/lang-English-red" alt="English"></a>
</p>

<p align="center"><strong>muselab 是一个基于 Claude Agent SDK 构建的自托管 Agent 原生工作台</strong></p>

<p align="center">支持 Agent 交互、多工作区切换、全局任务管理与多端同步。</p>

<p align="center"><a href="#快速开始">快速开始</a> · <a href="#核心特性">核心特性</a> · <a href="docs/README_zh.md">完整文档</a> · <a href="https://hesorchen.github.io/muselab/promo/">场景演示</a></p>

<p align="center"><em>Muse 来自希腊神话中的缪斯女神，象征灵感、艺术与知识。</em></p>

<p align="center"><a href="promo/media/screenshot-workbench-hero.png"><img src="promo/media/screenshot-workbench-hero.png" width="1000" alt="桌面工作台 · 文件、多会话标签与 HTML 预览"></a></p>
<p align="center">桌面工作台 · 文件、多会话标签与 HTML 预览</p>

<details>
<summary>更多截图：任务中心、深色主题与移动端</summary>

<table align="center">
<tr>
<td align="center"><a href="promo/media/screenshot-task-center-demo.png"><img src="promo/media/screenshot-task-center-demo.png" width="500" alt="任务管理中心 · 运行中、失败与已完成任务"></a></td>
<td align="center"><a href="promo/media/screenshot-desktop-dark.png"><img src="promo/media/screenshot-desktop-dark.png" width="500" alt="桌面端 · 深色主题"></a></td>
</tr>
<tr>
<td align="center">任务管理中心 · 运行中、失败与已完成任务</td>
<td align="center">桌面端 · 深色主题</td>
</tr>
</table>

<table align="center">
<tr>
<td align="center"><a href="promo/media/screenshot-mobile-files.png"><img src="promo/media/screenshot-mobile-files.png" width="180" alt="移动端文件区"></a></td>
<td align="center"><a href="promo/media/screenshot-mobile-chat.png"><img src="promo/media/screenshot-mobile-chat.png" width="180" alt="移动端 Agent 对话"></a></td>
<td align="center"><a href="promo/media/screenshot-mobile-preview.png"><img src="promo/media/screenshot-mobile-preview.png" width="180" alt="移动端 HTML 预览"></a></td>
</tr>
<tr>
<td align="center">移动端 · 文件区</td>
<td align="center">移动端 · Agent 对话</td>
<td align="center">移动端 · HTML 预览</td>
</tr>
</table>

</details>

## 核心特性

| 功能 | 说明 |
|---|---|
| **Agent 原生工作台** | 围绕 Agent 任务集成文件、会话、预览与真实终端，支持查看执行过程、编辑文件和验证结果 |
| **多工作区** | 按项目切换工作区，以项目指令、记忆文件、文档与 Skills 组织 Agent 的工作上下文 |
| **Agent 执行与扩展** | 展示工具调用、子代理时间线、代码差异、Hook 状态和模型用量，支持 Skills、MCP 与 Hook 配置 |
| **连续的会话工作流** | 支持多会话标签、后台执行、持久消息队列、会话分支与末轮重问 |
| **全局任务管理** | 集中查看跨会话的任务状态与执行结果，支持后台任务、定时任务和推送通知 |
| **长期记忆** | 通过做梦机制整理任务经历，支持混合召回、现场证据回溯和人工修订；默认关闭，需单独配置 |
| **多模型与 Provider** | 支持 Claude OAuth、API Key，以及经独立本地网关接入的 Codex；可配置 Anthropic-compatible 端点 |
| **文件预览与编辑** | 支持 Markdown、HTML、图片、PDF、XLSX、CSV、TSV 与文本预览，以及 Markdown 编辑和分屏预览 |
| **桌面与移动端** | 桌面浏览器与移动端 PWA 访问同一份工作文件与会话，支持中英双语与多主题 |

配置说明：[工作区](docs/personalize-claude-md_zh.md) · [模型接入](docs/providers_zh.md) · [Codex Gateway](docs/codex-gateway_zh.md) · [长期记忆](docs/memory_zh.md)。

## 快速开始

**一行命令安装**（Linux + macOS + WSL2）：

```bash
curl -fsSL https://raw.githubusercontent.com/hesorchen/muselab/main/scripts/quick-install.sh | bash
```

**手动安装**（先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)）：

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
bash scripts/install-linux.sh    # 或 install-macos.sh
```

需要 Python 3.12 及以上版本；前端无需 npm 构建。引导脚本会安装 `uv`，平台安装器会同步依赖并在需要时尝试补装 Node 与 CLI。Docker、开发模式和详细环境要求见[快速入门](docs/quickstart_zh.md)。

**安装后验证**：

1. 在安装机器的浏览器中打开 `http://localhost:8765`。
2. 使用安装时配置的 `MUSELAB_TOKEN` 登录。
3. 选择工作区，并配置至少一种模型。配置方式见 [Providers](docs/providers_zh.md)。
4. 发送 `你好`，确认 Muse 正常响应，再打开一个文件检查预览。

安装或运行异常时，可在仓库目录运行 `bash scripts/doctor.sh`，检查环境并获取修复建议。

> **Windows 用户：** 请通过 WSL2 安装（参见[快速入门](docs/quickstart_zh.md#windows-用户走-wsl2)）。
>
> **无人值守安装：** `MUSELAB_NONINTERACTIVE=1 bash scripts/install-linux.sh`。配置要求见平台安装文档。

## 文档

**[📚 完整文档索引](docs/README_zh.md)**

- **上手：** [快速入门](docs/quickstart_zh.md) · [Linux 安装](docs/install-linux_zh.md) · [macOS 安装](docs/install-macos_zh.md) · [升级](docs/upgrade_zh.md)
- **使用：** [工作台操作](docs/workbench-ui_zh.md) · [配置工作区 CLAUDE.md](docs/personalize-claude-md_zh.md) · [Skills](docs/skills_zh.md) · [终端](docs/terminal_zh.md) · [手机端 PWA](docs/mobile_zh.md) · [定时任务](docs/scheduler_zh.md) · [长期记忆](docs/memory_zh.md)
- **模型：** [Providers](docs/providers_zh.md) · [Codex Gateway](docs/codex-gateway_zh.md) · [接入新 provider](docs/add-provider_zh.md) · [模型路由](docs/routing_zh.md)
- **内部机制：** [架构](docs/architecture_zh.md) · [会话](docs/backend-sessions_zh.md) · [Files API](docs/backend-files_zh.md) · [安全模型](docs/backend-security_zh.md) · [前端](docs/frontend_zh.md) · [基础设施](docs/infrastructure_zh.md)
- **参考：** [配置](docs/configuration_zh.md) · [数据与备份](docs/data-and-backup_zh.md) · [排错](docs/troubleshooting_zh.md) · [词汇表](docs/glossary_zh.md)
- **概念：** [同类对比](docs/comparison_zh.md) · [九位缪斯](docs/muses_zh.md)
- **项目：** [安全](SECURITY.md) · [贡献指南](CONTRIBUTING.md) · [第三方授权](THIRD_PARTY_LICENSES.md)

## 状态

当前版本为 v1.2。升级已有安装前，请阅读[升级说明](docs/upgrade_zh.md)并备份数据。

[MIT](LICENSE)
