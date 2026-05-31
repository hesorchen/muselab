# 升级

> [English](upgrade.md)

muselab 依赖两个快速演进的上游组件 —— Claude Agent SDK 和 `claude` CLI。`scripts/upgrade.sh` 会同时升级两者、对结果做冒烟测试，并且不动你的数据。

## 步骤

```bash
cd ~/muselab            # 你的仓库
git pull                # 拉取最新 muselab 代码
bash scripts/upgrade.sh
```

它做的事：

1. 升级 Python 的 `claude-agent-sdk`（`uv lock --upgrade-package …` 后 `uv sync --frozen`）。
2. 升级 `claude` CLI（`npm install -g @anthropic-ai/claude-code@latest`）。
3. 跑测试套件（`uv run pytest tests/ -q`）作为冒烟测试。

若测试**失败**，脚本中止并回滚 Python 依赖（`git checkout uv.lock pyproject.toml && uv sync`）—— 多数情况意味着新版 SDK 改了 muselab 依赖的 API。查看打印的日志并提 issue。

## 升级之后

脚本**不会**重启服务，也不会提交 lockfile 改动 —— 它会打印出确切命令。重启以让新的 SDK/CLI 生效：

```bash
# Linux（systemd --user）
systemctl --user restart muselab

# macOS（launchd）
launchctl kickstart -k gui/$UID/com.muselab
```

若你的仓库纳入了 git，检查并提交依赖升级：

```bash
git diff uv.lock
git add uv.lock pyproject.toml && git commit -m "chore: bump claude-agent-sdk"
```

## 保留的内容

升级永远不动 `.env`、`sessions/` 或你的归档。被锁定的 `claude` CLI 版本在 `scripts/versions.env`（并在 Dockerfile 里镜像一份）；`upgrade.sh` 会把你带到最新。没有 schema 迁移步骤 —— JSON 状态文件向前兼容，少数确实存在的迁移（如 VAPID 密钥格式）在后端启动时自动执行。

## Docker

拉新镜像并重建容器：

```bash
docker compose pull && docker compose up -d
```

你的归档和 `.env` 是 bind-mount 挂载的，重建后依然保留。
