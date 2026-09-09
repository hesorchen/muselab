# 配置参考

> [English](configuration.md)

muselab 的配置分成四层。不要把所有状态都理解为 `.env`：

1. **部署环境**：`.env` 和进程环境，决定监听地址、默认工作目录、凭据与资源上限。
2. **应用配置文件**：provider、MCP、工作目录和终端 Profile 等 JSON 文件。
3. **持久化运行状态**：会话、调度、Activity、Push 和生图任务。
4. **浏览器偏好**：语言、布局、打开的 Tab 等本地 UI 状态。

手动修改 `.env` 后应重启服务。设置面板保存的 provider key、默认模型、默认权限和忙时发送方式会原子更新 `MUSELAB_ENV_PATH` 指向的文件与当前进程，通常无需重启。设置面板不会开放 token、根目录或监听地址。

## 必需与网络设置

| 变量 | 作用 | 默认 |
|---|---|---|
| `MUSELAB_TOKEN` | UI 与 API 的共享鉴权 token，至少 16 字符 | 必填 |
| `MUSELAB_ROOT` | 主工作区，也是 `.muselab` 全局状态的根目录 | 原生部署必填 |
| `MUSELAB_HOST` | uvicorn 监听接口 | `127.0.0.1` |
| `MUSELAB_PORT` | 监听端口 | `8765` |
| `MUSELAB_URL` | 可选远程客户端使用的公开 HTTPS origin | 本机 origin |
| `MUSELAB_CONFIG_DIR` | 可写 `.env`、MCP 与 provider 配置目录；启动前通过进程环境设置 | `<repo>`；Docker 为 `/app/sessions/config` |
| `MUSELAB_ENV_PATH` | 启动读取、设置 API 写入的显式 `.env` 路径；通过进程环境设置 | `$MUSELAB_CONFIG_DIR/.env` |
| `MUSELAB_ENV_OVERRIDE` | 启动时配置文件是否覆盖已有进程变量 | `0`；Docker 为 `1`，保证网页配置在重建后生效 |
| `MUSELAB_SESSIONS_DIR` | 持久化会话元数据目录 | `<repo>/sessions` |
| `MUSELAB_MODEL` | 新会话默认模型；未设置时使用内置 Claude 默认值 | `claude-sonnet-4-6` |
| `MUSELAB_DEFAULT_MODEL` | 设置面板保存的默认模型，与 `MUSELAB_MODEL` 同步 | `claude-sonnet-4-6` |
| `MUSELAB_DEFAULT_PERMISSION` | 新会话默认 SDK 权限模式 | `bypassPermissions` |
| `MUSELAB_BUSY_SEND_MODE` | 会话忙碌时的发送方式：`adjust` 在下一个安全的工具边界调整当前任务；`queue` 等当前 turn 完成后再执行 | `adjust` |
| `MUSELAB_MEMORY_DIR` | 可选的长期记忆 Registry／配置目录 | `$MUSELAB_ROOT/.muselab/memory` |

`MUSELAB_ROOT` 必须存在。`/`、`/etc`、`/root`、`/home`、`/var`、`/usr`、`/boot` 等系统级根路径会被拒绝；用户自己的 home 或其子目录可以使用。旧版本文档曾称它为 archive root；环境变量名为兼容已有部署而保留，当前产品概念统一为“主工作区”。

`MUSELAB_SESSIONS_DIR` 保存 muselab 的会话索引、sidecar、队列与重启恢复哨兵，
不保存 CLI transcript 正文。原生安装脚本会把当前 checkout 的绝对
`<repo>/sessions` 路径写入 `.env`；未设置时仍回退到仓库内默认目录，以兼容旧部署。
Docker 额外设置 `XDG_STATE_HOME=/app/sessions/state`，将第三方 CLI 正文放入同一个持久挂载，
可编辑配置位于 `config/` 子目录。替换旧容器前先阅读[旧状态迁移](docker-state-migration_zh.md)。

## 多工作区

主目录之外的工作目录通过 UI 注册，保存在：

