# 非阻塞消息投递

输入框接受新消息，不依赖 HTTP 回执、正在执行的轮次或尚未完成的停止请求。
在收到明确回执之前，本地提交不会被展示为服务端已接收。

## 行为契约

- 当前提交尚未确定结果时，后续输入先保存到持久化的浏览器发件箱，再清空输入框。
  如果本地存储失败，草稿保持不变。
- 恢复投递使用原始会话、消息 ID 和请求载荷。只有服务端明确表示回执不存在时，
  才能重试同一个幂等请求；尚未完成的提交预留不允许使用新 ID 再次执行。
- 送达状态未知只影响对应消息，不能锁住输入框。
  回执恢复期间，已经显示的消息气泡保持原位。
- 取消只针对一个提交身份。取消记录可以拦住迟到的 POST 请求；
  如果请求已经被接收，则只停止该请求所属的不可变轮次。
- 停止当前回复时，同时暂停点击前的待发消息快照。暂停状态在重启后保留，
  但不阻塞之后手动发送的新消息，也不占用可执行队列的容量。
  继续待发消息只解除显式暂停，不会重放失败或执行状态未知的记录。
- 迟到的停止请求不能暂停或中断后继轮次。
- 后台运行会话切换时，浏览器发件箱记录保留原来的回执归属。

单条浏览器发件箱记录的生命周期：

```mermaid
stateDiagram-v2
    state "就绪，已本地保存" as Ready
    state "发送原始请求" as Sending
    state "等待回执" as Waiting
    state "失败，内容保留" as Failed

    [*] --> Ready: 快照已持久化，再清空输入框
    Ready --> [*]: 首次发送前取消
    Ready --> Sending: 使用原始消息 ID 发起 POST
    Sending --> [*]: 已接收
    Sending --> Failed: 被拒绝
    Sending --> Waiting: 响应丢失、409 或 425
    Waiting --> Sending: 回执 not_found，用同一 ID 重放
    Waiting --> Waiting: 仍为 pending，继续轮询
    Waiting --> [*]: 已接收或已取消
    Waiting --> Failed: 回执为 failed

    note right of Ready
        本地存储失败时，
        草稿保留在输入框中。
    end note
```

服务端回执决定携带已知消息 ID 的 POST 能做什么：

```mermaid
flowchart TD
    P["携带会话、类型<br/>和消息 ID 的 POST"] --> L{"该 ID 是否有回执？"}
    L -->|"无"| N["写入 pending，<br/>只执行一次接收"]
    L -->|"cancelled 取消记录"| C409["409，永不执行"]
    L -->|"cancel_requested"| R425["425，先查询回执"]
    L -->|"pending、accepted 或 failed"| FP{"请求指纹相同？"}
    FP -->|"否"| D409["409，拒绝复用 ID"]
    FP -->|"是，仍为 pending"| R425
    FP -->|"是，已有结果"| OUT["返回已保存的结果"]
    N --> F{"接收期间收到取消？"}
    F -->|"否"| OK["记录 accepted 或 failed"]
    F -->|"是"| STOP["记录 cancelled，只停止其所属轮次<br/>或撤回其队列消息"]
```

取消与停止只作用于一个身份，绝不影响后继轮次：

```mermaid
sequenceDiagram
    participant B as 浏览器
    participant API as muselab
    participant R as 回执库
    participant Q as queue 文件

    Note over B,Q: POST 到达前取消
    B->>API: 取消该消息 ID
    API->>R: 写入 cancelled 取消记录
    B->>API: 同一 ID 的迟到 POST
    API-->>B: 409，不执行
    Note over B,Q: 停止正在生成的回复
    B->>API: 携带 turn ID 和待发快照 ID 的停止请求
    alt turn ID 仍是当前轮次
        API->>Q: 暂停快照中的消息，记住尚未入队的 ID
        API->>API: 只中断该轮次
    else 后继轮次已在运行
        API-->>B: 已过期，不暂停也不中断
    end
    B->>API: 已暂停 ID 的迟到 POST
    API->>Q: 以暂停状态入队
    B->>API: 继续
    API->>Q: 解除暂停，不改动待处理记录
```

## 回归检查

tests/test_nonblocking_delivery.py 覆盖显式暂停、重启、迟到请求、
幂等取消及过期停止请求的轮次归属。
tests/e2e/test_nonblocking_delivery.py 覆盖连续发送、回执丢失、本地取消、
停止与发送重叠、持久化恢复及存储失败。
现有队列、提交、多标签页和交互反馈测试仍是必需的回归检查。
tests/test_docs.py 检查文档一致性，包括必需的英文和中文文档配对。

传输回执恢复是实现细节，不是需要用户确认的审批步骤。

## 慢存储诊断

记忆配置检查在事件循环之外执行，包括缓存命中时的文件元数据检查。
同一会话的并发状态请求共享一次只读磁盘快照，再在事件循环中合并实时广播状态。
任务卡片写入仍须完成，所属操作才会越过持久化边界。

成本面板合并并发刷新，统计快照最多复用两秒。之后通过各文件的版本检查缓存失效，
复用未变化转录文件的统计结果。外部写入和侧车文件中的费用修改在下次刷新时纳入。

`memory.storage_read` 区分锁等待、连接、取数及解码、关闭连接的耗时。
`chat.task_overlay_persist` 区分运行会话链查询和持久化；
`chat.context_preflight` 同时记录成功、失败及耗时。存储执行时间长，不能单独证明 SQL 慢。
生成结果格式诊断只记录长度和解析位置，不记录响应正文。召回期限和原生压缩判断保持原有语义。

`tests/test_log_bottlenecks.py` 覆盖慢存储、共享读取、取消、所有者隔离、增量统计和异常 JSON。
