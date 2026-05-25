# muselab vs 同类自托管 AI workspace 对比报告

> 调研日期：2026-05-25
> 调研者：Muse（自动调研）
> 数据口径：各项目 GitHub README + 2026-04/05 第三方评测；星标 / 版本号取调研当日 GitHub 显示值
> 适用读者：评估 muselab 在生态中的差异化定位，或在多个开源方案中选型
>
> 既有的 [docs/comparison.md](docs/comparison.md) 是一页定位表；本报告是它的**深度版本**——补全数据点 / 评测出处 / 选型决策树。

---

## 0. TL;DR

在 2026-05 的「自托管 AI workspace」开源生态里，muselab **不是任何一类的最强者**，但占住了一个没人填的空位：

> **单用户 × 复用 Claude Pro/Max OAuth × 每个模型都跑同一套 Agent SDK × 文件系统就是 context × 零构建前端**

| 你的需求 | 推荐 | 理由 |
|---|---|---|
| 团队多人 + 本地模型 + ChatGPT 体验 | **Open WebUI** | 139k 星，offline-first，RBAC/LDAP/SCIM，Ollama 一等公民 |
| 团队 + 文档问答 + 拖文件就用 | **AnythingLLM** | 60.6k 星，专为 RAG 设计，向量库 6 选 1 |
| 多 provider + 企业鉴权 + code interpreter | **LibreChat** | 37.4k 星，9 种沙箱语言执行，Harvard 合作背书 |
| 想要 Agent 工作流 + 插件市场 | **Lobe Chat** | 77.6k 星，"Chief Agent Operator" 定位，24×7 调度 |
| Obsidian + 本地 LLM 二合一 | **Khoj** 或 **Reor** | Khoj 走 plugin 路线，Reor 走桌面 app |
| 单人 + 想物尽其用 Claude Pro 订阅 + 文件就是上下文 | **muselab** | 唯一同时占住这四条的项目 |

---

## 1. 入选项目与定位一句话

| 项目 | Stars | 最新版本 | License | 一句话定位 |
|---|---|---|---|---|
| **muselab** | (作者项目, pre-1.0) | dev | MIT | 单用户 self-hosted Muse + 你的 archive 就是工作集 |
| Open WebUI | 139k | v0.9.5 (2026-05-10) | Open WebUI License¹ | "完全离线运行" 的 ChatGPT 替代品，Ollama 一等公民 |
| Lobe Chat | 77.6k | v2.2.0 (2026-05-18) | LobeHub Community License | "Your Chief Agent Operator"——agent 编排平台 |
| AnythingLLM | 60.6k | v1.12.1 (2026-04-22) | MIT | "all-in-one AI productivity"——RAG-first 文档工作区 |
| LibreChat | 37.4k | (持续主干) | MIT | 多 provider 统一 chat UI + 企业级鉴权 |
| Khoj | 34.7k | v2.0.0-beta.28 (2026-03-26) | AGPL-3.0 | "Your AI second brain"——索引本地文档的对话二脑 |
| Reor | 8.6k | v0.2.32 (2025-04-05) | AGPL-3.0 | 桌面 PKM app，笔记自动语义连线（Obsidian-like） |

> ¹ Open WebUI 自 2025 起改用自有 license，**要求保留品牌标识**，不再是纯 MIT。商用前需细读。

---

## 2. 八维度对比矩阵

> ✅ = 完整支持；⚠️ = 部分支持 / 需配置；❌ = 不支持；—— = 不适用

### 2.1 定位 & 目标用户

|  | muselab | Open WebUI | Lobe Chat | AnythingLLM | LibreChat | Khoj | Reor |
|---|---|---|---|---|---|---|---|
| **目标用户** | 单人 | 团队 | 团队 | 团队 / 小组 | 团队 / 企业 | 单人 + 企业 | 单人 |
| **核心隐喻** | archive=context | ChatGPT 离线版 | Agent 编排 | 文档工作区 | 统一 chat UI | 第二大脑 | 桌面 PKM |
| **典型场景** | 个人 AI 助理 + 文件助记 | 内网团队多模型 chat | 调度多 agent | 上传文档问答 | 多 provider 网关 | 索引笔记问答 | 边写边自动连线笔记 |

