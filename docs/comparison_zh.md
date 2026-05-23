# 与同类对比

> [English](comparison.md)

以下两张表帮助快速判断 muselab 是否适合当前需求，或哪个替代工具更为合适。

## vs. 通用 chat UI

|  | muselab | claudecodeui | LobeChat | AnythingLLM | Claude Code CLI |
|---|---|---|---|---|---|
| 定位 | 个人档案 + AI 对话 | 多 CLI agent 的 IDE | 多模型对话 + 插件市场 | RAG over docs | 终端编程 agent |
| 自托管 | ✅ | ✅ | ✅ | ✅ | ❌ |
| 浏览器访问 | ✅ | ✅ | ✅ | ✅ | ❌ |
| HTML / PDF / 图片预览 | ✅ first-class | ⚠️ | ⚠️ | ⚠️ | ❌ |
| **所有模型均具备完整 agent SDK** | ✅ | ⚠️ 主要 Claude | ❌ 仅 chat | ❌ RAG focus | ✅ 仅 Claude |
| 复用 Claude Pro 订阅 | ✅ | ✅ | ❌ | ❌ | ✅ |
| 代码行数 | ~22 k | 几万 | 几十万 | ~150 k | 闭源 |
| 安装命令数 | 3 | 多 | docker compose | docker | brew / npm |

需要 **IDE 完整功能**，推荐 claudecodeui 或 code-server。
需要 **插件市场**，推荐 LobeChat。
需要 **基于爬取内容的 RAG**，推荐 AnythingLLM。

muselab 的定位与之相反：**精简、可完整审计、为所有模型提供 Claude 完整 agent 能力的归档管理与 AI 交互界面**。

## vs. 其他 Claude harness

|  | muselab | Claude Code CLI | Claude Desktop | claudecodeui | claude-code-router |
|---|---|---|---|---|---|
| 使用官方 **Claude Agent SDK** | ✅ 直接 | ✅（官方实现本体） | ✅ | ❌ 封装 CLI 进程 | ❌ 协议翻译器 |
| 浏览器 web UI | ✅ | ❌ TTY | ❌ 桌面 | ✅ | ❌ |
| 个人档案场景 | ✅ | ❌ 编程 | ❌ 通用 | ❌ 编程 | ❌ |
| **非 Claude 模型同 agent loop** | ✅ 经 vendor anthropic-compat | ❌ 仅 Anthropic | ❌ 仅 Anthropic | partial | ⚠ 翻译过程会丢失功能 |
| 自托管友好度 | ✅ | n/a（用户本机已有） | ❌ 闭源 binary | ✅ | ✅ |
| 开源 | ✅ MIT | ❌ | ❌ | ✅ MIT | ✅ MIT |

最简概括：**muselab 之于个人归档，犹如 Claude Code 之于代码库。**

## muselab 不适用的场景

以下场景有更合适的替代工具：

- **多租户 SaaS**——muselab 在设计上为单用户服务：一个 token、一个归档目录、无用户隔离。两人共用同一实例即共享全部数据。团队或家庭场景下，请为每人单独部署一个实例。
- **代码 IDE**——muselab 可读写归档目录中的代码，但不是代码开发环境。用于软件开发时，推荐使用 [claudecodeui](https://github.com/siteboon/claudecodeui)、[code-server](https://github.com/coder/code-server) 或直接使用 [Claude Code](https://github.com/anthropics/claude-code)。
- **RAG 文档问答机器人**——muselab 按需读取文件（文件树 + grep + read 工具），不会将归档目录预先向量化。若需对大量网页或 PDF 爬取内容进行问答，推荐使用 [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm)。
- **插件市场**——muselab 内置 11 个精选技能（代码审查、图表生成、摘要、网络搜索、PPT 生成、数据分析、文档翻译、会议纪要等），并自动发现已安装的 Claude Code 插件，但不提供应用内插件市场。如需此功能，推荐使用 [LobeChat](https://github.com/lobehub/lobe-chat)。
