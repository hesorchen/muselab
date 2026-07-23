# muselab 宪法

> [English](constitution.md)
>
> 关于 **muselab 如何构建、如何允许变更** 的唯一事实源。
> 规格（spec）与 AI 生成的代码都从本文派生；当代码与本文冲突时，**以本文为准，代码即 bug**。
>
> 范围：本文约束的是 *工程不变量*，不是功能愿望清单。功能意图写进每次改动的 spec，产品路线图与已知问题见 [GitHub Issues](https://github.com/hesorchen/muselab/issues)。

- **版本：** 2.0.0
- **批准日：** 2026-05-31
- **最近修订：** 2026-07-23
- **派生自：** `docs/architecture.md`、`CONTRIBUTING.md`、`SECURITY.md`、`pyproject.toml`，以及截至 2026-07-23 的前后端源码。

规范性关键词 **必须（MUST）/ 禁止（MUST NOT）/ 应当（SHOULD）/ 可以（MAY）** 遵循 RFC 2119。

---

## 1. 核心原则

### P1 — 可读优先于聪明，精简优先于功能堆砌
muselab 刻意保持小巧，让整个代码库始终人类可读。任何贡献**必须**优先选择清晰而非取巧。任何显著增大概念复杂度的改动，**必须**针对本原则给出正当理由。

### P2 — Clone 即跑：永不引入构建步骤
前端**必须**保持「改一个文件、刷新浏览器即可」的运行方式。
- **禁止**引入任何打包器 / 转译器 / npm install 步骤（webpack、vite、esbuild、tsc 等）。
- 第三方浏览器库**必须** vendored 在 `frontend/vendor/` 下，并在 `THIRD_PARTY_LICENSES.md` 记录许可证。
- 前端代码**必须**用 Alpine.js v3 + 现代浏览器能直接理解的方言书写——不允许靠编译步骤来弥合差距。

### P3 — 走 SDK，而非裸 API
muselab 通过 **Claude Agent SDK**（与 Claude Code 同一引擎）驱动 Claude，绝不直接调用裸 Messages API。正是这一点让 MCP、Skills、Subagents、plan mode、`CLAUDE.md` 自动加载在所有 provider 上行为一致。新能力**必须**通过 SDK 原生机制表达，而非绕过 SDK。

### P4 — 工作区属于用户，repo 永不染指
安装根与工作区根永久分离（见 §2）。代码**禁止**把自身状态写进用户工作区，唯一例外是默认工作区的保留路径 `<MUSELAB_ROOT>/.muselab/` 与每个工作区的可恢复回收站；并且**禁止**让安装、升级、迁移依赖用户文件。

### P5 — 以整文件为输入单位
助手按需通过 Read / Grep / Edit 触达用户文件。muselab **禁止**对用户工作区做预嵌入、预索引或 RAG 切块。上下文来自 SDK 按 cwd 自动发现的 `CLAUDE.md` 层级与按需工具读取。

### P6 — 个人数据在发布物里是放射性的
这是一个开源仓库。**禁止**任何真实个人数据出现在代码、文档、commit、测试夹具、示例或 README 文案里。测试**必须**跑在一个用完即弃的 archive 目录上。

---

## 2. 架构不变量

以下是不可妥协的结构性事实。违反其中任一条即属架构变更，落地前**必须**先修订本宪法（见 §8）。

### A1 — 安装根与工作区根刻意分离
| 根 | 内容 | 备份方式 |
|---|---|---|
| **repo**（`muselab/`） | 代码 + 每安装实例状态（`.env`、`sessions/`） | 随安装一起 |
| **默认工作区**（`MUSELAB_ROOT`） | 用户自己的文件 + `.muselab/` 应用状态 | 独立备份，不动安装 |
| **其他已登记工作区** | 额外的用户文件与工作区回收站 | 分别备份 |

`backend/settings.py` 持有 `ROOT`（默认工作区），`backend/workspaces.py` 管理其他已登记根目录。会话固定保存 cwd，并由 SDK 从对应层级发现 `CLAUDE.md`。

### A2 — 分层后端，一个 router 对应一个关注点
后端是 FastAPI，在 `backend/main.py` 挂载。每个领域是一个模块、暴露一个 `APIRouter`。新增的接口面**必须**遵循「一 router 一职责」的形态，而不是把一个上帝模块越养越大：

| 模块 | 职责 |
|---|---|
| `main.py` | app 工厂、uvicorn 入口、路由挂载、静态前端、日志 token 脱敏、资源版本戳 |
| `auth.py` | `X-Auth-Token` 守卫（header 或 `?token=`） |
| `chat.py` | `/api/chat/*`——SDK client 池 + SSE 回合循环 |
| `endpoints.py` | provider `CATALOG` + 每请求 env 装配 |
| `files.py` | `/api/files/*`——safe-resolve 读写 / grep + 回收站 |
| `workspaces.py` | 工作区登记、选择与目录边界 |
| `sessions.py` | 会话索引、cwd、sidecar、队列 |
| `terminal.py` / `terminal_worker.py` | `/api/terminals/*`、Profile、WebSocket ticket 与 PTY worker |
| `scheduler.py` / `api_scheduler.py` | asyncio cron 循环 + 其 API |
| `push.py` / `api_push.py` | Web Push / VAPID + 其 API |
| `activity.py` / `activity_api.py` | 活动中心状态 + 其 API |
| `transcript_index.py` | transcript 索引 |
| `api_settings.py` | `/api/settings`——热改写 `.env` + `os.environ` |
| `prompts.py` | 内置工作流的简短启动消息 |
| `ask_user_question.py` | 进程内 `muselab` MCP server |
| `permission_request.py` | 工具权限往返 |
| `settings.py` | `ROOT` / `PORT` / `HOST`、`atomic_write_text`、`env_int` |

### A3 — per-session env 覆盖 + 配置隔离
第三方 provider 通过每请求设置 `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY` + 一个**隔离的** `CLAUDE_CONFIG_DIR` 来接入。隔离配置目录是强制的：它阻止 CLI 静默回退到 Pro OAuth、把第三方流量计费到用户的 Anthropic 账号上。任何新 provider 路径**必须**保留这三者。

### A4 — client 池以 `(session_id, model, effort)` 为 key
`chat.py` 正是以这个 key 池化 `ClaudeSDKClient` 实例，默认 LRU 上限为 3，也可通过受支持的运行配置调整。每条助手消息存自己的 `model`，使刷新后徽标依旧准确。改动池的 key 属于架构变更（它与 MCP 进程派生相互作用——见 A6）。

### A5 — UI 默认保护模型连续性
首个真实回合确定会话模型。前端在非空会话切换不兼容模型时**必须** fork 新会话，避免跨 vendor thinking-block 签名导致不可恢复的 `400`。管理 API 可以显式修改会话模型，但调用者必须承担 transcript 兼容性风险。在任何 provider 存在之前创建的会话，会在首次发送时自愈到某个已配置模型。

### A6 — MCP：attribute 驱动、有门禁、默认零
- 出厂默认配置**零**个用户 MCP server；连接器是 opt-in。只有进程内 `muselab` server（供 `ask_user_question`）永远在场。
- 每个 server（预置或用户添加）在 `mcp.json` 里都按**属性**存储（`transport`、`disabled`、钉死的 `version`），绝不用写死的 catalog。
- 版本**必须**钉死。出厂配置**禁止**用 `npx -y latest` / 未钉版本的 `uvx`。
- 后端**就绪门禁**（`chat.py` 的 `_await_mcp_ready`）**必须**把第 1 回合挂住，直到每个启用的外部 MCP server 到达终态，以防回合中途工具集变化把会话卡死。当没有启用外部 MCP 时门禁**必须**跳过（`_has_enabled_external_mcp`）。
- MCP 用于**内置工具够不到的能力**（需鉴权的外部系统）。凡是 `Read`/`Edit`/`Grep`/`Glob`/`Bash`/`WebFetch` 已覆盖的，**禁止**做成 MCP server。

### A7 — MCP 与 Skill 的边界
若难点在于*连接并鉴权一个外部系统* → MCP。
若难点在于*把一件事做好的方法编码下来*（连接只是一个 API key）→ Skill（一个含 `SKILL.md` + 可选资产的文件夹，渐进式披露）。新扩展**必须**按此规则归类。

### A8 — transcript 归 CLI 所有，不归 muselab
对话 transcript 由 Claude CLI 所有：Claude 会话通常位于默认配置根，第三方 Provider 会话可能位于隔离的配置根。muselab 的 `sessions/` 只持有叠加在其上的 sidecar 元数据（名称、cwd、每消息 model 徽标、成本、附件）。muselab **禁止**复制或分叉 transcript 本身的所有权。

### A9 — 原生指令归属
muselab **禁止**注入全局或会话级自定义 system prompt。持久身份、回复偏好与个人上下文属于 SDK 自动发现的 `CLAUDE.md` 层级；可复用任务工作流属于 Skill；工具特有行为属于工具描述与代码强制的权限配置。UI 可以用开场 Prompt 调用这些原生能力，但**禁止**再建立一层行为指令体系。

---

## 3. 技术栈与约束

- **语言 / 运行时：** Python `>=3.12`。依赖与虚拟环境用 `uv` 管理。
- **Web：** FastAPI，基于 `starlette>=1.3.1`，由 `uvicorn[standard]` 提供服务。
- **Agent：** `claude-agent-sdk>=0.2.120,<0.3`——通往模型的唯一路径。应用代码里**禁止**直接用 `anthropic` SDK / 裸 HTTP 打模型端点。
- **前端：** vanilla HTML + Alpine.js v3 + CSS。无框架、无构建（P2）。
- **持久化：** 扁平文件（JSON sidecar、由 CLI 所有的 JSONL transcript）。**禁止**为核心功能引入数据库依赖。
- **新增依赖**：**必须**给出正当理由（是新能力，而非图方便）、钉一个版本下限；若它拉入运行时二进制（`npx`/`uvx`），安装脚本**必须**检测到并发出警告（见 CONTRIBUTING 清单）。
- **provider 接入**：**必须**暴露 Anthropic 兼容的 Messages 端点，并以 `endpoints.py` 里一条 `CATALOG` 落地。OpenAI-only 协议出局（SDK 期望 Anthropic 兼容端点）。

---

## 4. 代码约定

### Python
- PEP 8。**不强制 formatter**——不要引入会把现有紧凑风格搅乱的 `black` / 自动格式化。
- Lint：`ruff`，`select = ["E","F","W"]`，`ignore = ["E501"]`。新代码**必须**通过 `ruff check backend/ tests/`。带主观倾向的规则族（B、I、N、UP）**禁止**启用——会为边际收益搅动可用代码。
- **公开**函数上**必须**加类型标注；其他地方不强制。
- 写盘**必须**走 `atomic_write_text`（或同等原子路径）——绝不对用户可见文件做裸截断写。

### JavaScript / CSS
- 无转译器。写 Alpine v3 + 现代浏览器能直接跑的方言。分号风格跟随相邻代码。
- CSS：按组件分段，带注释头。主题用 CSS 变量（`--c-*`、`--sp-*`）；**禁止**写死颜色。
- 编辑 `app.js` / `styles.css` / `index.html` / `i18n/index.js` / `data/constants.js` 会参与资源版本戳——新增拆分模块时记得把它加进候选列表，好让客户端重新拉取。

### 国际化
面向用户的 UI 文案**必须**在 `frontend/i18n/index.js` 的 `en` 与 `zh` 两张表里都存在。带译本的文档遵循 `_zh.md` 同名兄弟文件约定。

---

## 5. 安全要求

（权威细节在 `SECURITY.md`；以下是代码评审与 spec **必须**强制执行的宪法级不变量。）

- **每请求鉴权。** 普通 API 使用 `X-Auth-Token`。浏览器实时连接必须先通过已鉴权请求取得短期、单次 ticket；兼容性 query token 仅限无法发送 header 的资源请求。新增端点**禁止**绕过相应鉴权依赖。
- **Files API 路径穿越已封死。** 浏览器文件接口的所有路径**必须**经过 `files.py` 的 safe-resolve 逻辑，且停留在当前已登记工作区内。写 / 上传 / 重命名 / 拷贝**必须**拒绝 `.muselab-dustbin/` 路径（`_guard_not_trash`）；删除是软删除（移入回收站），恢复 / 清除作为独立端点。
- **日志不泄密。** uvicorn 访问日志的 `token=` 脱敏过滤器（`main.py` 的 `_TokenFilter`）**必须**保留；新增可能携带 token / key 的日志面**必须**同样脱敏。
- **本地 MCP 需同意。** 添加 stdio server **必须**展示未截断的确切命令、警告它以 app 权限运行、标出危险模式（`sudo`、`rm -rf`、向 home/SSH 路径 `curl`），并要求明确批准。
- **本地 HTTP server** **必须**绑定 `127.0.0.1`、校验 `Origin`（防 DNS 重绑定）、并要求 token。优先用远程 HTTP 连接器而非 `npx` 命令（更少供应链风险）。
- **权限边界必须准确表达。** Files API 限定在选中的已登记工作区；Agent 工具和真实终端以服务用户权限运行，不得被描述成同一条路径沙箱。终端必须使用同源校验、单次 ticket 与安全环境变量集合。
- **零密钥** 进代码、commit 或测试夹具。`.env` 与 `sessions/` 保持 gitignored，**禁止**被添加进库。

---

## 6. 测试与质量门禁

下列全过之前，改动不算完成（CONTRIBUTING 清单是其强制形式）：

- [ ] `uv run pytest tests/` 全绿。
- [ ] `uv run ruff check backend/ tests/` 干净（CI 在 lint 失败时阻断合并）。
- [ ] `bash scripts/lint.sh` 干净（编码 / BOM / 类名冲突）。
- [ ] **每个后端改动新增或更新一个 `tests/` 里的测试。** bug 修复**必须**带回归测试。
- [ ] 安全相关改动（鉴权、路径解析、MCP）按需扩展 `test_security.py` / `test_files.py` / `test_mcp_gate.py`。
- [ ] 前端视觉改动在 PR 里附 before/after 说明（暂无视觉回归套件）。
- [ ] 零密钥；不向 `.env` / `sessions/` 添加内容。
- [ ] 测试**必须**跑在用完即弃的 archive 上——绝不用真实个人数据。

---

## 7. 范围边界（非目标）

muselab 是一个**个人工作区助理**，不是通用 AI 平台。以下事项在没有明确修宪之前**必须**拒绝：

- 任何形式的构建步骤（违反 P2）。
- 对用户工作区做文档 RAG / 爬取内容的流水线（违反 P5）。
- 超出个人工作区范围的通用聊天 UI 功能（插件市场等）。
- OpenAI-only 协议的 provider（违反 §3 / A3）。
- 默认预置重型 / 仅开发者 / 具写入或交易能力的 MCP server（违反 A6——如 GitHub MCP、DB 写、券商）。
- 任何需要真实个人数据才能测试的功能（违反 P6 / §6）。

---

## 8. 治理

- **本文凌驾于代码、注释与习惯。** 一个与此处不变量冲突的 PR 即属错误，要么改 PR，要么在同一 PR 里修订本文并经评审签字。
- **spec 从本宪法派生。** 每个功能 / 重构**应当**带一份简短 spec（改什么、边界、用 EARS 句式写验收标准：「当 <触发>，系统应当 <行为>」）。spec **禁止**复述不变量——引用即可。
- **修订宪法：** 升版本号（semver——移除 / 重定义不变量为 MAJOR，新增不变量为 MINOR，澄清为 PATCH），更新 *最近修订*，并记录理由。架构变更（§2）**必须**先修订后合并，而非事后。
- **漂移检查：** 当对代码的反推理解与本文冲突时，把冲突当作一个发现——要么修代码，要么带理由修订本文。**绝不**让两者悄悄背离。

---

## 9. 修订记录

- **2.0.0（2026-07-23）：** 将单 archive 模型更新为多工作区；纳入真实终端、短期实时连接 ticket 与 Activity 模块；把模型连续性明确为前端默认 fork、管理 API 显式覆盖；固化 CLAUDE.md／Skills／工具权限的原生指令归属。

---

*spec 告诉 AI 下一步该造什么。本宪法告诉它：无论造什么，什么必须始终为真。*
