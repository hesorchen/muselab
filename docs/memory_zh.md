# 长期记忆

> [English](memory.md)

MuseLab 的长期记忆是默认关闭的可选能力。它面向长期业务背景、偏好、决策和 Agent
经验，不是“把项目文件全部向量化”的代码库 RAG。

## 架构

```text
CLI JSONL 证据
  → SQLite Registry（真相源、来源、版本、冲突、审核、队列）
  → Episode（跨多轮任务经历）
  → Dreamer（候选事实／决策／反思）
  → Verifier（证据、冲突和未来价值检查）
  → lexical + dense + metadata 混合召回
  → 有界、不可信的聊天上下文
```

一个用户只有一个逻辑记忆池。工作区、业务、主题和实体是软元数据，不是彼此隔绝的
“记忆空间”。向量数据库只保存可重建的检索索引；切换 Embedding 模型或数据库后可
从 Registry 重建。

## 启用条件

在「设置 → 记忆」中配置：

1. 已有 Provider 中的一个生成模型，用于 Dreamer 和 Verifier；
2. OpenAI-compatible Embedding 服务；
3. Qdrant 或 PostgreSQL + pgvector；
4. 可选的 reranker。

保存启用状态前，后端会实际探测三项必需能力。探测失败时不会保存为启用状态。

运行模式：

- `off`：零聊天开销；Registry 中已确认的记忆仍可管理；
- `shadow`：形成 Episode 和候选，但不召回；
- `active`：后台巩固并在聊天前执行混合召回。

召回有 250 ms 默认软截止，任何 Embedding、向量库或 reranker 故障都会 fail-soft，
不会阻断回复。后台 Dreamer、Verifier、索引和 Skill 学习都不在聊天关键路径。

## 白盒与治理

记忆中心展示：

- 记忆内容、类型、权威级别、状态和原始来源；
- Episode、跨会话反思、后台任务和每轮召回记录；
- `pending_review`／`quarantined` 冲突候选；
- Skill 候选的完整结构、证据和风险。

聊天消息旁的脑形按钮是确定性保存操作。它不依赖“记住／更正／忘记”的自然语言
分类器；用户点击后，内容以 `confirmed` 权威级别写入。更正会建立 `supersedes`
关系，忘记会同时使 Registry 条目失效并清理向量索引。

后台 Worker 只能生成 Skill 草稿。草稿保存在 SQLite 中，不在 SDK 可发现目录。
只有带现有 Token 鉴权的用户明确点击“审核并启用”，才会安装到
`~/.claude/skills/muselab-generated-<name>/SKILL.md`。禁用会移出可发现目录并保留
审计记录。

## 反思与价值判定

跨 Episode 反思至少需要配置数量的独立 Episode。独立性按规范化后的证据内容判断，
因此 fork、复制或重复导入的同一批证据不会被当作多份支持。每条候选必须列出来源
Episode；Verifier 再检查证据支持、冲突、过度概括和预测价值。

最终价值不是只听模型自评，而是白盒组合四类信号：Verifier 预测分、独立 Episode
数量、历史召回查询匹配度和相对已有记忆的新颖度。信号和合成分都保存在记忆属性中。
证据不足或冲突的候选进入 `quarantined`，低价值或影子模式候选进入
`pending_review`。失败轮次会形成独立失败 Episode；取消轮次只保留证据，不进入
Dreamer 或 Skill Learner，避免污染之前的成功轨迹。

后台任务使用 SQLite 持久队列。进程在任务中途退出后，遗留的 `running` 任务会在
下次启用 Worker 时重新排队。

## 数据、迁移与恢复

默认数据位于：

```text
$MUSELAB_ROOT/.muselab/memory/
├── config.json       # 0600，包含服务凭据
├── registry.sqlite3  # Registry、FTS、队列和审计
└── disabled-skills/
```

可通过 `MUSELAB_MEMORY_DIR` 改变位置。备份时应包含 SQLite 的 WAL／SHM 文件，或先
停止服务再复制整个目录。

记忆中心支持把旧 Mem0 daemon 的记忆导入为低置信度 `pending_review` 条目。旧数据
没有 MuseLab 原始来源，不能直接升级为确认事实。`GET /api/memory/export` 输出不含
Embedding 的中立 JSON，可用于迁移；向量索引用“重建索引”重新生成。

选择第三方模型时使用其已配置 API key。选择通过 `claude login` 登录的 Claude 时，
后台会新建一个 `tools=[]`、无 MCP／Skill 的一次性 SDK 查询；不会复用活跃聊天
client，也不会获得 Agent 工具能力。

## 异机验收

不需要在开发机启动服务。部署机更新依赖后，可依次执行：

```bash
uv sync
.venv/bin/pytest -q \
  tests/test_memory_store.py tests/test_memory_api.py \
  tests/test_memory_engine.py tests/test_memory_providers.py \
  tests/test_memory_client.py tests/test_frontend_lint.py
node --check frontend/app.js
```

随后在设置中先选 `shadow` 并执行“环境自检”，确认生成模型、Embedding、向量库和
Registry 全部通过；用两段独立会话验证 Episode／反思来源；最后切到 `active`，确认
聊天底部可展开召回轨迹，且断开 Embedding 或向量库时回复仍能正常开始。Skill 候选
必须保持 `pending_review`，直到人工批准；批准后再验证启用和停用均有审计记录。