### 2.2 LLM Provider 支持

|  | muselab | Open WebUI | Lobe Chat | AnythingLLM | LibreChat | Khoj | Reor |
|---|---|---|---|---|---|---|---|
| **OpenAI** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Anthropic** | ✅ | ⚠️ via OpenAI-compat | ✅ | ✅ | ✅ | ✅ | ❌ |
| **Claude Pro/Max OAuth 订阅复用** | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Ollama / 本地模型** | ⚠️ via OpenAI-compat | ✅ (一等公民) | ✅ | ✅ | ⚠️ via compat | ✅ | ✅ (一等公民) |
| **国内厂商**（DeepSeek/GLM/通义/Kimi/MiMo 等） | ✅ 原生 anthropic-compat 接 | ⚠️ via compat | ⚠️ 部分 | ⚠️ 部分 | ✅ DeepSeek | ✅ DeepSeek | ❌ |
| **provider 总数** | 7 默认 + 任意 anthropic-compat | 5+ 直连，任意 OpenAI-compat | 主流 4+ | 30+ | 10+ | 5+ | 2 |

> **muselab 独有**：直接复用本机 `claude` CLI 的 Pro/Max OAuth 凭证——免去 Anthropic API 按 token 计费。其他所有项目都要求你掏 API key。
>
> **muselab 的国内 provider 路线特殊**：通过 `env_override` 把 SDK 指向 vendor 的 anthropic-compat 端点（DeepSeek/GLM/MiniMax/Kimi/Qwen/MiMo），让 Claude Agent SDK 的完整 agent loop（tool use / MCP / subagent / plan mode）在国产模型上原样跑通。竞品大多只把国内 provider 当 OpenAI-compat 端点接，工具调用语义会降级。

### 2.3 多模态能力

|  | muselab | Open WebUI | Lobe Chat | AnythingLLM | LibreChat | Khoj | Reor |
|---|---|---|---|---|---|---|---|
| **图片输入（vision）** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ 新增中 |
| **语音输入（STT）** | ❌ | ✅ Whisper/Deepgram/Azure | ✅ | ✅ Whisper | ✅ OpenAI/Azure/ElevenLabs | ✅ | ❌ |
| **语音输出（TTS）** | ❌ | ✅ | ✅ | ✅ Piper/ElevenLabs/OpenAI | ✅ | ✅ | ❌ |
| **图像生成** | ❌ | ✅ DALL-E/ComfyUI/A1111 | ⚠️ via plugin | ❌ | ✅ DALL-E/SD/Flux | ✅ | ❌ |
| **视频** | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ 新增（v0.2.32） |
| **代码执行 / Code Interpreter** | ✅ via Claude Agent SDK 工具 | ⚠️ via pipelines | ⚠️ | ❌ | ✅ 9 语言沙箱 | ❌ | ❌ |

> **muselab 的「多模态」走捷径**：不自建 STT/TTS/图像生成栈，全部依赖各 provider 自己的能力（Claude 4.x 的 vision、SD3 via MCP 工具等）。优势是不背包袱、好维护；劣势是「现成的离线语音/出图」需要用户自己接 MCP 服务。
>
> **muselab 的代码执行靠 Claude Agent SDK 自带的 Bash / Edit 工具**，不沙箱、直接跑在 archive 文件系统上——这是单人本机场景的合理选择，但**不适合多人共用**。

### 2.4 RAG / 文件 archive 处理

