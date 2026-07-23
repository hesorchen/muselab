# 定时任务

> [English](scheduler.md)

muselab 可以按计划运行保存好的 prompt。调度器运行在后端 asyncio 循环中，不依赖系统 cron；每次运行都使用完整的 Agent 能力，包括工具、MCP 和 Skills。

典型用途包括定期整理笔记、生成周期报告和检查外部状态。定时任务属于无人值守执行，请只保存你愿意自动运行的 prompt。

## 持久化与通知

- 任务、最近 200 条运行历史和未读数保存在 `$MUSELAB_ROOT/.muselab/scheduler.json`。
- 服务启动时会重新计算下次运行时间。服务停机期间错过的最近一次运行会补跑；多个任务会错峰启动。
- 运行完成后会写入历史并增加未读数。配置 Web Push 后，页面关闭时也可收到不含回复正文的完成通知。
- 删除任务不会删除它已创建或复用的会话。

## 调度类型

| 类型 | 触发规则 |
|---|---|
| `daily` | 每天一次；也可通过 `times` 设置每天最多 24 个时间点 |
| `weekly` | 指定星期几，`0` 为周一、`6` 为周日 |
| `monthly` | 每月指定日期；当月份没有该日期时使用该月最后一天 |
| `once` | 指定年月日运行一次，触发后自动停用 |

## 时区

新任务保存浏览器提供的 IANA 时区名称，例如 `Asia/Shanghai` 或 `America/New_York`。后端使用 IANA 时区计算墙上时间，因此夏令时切换不会让“每天 09:00”漂移一小时。

兼容顺序如下：

1. 优先使用 `schedule.tz` 的 IANA 时区。
2. 旧任务使用 `schedule.tz_offset_minutes` 固定偏移；该模式不感知夏令时。
3. 两者都缺失或无效时，回退到服务器本地时区。

## 会话模式

- `fresh`（默认）：每次运行创建新会话，运行之间不共享上下文。
- `reuse`：任务绑定一个会话，后续运行持续追加上下文。

每个任务同一时刻只允许一次运行。手动“立即运行”与计划触发撞车时也会串行执行，避免共享 SDK 会话产生交错输出。

## API

所有端点都需要 token 鉴权。

| 方法与路径 | 用途 |
|---|---|
| `GET /api/scheduler/tasks` | 列出任务和未读数 |
| `POST /api/scheduler/tasks` | 创建任务 |
| `PATCH /api/scheduler/tasks/{id}` | 修改任务、模型、时间或启用状态 |
| `DELETE /api/scheduler/tasks/{id}` | 删除任务，不删除绑定会话 |
| `POST /api/scheduler/tasks/{id}/run` | 立即运行一次，不改变下一次计划时间 |
| `GET /api/scheduler/history` | 获取最近运行历史 |
| `GET /api/scheduler/tasks/{id}/history` | 获取单个任务的运行历史 |
| `DELETE /api/scheduler/history` | 清空全部历史 |
| `DELETE /api/scheduler/history/{ts}` | 删除一条历史，可用 `task_id` 消除同时间戳歧义 |
| `POST /api/scheduler/ack` | 清零未读数 |

## 安全边界

定时任务以 muselab 服务用户权限运行，没有人在执行时实时确认工具调用。外部网页、邮件或文件可能包含提示注入内容；涉及写入、删除、发布或付款的无人值守任务应采用更严格的权限和隔离。
