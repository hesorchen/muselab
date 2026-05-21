# 与同类对比

> [English](comparison.md)

两张表帮你快速判断 muselab 适不适合，或者哪个替代品更对你的胃口。作者更
"私货"一点的内部定位 memo 在 [competitive-analysis.md](competitive-analysis.md)（英文，未实时更新）。

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

需要 **IDE 完整功能**：选 claudecodeui / code-server。
需要 **插件市场**：选 LobeChat。
需要 **爬虫 RAG**：选 AnythingLLM。

muselab 的定位反着来：**最小化、可完整审计、为所有模型提供 Claude 完整 agent
能力的档案 + AI 界面**。

## vs. 其他 Claude harness

|  | muselab | Claude Code CLI | Claude Desktop | claudecodeui | claude-code-router |
|---|---|---|---|---|---|
| 使用官方 **Claude Agent SDK** | ✅ 直接 | ✅（官方实现本体） | ✅ | ❌ 封装 CLI 进程 | ❌ 协议翻译器 |
| 浏览器 web UI | ✅ | ❌ TTY | ❌ 桌面 | ✅ | ❌ |
| 个人档案场景 | ✅ | ❌ 编程 | ❌ 通用 | ❌ 编程 | ❌ |
| **非 Claude 模型同 agent loop** | ✅ 经 vendor anthropic-compat | ❌ 仅 Anthropic | ❌ 仅 Anthropic | partial | ⚠ 翻译过程会丢失功能 |
| 自托管友好度 | ✅ | n/a（用户本机已有） | ❌ 闭源 binary | ✅ | ✅ |
| 开源 | ✅ MIT | ❌ | ❌ | ✅ MIT | ✅ MIT |

最简概括：**"muselab 之于个人 archive，等同 Claude Code 之于代码库。"**