|  | muselab | Open WebUI | Lobe Chat | AnythingLLM | LibreChat | Khoj | Reor |
|---|---|---|---|---|---|---|---|
| **核心模型** | 文件系统 = context | 向量库 RAG | 知识库 RAG | RAG-first | RAG API（独立 repo） | 索引本地文档 | 笔记向量库 |
| **向量数据库** | 不需要 | 9 种（Chroma/PGVector/Qdrant/Milvus/...） | 多种 | 6 种（LanceDB 默认） | 独立 RAG service | 内置 | 内置 |
| **文件格式** | 任何文本 / PDF / 图片预览 | PDF/DOCX 等 | 主流 | PDF/TXT/DOCX/HTML/MD/CSV/code | 多 | PDF/MD/Notion/Word/org | Markdown only |
| **检索方式** | grep + 文件树 + @mention + LLM 自取 | 语义 | 语义 | 语义 + 引用源 | 语义 | 语义 | 语义 |
| **写回 archive** | ✅ 原地 Edit/Write | ❌ 只读 | ❌ | ❌ | ❌ | ⚠️ Obsidian 插件 | ✅ 原生 |
| **更新延迟** | 0（实时文件系统） | 重新 embed | 重新 embed | 重新 embed | 重新 embed | 重新 embed | 重新 embed |

> **muselab 的差异化最大点在这里**：拒绝向量库 RAG，把整个 archive 当 LLM 的工作区。Muse 像 Claude Code 一样用 grep / glob / read 主动找文件，再决定要读哪部分。
>
> **代价**：超大档案（GB 级 PDF/影音库）下 Muse 找不到东西——muselab 不解决「在 100GB 文档里精准检索」问题，那是 AnythingLLM/Khoj 的主场。
>
> **收益**：
> - 不维护 embedding pipeline、不踩 embedding 模型版本 / 切块策略 / 向量库迁移的坑
> - 改文件立即生效，没有「重建索引」过程
> - 助理能 `Edit`/`Write` 直接更新 archive，**双向操作**而非只读

### 2.5 部署难度

|  | muselab | Open WebUI | Lobe Chat | AnythingLLM | LibreChat | Khoj | Reor |
|---|---|---|---|---|---|---|---|
| **一行安装** | ✅ curl \| bash | ✅ docker run | ⚠️ Vercel 一键 | ✅ docker | ⚠️ docker-compose | ⚠️ pip / docker | ✅ 桌面 app |
| **Docker** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| **桌面 app** | ❌ | ❌ | ❌ | ✅ Mac/Win/Linux | ❌ | ✅ | ✅ 仅此路线 |
| **托管云版** | ❌ | ❌ | ✅ lobehub.com | ❌ | ❌ | ✅ khoj.dev | ❌ |
| **构建步骤** | **零**（Vanilla HTML/Alpine.js/CSS） | Node 构建 | Next.js 构建 | Node 构建 | Node + Python 双栈 | Python + Web | Electron |
| **代码量** | ~31k 行 | 数十万行 | 数十万行 | ~150k 行 | 数十万行 | 数十万行 | 数万行 |
| **依赖外部服务** | 仅本机 `claude` CLI（可选） | 可选（vector DB / Ollama） | 可选（vector DB） | 可选（vector DB / embedding） | RAG API 独立部署 | Ollama 推荐 | Ollama 推荐 |

> **muselab 的「无构建前端」是反潮流**：所有竞品都用 React/Next.js 大栈，muselab 用 Alpine.js + 原生 HTML——`edit, refresh, done`。**单人场景下这是优势**（依赖少、启动快、改起来轻），团队场景下可能反成限制（缺现成组件库）。

### 2.6 隐私模型

|  | muselab | Open WebUI | Lobe Chat | AnythingLLM | LibreChat | Khoj | Reor |
|---|---|---|---|---|---|---|---|
| **数据离开本机** | ❌（仅 LLM API call） | ❌ | ⚠️ 用云版会 | ❌ | ❌ | ⚠️ 用云版会 | ❌ |
| **遥测 / Analytics** | ❌ | 可关闭 | 可关闭 | 可关闭（PostHog 匿名） | 无 | 自托管无 | 无 |
| **离线运行** | ⚠️ 取决于 provider（用本地模型可全离线） | ✅ 完全离线模式 | ⚠️ | ⚠️ | ⚠️ | ✅ 本地模型 | ✅ 本地模型 |
| **多用户隔离** | ❌（单用户设计） | ✅ RBAC | ✅ workspace | ✅ workspace | ✅ OAuth/LDAP | ⚠️ | ❌ |
| **企业鉴权** | ❌ | ✅ LDAP/SCIM 2.0/SSO | ⚠️ | ✅ | ✅ OAuth2/LDAP | ⚠️ | ❌ |

