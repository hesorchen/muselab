<h1 align="center">muselab</h1>

<p align="center">
  <a href="https://github.com/hesorchen/muselab/actions/workflows/ci.yml"><img src="https://github.com/hesorchen/muselab/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="docs/quickstart_zh.md"><img src="https://img.shields.io/badge/deploy-self--hosted-orange.svg" alt="Self-hosted"></a>
  <a href="https://github.com/hesorchen/muselab/pkgs/container/muselab"><img src="https://img.shields.io/badge/ghcr.io-muselab-blue?logo=docker" alt="Container"></a>
  <a href="https://deepwiki.com/hesorchen/muselab"><img src="https://deepwiki.com/badge.svg" alt="Ask DeepWiki"></a>
  <a href="README_en.md"><img src="https://img.shields.io/badge/lang-English-red" alt="English"></a>
</p>

<p align="center"><strong>muselab 是一个基于 Claude Agent SDK 构建的自托管 Agent 工作台</strong></p>

<p align="center"><em>Muse 来自希腊神话中的缪斯女神，象征灵感、艺术与知识。</em></p>

<table align="center">
<tr>
<td align="center"><a href="promo/media/screenshot-desktop.png"><img src="promo/media/screenshot-desktop.png" width="470" alt="桌面端完整工作台"></a></td>
<td align="center"><a href="promo/media/screenshot-desktop-terminal.png"><img src="promo/media/screenshot-desktop-terminal.png" width="470" alt="桌面端真实 Unix PTY 终端"></a></td>
</tr>
<tr>
<td align="center">桌面端 · 文件、预览与对话</td>
<td align="center">桌面端 · 真实 Unix PTY 终端</td>
</tr>
</table>

<table align="center">
<tr>
<td align="center"><a href="promo/media/screenshot-mobile-files.jpeg"><img src="promo/media/screenshot-mobile-files.jpeg" width="210" alt="移动端文件区"></a></td>
<td align="center"><a href="promo/media/screenshot-mobile-preview.png"><img src="promo/media/screenshot-mobile-preview.png" width="210" alt="移动端预览区"></a></td>
<td align="center"><a href="promo/media/screenshot-mobile-chat.png"><img src="promo/media/screenshot-mobile-chat.png" width="210" alt="移动端对话区"></a></td>
</tr>
<tr>
<td align="center">移动端 · 文件区</td>
<td align="center">移动端 · 原生预览</td>
<td align="center">移动端 · Agent 对话</td>
</tr>
</table>

<p align="center"><sub>点击任意图片查看原图</sub></p>

## 核心特性

| | |
|---|---|
| **复用已有订阅** | Claude 支持 OAuth；Codex 可通过 Gateway 接入，复用已有的 Claude、ChatGPT 订阅服务 |
| **一体化 Agent 工作台** | 在同一个页面中浏览文件、预览报告、与 Agent 对话交互，并通过真实终端运行和检查结果 |
| **多工作目录** | 创建并一键切换多个工作目录；每个工作目录都是一整套完整的 Agent 上下文和运行环境 |
| **真实 Unix PTY 多终端** | 创建、重命名和切换多个真实终端；每个终端绑定创建时的工作目录，并支持 Terminal Profile |
| **Claude Agent SDK** | 原生支持文件读写、工具调用、Skills 和 MCP，让 Agent 可以直接执行任务并生成成果 |
| **原生文件预览** | 支持 Markdown、文本、HTML、图片、PDF、XLSX、CSV 和 TSV 等文件格式 |
| **多模型与多 Provider** | 内置 Claude、Codex Gateway 等 Provider，也可接入所有 Anthropic-compatible 模型服务，如 Kimi、GLM、Deepseek 等 |
| **自托管与多端访问** | 工作目录、会话状态和生成文件保存在本地，可通过桌面浏览器和移动端 PWA 访问，移动端与 PC 端内容实时同步 |

## 快速开始

**一行命令安装**（Linux + macOS + WSL2）：

```bash
curl -fsSL https://raw.githubusercontent.com/hesorchen/muselab/main/scripts/quick-install.sh | bash
```

**手动安装**：

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
bash scripts/install-linux.sh    # 或 install-macos.sh
```

**安装后验证**：

1. 浏览器打开 `http://localhost:8765`
2. 粘贴 `MUSELAB_TOKEN` 登录
3. 配置至少一种模型
4. 发送 `你好` 确认 Muse 正常响应

出问题？运行 `bash scripts/doctor.sh`，逐层诊断并给出修复建议。

