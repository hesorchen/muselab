# muselab

[![CI](https://github.com/hesorchen/muselab/actions/workflows/ci.yml/badge.svg)](https://github.com/hesorchen/muselab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Self-hosted](https://img.shields.io/badge/deploy-self--hosted-orange.svg)](docs/quickstart_zh.md)
[![Container](https://img.shields.io/badge/ghcr.io-muselab-blue?logo=docker)](https://github.com/hesorchen/muselab/pkgs/container/muselab)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/hesorchen/muselab)
[![English](https://img.shields.io/badge/lang-English-red)](README_en.md)

**muselab 是一个基于 Claude Agent SDK 构建的自托管 AI 个人工作台**
> Muse 来自希腊神话中的缪斯女神，象征灵感、艺术与知识。

## 核心特性

| | |
|---|---|
| **完整的用户上下文** | 不断累积的个人档案，越用越懂你，产生 context 复利 |
| **领先的 Agent Harness** | 基于 Claude Agent SDK 构建，具备工具调用、Skills、MCP 扩展等 Agent 能力|
| **灵活切换的基座模型** |Claude / DeepSeek / GLM / MiniMax / Kimi / Qwen / MiMo / ERNIE 等 8 家模型一键切换 |
| **跨领域交叉分析** | 家庭信息 ✖️ 职业规划 ✖️ 健康档案 ✖️ 财务数据 ，Muse 给出跨领域洞察 |
| **原生渲染能力** | HTML 页面、Markdown 文档即写即渲染，无需插件 |
| **移动端 PWA** | 获得接近原生 App 的体验，电脑手机多端同步会话，出门在外手机接着聊 |

<p align="center">
  <img src="promo/media/screenshot-desktop.png" width="720"
       alt="muselab 桌面三栏：文件树、对话、预览区——黑夜模式下 HTML 实时渲染">
</p>
<p align="center"><em>桌面三栏布局——归档文件区、原生预览区、Muse 会话区（黑夜模式 + HTML 实时渲染）· 点击放大</em></p>

<table align="center">
<tr>
<td><img src="promo/media/screenshot-mobile-files.jpeg" width="200"></td>
<td><img src="promo/media/screenshot-mobile-preview.png" width="200"></td>
<td><img src="promo/media/screenshot-mobile-chat.png" width="200"></td>
</tr>
<tr>
<td align="center">移动端文件区</td>
<td align="center">移动端预览区</td>
<td align="center">移动端对话区</td>
</tr>
</table>

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
- **多模式多主题** —— 亮色 / 暗色 / 护眼，自选主题色
- **中英双语** —— 一键切换，不刷新页面
- **消息队列** —— Muse 思考时继续发送消息，消息队列依次执行，不错过每一个灵感
- **定时任务** —— 创建夜晚定时任务，早上醒来查看结果

## 文档

**[📚 完整文档索引](docs/README_zh.md)**

- **上手：** [快速入门](docs/quickstart_zh.md) · [Linux 安装](docs/install-linux_zh.md) · [macOS 安装](docs/install-macos_zh.md) · [升级](docs/upgrade_zh.md)
- **使用：** [定制 CLAUDE.md](docs/personalize-claude-md_zh.md) · [Skills](docs/skills_zh.md) · [手机端 PWA](docs/mobile_zh.md) · [定时任务](docs/scheduler_zh.md)
- **模型：** [Providers](docs/providers_zh.md) · [接入新 provider](docs/add-provider_zh.md) · [模型路由](docs/routing_zh.md)
- **内部机制：** [架构](docs/architecture_zh.md) · [会话](docs/backend-sessions_zh.md) · [Files API](docs/backend-files_zh.md) · [安全模型](docs/backend-security_zh.md) · [前端](docs/frontend_zh.md) · [基础设施](docs/infrastructure_zh.md)
- **参考：** [配置](docs/configuration_zh.md) · [数据与备份](docs/data-and-backup_zh.md) · [排错](docs/troubleshooting_zh.md) · [词汇表](docs/glossary_zh.md)
- **概念：** [同类对比](docs/comparison_zh.md) · [九位缪斯](docs/muses_zh.md)
- **项目：** [安全](SECURITY.md) · [贡献指南](CONTRIBUTING.md) · [第三方授权](THIRD_PARTY_LICENSES.md)

## 状态

v1.1 — 首个稳定增强版本。

[MIT](LICENSE)
