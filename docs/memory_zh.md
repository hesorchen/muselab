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

通过界面保存启用状态时，默认会探测三项必需能力。探测失败时不会保存为启用状态。

运行模式：

- `off`：零聊天开销；Registry 中已确认的记忆仍可管理；
- `shadow`：形成 Episode 和候选，但不召回；
- `active`：后台巩固并在聊天前执行混合召回。

界面只提供一个“召回超时时间（秒）”：`0` 表示无超时，也是新配置的默认值；正数限制整个召回流程。已有配置仍保留 `retrieval.soft_timeout_ms` 中保存的值。近期上下文、向量与关键词检索、候选补全、已启用的重排共用这一时间，不另设更短的内部时限。召回在 SDK 提交查询前完成，结果由 `UserPromptSubmit.additionalContext` 一次性注入，因此不会被 SDK Hook 的计时器提前丢弃；等待期间仍可点击停止。关键日志记录阶段开始、耗时、结果数量，并区分超时与取消，不记录查询或记忆正文。检索或重排发生故障、超时时，尽力保留可用结果，必要时不注入记忆并继续回复。Dreamer、Verifier、索引和 Skill 学习在后台运行。

## 做梦机制与混合召回

做梦机制负责整理任务经历。会话达到配置的轮次阈值或空闲时限后，Episode 会进入后台整理队列；也可以通过“立即做梦”提交整理任务。Dreamer 从经历中提炼候选事实、决策和反思，Verifier 检查证据、冲突与后续价值。自动整理需要启用记忆并运行 Worker；生成的记忆是推断记录，不等于用户确认的事实。

主动模式下，混合召回融合关键词检索与语义向量检索，再结合记忆的权威级别、置信度和使用反馈排序，可选启用 reranker。检索对象是 Registry 中可用的记忆，不会自动索引整个工作区。项目中的记忆文件属于普通项目材料，与这套跨会话记忆机制不同。

## 白盒与治理

记忆中心支持搜索、排序、分页浏览，以及人工确认、更正和删除。可查看：

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

## 现场证据回溯

记忆来源可以关联 Episode、会话消息和工具调用记录。通过来源入口可返回相关会话；显式消息来源包含有效消息标识时，还可定位到对应消息。Episode 或工具证据来源通常定位到会话，记录缺失或没有来源的条目可能无法回溯。

“复制现场证据”复制的是会话证据定位信息，包括会话、工作区和原始记录路径，而不是完整对话正文，也不会重建当时的执行环境。定位信息可能包含私有路径与标识，公开分享前应检查并脱敏。

## 反思与价值判定

跨 Episode 反思至少需要配置数量的独立 Episode。独立性按规范化后的证据内容判断，
因此 fork、复制或重复导入的同一批证据不会被当作多份支持。每条候选必须列出来源
Episode；Verifier 再检查证据支持、冲突、过度概括和预测价值。

最终价值不是只听模型自评，而是白盒组合四类信号：Verifier 预测分、独立 Episode
数量、历史召回查询匹配度和相对已有记忆的新颖度。信号和合成分都保存在记忆属性中。
Verifier 会拒绝证据不足或存在冲突的候选，不一定留下可审核的记忆条目。通过校验但价值较低或处于影子模式的候选进入 `pending_review`。失败轮次会形成独立失败 Episode；取消轮次只保留证据，不进入 Dreamer 或 Skill Learner，避免污染之前的成功轨迹。

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

### 召回时限与诊断

召回使用独立的 SQLite actor，以只读连接访问 WAL 数据库；后台巩固、
任务记账和召回遥测继续使用写 actor。召回读不会初始化或迁移数据库。

时限从召回入口开始，覆盖近期证据、稠密与关键词检索、正文读取及可选重排。
`0` 不设召回时限；正数约束所有阶段的总耗时，不再预留内部子预算或另设 facade 时限。
每个检索通道完成后立即读取对应正文；重排失败时保留已读取的记忆，标记为部分成功。
取消等待会同时中断正在执行的 SQLite 查询。

召回在 SDK 提交查询前完成，包括发送中的追加消息。Hook 只为对应的原始提示词消费一次结果，
保持用户消息不变。如果原任务在追加消息召回期间结束，该消息回到队列，交给下一轮处理。
尚未提交的追加消息不会覆盖当前回复已经注入的召回回执。

安全性能事件 `memory.recall_start`、`memory.recall_stage_start`、`memory.recall_stage`、
`memory.recall_wait`、`memory.recall_finish`、`memory.recall_hook_finish` 记录阶段状态、
耗时、条数和是否注入，不包含查询或记忆正文。等待中每五秒记录一次进度；取消单独标记。
同一召回 ID 传入 `done.memory_recall`；页脚条数按安全清洗和上下文限制后实际注入的记忆计算。
持久化回执保留 ID 与阶段诊断，不重复保存记忆正文。后台巩固的生成服务失败属于另一条链路，
应与即时召回分别诊断。