```text
$MUSELAB_ROOT/.muselab/workspaces.json
```

切换工作目录会同步切换文件树、预览、终端初始目录和新会话 `cwd`。会话历史会扫描所有已登记目录。注册目录并不创建操作系统隔离，只应登记愿意交给服务用户和 Web UI 的路径。

## Provider

Claude 可使用 `claude login` 或 `ANTHROPIC_API_KEY`。内置 Anthropic-compatible provider 使用以下 key：

| Provider | Key | 可选 endpoint 覆盖 |
|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | `DEEPSEEK_BASE_URL` |
| 智谱 GLM／内置 OpenAI 分组 | `ZHIPUAI_API_KEY` | `ZHIPUAI_BASE_URL` |
| MiniMax 国内 | `MINIMAX_API_KEY` | `MINIMAX_BASE_URL` |
| MiniMax 国际 | `MINIMAX_INTL_API_KEY` | provider 配置 |
| Kimi | `MOONSHOT_API_KEY` | `MOONSHOT_BASE_URL` |
| Qwen 国内 | `DASHSCOPE_API_KEY` | `DASHSCOPE_BASE_URL` |
| Qwen 国际 | `DASHSCOPE_API_KEY` | `DASHSCOPE_INTL_BASE_URL` |
| Xiaomi MiMo | `XIAOMI_MIMO_API_KEY` | `XIAOMI_MIMO_BASE_URL` |
| 百度千帆 | `QIANFAN_API_KEY` | `QIANFAN_BASE_URL` |
| Codex Gateway | `CODEX_GATEWAY_API_KEY` | `CODEX_GATEWAY_BASE_URL` |

MiniMax 国内与国际 key 不通用；Qwen 两个区域分组共用 key、使用不同 endpoint。模型与分组会随版本变化，设置面板和 `/api/chat/providers` 是当前事实来源。

设置面板对内置 provider 的修改、自定义 provider 和删除状态保存在 `$MUSELAB_CONFIG_DIR/provider_overrides.json`。MCP server 配置保存在 `$MUSELAB_CONFIG_DIR/mcp.json`，原生安装默认仍位于仓库内。自定义 provider key 使用 `MUSELAB_PROVIDER_<SLUG>_API_KEY`。

## 长期记忆

长期记忆默认关闭，结构化配置保存在
`$MUSELAB_MEMORY_DIR/config.json`（默认 `$MUSELAB_ROOT/.muselab/memory/config.json`）。
Embedding、向量库和 reranker 凭据不写入 `.env`，设置页读取时也不会回传明文。
完整配置、运行模式和备份要求见[长期记忆](memory_zh.md)。

## 生图

| 变量 | 作用 | 默认 |
|---|---|---|
| `OPENAI_IMAGE_API_KEY` | Images API key；未设置时可复用 `OPENAI_API_KEY` | 空 |
| `OPENAI_IMAGE_BASE_URL` | OpenAI-compatible `/v1` base URL，包括自托管适配器 | `https://api.openai.com/v1` |
| `MUSELAB_IMAGE_GENERATION_TIMEOUT` | API 生图超时秒数 | `180` |

muselab 只作为 Images API 客户端，不再启动本地生图模型或 Codex 进程。持久化任务与图片位于 `$MUSELAB_ROOT/.muselab/imagegen/`。

## 资源与行为调优

