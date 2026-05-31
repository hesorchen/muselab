# 数据与备份

> [English](data-and-backup.md)

muselab 没有数据库 —— 所有状态都是分布在三处的纯文件：

1. **归档**（`MUSELAB_ROOT`）—— 你自己的文件，
2. **仓库** —— 配置与会话元数据，
3. **`~/.claude/`** —— Claude CLI 的对话记录与登录凭证。

要把一个 muselab 实例迁到新机器，复制下面三组"必须备份"即可。其余的会自行重建。

## 需要备份的

| 路径 | 内容 | 为何重要 |
|---|---|---|
| `$MUSELAB_ROOT/` | 你的归档 —— 你放进去的每个文件 | 这就是你的数据 |
| `$MUSELAB_ROOT/.muselab/scheduler.json` | 定时任务 + 运行历史 | 丢了就得重建所有定时任务 |
| `<repo>/.env` | 全部配置**含密钥**（token + provider key） | 含凭证 —— 安全备份，切勿提交 |
| `<repo>/sessions/` | 会话索引、每条消息的 sidecar（成本、模型标识、上传附件）、待发队列 | muselab 专属元数据，不在 CLI 记录里 |
| `<repo>/mcp.json` | MCP server 配置 | 仅当你配了 MCP |
| `<repo>/provider_overrides.json` | 对内置 provider 的修改 + 自定义 provider | 仅当你定制过 provider |
| `~/.claude/projects/<cwd-key>/*.jsonl` | **实际的对话记录** | 真正的聊天历史 —— 归 CLI 所有 |
| `~/.claude/.credentials.json` | Claude Pro/Max OAuth 登录 | 不备份就重新跑一次 `claude login` |

> `.env` 和 `~/.claude/.credentials.json` 含密钥。备份到私密位置；不要放进 git 仓库或共享盘。

## 不需要备份的

这些会自动重建：

| 路径 | 说明 |
|---|---|
| `$MUSELAB_ROOT/.muselab/vapid.json` | Web-push 密钥对 —— 会重建，但删掉会强制所有设备重新订阅 |
| `$MUSELAB_ROOT/.muselab/push_subs.json` | 推送订阅 —— 设备会自行重新订阅 |
| `$MUSELAB_ROOT/.muselab-dustbin/` | 软删除回收站，超过 `MUSELAB_TRASH_TTL_DAYS` 自动清理 |
| `/tmp/muselab-vendor-cli-config-*` | 第三方 provider 的临时隔离 CLI 配置 |
| `<repo>/.venv/`、缓存、日志 | 由 `uv sync` / 运行时重建 |

## 在新机器上恢复

1. 正常安装 muselab（见 [快速入门](quickstart_zh.md)）。
2. 停掉服务。
3. 恢复 `$MUSELAB_ROOT/`（含其 `.muselab/scheduler.json`）、仓库的 `.env` / `sessions/` / `mcp.json` / `provider_overrides.json`，以及 `~/.claude/`。
4. 确认恢复后的 `.env` 里 `MUSELAB_ROOT` 指向归档的新位置。
5. 启动服务。会话、定时任务、历史都会原样回来。

恢复后快速自检：`bash scripts/doctor.sh`。