> **muselab 明确不做企业级**：单 token 一个 archive、单进程单用户。这是设计选择不是 bug——把企业鉴权 / 多租户 / RBAC 全部砍掉换取代码简单（~31k 行）。要团队场景请用 Open WebUI / LibreChat。

### 2.7 社区与活跃度（2026-05）

|  | muselab | Open WebUI | Lobe Chat | AnythingLLM | LibreChat | Khoj | Reor |
|---|---|---|---|---|---|---|---|
| **GitHub Stars** | (作者项目) | 139k | 77.6k | 60.6k | 37.4k | 34.7k | 8.6k |
| **下载量级** | 自托管为主，不可统计 | 282M+（Open WebUI 自报） | 大 | 大 | 大 | 5.5M（参考 Jan.ai 量级） | 桌面 app 分发 |
| **最近版本** | dev | v0.9.5（半月前） | v2.2.0（一周前） | v1.12.1（一个月前） | 主干持续 | v2.0.0-beta.28（两月前） | v0.2.32（一年前） |
| **更新节奏** | 单作者 | 高频 | 高频 | 高频 | 高频 | 月度 | 慢（小团队） |
| **企业合作 / 背书** | 无 | 大量企业部署 | 有商业云 | 有商业 SaaS | Harvard 数字无障碍合作 | 有云版 | 无 |

> Reor 一年没发版，活跃度堪忧，可能逐步进入 maintain-only。其他五个项目都在高频更新。

### 2.8 独门功能

|  | 独有的甜点 |
|---|---|
| **muselab** | Claude Pro/Max OAuth 复用、9 个缪斯 mascot 切人格、archive 直接 Edit/Write、Claude Agent SDK 完整 agent loop 跑在国产模型 |
| **Open WebUI** | Pipelines 插件框架、Whisper/Deepgram 全语音、9 种向量库、SCIM 2.0/RBAC |
| **Lobe Chat** | "Chief Agent Operator" 编排 24×7 调度 agent、10k+ skill marketplace、Agent Groups 多 agent 协作 |
| **AnythingLLM** | No-code agent builder、Scheduled tasks、Embeddable chat widget、Dynamic model routing |
| **LibreChat** | 9 语言沙箱代码执行、Code Artifacts（React/HTML/Mermaid in chat）、Multi-tab/Multi-device resumable streams、Harvard 数字无障碍合作 |
| **Khoj** | Obsidian / Emacs / WhatsApp 客户端、研究 agent、自动 newsletter |
| **Reor** | 编辑时自动语义连线相关笔记、Obsidian-like markdown UI |

---

## 3. 各项目相对 muselab 的优劣（对比镜）

### 3.1 Open WebUI

**比 muselab 强**：Ollama 一等公民、企业鉴权（LDAP/SCIM）、多用户、139k 星巨大社区、Pipelines 插件生态、完整语音栈、9 种向量库。

**比 muselab 弱**：
- 不能复用 Claude Pro OAuth（必须给 API key）
- License 不再是 MIT（要求保留品牌标识，二开发布约束）
- 没有「archive 即工作集」模型——文件要进向量库才用得上
- React/Next.js 大栈，单人改不动

**何时选它**：内网团队部署 / 本地 Ollama 重度用户 / 要 ChatGPT 替代品体验。

### 3.2 Lobe Chat

**比 muselab 强**：77.6k 星、Agent 编排和调度（"24×7 operations"）、插件市场、多 agent 协作。

