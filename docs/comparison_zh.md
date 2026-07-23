# 与同类对比

> [English](comparison.md)

以下两张表帮助快速判断 muselab 是否适合当前需求，或哪个替代工具更为合适。

## vs. 通用 chat UI

|  | muselab | claudecodeui | LobeChat | AnythingLLM | Claude Code CLI |
|---|---|---|---|---|---|
| 定位 | 个人档案 + AI 对话 | 多 CLI agent 的 IDE | 多模型对话 + 插件市场 | RAG over docs | 终端编程 agent |
| 自托管 | ✅ | ✅ | ✅ | ✅ | ❌ |
| 浏览器访问 | ✅ | ✅ | ✅ | ✅ | ❌ |
| HTML / PDF / 图片预览 | ✅ | ⚠️ | ⚠️ | ⚠️ | ❌ |
| 浏览器内真实 PTY 终端 | ✅ 多终端 + Profile | ✅ 多标签 | ❌ | ❌ | n/a（本身运行于终端） |
| **全模型完整 agent SDK** | ✅ | ⚠️ 主要 Claude | ⚠️ 自有 agent + MCP | ❌ RAG 专用 | ✅ 仅 Claude |
| 复用 Claude Pro 订阅 | ✅ | ✅ | ❌ | ❌ | ✅ |
| 安装命令数 | 1（curl \| bash） | 多 | docker compose | docker | brew / npm |

需要 **IDE 完整功能**，推荐 claudecodeui 或 code-server。
需要 **插件市场**，推荐 LobeChat。
需要 **基于爬取内容的 RAG**，推荐 AnythingLLM。

muselab 的终端与文件、预览和会话使用同一个工作目录。可以同时保留多个真实 PTY
会话，并通过 Profile 在新建终端时自动执行固定命令；切换页面不会终止其中的进程。

同类搜索中经常出现的其他名字：

- [Open WebUI](https://github.com/open-webui/open-webui) —— 本地模型（Ollama）及 OpenAI 兼容端点的首选自托管 chat UI，自带 RAG 与工具体系。以本地模型对话为核心时选它；需要在自己文件上跑 Claude Code agent loop（Read / Grep / Edit / Bash、Skills、MCP）时选 muselab。
- [LibreChat](https://github.com/danny-avila/LibreChat) —— 多提供商对话，带多用户鉴权和 agent 框架。需要面向团队的共享对话门户时选它；muselab 刻意设计为单用户（见[边界](#边界)）。
- **Obsidian / Logseq AI 插件** —— 笔记应用内嵌 AI。它们只看得到笔记库里的文件；muselab 的 agent 可作用于整个归档目录（任意文件类型），并能对其执行多步骤任务，而不仅仅是生成文字。

## vs. 其他 Claude harness

|  | muselab | Claude Code CLI | Claude Desktop | claudecodeui | claude-code-router |
|---|---|---|---|---|---|
| 使用官方 **Claude Agent SDK** | ✅ 直接 | ✅（官方实现本体） | ✅ | ❌ 封装 CLI 进程 | ❌ 协议翻译器 |
| 浏览器 web UI | ✅ | ❌ TTY | ❌ 桌面 | ✅ | ❌ |
| 个人档案场景 | ✅ | ❌ 编程 | ❌ 通用 | ❌ 编程 | ❌ |
| **非 Claude 模型同 agent loop** | ✅ 经 vendor anthropic-compat | ❌ 仅 Anthropic | ❌ 仅 Anthropic | partial | ⚠ 翻译过程会丢失功能 |
| 自托管友好度 | ✅ | n/a（用户本机已有） | ❌ 闭源 binary | ✅ | ✅ |
| 开源 | ✅ MIT | ❌ | ❌ | ✅ AGPL-3.0 | ✅ MIT |

最简概括：muselab 之于个人归档，犹如 Claude Code 之于代码库。

## 边界

- 单用户、单 token —— 两人共用即共享全部数据，团队/家庭场景请每人一份实例
- 不是完整 IDE：内置终端适合在工作目录执行命令和辅助 Agent 工作，但不提供完整的
  代码导航、调试器与 IDE 插件生态；重度软件开发仍建议使用 claudecodeui 或
  Claude Code
- 不是 RAG：归档按需 Read / Grep，不预先向量化；爬虫式文档问答用 [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm)
- 不带插件市场：内置 12 个精选技能并自动发现已安装的 Claude Code 插件，但没有应用内 marketplace，如需用 [LobeChat](https://github.com/lobehub/lobe-chat)
