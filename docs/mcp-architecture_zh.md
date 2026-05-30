# MCP 架构与连接器策略

> [English](mcp-architecture.md)

发布前的一份决策记录：muselab 如何处理 MCP（Model Context Protocol）server——预置什么、砍掉什么，以及前后端如何用统一的方式对待所有 server。

## 一句话总结

- **MCP 用于 Claude Code 本身没有的能力**——外部集成（邮件、日历、笔记、你自己的 API）。内置工具能干的，**不要**做成 MCP。
- **开箱默认不预置任何用户 MCP server**（只有进程内的 `muselab` server 供 `ask_user_question` 用）。连接器一律 opt-in。
- **借鉴成熟客户端（如 Claude app）的连接器生命周期：** 安装级的*连接*、可选的会话级*启停*、以及会话级*隔离*。见下文「三层模型」。
- **按属性对待每个 server——而非维护一份固定目录**：`transport`（`stdio` | `http`/`sse`）、muselab 本地的 `disabled` 标志、锁定的 `version`。
- **预置一个小而精的连接器画廊（默认关）+ registry/自定义入口**，而不是捆绑一堆 server 全开。

## 三层模型（对齐 Claude app）

一个有用的心智模型，借自成熟的 MCP 客户端，是把三件事分开。muselab 是单用户自托管，所以「账户级」收敛成「安装级」，但分层依然成立：

| 层 | 含义 | muselab 现状 |
|---|---|---|
| **1. 连接** | 配置一次连接器，所有会话都能用 | `mcp.json` + 继承的 Claude Code 配置。**已就位。** |
| **2. 会话级启停** | 同一连接器，这个对话开、那个关 | 现在只有**全局** `disabled` 标志；会话级覆盖**暂缓**（见注） |
| **3. 会话隔离** | 工具返回的数据不在对话间串台 | 每个 SDK client 按会话独立 ⇒ **免费。** |

> **为什么会话级启停暂缓。** 它只有在连接器持有*常驻*访问、值得从某些对话里隔离出去（券商、收件箱）时才配得上这份复杂度。默认连接器为零时，现在就做会话级开关是「为不存在的问题提前加复杂度」。它会和第一批远程连接器（Tier-1）一起落地，那时机制本来就很轻——`backend/chat.py` 里 `mcp_dict` 已是逐会话构建。

**传输与这三层正交**，但它决定第 1 层能多便宜地共享：`http`/`sse` 连接器是一个常驻端点、所有会话连过去（共享、无冷启动）；`stdio` server 是 CLI **每个 SDK client** spawn 一个子进程（跨会话不可共享、有冷启动）。

## 背景：这些失败模式是怎么暴露的

muselab 自己不 spawn MCP server——它通过 Claude Agent SDK（`ClaudeSDKClient`）驱动 Claude，是被拉起的 `claude` CLI 子进程在启动配置好的 server。发布默认**一个都不配**（`mcp.json` 被 gitignore、没有任何 installer 去 seed 它；git 里只跟踪 `mcp.json.example`——那只是 UI 画廊）。下面三个失败模式，是在**同时**配置并开启了 6 个 stdio server（`filesystem`、`fetch`、`memory`、`git`、`sequential-thinking`、`time`）这种重手配置下观察到的，**不是**开箱状态。之所以记录，是因为**任何** stdio 偏重的配置都会撞上：

1. **重复进程。** SDK client 按 `(session, model, effort)` 三元组缓存，LRU 上限 3（`backend/chat.py` 的 `_CLIENT_POOL_CAP`）。每个缓存的 client 各自 spawn 自己的 stdio server ⇒ **最多 3 份完整 server 集合，跨会话从不共享**。（第 1 层共享正是 `http` 传输买回来的东西。）
2. **冷启动慢。** 不锁版本的 `npx -y` / `uvx` 每次都从 registry 重新解析。6 个这样的 server，实测从启动到全部工具可用约 80 秒。
3. **turn 中途变工具集 → 会话死锁。** server 在后台懒加载，可用工具集会在一个 turn 进行到一半时变化。当扩展思考开启时，这会撞上 API 的 thinking 块签名校验（最新 assistant 消息里的 thinking 块必须原样返回），产生一个无法被后续任何对话修复的 `400 ... thinking blocks ... cannot be modified`。已由**后端就绪门控根治**（见后端/前端要求）。

## 原则 1 —— MCP vs 内置工具