**比 muselab 弱**：
- 不能复用 Claude Pro OAuth
- 国内 provider 支持比 muselab 少（不通过 anthropic-compat 接，工具调用语义降级）
- License 是 LobeHub Community License（非纯开源）
- 商业云版（lobehub.com）是主推路线，self-host 是次要

**何时选它**：你想搭一个能 24/7 自动跑任务的 agent 平台、需要插件 marketplace。

### 3.3 AnythingLLM

**比 muselab 强**：60.6k 星、RAG-first（这是 muselab 故意不做的事）、30+ provider、6 种向量库、有桌面 app、多用户 workspace。

**比 muselab 弱**：
- 不能复用 Claude Pro OAuth
- 没有「archive 双向编辑」——纯只读 RAG
- 国产 provider 支持比 muselab 弱
- 不跑 Claude Agent SDK 完整 agent loop（自己实现 agent，没有 MCP / Skill 生态）

**何时选它**：你的核心场景是「上传一堆 PDF / Word 问答」、需要向量检索引用源、团队用。

### 3.4 LibreChat

**比 muselab 强**：37.4k 星、多 provider chat 集大成者、9 语言沙箱 code interpreter（muselab 没沙箱）、Code Artifacts inline 渲染、企业级 OAuth/LDAP/邮箱鉴权、Harvard 背书。

**比 muselab 弱**：
- 不能复用 Claude Pro OAuth
- "Enhanced ChatGPT Clone" 定位是 chat UI，不管你的文件
- RAG 是独立 service（拆得太开）
- Node + Python 双栈部署门槛高

**何时选它**：要给团队装一个企业级 ChatGPT、需要安全的代码执行沙箱。

### 3.5 Khoj

**比 muselab 强**：34.7k 星、Obsidian/Emacs/WhatsApp 多端、自动 newsletter、研究 agent、有云版。

**比 muselab 弱**：
- AGPL-3.0 license（自托管 OK，二开商用要谨慎）
- 还是基于向量索引的 RAG（不是双向 archive）
- 不能复用 Claude Pro OAuth
- 国产 provider 支持有限

**何时选它**：你重度用 Obsidian / Emacs / WhatsApp，想要一个穿透多端的"第二大脑"。

### 3.6 Reor

**比 muselab 强**：桌面 app 体验（开箱即用，无服务运维）、笔记自动语义连线、Obsidian-like 编辑器 UX。

**比 muselab 弱**：
- 一年没发版，活跃度低
- 桌面 app only，不能多设备共享
- 仅 Markdown，不处理 PDF/图片预览
- 仅支持 Ollama + OpenAI-compat 两条 provider 路线
- 仅 8.6k 星，社区小

**何时选它**：你要单机本地、Obsidian 替代、能接受小众工具的迭代节奏。

---

## 4. muselab 的位置（差异化矩阵）

把所有项目放进二维平面，看 muselab 占的空格：

```
                    多用户 / 团队
                          ▲
                          │
        AnythingLLM ────  │ ────  Open WebUI
        LibreChat ────────│──────── Lobe Chat
                          │
   ──────── Khoj ─────────┼─────── (云版偏团队)
                          │
                          │   muselab ★
                          │   Reor
                          │
                          ▼
                       单用户 / 个人

  向量 RAG / 重栈 ◄────────────────► 文件系统 / 轻栈
```

muselab 占的格子是 **「单用户 × 文件系统 / 轻栈」**——竞争对手只有 Reor，而 Reor 活跃度下滑、是桌面 app、不能多设备共享。

再叠加第三个维度「能否复用 Claude Pro/Max 订阅」：

| 项目 | 单用户友好 | 文件系统 / 轻栈 | 复用 Claude Pro |
|---|---|---|---|
| Open WebUI | ❌ | ❌ | ❌ |
| Lobe Chat | ❌ | ❌ | ❌ |
| AnythingLLM | ❌ | ❌ | ❌ |
| LibreChat | ❌ | ❌ | ❌ |
| Khoj | ⚠️ | ❌ | ❌ |
| Reor | ✅ | ✅ | ❌ |
| **muselab** | ✅ | ✅ | ✅ |

