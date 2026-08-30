# Skills（技能包）

> [English](skills.md)

Skills 是 Claude Agent SDK 自动发现的 `SKILL.md` 指令包。任务与某个 Skill
的描述匹配时，模型可以加载其中的可复用流程。交互式聊天、
[定时任务](scheduler_zh.md)及其他运行完整 Agent 循环的场景均使用同一机制。

muselab 默认 checkout **不预装任何 Skill payload**。仓库只保留空的
`skills/` 扩展槽，以及通用的发现、列表展示和经审核生成机制。

升级后不会保留原预设名称。已保存的 prompt、定时任务或外部客户端如果显式调用
`archive-curator`、`workspace-curator`、`web-search` 等名称，需要改成直接描述任务，
或由用户／plugin 提供替代 Skill。

## 支持的来源

muselab 保留以下来源的 Skills：

- `~/.claude/skills/` 下的用户全局 Skills；
- 当前活动 workspace 中由 project 和 local scope 发现的 Skills；
- 已安装的 Claude plugins；
- 经用户审核后生成的 Skills；
- 可选的 `<muselab-repo>/skills/` 仓库级扩展。

Settings 与对话区的 Skills 界面动态枚举仓库扩展、用户全局和已安装 plugin Skills。
当前 workspace 的 project／local Skills 仍由 SDK 在运行时原生发现，不进入该管理
列表；两条路径都不依赖固定预设目录。

## 发现机制

muselab 向 `ClaudeAgentOptions` 传入 SDK 原生发现参数：

```python
setting_sources=["user", "project", "local"]
cwd=str(workspace_root)
plugins=[{"type": "local", "path": "<muselab-repo>"}]
skills="all"
```

`cwd` 是当前活动 workspace，因此 project 与 local 配置跟随会话所选工作区。
本地 plugin 让仓库的空 `skills/` 扩展槽保持可用，无需复制或创建符号链接；
用户和 plugin Skills 继续使用 SDK 的正常发现路径。

第三方 Provider 使用隔离的 `CLAUDE_CONFIG_DIR`，防止 Claude OAuth 凭据泄漏
或被错误回退使用。muselab 只把 `~/.claude/skills/` 映射进隔离的用户 scope；
当前 workspace 的 project／local Skills 和显式传入的仓库扩展 plugin 仍可用，
已安装的用户 plugins、settings、hooks、凭据与 transcripts 则继续隔离。因此，
只由已安装用户 plugin 提供的 Skill 无法在隔离的第三方路由中使用。

`GET /api/settings/skills` 为前端只读列表独立枚举仓库扩展、用户全局和已安装
plugin Skills，支持 `SKILL.md` 与 `skill.md` 两种文件名。该列表不包含当前
workspace 的 project／local Skills，也不控制运行时激活。

## 添加 Skill

常用位置如下：

| 位置 | 作用域 |
|---|---|
| `<workspace>/.claude/skills/your-skill/SKILL.md` | 当前 workspace |
| `~/.claude/skills/your-skill/SKILL.md` | 用户全局 |
| `<muselab-repo>/skills/your-skill/SKILL.md` | muselab 仓库扩展 |

最小结构如下：

```yaml
---
name: your-skill
description: "USE WHEN ... — 描述触发条件和能力"
---
```

在 Markdown 正文中写明可复用流程及安全边界，并保持简洁；必要时加入不应触发
的反例，可选脚本或参考资料可放在 `SKILL.md` 同目录。原生安装在添加或编辑后需
重启 muselab，让新的 SDK client 重新发现。Docker 部署会在构建镜像时复制
`skills/`，因此不能只重启服务，需要重新构建并创建容器：

```bash
docker compose up -d --build --force-recreate
```

## 终止开关

完整 Agent 运行时默认启用 Skills。要为 muselab 会话全局关闭，请设置：

```text
MUSELAB_DISABLE_SKILLS=1
```

可接受值为 `1`、`true`、`yes`（不区分大小写）。muselab 随后会向 SDK
显式传入空 Skill 列表（`skills=[]`），避免 SDK 默认值重新启用发现。

*相关文档：[architecture_zh.md](architecture_zh.md) · [routing_zh.md](routing_zh.md) · [providers_zh.md](providers_zh.md)*
