# Skills（技能包）

> [English](skills.md)

Skills 是 SKILL.md 指令包，由 Claude Agent SDK 在启动时加载并提供给 Muse 使用。当任务与某个 skill 的触发条件匹配时，模型会读取该 skill 的正文并遵循其协议——你无需在端上做任何额外配置。Skills 在交互式聊天、[定时任务](scheduler_zh.md)以及其他运行完整 agent 循环的上下文中均以相同方式工作。

**示例。** 一个名为 `changelog-formatter` 的 skill，其 `description` 字段可能以 `"USE WHEN the user asks to format or generate a CHANGELOG entry"` 开头。每当你让 Muse 编写 changelog 时，SDK 就会浮现该 skill，模型将自动采用其输出规范。

---

## 内置 Skills

Muselab 开箱即附 12 个 skill。前八个是 muselab 原生 skill（MIT 许可）；后四个来自社区贡献并注明了出处——上游 URL 和许可证详情见 `THIRD_PARTY_LICENSES.md`。

| Skill | 功能 | 来源 | 外部依赖 |
|---|---|---|---|
| `web-search` | 将模糊查询转化为精准搜索，至少打开一个来源确认时效性，返回带日期的引用答案 | muselab 原生 | `WebSearch` / `WebFetch` 工具或 `mcp__fetch__fetch` |
| `markdown-formatter` | 规范化标题层级、列表、表格、代码围栏、数学分隔符以及中文全角标点；仅返回改写后的文档 | muselab 原生 | 无 |
| `mermaid-helper` | 选择合适的 Mermaid 图表类型，编写经验证的语法，返回带简短说明的围栏代码块 | muselab 原生 | 无 |
| `code-reviewer` | 按严重程度顺序（Bug → 安全 → 正确性 → 性能 → 可维护性）审查代码，提供行号引用和修复片段 | muselab 原生 | 无 |
| `citation-formatter` | 将 DOI、arXiv ID、PubMed ID 和原始文本转换为 APA 7 / IEEE / GB/T 7714 / BibTeX 格式；尽可能获取权威元数据 | muselab 原生 | `WebFetch` 或 `mcp__fetch__fetch`（可选）|
| `task-decomposer` | 将模糊目标拆解为有序任务列表，附带规模估算、完成标准、关键路径步骤和已标记的未知项 | muselab 原生 | 无 |
| `summary-distiller` | 根据来源类型选择合适的摘要形式（TL;DR、要点、结构化、行动项）；逐字保留数字、人名和日期 | muselab 原生 | 无 |
| `archive-curator` | 扫描并整理个人 archive，文件变更前先提案确认，并以对话方式补全有意义的 `CLAUDE.md` 空白 | muselab 原生 | 无 |
| `pptx` | 通过 Bash 工具编写并运行内联 Python（`python-pptx`）生成 PowerPoint 文件 | 社区 | `python-pptx`（`pip install python-pptx`）|
| `csv-analyzer` | 用 `pandas` 加载 CSV，分析列类型，生成条件图表（PNG），在单次响应中输出完整分析 | 社区 | `pandas`；`matplotlib` / `seaborn` 可选 |
| `translate` | 三阶段内部流水线（直译 → 问题识别 → 润色再诠释）；仅输出最终中文文本，保留技术术语 | 社区 | 无 |
| `meeting-notes` | 使用四个预置模板，从原始笔记或会议记录中提取决策、行动项（含负责人和截止日期）及后续步骤 | 社区 | 无 |

---

## 发现机制

Skill 发现由传给 `ClaudeAgentOptions` 的 SDK 原生参数控制：

**`setting_sources`：**

```python
setting_sources=["user", "project", "local"]
```

该配置告诉 SDK 从三个作用域加载 `CLAUDE.md` 与 Claude 配置：

| 作用域 | 解析路径 |
|---|---|
| `user` | `~/.claude/`——与 Claude Code CLI 共享的用户全局配置 |
| `project` | 归档根目录 `cwd`（见下文）|
| `local` | `cwd` 内的 `.claude/` |

**`cwd` 即当前活动工作区：**

```python
cwd=str(workspace_root)
```

由于活动工作区并不是 muselab checkout，muselab 还会把仓库作为本地 SDK plugin 传入：

```python
plugins=[{"type": "local", "path": "<muselab-repo>"}]
```

这个 plugin 让内置 `skills/` 在每个工作区都可发现。`pptx` 或 `csv-analyzer` 等 skill 产生的输出文件，若未指定路径，仍落在当前活动工作区。

**`skills="all"`：**

```python
if not skills_off:
    opts_kwargs["skills"] = "all"
```

设置该标志后，SDK 会为所有 provider 加载可发现的 `SKILL.md`。无需复制或创建符号链接——内置 `skills/` 通过本地 plugin 直接提供。

第三方 Provider 仍使用隔离的 `CLAUDE_CONFIG_DIR`，防止 Claude OAuth 凭据泄漏或被错误回退使用。muselab 只把 `~/.claude/skills/` 映射进隔离目录，因此用户 Skills 与 Claude 模型保持一致，而凭据、settings、hooks、plugins 和会话记录仍然隔离。

**UI 列表。** `GET /api/settings/skills` 独立地为前端枚举仓库内置、
用户全局和已安装 plugin 中的 skill。`SKILL.md` 和 `skill.md` 两种文件名
均被接受。该列表为只读，不影响模型在运行时实际使用的内容。

---

## 添加自定义 Skill

### 存放位置

| 位置 | 作用域 | 对谁可见 |
|---|---|---|
| `<muselab-repo>/skills/your-skill/SKILL.md` | project | 仅 muselab |
| `~/.claude/skills/your-skill/SKILL.md` | user | muselab + 所有 Claude Code 项目 |

仓库 skill 在 SDK 内部带 plugin 命名空间，因此可与同短名的用户全局 skill 共存。

### 必需结构

```
skills/your-skill/
└── SKILL.md          ← 必须包含 YAML frontmatter
```

frontmatter 块至少须包含 `name` 和 `description`：

```yaml
---
name: your-skill
description: "USE WHEN ... — 一句话描述触发条件和功能"
---
```

正文是自由格式的 Markdown，模型每次调用时都会读取——保持简洁。推荐实践见 `skills/README.md`：

- `description` 以 `"USE WHEN ..."` 开头——这是模型选择 skill 时最主要的信号。
- 用表格将场景映射到动作。
- 添加 `NOT use when` 节以防止过度触发。
- 可选：在同一子目录中放置参考脚本（`*.py`）或配置文件（`config.yaml`），并在 SKILL.md 正文中引用。

### 需要重启

Skills 在 SDK 初始化期间加载。添加或编辑 skill 后，须重启 muselab 服务：

**Linux（systemd）：**
```bash
systemctl --user restart muselab
```

**macOS（launchd）：**
```bash
launchctl kickstart -k "gui/$(id -u)/com.muselab"
```

---

## 终止开关

Skills 默认对所有 provider 开启。若要全局禁用 skill，请在 `.env` 中设置：

```
MUSELAB_DISABLE_SKILLS=1
```

可接受的值：`1`、`true`、`yes`（不区分大小写）。

---

*相关文档：[architecture_zh.md](architecture_zh.md) · [routing_zh.md](routing_zh.md) · [providers_zh.md](providers_zh.md)*