| 变量 | 作用 | 默认 |
|---|---|---|
| `MUSELAB_PROMPT_CACHE_TTL` | Claude prompt cache TTL | `1h` |
| `MUSELAB_BUDGET_USD` | 月度 UI 软预算，不会硬中断 | `0` |
| `MUSELAB_PERF_LOG` | 输出核心链路的隐私受限性能摘要；设为 `0` 可关闭 | `1` |
| `MUSELAB_SLOW_REQUEST_MS` | 慢 HTTP 请求和工作区操作的耗时阈值，限制在 25–60000 毫秒 | `500` |
| `MUSELAB_MAX_UPLOAD_MB` | Files API 单文件上传上限 MiB | `1024` |
| `MUSELAB_MAX_TURNS` | 每会话最大回合数，`0` 表示不额外限制 | `0` |
| `MUSELAB_THINKING_BUDGET` | 扩展思考 token 预算 | `10000` |
| `MUSELAB_RUNTIME_BUFFER_EVENTS` | 每个回合／后台 SDK 交接队列的消息数上限，最小 1 | `8192` |
| `MUSELAB_RUNTIME_BUFFER_BYTES` | 每个 SDK 交接队列的对象内存估算字节上限，最小 1024 | `67108864` |
| `MUSELAB_CLIENT_POOL_CAP` | 保活 SDK client 数量 | `3` |
| `MUSELAB_RECENT_TURN_TTL` | 已结束回合供重连接回的秒数 | `60` |
| `MUSELAB_STREAM_REPLAY_MAX_EVENTS` | 移动端最大 replay 事件数，超过后 resync | `512` |
| `MUSELAB_STREAM_REPLAY_MAX_BYTES` | 移动端最大 replay 字节数，超过后 resync | `2097152` |
| `MUSELAB_DISABLED_PROVIDERS` | 隐藏的稳定 provider ID，逗号分隔 | 空 |
| `MUSELAB_DISABLE_SKILLS` | 禁用 Skills | `0` |
| `MUSELAB_PRUNE_EMPTY_SESSIONS` | 清理满足严格条件的空会话 | `false` |
| `MUSELAB_TRASH_TTL_DAYS` | 回收站保留天数，`0` 表示永久 | `30` |
| `MUSELAB_VAPID_SUBJECT` | Web Push VAPID subject | `mailto:noreply@muselab.dev` |

SDK 交接预算位于浏览器 SSE 回放之前，孤立消息暂存另有 512 条上限。
单条超大消息或累计积压超限时，会明确返回 `runtime_buffer_exceeded`，中断并断开所属的精确 SDK client，
进入既有失败回合与历史恢复流程；不会静默淘汰消息，也不会让唯一 SDK reader 等待队列空间。
这里限制的是 Python 对象内存估算值，不是 token 或上传大小。修改后需重启服务。

服务面板从 `GET /api/settings/service` 的 `diagnostics.runtime_buffers` 读取纯数字指标：
当前队列深度／估算字节、最老等待、已观测的单队列深度／字节峰值与出队等待峰值、进程累计溢出次数。
日志不记录提示词、工具结果、路径或协议载荷。
MCP 状态／重连接口采用统一三秒观察截止时间，最多并发四个控制请求；未完成项明确标记为 `pending`。
后续完成超时仍由 SDK 负责，重复请求共享正在执行的操作，避免取消泄漏或重复重连。

VAPID keypair 不是环境变量，会自动生成在 `$MUSELAB_ROOT/.muselab/vapid.json`。

## 真实终端

| 变量 | 作用 | 默认 |
|---|---|---|
| `MUSELAB_TERMINAL_ENABLED` | 启用真实 PTY 终端 | `1` |
| `MUSELAB_TERMINAL_SHELL` | shell 可执行文件 | `$SHELL`，再回退 bash/zsh/sh |
| `MUSELAB_TERMINAL_MAX_SESSIONS` | 同时保留的终端上限，范围 1–32 | `8` |
| `MUSELAB_TERMINAL_BUFFER_BYTES` | 每终端断线回放上限，范围 64 KiB–16 MiB | `2097152` |
| `MUSELAB_TERMINAL_DETACHED_TTL` | 运行中且无人连接的终端回收秒数 | `1800` |
| `MUSELAB_TERMINAL_EXITED_TTL` | 已退出终端保留在列表的秒数 | `3600` |

终端以服务用户真实权限运行，初始目录是当前工作目录，不是文件 API 沙箱。shell 环境只保留基本系统变量，不包含 muselab token 或 provider key。

