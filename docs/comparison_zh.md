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
| **全模型完整 agent SDK** | ✅ | ⚠️ 主要 Claude | ⚠️ 自有 agent + MCP | ❌ RAG 专用 | ✅ 仅 Claude |
| 复用 Claude Pro 订阅 | ✅ | ✅ | ❌ | ❌ | ✅ |
| 代码行数 | ~40 k（后端 + 前端） | 几万 | 几十万 | ~150 k | 闭源 |
| 安装命令数 | 1（curl \| bash） | 多 | docker compose | docker | brew / npm |

需要 **IDE 完整功能**，推荐 claudecodeui 或 code-server。
需要 **插件市场**，推荐 LobeChat。
需要 **基于爬取内容的 RAG**，推荐 AnythingLLM。

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
- 不是 IDE：归档目录可以放代码，但不要在这里做软件开发，用 [claudecodeui](https://github.com/siteboon/claudecodeui) 或 [Claude Code](https://github.com/anthropics/claude-code)
- 不是 RAG：归档按需 Read / Grep，不预先向量化；爬虫式文档问答用 [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm)
- 不带插件市场：内置 11 个精选技能并自动发现已安装的 Claude Code 插件，但没有应用内 marketplace，如需用 [LobeChat](https://github.com/lobehub/lobe-chat)