Claude Code 已自带一线工具：`Read`（含图/PDF）、`Edit`、`Write`、`Grep`（ripgrep）、`Glob`、`Bash`、`WebFetch`，以及原生扩展思考。**这些覆盖的，就不该再做 MCP。** 多一种做同一件事的方式，会稀释工具选择准确率、撑大 context，却没换来任何新能力。

旧预置对照内置：

| 预置 MCP | 内置等价物 | 结论 |
|---|---|---|
| `filesystem` | `Read` / `Edit` / `Write` / `Grep` / `Glob`（更强） | **砍 / 默认关** |
| `git` | `Bash`（`git ...`） | **默认关** |
| `time` | context 已注入当前日期 + `Bash date` | **砍** |
| `fetch` | `WebFetch`（HTML→markdown） | **默认关**（登录页有一点边际价值） |
| `sequential-thinking` | 原生扩展思考 | **砍**（也是死锁诱因） |
| `memory` | 文件/markdown 记忆系统 | **砍**（重复，实际从未使用） |

> 关于 `memory`：`@modelcontextprotocol/server-memory` 是一个知识图谱存储（`create_entities` / `relations` / `search_nodes`），和 muselab 已在用的文件式记忆系统**完全是两回事**，只是名字撞了。MCP 这个属于冗余。

## 原则 2 —— MCP vs Skill

两者都扩展 agent，但方式不同：

| | MCP | Skill |
|---|---|---|
| 本质 | 暴露**工具**的外部程序 | 一个文件夹：`SKILL.md`（指令）+ 可选脚本/资源 |
| 增加 | 模型够不到的**新能力**（发邮件、查数据库） | 用已有工具把事做好的**新流程/方法论**（报告模板、调研流程） |
| 运行时 | 一个运行中的进程/远程服务，带鉴权（OAuth） | 文件，按需加载；脚本经 `Bash` 临时运行 |
| context 成本 | 一连上，工具 schema 常驻 | 渐进式——触发前几乎不占 |
| 标准 | 开放、跨客户端协议 | Claude/Anthropic 构造（markdown + 资源） |

**经验法则：** 难点在于*连接和鉴权一个外部系统* → MCP；难点在于*固化一套做法*（连接只是一个 API key）→ Skill。例：web 搜索做成 **Skill**（一个 key，价值在如何用结果）；Gmail 做成 **MCP**（OAuth、会话、跨客户端复用）。

## 原则 3 —— 属性驱动，而非固定目录

用户会不断新增 server，所以行为必须通用。每个 server——预置或用户添加——在 `mcp.json` 里存为两种形态之一：

```
stdio ： { command, args, env,   disabled }      # type 推断为 "stdio"
remote： { type: "http"|"sse", url, headers, disabled }
```

- `type=http`/`sse` → 连一个 URL；**天然跨会话共享、无冷启动**。有状态或需共享的优先用它。（添加路径已于 2026-05-30 落地：添加表单和 `MCPServerSpec` 两种形态都接受；后端把 `url` 形态的 spec 原样透传给 SDK。）
- `type=stdio` → CLI 每个 client spawn 一个子进程；适合本地、无状态、可信的工具。
- `disabled` → muselab 本地布尔（UI 开关）。为 true 时该 server 不会进交给 SDK 的字典。这是现在的**全局**第 2 层控制；逐会话覆盖是上面那块暂缓的部分。
- `version` → 在 `args` 里锁死（如 `mcp-server-foo==1.2.3`）；绝不 `npx -y latest`。

就绪门控、健康显示、版本/安全校验随后对**任何** server（含用户后加的）一视同仁。

## 决策 —— 预置连接器画廊

预置一个小而精的**外部**连接器画廊，**默认关**，一键 + OAuth + consent。优先选**第一方 / 官方 registry**的 server，而非 random 社区 `npx` 包（信任 + 维护）。

**Tier 1 —— 主推（广谱、成熟、第一方）：**

| 连接器 | 理由 |
|---|---|
| Gmail | 邮件是个人生活核心入口 |
| Google Calendar | 日程；与 Gmail 配套 |
| Notion | 笔记 / PKM；官方 server；与 notes 场景重叠 |
| Google Drive / Docs | 个人文档库 |

**Tier 2 —— 提供，默认关：** Slack（沟通）、Linear / Todoist（任务）。

**Tier 3 —— 小众但贴合自托管人群：** Home Assistant（自托管家庭自动化——受众完全重叠）。

**不要预置：** `filesystem` / `git` / `time` / `memory` / `sequential-thinking`（内置已覆盖）；GitHub MCP（工具面巨大、纯开发者向——工具过载的典型反例）；数据库写权限 / 交易下单类（开箱默认风险过高）。

