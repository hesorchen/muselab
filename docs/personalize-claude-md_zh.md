# 配置工作区 CLAUDE.md

> [English](personalize-claude-md.md)

`CLAUDE.md` 是 Claude Agent SDK 原生支持的可选工作区说明。它适合记录项目目标、事实来源、可修改范围、运行命令和验收方式。muselab 不要求存在这个文件；没有它也可以正常使用。

安装器只配置主工作区、登录 token、端口和模型接入；它不采集个人资料，也不创建预设目录。

## 它如何生效

- 主工作区由 `MUSELAB_ROOT` 指定。
- 其他本地目录可以在界面中登记。
- 每个工作区都可以有自己的 `CLAUDE.md`。
- 新会话以当前工作区作为 `cwd`，SDK 会按自身规则加载该目录及上级作用域中的说明。
- 修改 `CLAUDE.md` 后无需重启服务；下一次对话会读取新内容。

`CLAUDE.md` 应描述长期、可复用的约定。一次性任务仍直接写在聊天里，可复用流程更适合放到 Skill。

## 可选生成器

安装完成后，可以显式运行：

```bash
bash scripts/intake.sh
```

脚本会读取 `.env` 中的 `MUSELAB_ROOT`，询问少量工作区问题，然后从通用模板生成 `CLAUDE.md`。它只写这一个文件，不创建或修改其他目录。已有文件会先备份为 `CLAUDE.md.bak`，且覆盖前需要确认。

模板语言默认跟随 shell locale，也可以指定：

```bash
MUSELAB_LOCALE=zh bash scripts/intake.sh
MUSELAB_LOCALE=en bash scripts/intake.sh
```

通用模板位于：

- `scripts/templates/default-CLAUDE.md`
- `scripts/templates/default-CLAUDE.en.md`

## 推荐内容

一个有效的工作区说明通常只需要四部分：

```markdown
# CLAUDE.md

## 工作区目标
- 这是一个 Python 服务。
- 当前目标是保持 API 兼容并降低请求延迟。

## 事实来源与范围
- 规格以 docs/api.md 和 tests/ 为准。
- 可以修改 backend/ 和 tests/。
- 不要修改 vendor/ 或覆盖本地未提交改动。

## 运行与验证
- 本地运行：uv run python -m backend.main
- 完成前验证：uv run pytest tests/ -q
- 产物目录：docs/reports/

## 协作约定
- 先定位根因，再做最小改动。
- 报告改动范围、验证结果和剩余风险。
```

重点是具体、可执行：

- 写出真实文件和命令，不写“尽量做好”之类空泛要求。
- 区分事实来源、允许修改的范围和禁止触碰的内容。
- 只记录在多数会话中仍成立的规则。
- 不把密码、token、私钥或其他秘密写入 `CLAUDE.md`。

## 多工作区

工作区说明属于目录，不属于 muselab 实例。代码仓库、研究资料和运营数据可以分别登记为工作区，并各自维护不同的 `CLAUDE.md`。切换工作区时，文件树、预览、终端和新会话 `cwd` 会一起切换。

主工作区还承载 `.muselab/` 下的全局状态，因此即使日常任务主要发生在其他目录，也不应随意删除或移动它。完整关系见[配置参考](configuration_zh.md)和[数据与备份](data-and-backup_zh.md)。

## 安全边界

`CLAUDE.md` 是给 Agent 的指令，不是权限沙箱。文件 API 会限制在已登记工作区内，但真实终端以 muselab 服务用户的操作系统权限运行，可能访问工作区之外的路径。只登记你愿意暴露给该实例的目录，并避免把服务直接开放到不可信网络。
