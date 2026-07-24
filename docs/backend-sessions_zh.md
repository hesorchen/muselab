# 会话内部机制

> [English](backend-sessions.md)

本页说明会话的所有权、存储、队列、附件、分支和重启恢复。流式传输与重连机制见[模型路由与回合循环](routing_zh.md)。

## 双层存储

每个会话由 Claude CLI 与 muselab 共同持有：

```text
~/.claude/projects/<cwd-key>/<sid>.jsonl
    Claude Provider 的消息、工具调用和压缩边界

${XDG_STATE_HOME:-~/.local/state}/muselab/vendor-cli/projects/<cwd-key>/<sid>.jsonl
    第三方 Provider 的隔离 CLI transcript

<repo>/sessions/
├── index.json
├── <sid>.sidecar.json
├── <sid>.queue.json
└── active_turns/<sid>.json

$MUSELAB_ROOT/.muselab-attach/<sid>/
    muselab 持久化的图片和 PDF 原文件
```

- CLI JSONL 是对话正文的事实来源。Claude 与第三方 Provider 使用不同配置根；
  第三方根属于持久用户状态，应纳入备份。
- `sessions/index.json` 保存 muselab 专属的展示和运行设置。
- sidecar 保存逐消息费用、模型、时间、附件和上下文容量等标注。
- queue 文件保存待处理消息。
- 附件统一放在主 `MUSELAB_ROOT` 下，即使会话属于其他已登记工作目录。

所有层使用同一个会话 UUID。

## 多工作区

每个会话在 `index.json` 中保存 `cwd`。Claude SDK 在该目录启动，对应 JSONL 位于该工作目录派生出的 CLI project 目录。

会话列表会扫描所有已登记工作目录并与全局索引合并。旧会话没有 `cwd` 时归入主 `MUSELAB_ROOT`；工作目录被移除后，其本地索引记录不会被悄悄迁移到其他目录。

创建、分支和定时任务产生的新会话都会保存明确的 `cwd`。模型历史不会跨 provider 自动迁移，避免 thinking signature 不兼容。

## 会话索引

`sessions/index.json` 主要字段：

| 字段 | 说明 |
|---|---|
| `id` | 会话 UUID，与 CLI JSONL 文件名一致 |
| `name` | UI 显示名 |
| `model` | 锁定模型；空值表示首回合解析默认 provider |
| `permission` | SDK 权限模式 |
| `cwd` | 会话所属工作目录 |
| `created_at`、`updated_at` | Unix 秒 |
| `message_count`、`turn_count` | 消息帧数与真实用户回合数 |
| `pinned`、`auto_named` | 置顶与自动命名状态 |
| `effort`、`thinking` | 推理强度与扩展思考开关 |

会话列表支持分页、搜索、强制包含已打开 Tab，并用 ETag 让无变化轮询返回 `304`。内部写入会立即使缓存失效；外部 Claude CLI 写入可能在短暂缓存期后出现。

## Sidecar

`<sid>.sidecar.json` 不复制对话正文。其顶层包含：

- `messages`：以 CLI 消息 UUID 为键的逐消息标注。
- `context_max_tokens`：SDK 测得的上下文上限。
- `pending_attachments`：消息 UUID 尚未确定时的附件绑定队列。

常见逐消息标注包括 `cost`、`model`、`ts`、`elapsed_s`、`images` 和 `docs`。索引与 sidecar 各自有进程内锁，并通过临时文件加原子替换写入。

## 服务端消息队列

进行中的回合不会吞掉后续消息。新消息进入 `<sid>.queue.json`，当前回合结束后由后端继续排空，即使浏览器已关闭。

```json
{
  "items": [
    {
      "id": "q-1234abcd",
      "text": "下一条消息",
      "image_ids": "a1,b2",
      "permission": "default",
      "enqueued_at": 1718000000000
    }
  ],
  "paused": false
}
```

- 每个会话最多 10 条。
- 顺序默认 FIFO，可重排或删除。
- 排队时会快照当时的权限模式。
- 回合出错、需要提问或被用户中断时，队列暂停等待处理。
- 启动竞争失败会把消息重新放回队首。
- 空且未暂停时删除 queue 文件。
- `image_ids` 引用 10 分钟有效的内存暂存附件；排队过久时附件可能过期，但文本仍会发送。

## 附件

`POST /api/chat/upload-image` 虽保留历史名称，但支持：

- PNG、JPEG、GIF、WebP；
- PDF；
- Markdown、文本、CSV、JSON、YAML、代码等文本文件；
- XLSX 系列工作簿。

发送前附件保存在内存中，TTL 为 10 分钟；最多 48 项或约 256 MiB，单文件最多 10 MiB，内联文本最多 200 KiB。服务重启会丢失尚未发送的暂存附件。

发送时：

- 图片以 SDK image block 发送，并把原图保存到 `$MUSELAB_ROOT/.muselab-attach/<sid>/<attachment-id>.<ext>`。
- PDF 以 document block 发送，同时保存本地副本，便于兼容 provider 使用工具读取。
- 文本与工作簿转为有界文本后加入 prompt。
- 图片气泡使用小缩略图，原图通过 `GET /api/chat/attachments/{sid}/{filename}?token=...` 获取。

消息 UUID 由 SDK 稍后写入 JSONL，因此 muselab 先把附件记录放入 `pending_attachments`，再按 FIFO 绑定到下一个匹配的用户消息。队列最多 50 组，并清理超过 24 小时的记录。

## 分支与删除

`POST /api/chat/sessions/{sid}/fork` 可复制完整会话，或复制到 `up_to_message_id` 为止。新分支获得新的会话和消息 UUID，继承源会话的模型、工作目录、权限、effort 与 thinking 设置，并记录来源会话关系。标签页菜单用于 Fork 完整对话，每个已完成回合下方的操作用于 Fork 到该回合。历史消息编辑仍是在当前会话中重新发送，不会 Fork；非空会话切换模型则创建独立的空会话，不复制对话记录。

删除会话会一并清理：

- 对应工作目录下的 CLI JSONL；
- index、sidecar、queue 和活动回合哨兵；
- transcript index 与附件目录；
- 内存客户端、回放注册表、临时 spool 和后台任务状态。

## 重启恢复

回合开始后会写入 `sessions/active_turns/<sid>.json`，正常结束时删除。若进程被强制终止，启动扫描会把残留回合标记为中断并提示用户决定是否重发；muselab 不会自动消耗 token 重试。

| 状态 | 重启后行为 |
|---|---|
| 会话列表 | 从 index 与所有已登记工作目录的 CLI JSONL 合并 |
| 上下文容量 | 从 sidecar 恢复 |
| 服务端队列 | 从 queue 文件恢复，后续回合会继续排空 |
| 未发送附件 | 丢失，需要重新上传 |
| 进行中回合 | 作为中断项提示，不自动重跑 |

备份与迁移路径见[数据与备份](data-and-backup_zh.md)。