**muselab 占住的是一个不大、但没人填的空位**。

---

## 5. 共同的 2026 行业趋势

从对各项目当前主推功能 / release notes 的横扫看，2026 自托管 AI workspace 都在抢这几个高地：

1. **MCP 兼容**：所有头部项目（Open WebUI、AnythingLLM、LibreChat、Lobe Chat、muselab）都已声明支持 MCP server。这是 Anthropic 推出的协议变成了事实标准。
2. **Agent 而非 chat**：AnythingLLM 的 no-code agent builder、LibreChat 的 agents+code interpreter、Lobe Chat 的 "Chief Agent Operator"、muselab 的 Claude Agent SDK 完整 loop——大家都在脱离纯 chat 形态。
3. **Code Artifacts inline 渲染**：LibreChat 已落地，AnythingLLM 在路上。muselab **缺这块**——可作为差距清单 1 项。
4. **Web search 集成**：Open WebUI 接 15+ 搜索 provider、LibreChat 内置 reranking 搜索。muselab 走 MCP server 路线（让 brave-search / linkup / kagi 等 MCP 接入），不内置。
5. **多端 / Resumable streams**：LibreChat 重点宣传 multi-tab/multi-device resumable，muselab 用 SSE 重连解决（[CHANGELOG mention](docs/architecture.md)）。
6. **离线 / on-device 模型**：Open WebUI、Reor、Khoj 都把本地模型作为一等公民。muselab 走 OpenAI-compat 转接，本地模型可接但不是主推方向。

---

## 6. muselab 应该警惕的弱项

按优先级排：

| 短板 | 严重度 | 应对建议 |
|---|---|---|
| **没有 Code Artifacts inline 渲染** | 高 | 看 LibreChat 实现，加 sandboxed iframe 渲染 React/HTML/Mermaid，零依赖即可做 |
| **GB+ 级 archive 缺检索能力** | 中 | 目前定位避开此场景；要支持需引入可选的 embedding（违反"无构建"哲学，慎重） |
| **没有语音输入** | 中 | 移动端 PWA 用户痛点；Whisper.cpp 接 MCP server 即可，不入主进程 |
| **国内 provider 增加是手工活** | 中 | 每加一家要写 env_override 适配；可考虑读 vendor 配置文件统一化 |
| **没有 OAuth multi-device share session 加密层** | 低 | 单 token 单用户场景下不严重，但官网用户教育要明确"别共享 token" |
| **缺中文社区入口** | 低 | README_zh 已有，可补一个 V2EX / Reddit Self-hosted 帖子 |

---

## 7. 选型决策树

```
你要给团队 (>1 人) 装吗？
├─ 是 → 团队大小？
│       ├─ 5 人内、想要文档问答 → AnythingLLM
│       ├─ 团队、要 ChatGPT 体验 + 本地模型 → Open WebUI
│       └─ 企业、要 OAuth/LDAP + code interpreter → LibreChat
│
└─ 否（单人）→ 主用场景？
        ├─ 重度 Obsidian 用户、要本地笔记问答 → Khoj 或 Reor
        ├─ 要多 agent 调度 + 插件市场 → Lobe Chat（注意 license）
        ├─ 文档堆 PDF / 客户资料、找答案为主 → AnythingLLM 桌面版
        ├─ 已订 Claude Pro/Max 想物尽其用 + 文件就是 context + 移动端能用 → muselab
        └─ 就想要 ChatGPT 替代品个人单机版 → Open WebUI 单容器
```

---

## 8. 局限说明