Profile 保存在 `$MUSELAB_ROOT/.muselab/terminal_profiles.json`，在所有工作目录间共享。`profile_id` 未指定时使用默认 Profile；显式空字符串表示启动纯 shell。命令会在交互式 shell 启动后自动执行，不应在命令中保存密码或 API key。

## Docker Compose

以下变量由 `docker-compose.yml` 使用，不是后端业务配置：

| 变量 | 作用 | 默认 |
|---|---|---|
| `ARCHIVE_DIR` | 挂载到容器 `/data` 的宿主机工作区；名称仅为 Docker Compose 向后兼容保留 | `./data` |
| `CLAUDE_HOME` | 宿主机 Claude CLI 配置目录 | `${HOME}/.claude` |
| `MUSELAB_BIND` | 宿主机发布端口绑定地址 | `127.0.0.1` |

## 安装期

| 变量 | 作用 | 默认 |
|---|---|---|
| `MUSELAB_NONINTERACTIVE` | 安装脚本采用默认值并跳过交互 | `0` |
| `MUSELAB_LOCALE` | 可选 `scripts/intake.sh` 生成工作区模板时使用的语言 | 从 `LANG` 自动判断 |
| `MUSELAB_SKIP_SERVICE` | 只安装文件与依赖，不注册或启动 systemd／launchd 服务 | `0` |
| `MUSELAB_NO_BROWSER` | 安装完成后不自动打开浏览器 | `0` |

运行中的语言偏好由浏览器保存；后端模板选择参考 `LANG`、`LC_ALL` 与 `LC_MESSAGES`。

## 对外暴露

`MUSELAB_HOST` 和 Docker 的 `MUSELAB_BIND` 默认仅监听 localhost。改为 `0.0.0.0` 时，必须使用 HTTPS 反向代理、防火墙和独立低权限服务用户。真实终端使 token 泄露的影响远大于只读笔记站点。

## 额外的索引排除目录

`MUSELAB_IGNORED_SUBTREES` 用逗号分隔的目录名扩展内置排除列表；`MUSELAB_IGNORED_SUBTREE_PREFIXES` 用逗号分隔的目录名前缀排除整类目录。匹配区分大小写，在任意层级生效，忽略空项和首尾空格。例如 `MUSELAB_IGNORED_SUBTREES=bulk,generated` 和 `MUSELAB_IGNORED_SUBTREE_PREFIXES=snapshot.,backup.`。这些目录的后代不会进入递归索引或原生监听，目录本身可能仍作为不展开的条目显示。

修改后重启服务；下一次完整对账会移除已索引的后代。取消排除后，对账会恢复这些条目。配置不会删除源文件，也不保证 SQLite 文件体积缩小；数据库压缩属于独立维护操作。避免使用会匹配正常工作文档目录的宽泛前缀。

## 设置中的上下文预算

设置 → 模型 Provider → 上下文窗口预算，可设置 Provider 默认值（包括 DUCC），或覆盖单个模型。单模型优先；留空并保存可删除该覆盖值。配置以 `MUSELAB_CONTEXT_LIMITS` JSON 写入当前 `.env`，后续预算计算无需重启即可生效。已有明确的环境变量覆盖仍优先；未覆盖时继续采用运行时、模型目录和静态回退。

预算影响 MuseLab 用量显示及发送前检查、压缩判断，不会扩大上游模型实际容量。DUCC 是独立 CLI 路由：自动模式采用 SDK 返回的窗口，未匹配的带前缀模型 ID 回退为 128000 tokens，不能借用同名直连 API 模型的容量。用量提示会区分模型设置、Provider 设置、SDK、模型目录和估算回退等来源。

记忆重建按用户、记忆语义快照及配置版本复用尚未结束的操作，重启后仍有效。相同 Episode 集合与配置版本的 Dream 请求复用排队或运行中的任务。内容或配置变化会创建不同请求；全部批次终结后允许用户重新提交。任务诊断可查看当前用户最近的任务、重试次数、安全失败分类、待索引数量和最近重建批次进度，点击刷新状态获取最新结果，不展示任务正文或原始异常。
