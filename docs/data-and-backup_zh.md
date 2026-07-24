# 数据与备份

> [English](data-and-backup.md)

muselab 没有数据库，但状态并不只在一个目录。完整迁移需要同时考虑工作目录、仓库状态、Claude CLI 数据和浏览器本地偏好。

## 主工作目录

建议完整备份 `$MUSELAB_ROOT/`。除用户文件外，它还包含：

| 路径 | 内容 | 建议 |
|---|---|---|
| `.muselab/workspaces.json` | 已登记工作目录及排序 | 必备；迁移后可能要更新绝对路径 |
| `.muselab/scheduler.json` | 定时任务、历史和未读数 | 使用定时任务时必备 |
| `.muselab/activity.json` | 跨工作目录 Activity Center 状态 | 建议备份 |
| `.muselab/terminal_profiles.json` | 终端 Profile、启动命令和默认项 | 使用 Profile 时必备；可能含敏感命令 |
| `.muselab/vapid.json` | Web Push VAPID 私钥与公钥 | 建议与订阅一起备份 |
| `.muselab/push_subs.json` | 设备 Push 订阅 | 想保留现有订阅时与 `vapid.json` 一起备份 |
| `.muselab/imagegen/` | 生图任务历史和持久化图片 | 想保留生图历史时备份 |
| `.muselab-attach/` | 会话图片和 PDF 原文件 | 想保留附件预览时必备 |
| `.muselab-dustbin/` | 主工作目录可恢复回收站 | 想保留恢复能力时备份 |

删除 `vapid.json` 会生成新 keypair，已有浏览器订阅随即失效。只恢复 `push_subs.json` 而不恢复匹配的 VAPID key 没有意义。

## 额外工作目录

每个已登记工作目录都应按需求备份：

- 用户文件；
- 该目录自己的 `.muselab-dustbin/`；
- 对应的 CLI JSONL 不在工作目录本身：Claude 位于 `~/.claude/projects/`，
  第三方 Provider 位于隔离的持久状态根。

全局状态仍只放在主 `MUSELAB_ROOT/.muselab/`，不会复制到每个工作目录。

## 仓库状态

| 路径 | 内容 |
|---|---|
| `<repo>/.env` | token、provider key 和部署配置，包含秘密 |
| `<repo>/sessions/` | 会话索引、sidecar、队列、活动回合哨兵和派生索引 |
| `<repo>/mcp.json` | MCP server 配置，可能包含凭据 |
| `<repo>/provider_overrides.json` | 内置 provider 修改和自定义 provider |

代码、`.venv/`、依赖缓存、构建产物和日志可从版本库或安装器恢复，不必作为数据备份。

## Claude CLI 数据

| 路径 | 内容 |
|---|---|
| `~/.claude/projects/<cwd-key>/*.jsonl` | 各工作目录的真实对话记录 |
| `~/.claude/.credentials.json` | Claude Pro/Max OAuth 登录 |
| `~/.claude/` 其他配置 | 用户级 CLAUDE.md、Skills、权限和 CLI 偏好 |
| `${XDG_STATE_HOME:-~/.local/state}/muselab/vendor-cli/` | 第三方 Provider 的隔离 transcript、任务和 CLI 状态 |

只使用 Claude 时，最简单的做法是安全备份整个 `~/.claude/`。使用第三方 Provider
时还必须备份对应的 `vendor-cli/` 状态目录，否则这些会话的正文不会包含在
`~/.claude/` 备份中。如果不迁移凭据，可以在新机器重新执行 `claude login`。

升级后的首次启动会把旧
`<系统临时目录>/muselab-vendor-cli-config-<uid>/` 自动迁移到持久目录。已有的
持久文件不会被覆盖；若存在同路径冲突，旧文件会保存在
`vendor-cli/.migration-conflicts/` 供手动恢复。唯一例外是滚动重启时旧进程补写的
有效、更新 JSONL 尾部；其中不重复的记录会安全追加到持久副本。

## 不持久化或无需迁移

| 状态 | 原因 |
|---|---|
| 正在运行或已退出的终端会话 | 只存在于当前后端进程；Profile 才是持久化配置 |
| SSE replay spool | 操作系统临时文件，只服务当前进程的断线重连 |
| 尚未发送的附件暂存 | 仅在内存中，TTL 为 10 分钟 |
| SDK client、限速桶和内存缓存 | 启动后自动重建 |
| 浏览器打开的 Tab、布局和部分偏好 | 存在浏览器 localStorage，需要单独迁移浏览器数据 |

## 恢复步骤

1. 在新机器安装同版本或更新版本的 muselab。
2. 停止服务。
3. 恢复主工作目录、需要的额外工作目录、仓库状态、`~/.claude/`，以及使用第三方 Provider 时的隔离 transcript。
4. 检查 `.env` 中的 `MUSELAB_ROOT`，并在工作目录选择器里更新失效的绝对路径。
5. 检查文件所有者和权限，特别是 `.env`、Claude 凭据、VAPID key 与终端 Profile。
6. 启动服务，依次验证工作目录、历史会话、附件、定时任务、终端 Profile、生图历史和 Push。
7. 运行 `bash scripts/doctor.sh` 做基础自检。

备份中包含 token、API key、OAuth 凭据、Push 私钥，终端 Profile 还可能包含用户写入的命令。应加密保存，切勿提交到 Git 或共享盘。
