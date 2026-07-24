# 架构

> [English](architecture.md)

muselab 是一个无前端构建步骤的自托管工作台。浏览器负责文件、预览、终端和会话交互；FastAPI 负责鉴权、持久状态与实时传输；所有模型调用统一经过 Claude Agent SDK。

```text
浏览器
├── 文件树与全局搜索
├── 文件预览、编辑器与真实 PTY 终端
├── 多工作区、多会话、队列与活动中心
└── 设置、Providers、Skills、MCP、定时任务
        │
        ├── HTTP / ticket SSE
        └── ticket WebSocket
                │
FastAPI
├── Files / Workspaces / Preview
├── Chat / Sessions / Replay spool / Attachments
├── Terminals / Profiles / PTY workers
├── Scheduler / Push / Activity
└── Settings / Providers / MCP / Skills
                │
Claude Agent SDK → claude CLI
├── Claude OAuth
└── Anthropic-compatible providers
```

## 关键设计

- **SDK 是唯一模型入口。** 工具调用、MCP、Skills、Subagent、plan mode 与 `CLAUDE.md` 均由 Claude Agent SDK 提供。muselab 不建立另一套 Agent 或 system prompt 体系。
- **原生指令归属。** 持久身份、回复风格、个人背景与长期规则放在 SDK 自动发现的 `CLAUDE.md` 层级；可复用工作流放在 Skills；工具行为由工具描述和权限配置约束。
- **工作区绑定。** `MUSELAB_ROOT` 是默认工作区，也可登记其他本地目录。文件面板、预览、终端和新会话 cwd 随当前工作区切换；每个会话保存自己的 cwd。
- **整文件输入。** 助手通过 Read、Grep、Edit 等工具按需读取完整文件，不预先向量化或切块。
- **第三方 Provider 隔离。** 每个第三方 Provider 按请求设置 `ANTHROPIC_BASE_URL`、`ANTHROPIC_API_KEY` 与隔离的 `CLAUDE_CONFIG_DIR`，避免 CLI 回退到错误账户。
- **无前端构建步骤。** HTML、JavaScript 和 CSS 由浏览器直接运行；第三方浏览器库 vendored 在 `frontend/vendor/`。
- **实时连接使用短期 ticket。** Chat 先通过带 header 鉴权的 POST 获取一次性 SSE ticket；终端先获取一次性 WebSocket ticket。prompt 与长期 token 不进入实时连接 URL。
- **断线不终止任务。** Chat 回合写入磁盘 replay spool；浏览器重连后从 cursor 继续。终端保留有界输出缓冲，并在短时离开页面后允许重新连接。

## 运行目录

```text
muselab/
├── backend/
│   ├── main.py                    # 应用生命周期与路由挂载
│   ├── chat.py                    # Agent 回合、SSE、队列和后台任务
│   ├── sessions.py                # 会话索引与 sidecar
│   ├── files.py                   # 文件读写、预览数据和回收站
│   ├── workspaces.py              # 工作区登记与选择
│   ├── terminal.py                # 终端 API、Profile 与连接管理
│   ├── terminal_worker.py         # PTY 子进程
│   ├── endpoints.py               # Provider catalog 与连接参数
│   ├── scheduler.py               # 定时任务执行器
│   ├── push.py                    # Web Push 与 VAPID
│   ├── activity.py                # 活动中心持久状态
│   ├── transcript_index.py        # transcript 索引
│   └── api_*.py                   # 各领域 API router
├── frontend/
│   ├── index.html
│   ├── app.js
│   ├── styles.css
│   ├── i18n/
│   └── vendor/
├── skills/                        # 随应用提供的 Skills
├── scripts/                       # 安装、升级、诊断与模板
├── docs/                          # 用户与公开技术文档
├── .claude/docs/                  # 维护者文档
├── .env                           # 实例配置与密钥
└── sessions/                      # 会话元数据、队列与附件

$MUSELAB_ROOT/
├── CLAUDE.md                      # 默认工作区指令与个人上下文
├── 用户文件
├── .muselab/
│   ├── workspaces.json
│   ├── scheduler.json
│   ├── activity.json
│   ├── terminal_profiles.json
│   ├── vapid.json
│   └── push_subs.json
└── .muselab-dustbin/

<其他已登记工作区>/
├── CLAUDE.md
├── 用户文件
└── .muselab-dustbin/
```

仓库状态、默认工作区和其他工作区是不同的备份单元。对话 transcript 由 Claude CLI 所有；Claude 会话通常在其配置目录下，第三方 Provider 会话可能在隔离的配置目录下。muselab 的 `sessions/` 保存名称、cwd、模型、成本、附件和队列等叠加元数据。

## 对话回合

1. 浏览器以 `X-Auth-Token` 调用 `POST /api/chat/stream/start`，提交 prompt、session、model、permission、effort 和附件等回合参数。
2. 后端校验会话与工作区，签发短期、单次使用的 ticket。
3. 浏览器用 ticket 建立 SSE；旧版 query 方式只用于兼容旧后端。
4. 后端按会话、模型与推理参数取得或建立 SDK client，并以会话绑定工作区作为 cwd。
5. SDK 加载 `CLAUDE.md`、Skills 与 MCP，运行工具和模型循环。
6. 事件写入 replay spool 并流向浏览器；断线后可按 cursor 补发。
7. Claude CLI 持久化 transcript，muselab 更新 sidecar、用量、活动状态和通知。

非空会话切换模型时，UI 会创建一个空会话并保留原会话，避免跨 Provider thinking signature 冲突。后端 API 仍允许显式更新会话模型，因此 API 调用者需要自行承担上下文兼容性。

## 终端链路

1. 用户在当前工作区新建终端，可选择 Profile 自动执行启动命令。
2. 后端创建独立 PTY worker，并只向终端进程传递安全环境变量集合。
3. 浏览器取得短期、单次使用的 WebSocket ticket，再以同源 WebSocket 连接。
4. 后端在内存中维护有界输出缓冲；页面暂时离开后可重放，超过保留时间或服务重启后进程会终止。

终端是真实的服务用户 shell，不受 Files API 的工作区路径边界约束。只应把 muselab 暴露给可信用户。

## 子系统文档

| 页面 | 内容 |
|---|---|
| [模型路由与对话循环](routing_zh.md) | Provider 解析、client 生命周期、replay 与 SSE |
| [会话机制](backend-sessions_zh.md) | 索引、sidecar、队列、附件、fork 与恢复 |
| [Files API](backend-files_zh.md) | 文件接口、路径边界与回收站 |
| [终端](terminal_zh.md) | PTY、Profile、连接、移动端与安全边界 |
| [安全模型](backend-security_zh.md) | 鉴权、权限面、Provider 隔离与已知限制 |
| [前端](frontend_zh.md) | 工作台状态、渲染、性能与 PWA |
| [Skills](skills_zh.md) | 内置 Skills、发现机制与自定义扩展 |
| [基础设施](infrastructure_zh.md) | 安装、服务、Docker、测试与发布 |
