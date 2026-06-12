# muselab

[![CI](https://github.com/hesorchen/muselab/actions/workflows/ci.yml/badge.svg)](https://github.com/hesorchen/muselab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Self-hosted](https://img.shields.io/badge/deploy-self--hosted-orange.svg)](docs/quickstart_zh.md)
[![Container](https://img.shields.io/badge/ghcr.io-muselab-blue?logo=docker)](https://github.com/hesorchen/muselab/pkgs/container/muselab)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/hesorchen/muselab)
[![English](https://img.shields.io/badge/lang-English-red)](README.md)

**muselab 之于你的人生档案，犹如 Claude Code 之于你的代码库。**

模型每个月都在换代，但 context 只属于你——而且它在复利。那些你不敢交给 SaaS 的文件——体检报告、记账表格、读过的论文、写了一半的笔记——恰恰是 AI 最该看到的。muselab 是一个自托管 AI 工作台：档案留在你自己的硬盘上，由 Muse——基于驱动 Claude Code 的同一套 agent loop——直接在上面干活。离开你机器的，只有发给你所选模型的那一次请求。

- 🔐 **私有，所以放得全；放得全，所以看得深。** 没有 SaaS 账号、没有云端副本——健康、财务、工作才敢放进同一个档案库。Muse 同时读到它们，给出任何单一领域都给不出的跨领域建议。

- 📈 **你的 context 在复利。** 整文件原样进入上下文——不向量化、不切块、不建检索索引。每一代更强的模型，都是你的助理的免费升级——因为它接手的档案早已在那里，并且越积越厚。八家模型一键切换：Claude / DeepSeek / GLM / MiniMax / Kimi / Qwen / MiMo / ERNIE——引擎随便换，资产永远是你的。可以复用已有的 Claude 订阅，也可以跑在 DeepSeek、MiMo 这些便宜到忽略不计的国产模型上。

- 📄 **产出是交付物，不是聊天气泡。** Muse 写 HTML 报告、Markdown 文档，预览区即写即渲染——零插件、零配置。一篇论文变成一页精读，一摞流水变成一份带图表的报告。

- 📱 **口袋里的 agent。** Claude Code 困在终端里；muselab 在电脑上开的任务，出门路上用手机接着指挥——PWA 安装到桌面，长任务跑完锁屏推送叫你。

<p align="center">
  <img src="promo/media/screenshot-desktop.png" height="340"
       alt="muselab 桌面三栏：文件树、对话、预览区实时渲染">
  &nbsp;&nbsp;
  <img src="promo/media/screenshot-mobile.png" height="340"
       alt="muselab 手机端 —— 同一会话接着聊">
</p>
<p align="center"><em>桌面三栏布局——档案树、与 Muse 的对话、实时预览；右侧是同一会话在手机上接着聊。</em></p>

## 一次会话长什么样

> 「对比这份新体检报告和去年那份，把指标变化做成一页 HTML 趋势报告。」

Muse 在 `health/` 里找到两份 PDF，整文件读入，提出指标，写出带图表的单文件 HTML——右侧预览区直接渲染。你接着补一句：

> 「再结合 `money/` 里的保单，看看这些变化有没有该补的保障缺口。」

这就是交叉：两个领域的档案在同一个 context 里，答案才会指向具体行动。出门路上，手机打开同一个会话，接着聊。

🌐 更多场景演示见 [muselab 介绍页](https://hesorchen.github.io/muselab/promo/)。

## 为什么不直接用 ChatGPT？

| 你现在用的 | 卡在哪 | muselab |
|---|---|---|
| ChatGPT / Claude.ai | 文件逐次上传、记忆是黑盒，敏感档案不敢放全 | 档案常驻你的硬盘，全量可读 |
| Claude Code | 最强的 agent loop——但生在终端、为代码而生 | 同一套 loop，面向生活档案，浏览器 + 手机可用 |
| RAG 文档问答 | 切块 + 检索，跨文档语义有损 | 整文件进 context，零损耗 |

完整对比（含 Open WebUI / LobeChat / AnythingLLM / claudecodeui 等）见[同类对比](docs/comparison_zh.md)。

## 小心思

- **排队不丢话**——Muse 干活时尽管继续发，服务端 FIFO 队列依次执行
- **定时任务**——daily / weekly / monthly / once 四种节奏，宕机漏跑自动补跑，结果进铃铛抽屉、推送到手机
- **会话分叉**——从任意一条消息开分支、改写重跑
- **重启不断线**——后端重启后，会话与排队中的消息原样恢复
- **现代文件树**——拖拽上传、搜索、行内重命名、拖进回收站
- **三主题 × 自选强调色**——亮色 / 暗色 / 护眼，外加 accent 色板
- **中英双语界面**——一键切换
- **无构建步骤**——改前端文件，刷新浏览器就生效

## 安装

> 前置：`git`、`curl`（Linux / macOS 自带；WSL2 需 `sudo apt install git curl`）。

**一行命令**（Linux + macOS + WSL2）——安装 `uv`，克隆仓库至 `~/muselab`，由平台安装程序自动安装 Node LTS 与 Anthropic `claude` CLI，并完成服务注册：

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

访问 `http://localhost:8765`，粘贴 `.env` 中的 token。若安装脚本末尾提示「claude CLI 已装但未登录」，执行一次 `claude login` 即可激活 Anthropic 模型。

环境要求、Docker、开发模式与各平台详细说明，参见 [快速入门](docs/quickstart_zh.md)。

## 文档

**[📚 完整文档索引](docs/README_zh.md)**

- **上手：** [快速入门](docs/quickstart_zh.md) ·
  [定制 CLAUDE.md](docs/personalize-claude-md_zh.md) ·
  [Skills](docs/skills_zh.md) ·
  [手机端 PWA](docs/mobile_zh.md) ·
  [定时任务](docs/scheduler_zh.md)
- **模型：** [Providers](docs/providers_zh.md) ·
  [接入新 provider](docs/add-provider_zh.md) ·
  [模型路由](docs/routing_zh.md)
- **内部机制：** [架构](docs/architecture_zh.md) ·
  [会话](docs/backend-sessions_zh.md) ·
  [Files API](docs/backend-files_zh.md) ·
  [安全模型](docs/backend-security_zh.md) ·
  [前端](docs/frontend_zh.md) ·
  [基础设施](docs/infrastructure_zh.md)
- **参考：** [配置](docs/configuration_zh.md) ·
  [数据与备份](docs/data-and-backup_zh.md) ·
  [排错](docs/troubleshooting_zh.md) ·
  [升级](docs/upgrade_zh.md) ·
  [词汇表](docs/glossary_zh.md)
- **概念：** [同类对比](docs/comparison_zh.md) ·
  [九位缪斯](docs/muses_zh.md)
- **项目：** [安全](SECURITY.md) ·
  [贡献指南](CONTRIBUTING.md) ·
  [第三方授权](THIRD_PARTY_LICENSES.md)

## 状态

v1.0——首个稳定版。如果「context 复利」这个理念打动了你——⭐ 能帮更多人看到它，而你的档案，今天就是最好的开始日。欢迎提交 PR——参见 [CONTRIBUTING.md](CONTRIBUTING.md)。路线图与已知问题见 [GitHub Issues](https://github.com/hesorchen/muselab/issues)。

[MIT](LICENSE)