- **muselab 自身数据缺**：Stars / Downloads / 用户数无对外数据；本报告对 muselab 的描述基于代码和 README 自我陈述，未做第三方评测核对
- **快速变化领域**：所有 6 个竞品的 release 节奏都是「半月级」，本报告快照=2026-05-25；建议**每季度回看**一次
- **未覆盖的项目**：本报告聚焦用户点名的 6 个，未涵盖 Jan.ai（桌面端、5.5M 下载，定位接近 Reor 但更活跃）、Cherry Studio（Electron 多 provider，中国社区热）、Chatbox（轻量 Electron）、SillyTavern（角色扮演向）。如果你的实际选型对手是这些，需要补充调研
- **License 细节未法律核对**：Open WebUI License / LobeHub Community License 的商用 / 二次开发约束本报告只做表面引用，认真发行前请律师过

---

## 9. 出处

### muselab 自身
- [muselab/README.md](README.md) - 项目主介绍
- [muselab/docs/comparison.md](docs/comparison.md) - 既有对比文档
- [muselab/docs/architecture.md](docs/architecture.md) - 架构文档

### 竞品 GitHub READMEs（数据快照 2026-05-25）
- [github.com/Mintplex-Labs/anything-llm](https://github.com/Mintplex-Labs/anything-llm)（60.6k stars, v1.12.1 2026-04-22, MIT）
- [github.com/danny-avila/LibreChat](https://github.com/danny-avila/LibreChat)（37.4k stars, MIT）
- [github.com/open-webui/open-webui](https://github.com/open-webui/open-webui)（139k stars, v0.9.5 2026-05-10, Open WebUI License）
- [github.com/lobehub/lobe-chat](https://github.com/lobehub/lobe-chat)（77.6k stars, v2.2.0 2026-05-18, LobeHub Community License）
- [github.com/khoj-ai/khoj](https://github.com/khoj-ai/khoj)（34.7k stars, v2.0.0-beta.28 2026-03-26, AGPL-3.0）
- [github.com/reorproject/reor](https://github.com/reorproject/reor)（8.6k stars, v0.2.32 2025-04-05, AGPL-3.0）

### 第三方对比评测
- [Best OpenWebUI Alternatives for Teams (2026) - Onyx](https://onyx.app/insights/openwebui-alternatives)
- [Open WebUI vs AnythingLLM vs LibreChat: Best Self-Hosted AI Chat in 2026 - ToolHalla](https://toolhalla.ai/blog/open-webui-vs-anythingllm-vs-librechat-2026)
- [Open WebUI vs AnythingLLM vs LibreChat: Which Ollama Interface Should You Use? - Serverman](https://www.serverman.co.uk/ai/ollama/open-webui-vs-anythingllm-vs-librechat/)
- [9 Open WebUI Alternatives for 2026 - Budibase](https://budibase.com/blog/alternatives/open-webui/)
- [OpenWebUI vs LibreChat: Self-Hosted LLM UI Battle (2026 Guide) - TokenMix](https://tokenmix.ai/blog/openwebui-vs-librechat-self-hosted-comparison-2026)
- [Best Open-Source Alternatives to ChatGPT in 2026 - Pinggy](https://pinggy.io/blog/best_open_source_alternatives_to_chatgpt/)
- [10 Best AnythingLLM Alternatives in 2026 - Vellum](https://www.vellum.ai/blog/best-anythingllm-alternatives)
- [LLM chat UIs that support MCP - ClickHouse](https://clickhouse.com/blog/llm-chat-mcp-support)
- [Best Open-Source ChatGPT Alternatives in 2026 - DEV Community](https://dev.to/lightningdev123/best-open-source-chatgpt-alternatives-in-2026-53el)

---

## 10. 方法论

- **检索流程**：GitHub README 直拉（WebFetch + 结构化提取 prompt）+ 第三方 2026 评测交叉验证
- **数据点**：Stars / 最新版本 / License / Provider 列表 / 多模态 / RAG 模型 / 部署 / 隐私 / 多用户 / 独门特性
- **未做的事**：未克隆各 repo 跑测试；未做性能 / 延迟 benchmark；未亲自部署各方案对比 UX；License 未法律审核
- **未来增量**：每季度刷一次，关注新晋项目（Jan.ai / Cherry Studio / 国内 self-host 方案）；关注 Open WebUI license 变化对生态影响
