# skills/

Skills are model-discovered capability packs. The Claude Agent SDK loads
`SKILL.md` files from this directory (when `setting_sources` includes
`"local"` and `skills="all"`), and the model picks the relevant skill based
on the task at hand.

skills 是模型可发现的能力包。Claude Agent SDK 启动时（配置了
`setting_sources=["user", "project", "local"]` 和 `skills="all"`，
muselab 默认开启）会加载这个目录里所有 `SKILL.md`，模型按任务自主选用。

## 预置的 7 个 skill

| Skill | 触发时机 | 用途 |
|-------|---------|------|
| **web-search** | 询问时效性事实（汇率/价格/新闻） | 计划查询 → 多源 → 带日期引用的回答 |
| **markdown-formatter** | 让 Muse 清理或重排 markdown | 统一标题层级、列表、表格、Chinese 全角标点 |
| **mermaid-helper** | 画架构图 / 流程图 / 时序图 | 选对图表类型 + 写正确语法 + 校验 |
| **code-reviewer** | 代码审查 | 按 bug→安全→性能→可维护性 优先级输出 |
| **citation-formatter** | 学术引用格式化 | APA / IEEE / GB/T 7714 / BibTeX |
| **task-decomposer** | 模糊目标拆分 | 输出有 DoD / 估时 / 依赖 / 临界路径的任务列表 |
| **summary-distiller** | 长文摘要 | 按源类型选 TL;DR / 关键点 / 行动项 等形态 |

## 添加你自己的 skill

1. 新建子目录：`skills/your-skill/`
2. 写 `SKILL.md`，必须含 frontmatter：
   ```yaml
   ---
   name: your-skill
   description: "USE WHEN ... — 一句话说明触发场景和能力"
   ---
   ```
3. body 写给模型看：何时用 / 怎么用 / 注意事项 / 反例
4. 可选：放参考脚本（`*.py`）或配置（`config.yaml`）在同目录，文档里引用即可

重启 muselab 服务后生效（skills 在 SDK 初始化时加载）。

## 推荐写法

- description 一定以 "USE WHEN ..." 开头——这是模型选择 skill 的主要信号
- body 用 markdown 表格说"什么场景用什么"
- 给反例（"NOT use when ..."）防止滥用
- 不要在 body 里放冗长前言，模型每次都要读

## 与 ~/.claude/skills/ 的关系

- `~/.claude/skills/` 是用户全局 skill（所有项目共享）
- 本目录是 muselab 内置 skill（只对 muselab 生效）
- 同名 skill 时项目级覆盖用户级
