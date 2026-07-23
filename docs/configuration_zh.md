# 配置参考

> [English](configuration.md)

muselab 的配置分成四层。不要把所有状态都理解为 `.env`：

1. **部署环境**：`.env` 和进程环境，决定监听地址、默认工作目录、凭据与资源上限。
2. **应用配置文件**：provider、MCP、工作目录和终端 Profile 等 JSON 文件。
3. **持久化运行状态**：会话、调度、Activity、Push 和生图任务。
4. **浏览器偏好**：语言、布局、打开的 Tab 等本地 UI 状态。

手动修改 `.env` 后应重启服务。设置面板保存的 provider key、默认模型和默认权限会原子更新 `MUSELAB_ENV_PATH` 指向的文件与当前进程，通常无需重启。设置面板不会开放 token、根目录或监听地址。

## 必需与网络设置

| 变量 | 作用 | 默认 |
|---|---|---|
| `MUSELAB_TOKEN` | UI 与 API 的共享鉴权 token，至少 16 字符 | 必填 |
| `MUSELAB_ROOT` | 主工作目录，也是 `.muselab` 全局状态的根目录 | 原生部署必填 |
| `MUSELAB_HOST` | uvicorn 监听接口 | `127.0.0.1` |
| `MUSELAB_PORT` | 监听端口 | `8765` |
| `MUSELAB_URL` | 可选远程客户端使用的公开 HTTPS origin | 本机 origin |
| `MUSELAB_ENV_PATH` | 设置 API 读写的 `.env` 路径；测试或特殊部署使用 | `<repo>/.env` |
| `MUSELAB_MODEL` | 新会话默认模型；未设置时使用内置 Claude 默认值 | `claude-sonnet-4-6` |
| `MUSELAB_DEFAULT_MODEL` | 设置面板保存的默认模型，与 `MUSELAB_MODEL` 同步 | `claude-sonnet-4-6` |
| `MUSELAB_DEFAULT_PERMISSION` | 新会话默认 SDK 权限模式 | `bypassPermissions` |

`MUSELAB_ROOT` 必须存在。`/`、`/etc`、`/root`、`/home`、`/var`、`/usr`、`/boot` 等系统级根路径会被拒绝；用户自己的 home 或其子目录可以使用。

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

设置面板对内置 provider 的修改、自定义 provider 和删除状态保存在 `<repo>/provider_overrides.json`。MCP server 配置保存在 `<repo>/mcp.json`。自定义 provider key 使用 `MUSELAB_PROVIDER_<SLUG>_API_KEY`。

## 生图

| 变量 | 作用 | 默认 |
|---|---|---|
| `MUSELAB_IMAGE_PROVIDER` | `auto`、`openai` 或 `codex_imagegen` | `auto` |
| `OPENAI_IMAGE_API_KEY` | OpenAI Image API key；未设置时可复用 `OPENAI_API_KEY` | 空 |
| `OPENAI_IMAGE_BASE_URL` | OpenAI-compatible `/v1` base URL | `https://api.openai.com/v1` |
| `MUSELAB_IMAGE_GENERATION_TIMEOUT` | API 生图超时秒数 | `180` |
| `CODEX_IMAGEGEN_ENABLED` | 是否允许启动本机 Codex 生图进程 | `false` |
| `CODEX_IMAGEGEN_TIMEOUT_SECONDS` | 本机 Codex 生图超时 | `300` |

本机 Codex 生图只适用于可信实例。持久化任务与图片位于 `$MUSELAB_ROOT/.muselab/imagegen/`。

## 资源与行为调优

| 变量 | 作用 | 默认 |
|---|---|---|
| `MUSELAB_PROMPT_CACHE_TTL` | Claude prompt cache TTL | `1h` |
| `MUSELAB_BUDGET_USD` | 月度 UI 软预算，不会硬中断 | `0` |
| `MUSELAB_MAX_UPLOAD_MB` | Files API 单文件上传上限 MiB | `100` |
| `MUSELAB_MAX_TURNS` | 每会话最大回合数，`0` 表示不额外限制 | `0` |
| `MUSELAB_THINKING_BUDGET` | 扩展思考 token 预算 | `10000` |
| `MUSELAB_CLIENT_POOL_CAP` | 保活 SDK client 数量 | `3` |
| `MUSELAB_RECENT_TURN_TTL` | 已结束回合供重连接回的秒数 | `60` |
| `MUSELAB_STREAM_REPLAY_MAX_EVENTS` | 移动端最大 replay 事件数，超过后 resync | `512` |
| `MUSELAB_STREAM_REPLAY_MAX_BYTES` | 移动端最大 replay 字节数，超过后 resync | `2097152` |
| `MUSELAB_DISABLED_PROVIDERS` | 隐藏的稳定 provider ID，逗号分隔 | 空 |
| `MUSELAB_DISABLE_SKILLS` | 禁用 Skills | `0` |
| `MUSELAB_PRUNE_EMPTY_SESSIONS` | 清理满足严格条件的空会话 | `false` |
| `MUSELAB_TRASH_TTL_DAYS` | 回收站保留天数，`0` 表示永久 | `30` |
| `MUSELAB_VAPID_SUBJECT` | Web Push VAPID subject | `mailto:noreply@muselab.dev` |

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
| `ARCHIVE_DIR` | 挂载到容器 `/data` 的宿主目录 | `./data` |
| `CLAUDE_HOME` | 宿主机 Claude CLI 配置目录 | `${HOME}/.claude` |
| `MUSELAB_BIND` | 宿主机发布端口绑定地址 | `127.0.0.1` |

## 安装期

| 变量 | 作用 | 默认 |
|---|---|---|
| `MUSELAB_NONINTERACTIVE` | 安装脚本采用默认值并跳过交互 | `0` |
| `MUSELAB_LOCALE` | 安装引导与初始模板语言 | 从 `LANG` 自动判断 |
| `MUSELAB_SKIP_SERVICE` | 只安装文件与依赖，不注册或启动 systemd／launchd 服务 | `0` |
| `MUSELAB_NO_BROWSER` | 安装完成后不自动打开浏览器 | `0` |

运行中的语言偏好由浏览器保存；后端模板选择参考 `LANG`、`LC_ALL` 与 `LC_MESSAGES`。

## 对外暴露

`MUSELAB_HOST` 和 Docker 的 `MUSELAB_BIND` 默认仅监听 localhost。改为 `0.0.0.0` 时，必须使用 HTTPS 反向代理、防火墙和独立低权限服务用户。真实终端使 token 泄露的影响远大于只读笔记站点。