**长尾：** 与其手工维护一份大目录，不如链接**官方 MCP registry**（`registry.modelcontextprotocol.io`）供浏览/添加，再加一个"添加自定义 server"表单。muselab 只背 Tier-1 那几个连接器的维护责任。

## 安全要求（对齐 spec）

来自 MCP spec 的 "Local MCP Server Compromise" 与 Streamable HTTP 指引：

- **运行本地 server 前先 consent。** 用户添加 stdio server 时，展示**完整命令**（不截断），警告它以应用同等权限运行，高亮危险模式（`sudo`、`rm -rf`、`curl` 到 home/SSH 路径），要求显式确认。
- **锁版本、校验完整性。** 发布配置里不出现 `npx -y latest`。
- **最小权限。** filesystem 式访问限定在数据目录内。
- **本地 HTTP server** 必须绑 `127.0.0.1`、校验 `Origin` 头（防 DNS rebinding）、要求 token。
- **添加表单里优先 HTTP URL 而非 `npx` 命令**——更少供应链风险、可共享、无冷启动。

## 后端要求 —— 就绪门控（死锁根治）

turn 中途死锁**无法**靠前端单独修复。turn 1 还没有 SDK client；`_start_turn` 阻塞在 `get_client(...)` 上，SSE 流要等它返回才打开，所以在那个 turn *之前*前端根本没有东西可轮询去阻止工具集中途变化。

修复在后端（`backend/chat.py`，已作为 C1 门控落地）：`client.connect()` 返回后，**阻塞直到每个 enabled 的 MCP server 进入终态**（`_await_mcp_ready`，轮询 `get_mcp_status`），再把 client 提交进池、再开始 turn。由 `_has_enabled_external_mcp()` 守护，所以零连接器的默认会完全跳过这次往返。这在机制上消除了"第一个 turn 进行到一半工具集变化"——也就是让 thinking 块签名失效的那个条件。

## 前端要求

以下对每个 enabled 的 server（预置或用户加的）通用：

1. **连接提示**——后端门控正把 turn 1 撑住时，乐观地显示一个"连接工具中…"的提示（延迟显示以避免在热缓存 client 上闪一下）。这只是 UX；*门控*是后端的事（见上），不是前端的。
2. **实时健康 UI**——把 MCP 抽屉里的静态条数换成逐 server 状态（连接中 / ok / 错误），数据取自实时 status 端点，而非静态配置列表。
3. **死锁错误 CTA**——`400 ... thinking blocks ... cannot be modified` 无法靠 "Compact" 修复；CTA 应改为"此会话无法继续——新开 / 回退到出错前"。
4. **连接器画廊 + consent 弹窗**——Tier-1 画廊一键 OAuth，自定义 server 走安全章节的 consent 弹窗。
5. **本地 / 远程添加表单**——一个传输切换（本地 stdio 命令 / 远程 http+header 连接器）。已于 2026-05-30 落地。

## 落地节奏

| 阶段 | 范围 | 理由 |
|---|---|---|
| **P0（发布前）** ✅ 已落地 | 默认不预置任何用户 MCP；Docker 不预装任何 MCP server（只装 `claude` CLI）；**后端就绪门控** + 前端"连接工具中…"提示；**远程（http/sse）添加路径**（表单 + `MCPServerSpec` + 透传） | 从根上解决"启动慢"和"会话死锁"；默认没东西要 spawn，冷启动消失。低风险，不动深层架构。 |
| **P1（发布后）** | Tier-1 连接器画廊（Gmail / Calendar / Notion / Drive）带 OAuth；添加 server 的 consent 弹窗；死锁 CTA 修正；**会话级启停**（第 2 层）；MCP 集合与 `(model, effort)` 解耦 | 真正的产品价值（好用的外部连接器）+ 安全底线 + 那块暂缓的会话级控制，此时已有持常驻访问的连接器。 |
| **P2（优化）** | registry 浏览/添加；属性 schema 落库；Tier-2/3 连接器；实时健康 UI | 长尾扩展，且不让 muselab 背目录维护。 |

## 已定 / 待定

- **已定：** `memory` MCP 砍掉（与文件式记忆系统重复；砍掉后 P0 不需要任何有状态 HTTP 常驻进程）。
- **待定：** Tier-1 连接器在 OAuth 成功后是否默认开启，还是严格保持 opt-in。当前倾向：opt-in。
