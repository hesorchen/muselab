# muselab

### 见见 **Muse** —— 真的认识你的 AI 助理。
*muselab 是 Muse 居住的自托管"工坊"，跟你的档案为邻。*

> **Anthropic Claude Agent SDK** 的 web harness，指向**你自己的档案**，**完全本地跑**，用**纯 HTML** 写。

[![CI](https://github.com/hesorchen/muselab/actions/workflows/ci.yml/badge.svg)](https://github.com/hesorchen/muselab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-148_passing-brightgreen.svg)](tests/)
[![Container](https://img.shields.io/badge/ghcr.io-muselab-blue?logo=docker)](https://github.com/hesorchen/muselab/pkgs/container/muselab)
[![English](https://img.shields.io/badge/lang-English-red)](README.md)

---

### `muselab` 真正的三个亮点

**🧠 直接基于 Anthropic 官方 Claude Agent SDK 的 harness**
完整 agent 能力（MCP / Skills / Subagent / plan / 工具调用 / CLAUDE.md
自动加载）——跟 Claude Code 同款引擎，但通过浏览器暴露 + 指向你的个人
archive。大多数所谓的 "Claude UI" 是 wrap CLI 进程，或直接 raw API；
muselab 直接用官方 SDK，**Anthropic 出新功能自动亮起**。同一套 agent
loop 通过 anthropic-compatible 端点也跑在 DeepSeek / GLM / MiniMax 上
——中间无协议翻译。

**🏠 本地自部署，数据不离机**
整个应用占 ~150 MB 内存，默认只绑 `127.0.0.1`，archive 在你指定的路径。
Anthropic / DeepSeek / GLM 只看到你真发给它们的消息；archive 文件 /
session 历史 / intake 答案 / CLAUDE.md 都**永远不离开机器**。VPS 部署
用 SSH tunnel 从笔记本访问；想"一直在线"用 Tailscale；**永远不要**把
8765 端口裸开到公网。

**🛠 HTML-native，零 JavaScript 构建链**
纯 HTML + [Alpine.js](https://alpinejs.dev) + 原生 CSS，静态文件服务。
无 npm 无 webpack 无 transpiler 无 React 无 Vue 无 Svelte。**前端能一晚
读完**。这跟 [htmx](https://htmx.org) / [11ty](https://www.11ty.dev) /
[Hotwire](https://hotwired.dev) / [Pieter Levels 用 PHP+jQuery 做到
$1M/年的 indie 案例](https://twitter.com/levelsio) / 及更大的[反对 web
逐年变胖](https://infrequently.org/2024/01/performance-inequality-gap-2024/)
潮流是同一直觉——**能完全看清的笨办法，胜过看不清的聪明办法**。

---

- 💸 复用 ¥150–700/月的 Pro / Max 订阅走 OAuth——不按 token 付费
- 🌏 也可填 **DeepSeek / GLM / MiniMax** key——同一套 SDK loop，无需 proxy
- 🚀 三个 OS 一条 install 命令，或 `docker run` 拉 GHCR 镜像
- ⚡ ~4.4 k 行 · 148 tests · 1 GB 内存 VPS 跑得动

> 📸 *Demo gif 还在录中。先看 [架构](#架构原理) 看数据流的 mermaid 图，或直接 [Quick start](#quick-start) 3 行命令跑起来。*

---

## 目录

- [适不适合我](#适不适合我)
- [Quick start](#quick-start) — 3 行命令
- [用哪个模型](#用哪个模型)
- [跟同类怎么比](#跟同类怎么比)
- [一天里怎么用](#一天里怎么用)
- [架构原理](#架构原理)
- [安全模型](#安全模型)
- [九位缪斯](#九位缪斯)
- [状态与贡献](#状态与贡献)

---

## 适不适合我

| 你... | muselab? |
|--------|----------|
| ✅ 有 Claude Pro / Max 订阅，不想再额外付 API 费 | **适合** |
| ✅ 笔记 / 体检 / 财务 都放在文件夹里，想让 AI 真的能读 | **适合** |
| ✅ 自托管 VPS / 小主机，想要一个能读完源码的工具 | **适合** |
| ✅ 想要同一套 agent loop 跨 Claude 和 DeepSeek/GLM/MiniMax | **适合** |
| ❌ 想要一个集成 Claude 的代码 IDE | 看 [claudecodeui](https://github.com/siteboon/claudecodeui) / [code-server + Cline](https://github.com/cline/cline) |
| ❌ 想跟一堆爬来的 / RAG 索引的公开文档对话 | 看 [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm) |
| ❌ 想要托管 SaaS，不想装 | muselab 只自托管；可看 [LobeChat Cloud](https://lobehub.com) |

---

## Quick start

### 0. 前置（3 分钟）

只要两件事：

#### 至少配一个模型 provider

| 你有的 | 怎么配 |
|----------------|-------|
| **Claude Pro / Max 订阅**（¥150–700/月） | 装 [`claude` CLI](https://docs.claude.com/claude-code) 然后 `claude login` 一次。OAuth 存在 `~/.claude/.credentials.json` |
| 只想用便宜 key | 从 [DeepSeek](https://platform.deepseek.com) / [智谱 GLM](https://bigmodel.cn) / [MiniMax 国内站](https://minimaxi.com) 任选一个去拿 key。之后在 Settings 里填，不需要 CLI |
| 两者都有 | Claude 解难题，DeepSeek 跑日常。dropdown 一键切 |

**一个都没配的话，muselab 能装但第一条 chat 会报错**。UI 会显示「未配模型 — 打开 Settings」提示，不至于让你懵。

#### 装 `uv`

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows PowerShell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 1. 一键安装

登录后自动起，默认只绑 localhost，~3 分钟（VPS 慢的话 10+ 分钟）。

```bash
git clone https://github.com/hesorchen/muselab && cd muselab

# macOS — 用户级 LaunchAgent
bash scripts/install-macos.sh

# Linux — 用户级 systemd
bash scripts/install-linux.sh

# Windows — Task Scheduler
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

脚本做的：pre-flight 检查 → `uv sync` → 写 `.env`（含随机 token）→ 7 问 intake 写 CLAUDE.md → 注册自启 → 等服务起来（30s retry）。

### 2. 打开

本机：`http://localhost:8765` → 粘 `.env` 里的 token。

**装在 VPS 上的话**：不要把端口暴露公网。SSH tunnel 一行从笔记本访问：

```bash
ssh -L 8765:127.0.0.1:8765 your-vps-user@your-vps-host
# 然后在笔记本浏览器开 http://localhost:8765
```

或者用 [Tailscale](https://tailscale.com)——同效果，不用 terminal。

### 3. 验证

```bash
bash scripts/doctor.sh        # Linux / macOS
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1   # Windows
```

`doctor` 检查每一层（uv / claude CLI / .env / service / HTTP / token / provider keys），有问题给具体建议。**装完跑一次**，之后觉得不对劲也跑一次。

#### 重启后还在吗？

| OS | 重启 → 重新登录 | 重启 → 一直不登录 |
|----|---------------------|------------------------|
| **macOS** | ✅ 自启 | n/a（Mac 重启总要登录）|
| **Linux** | ✅ 自启 | ⚠️ 一次性 `sudo loginctl enable-linger $USER` |
| **Windows** | ✅ 自启 | n/a（Task Scheduler 是 "At Logon"）|

各 OS 详细指南（验证 / 重启 / tail 日志 / 暴露 LAN / 卸载）：
[macOS](docs/install-macos.md) · [Linux](docs/install-linux.md) · [Windows](docs/install-windows.md)。

### 备选 — Docker

<details>
<summary><b>GHCR 预构建镜像（多架构 amd64 + arm64）</b></summary>

```bash
docker run -d --name muselab \
  -p 8765:8765 \
  -e MUSELAB_TOKEN=$(openssl rand -hex 32) \
  -v $HOME/muselab-archive:/root/muselab-archive \
  -e MUSELAB_ROOT=/root/muselab-archive \
  -v $HOME/.claude:/root/.claude \
  ghcr.io/hesorchen/muselab:latest
```

钉版本：`ghcr.io/hesorchen/muselab:1.2.3` / `:1.2` / `:sha-abc1234`。
</details>

<details>
<summary><b>Docker Compose</b></summary>

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
cp .env.example .env && $EDITOR .env
claude login                              # 宿主机做，容器复用 OAuth
docker compose up -d
```
</details>

<details>
<summary><b>原生开发 (uv，无 service)</b></summary>

```bash
cd muselab && uv sync
cp .env.example .env && $EDITOR .env
claude login
uv run python -m backend.main
```
</details>

---

## 用哪个模型

muselab 用 **Claude Agent SDK** 作为唯一 chat 后端。非 Claude 模型通过 per-session
env override 把 SDK 指向 vendor 的 Anthropic 兼容端点。**所有 provider 都拿到完整的
agent loop**——不只是 chat。无 proxy，无协议翻译。

| Provider | 怎么开 | 工具调用 | 备注 |
|---|---|---|---|
| **Anthropic Claude**（Opus / Sonnet / Haiku） | `claude login` 一次 | ✅ | 复用 Pro/Max OAuth — 不用 API key，不按 token 付费 |
| **DeepSeek**（V4 Pro / V4 Flash / Chat / Reasoner） | Settings 填 `DEEPSEEK_API_KEY` | ✅ | 对话场景比 Claude 便宜约 10× |
| **智谱 GLM**（GLM-5 / GLM-5 Air / GLM-4.7 / 4 Plus） | `ZHIPUAI_API_KEY` | ✅ | bigmodel.cn 有免费额度 |
| **MiniMax**（M2.7 / M2.7 Highspeed / M2.5） | `MINIMAX_API_KEY` | ✅ | M2.7 默认返回 thinking block |

**对话中切模型**：dropdown → confirm modal → 自动新建会话（避免跨厂商 thinking signature 错乱）。

**加新 provider** `backend/endpoints.py` 加 3 行——见 [docs/add-provider.md](docs/add-provider.md)。

---

## 跟同类怎么比

|  | muselab | claudecodeui | LobeChat | AnythingLLM | Claude Code CLI |
|---|---|---|---|---|---|
| 定位 | 档案 + AI 对话 | 多 CLI agent 的 IDE | 多模型对话 + 插件市场 | RAG over your docs | 终端编程 agent |
| 自托管 | ✅ | ✅ | ✅ | ✅ | ❌ |
| 浏览器访问 | ✅ | ✅ | ✅ | ✅ | ❌ |
| HTML / PDF / 图片预览 | ✅ first-class | ⚠️ | ⚠️ | ⚠️ | ❌ |
| **所有模型都有完整 agent SDK** | ✅ | ⚠️ 主要 Claude | ❌ 只 chat | ❌ RAG focus | ✅ 仅 Claude |
| 复用 Claude Pro 订阅 | ✅ | ✅ | ❌ | ❌ | ✅ |
| 代码行数 | ~4.4 k | 几万 | 几十万 | ~150 k | 闭源 |
| 安装命令数 | 3 | 多 | docker compose | docker | brew/npm |

要 **IDE 全能**选 claudecodeui / code-server；要 **插件市场**选 LobeChat；要 **爬 RAG**选 AnythingLLM。

muselab 走反方向：**最小可读、给所有模型 Claude 完整 agent 能力的档案 + AI 界面**。

### 跟其他 Claude harness 具体怎么比

| | muselab | Claude Code CLI | Claude Desktop | claudecodeui | claude-code-router |
|---|---|---|---|---|---|
| 用官方 **Claude Agent SDK** | ✅ 直接 | ✅ (官方实现本体) | ✅ | ❌ wrap CLI 进程 | ❌ 协议翻译器 |
| 浏览器 web UI | ✅ | ❌ TTY | ❌ 桌面 | ✅ | ❌ |
| 个人档案场景 | ✅ | ❌ 编程 | ❌ 通用 | ❌ 编程 | ❌ |
| **非 Claude 模型也有同套 agent loop** | ✅ 走 vendor anthropic-compat | ❌ 只 Anthropic | ❌ 只 Anthropic | partial | ⚠ 翻译会丢功能 |
| 自托管友好 | ✅ | n/a（你已经有了）| ❌ 闭源 binary | ✅ | ✅ |
| 开源 | ✅ MIT | ❌ | ❌ | ✅ MIT | ✅ MIT |

最短的精确一句话：**"muselab 之于你的 archive，就像 Claude Code 之于你的代码库。"**

---

## 一天里怎么用

真实工作 session 的样子：

```
早上  →  @health/2026-04-checkup.pdf 解读这份体检报告，对比去年同期，
         重点说骨密度趋势。 (Muse 引 Endocrine Society 指南，引具体数字，
                              给下一步建议)

中午  →  拖一份 PDF 到 investment/research/HSTU-paper.pdf
      →  @investment/HSTU-paper.pdf @investment/portfolio.md
         这个新策略适合纳入我现有持仓吗？ (Muse 交叉读两份，按你 CLAUDE.md
                                          投资护栏给答)

晚上  →  health/training-log.md 在 CodeMirror 里加一行今日训练，Ctrl+S
      →  分析最近 3 个月的训练频率和强度变化 (Muse 发现规律)

随时  →  输入 / 看斜杠命令：/help /compact /clear /resume
      →  输入 @ 自动从 archive 文件树补全
      →  dropdown 切模型 → 确认 → 新 session 用新模型
```

**CLAUDE.md 自动加载**：`~/.claude/CLAUDE.md`（全局规则）+
`<archive-root>/CLAUDE.md`（per-archive 规则），所有模型都生效。Installer 的
7 问 intake 把你真实档案写进 CLAUDE.md——见
[docs/personalize-claude-md.md](docs/personalize-claude-md.md)。

---

## 架构原理

```mermaid
flowchart TB
  subgraph Browser["浏览器 · ~3.2k 行 vanilla HTML + Alpine.js + CSS"]
    F[📁 文件] --- P[📄 预览 + 多 tab] --- C[💬 chat + 多模型]
  end
  Browser ==>|HTTP / SSE| BE
  subgraph BE["后端 · FastAPI ~1.2k 行"]
    A["/api/files/*<br/>safe-resolve · 读写 · grep"]
    B["/api/chat/*<br/>ClaudeSDKClient 池<br/>per (session, model)"]
  end
  BE ==> SDK[Claude Agent SDK<br/>跑 claude CLI 子进程]
  SDK -->|claude-* 模型<br/>走 Pro OAuth| AN[api.anthropic.com]
  SDK -->|per-request env override| V[Vendor anthropic 兼容端点]
  V --> DS[api.deepseek.com/anthropic]
  V --> GL[open.bigmodel.cn/api/anthropic]
  V --> MM[api.minimaxi.com/anthropic]
```

**关键设计决策**：

- **用 SDK 而非裸 API**。Claude Agent SDK 是 Claude Code 同款引擎，所以 MCP / Skills / Subagent / plan / CLAUDE.md auto-load 在所有 provider 上一致工作。加新 provider 是 3 行不是 300 行。
- **per-session `env=` override**。SDK 给子进程传 fresh env dict。DeepSeek/GLM/MiniMax 设 `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY` + 隔离的 `CLAUDE_CONFIG_DIR`（不隔离的话 CLI 会偷偷回落到 Pro OAuth → 把账单挂到 Anthropic）。
- **无 bundler 无 transpiler**。改文件，刷新浏览器，搞定。`vendor/` 里放了校验过的 runtime（Alpine / marked / DOMPurify / KaTeX / hljs），装的时候不下 npm。
- **Session = `(session_id, model)`** 缓存 client。切 model 起新 client；每条 assistant 消息存自己的 `model` 字段，reload 后 bubble badge 仍然准确。

---

## 安全模型

⚠️ **`MUSELAB_TOKEN` 泄露 ≈ `MUSELAB_ROOT` 下的 shell 读写权限。**
Chat 默认 `permission_mode="bypassPermissions"`——Claude 可读写 archive 下任何文件，不会逐次问。

**已内置**：

- `MUSELAB_ROOT` 黑名单：拒 `/`、`/etc`、`/root`、`/home`、`/var`、`/usr`、`/boot`、`$HOME`
- `MUSELAB_TOKEN` 最小 16 字符，启动时校验
- 路径穿越 & **symlink 逃逸**防护（`safe_resolve` 校验实际 resolved 路径）
- 敏感文件名硬阻：`.env*`、`id_rsa`、`*.pem`、`credentials*`——读和上传都拒
- 上传大小上限 100 MB（可配）+ 可执行扩展名黑名单
- XSS 防护：所有 markdown 走 DOMPurify
- HTML / SVG 预览在 `iframe sandbox="allow-scripts"` + 严格 CSP（sandbox 拿不到 token）
- 文件预览：黑名单 + 内容嗅探（不需要每加一种新格式就更新白名单）

**你来做**：

- 跑在普通用户下——installer 拒绝 sudo
- `MUSELAB_ROOT` 指专门目录，不要指你 home
- token 随机长 + 不入 git
- LAN 暴露：上 HTTPS + nginx basic auth
- VPS：用 SSH tunnel 或 Tailscale，**不要**裸开 8765 到公网

完整威胁模型 + 漏洞披露：见 [SECURITY.md](SECURITY.md)。

---

## 九位缪斯

muselab 名字来自希腊神话的**九位缪斯**——艺术与学问之神。**Muse** 是里面的
AI 人格；**muselab** 是她居住的工坊。

每个 session 启动时按 (date + hour) hash 选一位缪斯——一小时内稳定，每天轮换。
点 chat header 的 mascot 切下一位。favicon 跟着变——你浏览器 tab 里悄悄
带着今日的缪斯。

| 缪斯 | 领域 | 几何形 |
|---|---|---|
| Calliope（卡利俄佩） | 史诗 | 六边形 |
| Clio（克利俄） | 历史 | 卷轴 |
| Erato（厄拉托） | 情诗 | Vesica piscis（双圆交） |
| Euterpe（欧忒耳佩） | 音乐 | 声波 |
| Melpomene（墨尔波墨涅） | 悲剧 | 残月 |
| Polyhymnia（波吕许谟尼亚） | 圣诗 | 圣光环 |
| Terpsichore（忒耳普西科瑞） | 舞蹈 | 三美神 |
| Thalia（塔利亚） | 喜剧 | 火花 |
| Urania（乌拉尼亚） | 天文 | 行星轨道 |

---

## 状态与贡献

**Pre-1.0**，作者每天用的个人项目。欢迎 PR——见 [CONTRIBUTING.md](CONTRIBUTING.md)。
维护者保留拒绝"让代码 bloated 到无法一晚读完"的 feature 的权利。

- 🐛 **Bug**：用 [bug 模板](.github/ISSUE_TEMPLATE/bug_report.md) 提 issue
- 💡 **新功能**：用 [feature 模板](.github/ISSUE_TEMPLATE/feature_request.md)
- 🔌 **provider 不工作**：用 [provider 模板](.github/ISSUE_TEMPLATE/provider_issue.md)（贴脱敏后的 vendor response）
- 📋 **路线图 / 已知问题**：[TODO.md](TODO.md)
- 🔒 **安全问题**：不要开 public issue——见 [SECURITY.md](SECURITY.md)

## License

[MIT](LICENSE)——随便用，不保证。
