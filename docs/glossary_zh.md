# 词汇表

> [English](glossary.md)

muselab 代码库与文档中使用的专有术语，集中定义，供各处引用。

---

**active-turn sidecar** — 每次回合开始时写入 `sessions/active_turns/<sid>.json` 的小型 JSON 文件，干净完成后删除。若 muselab 在回合进行中被强制退出，该文件会保留下来，并在下次浏览器会话打开时显示「未完成的回合」提示条。参见 [`backend-sessions.md — Active-turn sidecars`](backend-sessions.md#active-turn-sidecars)。

**active turn** — 当前正在流式传输的聊天回合。muselab 在 `_active_turns[sid]` 中追踪活跃回合；一个会话同时最多只能有一个活跃回合。尝试在一个回合运行时启动第二个回合会引发 `_TurnBusy`。参见 [`routing.md — The SSE Turn Loop`](routing.md#4-the-sse-turn-loop)。

**archive root（MUSELAB_ROOT）** — `MUSELAB_ROOT` 所指向的目录。这里存放你自己的文件：`CLAUDE.md`、子目录、附件（`.muselab-attach/`）、定时任务状态（`.muselab/`）以及回收站（`.muselab-dustbin/`）。它刻意位于仓库根目录之外，使数据和代码可以独立备份或迁移。参见 [`architecture_zh.md`](architecture_zh.md#目录地图)。

**cost capture（成本记录）** — 每次回合结束后，`ResultMessage.total_cost_usd` 的值会累加到进程级聚合计数器和每会话累计值中，并作为每条消息的注解写入 sidecar。成本字段仅在 Claude（Anthropic）模型中有值；第三方提供商返回 0。参见 [`routing.md`](routing.md) 和 [`backend/chat.py:L6176`](../backend/chat.py#L6176)。

**cwd-key** — Claude CLI 用于在 `~/.claude/projects/` 下为 JSONL 文件命名空间的哈希化目录路径。对于给定的归档根目录，该键形如 `-home-alice-archive`。muselab 使用相同的推导方式，使其会话 ID 与 CLI 文件名一一对应。参见 [`backend-sessions.md — Two-layer store`](backend-sessions.md#1-two-layer-store--ownership)。

**CLAUDE.md** — 位于归档根目录的纯文本 Markdown 文件。Claude Agent SDK 在每次对话时自动将其作为上下文加载，使其成为个性化 Muse 行为的主要渠道。参见 [`personalize-claude-md.md`](personalize-claude-md.md)。

**CLAUDE_CONFIG_DIR isolation（CLAUDE_CONFIG_DIR 隔离）** — 对于第三方提供商，muselab 将 `CLAUDE_CONFIG_DIR` 设置为每用户的临时目录（`$(tmpdir)/muselab-vendor-cli-config-<uid>/`），其中不含 `credentials.json`。这可防止 CLI 静默回退到 Claude Pro OAuth，避免将第三方流量计入你的 Anthropic 账单。参见 [`routing.md — CLAUDE_CONFIG_DIR isolation`](routing.md#why-claude_config_dir-isolation-prevents-oauth-fallback-billing) 和 [`backend/endpoints.py:L879`](../backend/endpoints.py#L879)。

**client pool（客户端池）** — 活跃 `ClaudeSDKClient` 实例的内存缓存，以 `(session_id, model, effort)` 为键。默认容量为 3 个条目（可通过 `MUSELAB_CLIENT_POOL_CAP` 配置）；超出上限时按最近最少使用（LRU）策略淘汰，但有活跃回合或后台任务进行中的条目除外。参见 [`routing.md — The Client Pool`](routing.md#2-the-client-pool) 和 [`backend/chat.py:L302`](../backend/chat.py#L302)。

**effort（推理强度）** — 传给 `ClaudeAgentOptions` 的推理强度等级。有效值为 `"low"`、`"medium"`、`"high"`、`"xhigh"`、`"max"` 或 `""`（SDK 自适应默认值）。按会话存储于 `sessions/index.json`；修改后会断开缓存的 client，下次回合以新值重建。effort 是 client pool 缓存键的组成部分。参见 [`routing.md — Reasoning Effort and Extended Thinking`](routing.md#5-reasoning-effort-and-extended-thinking)。

**extended thinking / thinking signature（扩展思考 / 思考签名）** — `ThinkingConfigEnabled` 激活时 Claude 产生的推理轨迹。thinking 块通过 `thinking` SSE 事件流式传输。签名是不可修改的不透明令牌；muselab 将会话锁定到单一模型以避免跨提供商的签名损坏。对于 Opus 4.7+，需要 `display="summarized"` 才能获得纯文本 thinking 块，而非仅有签名。参见 [`routing.md — budget_tokens and display`](routing.md#budget_tokens-and-displaysummarized)。

**fork（派生）** — 将会话记录截至某条消息 UUID 的副本，以新会话 ID 存储。内部用于实现消息编辑（UI 在上一条助手回合处派生，然后重新发送）。JSONL 和 `sessions/index.json` 条目均会为派生会话创建。参见 [`backend-sessions.md — Fork & edit-a-message`](backend-sessions.md#6-fork--edit-a-message)。

**legacy session self-heal（旧会话自愈）** — 若某个会话在配置任何提供商之前创建，它会被锁定到 `MODEL` 常量。当用户后来只配置了第三方提供商时，每次发送都会收到 401 错误。下次发送时，muselab 会检测到被锁定模型的提供商不可用且会话在磁盘上没有 JSONL 文件（从未运行过），然后重新解析模型。已有历史记录的会话永远不会被重新解析。参见 [`routing.md — Legacy-session self-heal`](routing.md#legacy-session-self-heal) 和 [`backend/chat.py:L1680`](../backend/chat.py#L1680)。

**longest-prefix routing（最长前缀路由）** — muselab 用于将模型 ID 映射到其提供商的算法。`backend/endpoints.py` 中的 `lookup(model)` 将所有提供商前缀按长度降序排列，返回第一个匹配项（不区分大小写）。冒号标记的前缀（如 `qwen-intl:`）在将模型 ID 发送给提供商前会被规范化，提供商永远看不到路由标签。参见 [`backend/endpoints.py:L806`](../backend/endpoints.py#L806)。

**MCP（Model Context Protocol，模型上下文协议）** — 将外部工具服务器附加到 agent 的标准。muselab 在「设置 → MCP」中暴露 MCP 配置，并将自身的 `mcp.json` 与 Claude Code 的全局配置合并。`ask_user_question` MCP 工具受到特殊处理：muselab 不会阻塞它，而是通过进程内队列重新路由，以便浏览器能在行内显示问题。参见 [`mcp-architecture.md`](mcp-architecture.md)。

**message queue（消息队列）** — 每会话的 FIFO 队列（`sessions/<sid>.queue.json`），在一个回合已在运行时暂存提交的 prompt。当前回合完成后，排空循环自动启动下一个回合。最大深度为 10。若回合出错或被取消，队列自动暂停。参见 [`backend-sessions.md — The message queue`](backend-sessions.md#4-the-message-queue)。

**model lock（模型锁定）** — 第一次成功回合后，模型 ID 写入 `sessions/index.json`，之后该会话的每次回合都使用该 ID，无论 UI 下拉菜单显示什么。这可防止跨提供商的 thinking signature 损坏。通过 `PATCH /sessions/{sid}` 修改模型会更新锁定值并断开缓存的 client。参见 [`routing.md — Session model lock`](routing.md#session-model-lock)。

**no-build frontend（无构建前端）** — 前端以纯 HTML + JavaScript + CSS 提供服务，无需打包工具、编译器或 `npm install`。经过审查的第三方库已提交至 `frontend/vendor/`。重型库（KaTeX、CodeMirror、Mermaid、highlight.js）在首次使用时延迟加载。参见 [`architecture_zh.md — 关键设计决策`](architecture_zh.md#关键设计决策)。

**provider（提供商）** — `backend/endpoints.py` 中定义的提供商配置记录。每个提供商有 `prefix`（用于最长前缀路由）、`base_url`、`env_key`（API 密钥环境变量名），以及 `supports_thinking`、`supports_effort` 和 `max_output_tokens` 等标志。内置目录覆盖 9 个提供商（含本地网关预设）；用户自定义提供商使用 `c:<slug>` 稳定 ID 格式。参见 [`routing.md — Model Resolution`](routing.md#1-model-resolution) 和 [`providers.md`](providers.md)。

**provider catalog（提供商目录）** — `backend/endpoints.py` 中 `catalog()` 返回的全部可用提供商列表。内置条目带有以 `b:` 为前缀的稳定 ID。目录按 `provider_overrides.json` 的 `(mtime_ns, size)` 缓存，文件变更时重新读取。Claude（Anthropic）单独管理，不在目录中。参见 [`backend/endpoints.py:L170`](../backend/endpoints.py#L170)。

**provider override（提供商覆盖）** — 存储在 `provider_overrides.json`（仓库根目录，与 `mcp.json` 同级）中的字段级补丁。覆盖可以禁用内置提供商、更改其 `base_url` 或 `env_key`，或定义完全自定义的提供商。`supports_thinking`、`supports_effort` 和 `max_output_tokens` 字段无法通过设置 UI 修改。参见 [`add-provider.md`](add-provider.md) 和 [`routing.md — Model Resolution`](routing.md#1-model-resolution)。

**PWA / service worker（渐进式 Web 应用 / 服务工作线程，仅推送）** — muselab 附带 `manifest.webmanifest`，允许浏览器将其作为渐进式 Web 应用安装（独立显示模式，主屏幕图标）。service worker（`frontend/sw.js`）刻意**不**缓存任何资源；其唯一功能是接收 Web Push 通知并将其路由到正确的已打开标签页。参见 [`mobile_zh.md`](mobile_zh.md)。

**pending attachment queue（待处理附件队列）** — sidecar 中的 `pending_attachments` 列表，在 SDK 写入用户消息 UUID 之前暂存上传元数据。由于 CLI 异步追加 JSONL 记录，上传时消息 UUID 尚不可知；muselab 在下次读取会话时将附件绑定到正确的消息 UUID。参见 [`backend-sessions.md — Pending attachment queue`](backend-sessions.md#pending-attachment-queue-pre-uuid-binding)。

**repo root（仓库根目录）** — 包含 muselab checkout 的目录（`backend/`、`frontend/`、`sessions/`、`.env` 等）。与归档根目录（archive root）不同。仓库是安装本身；归档是你的数据。参见 [`architecture_zh.md — 目录地图`](architecture_zh.md#目录地图)。

**safe_resolve** — `backend/files.py:L316` 中的路径验证函数，每个 `/api/files/*` 端点在操作文件系统前都会调用它。它会阻止 `..` 路径穿越、符号链接逃逸、NUL 字节注入以及敏感文件名。所有路径必须解析为 `MUSELAB_ROOT` 的后代路径。参见 [`backend-files.md — safe_resolve in depth`](backend-files.md#safe_resolve-in-depth) 和 [`backend/files.py:L316`](../backend/files.py#L316)。

**scheduler（定时任务调度器）** — 内置的 asyncio 定时循环（`backend/scheduler.py`），按时间计划运行保存的 prompt。状态（任务、下次运行时间、上次运行历史）持久化至 `$MUSELAB_ROOT/.muselab/scheduler.json`。定时运行使用与交互式回合相同的 client pool 和 SSE 路径。参见 [`scheduler_zh.md`](scheduler_zh.md)。

**sensitive-filename blocklist（敏感文件名黑名单）** — `backend/files.py` 中的两个集合（`SENSITIVE_NAMES` 和 `SENSITIVE_SUFFIX`），涵盖凭据文件、私钥、shell 历史记录和 `.env` 变体。匹配这些规则的路径会被 `safe_resolve` 以 HTTP 403 拒绝，除非明确传入 `allow_sensitive=True`（仅用于回收站还原和备份复制）。参见 [`backend-security.md — Filesystem containment`](backend-security.md#filesystem-containment) 和 [`backend/files.py:L286`](../backend/files.py#L286)。

**session（会话）** — 对话的顶级单元。一个会话拥有一个 UUID，该 UUID 由 muselab 索引条目、sidecar 文件、CLI JSONL 和消息队列文件共用。会话在第一次回合后锁定到一个模型，并携带自己的 effort 和思考设置。参见 [`backend-sessions.md`](backend-sessions.md)。

**session index（会话索引）** — 文件 `sessions/index.json`（位于仓库内，而非归档中）。它是 muselab 对于 CLI 未追踪的每会话元数据的真相源：模型锁定、系统提示、effort、thinking 开关、置顶状态和自动命名标志。CLI JSONL（`~/.claude/projects/<cwd-key>/<sid>.jsonl`）是对话记录的真相源。参见 [`backend-sessions.md — The session index`](backend-sessions.md#2-the-session-index)。

**sidecar** — 文件 `sessions/<sid>.sidecar.json`，存储叠加在 CLI JSONL 之上的每条消息注解：成本（USD）、模型标识、时间戳、上传图片缩略图和文档引用。每次回合后写入；从不存储对话记录本身。参见 [`backend-sessions.md — Sidecar files`](backend-sessions.md#3-sidecar-files)。

**setting_sources** — 在 `ClaudeAgentOptions` 中传入的 SDK 参数 `["user", "project", "local"]`，告知 Claude Agent SDK 要加载哪些配置作用域。`local` 作用域（相对于 `cwd`，即 `MUSELAB_ROOT`）是内置 `skills/` 目录和归档中 `CLAUDE.md` 被发现的方式。参见 [`backend/chat.py:L944`](../backend/chat.py#L944)。

**skill / SKILL.md** — skill 是 `skills/`（或 `~/.claude/skills/`）下包含带 YAML frontmatter 的 `SKILL.md` 文件的目录。`description` 字段（以 `"USE WHEN …"` 开头）是模型决定是否激活该 skill 的主要信号。设置 `skills="all"` 后，Claude Agent SDK 将所有可发现的 SKILL.md 文件作为额外上下文注入。Skills 在第三方提供商上被禁用，以避免 payload 过大引发的错误。参见 [`backend/chat.py:L958`](../backend/chat.py#L958)。

**SSE / TurnBroadcast** — 聊天回合输出以 Server-Sent Events（服务端推送事件）流的形式从 `GET /api/chat/stream` 投递。在内部，每个回合被包装在一个 `TurnBroadcast` 对象中，该对象缓冲所有事件，以便延迟订阅者（重新连接的浏览器标签页）可以获得完整的重放。后台泵任务即使在浏览器断开连接后也会继续运行，因此回合无论如何都会完整写入磁盘。参见 [`routing.md — TurnBroadcast: survive-disconnect design`](routing.md#turnbroadcast-survive-disconnect-design)。

**trash（回收站）** — 位于 `$MUSELAB_ROOT/.muselab-dustbin/` 的软删除暂存区。删除的文件被移动至此，而非永久清除。条目在 `MUSELAB_TRASH_TTL_DAYS`（默认 30 天）后自动过期。`_guard_not_trash()` 函数阻止所有写端点直接操作回收站；只有专用的 `/api/files/trash/*` 端点可以访问它。参见 [`backend-files.md — Trash semantics`](backend-files.md#trash-semantics)。

**thinking toggle（thinking 开关）** — 每会话的布尔值（`thinking`，默认 `true`），用于启用或禁用 `ThinkingConfigEnabled`。通过 `PATCH /sessions/{sid}` 将其设为 `false` 是处理特定工具调用交错模式引发的「最新助手消息中的 thinking 块不可修改」400 错误的应急手段。修改后会使缓存的 client 失效。参见 [`routing.md — Reasoning Effort and Extended Thinking`](routing.md#5-reasoning-effort-and-extended-thinking)。

**token（MUSELAB_TOKEN）** — `.env` 中设置的至少 16 字符的共享密钥，用于保护每个 API 端点。比较时使用 `hmac.compare_digest`（常量时间）以抵御时序攻击。对于大多数端点，令牌通过 `X-Auth-Token` header 传入；对于浏览器无法发送自定义 header 的 SSE 和文件下载端点，则通过 `?token=` 查询参数传入。需要鉴权的图片预览会用 header fetch 后渲染为 blob URL，避免把全局 token 放进图片 URL。参见 [`backend-security.md — Authentication`](backend-security.md#authentication)。

**vendored libraries（内置第三方库）** — 提交到 `frontend/vendor/` 下的第三方 JavaScript 库，使前端无需运行时 npm 依赖。包含 Alpine.js、marked、DOMPurify、Mermaid、highlight.js、KaTeX 和 CodeMirror。许可证信息见 [`THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md)。参见 [`architecture_zh.md — 关键设计决策`](architecture_zh.md#关键设计决策)。

**vendor config dir（提供商配置目录）** — 参见 *CLAUDE_CONFIG_DIR isolation*。