> **Windows 用户：** 请通过 WSL2 安装（参见 [快速入门](docs/quickstart_zh.md#windows-用户走-wsl2)）。
>
> **无人值守**（CI / Docker / 录 demo）：`MUSELAB_NONINTERACTIVE=1 bash ...`

## 会话实践

> 「这是我今年的体检报告，你帮我和去年那份对比一下，把指标变化做成一页 HTML 趋势报告。」

Muse 在 `health/` 里找到两份 PDF，读取文件，提取指标，写出带图表的单文件 HTML——预览区直接渲染。你接着说：

> 「再结合 `money/` 里的保单，看看这些变化指标有没有保障缺口。」

两个领域的档案在同一个会话里交叉分析，提供具体指导。

🌐 更多场景演示见 [muselab 介绍页](https://hesorchen.github.io/muselab/promo/)。

## 为什么不是现有方案？

| 方案 | 局限 | muselab 怎么做 |
|---|---|---|
| ChatGPT / Claude.ai | 文件临时上传、记忆内容黑盒 | 归档文件常驻本地，白盒记忆机制 |
| Claude Code | 生在终端、为代码而生 | 同一套 Agent Harness，面向生活，电脑手机多端可用 |
| RAG 文档问答 | 切块 + 检索，跨文档语义有损，适合海量文档 | 保存资料文档，完整文件理解，零语义损耗 |

完整对比（Open WebUI / LobeChat / AnythingLLM / claudecodeui 等）见[同类对比](docs/comparison_zh.md)。

## 实用细节

- **现代文件树** —— 现代化的文件操作，拖拽上传、模糊搜索、重命名、回收站
- **文件与终端双预览面** —— Markdown、文本、表格和 HTML 恢复上次阅读位置；真实 PTY 终端支持多实例、Profile 与移动端操作
- **工作目录隔离** —— 可登记并切换多个本地目录，文件、预览、会话标签和新会话 cwd 作为一个整体切换
- **会话工作台** —— 多会话标签、置顶与搜索、后台流式回复、持久消息队列、上下文用量与回合耗时
- **全局搜索** —— 文件名、文件内容、会话、历史消息和常用操作统一检索
- **编辑与预览** —— Markdown 编辑、分屏预览、页内查找、缩放和 HTML 活动页面状态保留
- **会话自动同步** —— 移动端休眠或 SSE 静默断开后自动探测、重连并补齐最终消息
- **多模式多主题** —— 亮色 / 暗色 / 护眼，自选主题色
- **中英双语** —— 一键切换，不刷新页面
- **消息队列** —— Muse 思考时继续发送消息，消息队列依次执行，不错过每一个灵感
- **任务与通知** —— 定时任务、后台任务、活动中心与 Web Push 统一反馈执行结果

## 文档

**[📚 完整文档索引](docs/README_zh.md)**

- **上手：** [快速入门](docs/quickstart_zh.md) · [Linux 安装](docs/install-linux_zh.md) · [macOS 安装](docs/install-macos_zh.md) · [升级](docs/upgrade_zh.md)
- **使用：** [定制 CLAUDE.md](docs/personalize-claude-md_zh.md) · [Skills](docs/skills_zh.md) · [终端](docs/terminal_zh.md) · [手机端 PWA](docs/mobile_zh.md) · [定时任务](docs/scheduler_zh.md)
- **模型：** [Providers](docs/providers_zh.md) · [Codex Gateway](docs/codex-gateway_zh.md) · [接入新 provider](docs/add-provider_zh.md) · [模型路由](docs/routing_zh.md)
- **内部机制：** [架构](docs/architecture_zh.md) · [会话](docs/backend-sessions_zh.md) · [Files API](docs/backend-files_zh.md) · [安全模型](docs/backend-security_zh.md) · [前端](docs/frontend_zh.md) · [基础设施](docs/infrastructure_zh.md)
- **参考：** [配置](docs/configuration_zh.md) · [数据与备份](docs/data-and-backup_zh.md) · [排错](docs/troubleshooting_zh.md) · [词汇表](docs/glossary_zh.md)
- **概念：** [同类对比](docs/comparison_zh.md) · [九位缪斯](docs/muses_zh.md)
- **项目：** [安全](SECURITY.md) · [贡献指南](CONTRIBUTING.md) · [第三方授权](THIRD_PARTY_LICENSES.md)

## 状态

v1.2 — 多工作区、真实终端与更完整的 Agent 工作流。

[MIT](LICENSE)
