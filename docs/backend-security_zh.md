# 后端安全模型

> [English](backend-security.md)

muselab 是单用户应用。一个共享 token 保护 UI 与 API，但该 token 不是细粒度权限系统：获得 token 的人应被视为获得了 muselab 服务用户能够执行的全部操作。

## 鉴权

- `MUSELAB_TOKEN` 至少 16 个字符；缺失或过短时服务拒绝启动。
- 普通 API 优先使用 `X-Auth-Token` header。
- 必须由浏览器直接导航的少数资源接受 `?token=`，例如下载、原始预览和持久化附件。
- token 比较使用恒定时间比较。
- `GET /api/health` 和限速后的浏览器错误上报不要求 token；业务 API 默认全部要求鉴权。

查询参数可能进入浏览器历史与代理日志。muselab 自身会清理访问日志中的 token，但反向代理也必须配置脱敏。公网部署必须使用 HTTPS。

## SSE 一次性 ticket

浏览器发送消息时采用两步流程：

1. `POST /api/chat/stream/start` 使用 header 鉴权，并在 JSON body 中提交 prompt、会话、模型、权限、附件 ID 和移动端标记。
2. 后端返回一个有效期 60 秒、单次使用的 ticket；浏览器再连接 `GET /api/chat/stream?ticket=...`。

ticket 首次兑换时立即从内存删除，因此 prompt 和长期 token 不进入 SSE URL。旧版 `GET /stream?token=...` 查询参数形式仅为兼容旧客户端保留，不应作为新集成方式。

## 终端 WebSocket ticket

真实终端采用独立的两步鉴权：

1. 已鉴权请求调用 `POST /api/terminals/{terminal_id}/ticket`。
2. 后端返回 30 秒有效、单次使用的 ticket；浏览器通过 WebSocket subprotocol 同时发送 `muselab-terminal-v1` 与该 ticket。

服务端只保存 ticket 的 SHA-256 摘要，兑换时绑定终端 ID；请求带有 WebSocket `Origin` 时，还会校验其与请求 `Host` 一致。浏览器不会把长期 token 放入 WebSocket URL。

## 权限边界

### 文件 API

文件 API 的每个请求绑定默认或已登记工作目录。路径会规范化并验证仍位于选中目录内；符号链接目标也必须留在边界内。敏感文件名、NUL 字节和对回收站内部的直接写入会被拒绝。

工作目录注册并不是服务用户权限隔离。只应登记愿意通过 Web UI 暴露的目录。

### Agent 与终端

文件 API 的路径边界**不适用于** Agent 工具或真实终端：

- Agent 的 Bash、Read、Write 等工具依照当前 SDK 权限模式运行。
- 预览区终端是真实 Unix PTY，能以 muselab 服务用户权限访问工作目录之外的路径。
- 终端进程使用最小环境白名单，不继承 `MUSELAB_TOKEN` 或 provider API key；这能减少凭据泄露，但不能限制文件系统权限。

若不需要真实终端，设置 `MUSELAB_TERMINAL_ENABLED=0`。生产或共享机器应使用专门的低权限系统用户运行 muselab，并依赖容器、虚拟机或操作系统权限建立真正隔离。

## Provider 凭据隔离

非 Anthropic provider 的 CLI 子进程收到一份最小化的完整环境替换，而不是父进程环境的合并副本。环境只包含进程运行、代理和 TLS 所需变量，以及当前 provider 的 endpoint 和凭据。

`CLAUDE_CONFIG_DIR` 指向按系统用户隔离的临时目录，并清除可能出现的 Claude OAuth 凭据，避免第三方请求静默回退到 Anthropic。其他 provider 的 key 与 `MUSELAB_TOKEN` 不会传入该子进程。

## 设置写入

设置 API 只允许写入明确白名单中的字段。`MUSELAB_TOKEN`、`MUSELAB_ROOT`、`PATH` 等部署级变量不能从 UI 修改。

- API key 返回前会脱敏。
- 包含脱敏符号的值不会覆盖真实 key。
- 值中的 CR/LF 会被移除，防止 `.env` 换行注入。
- `.env` 通过临时文件和原子替换写入；可热更新的值会同步到当前进程。

## HTTP 防护

所有响应设置：

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: SAMEORIGIN`
- `Referrer-Policy: same-origin`

HTML 与 SVG 原始预览使用隔离响应和严格 CSP。主 UI 由于 Alpine 内联指令没有启用全局严格 CSP。

## 威胁模型与部署建议

| 风险 | 实际影响 | 建议 |
|---|---|---|
| token 泄露 | 可操作会话、工作目录、Agent、设置和真实终端，等价于获得服务用户级远程操作能力 | 使用长随机 token、HTTPS、代理日志脱敏和独立服务用户 |
| 恶意预览文件 | HTML/SVG 可能主动执行内容 | 保持预览 CSP，不关闭隔离头 |
| 恶意附件或网页 | 可能通过 prompt injection 操纵 Agent | 对外部内容使用更严格权限，不自动批准高风险操作 |
| 升级接口 | 已鉴权用户可触发固定包的安装脚本 | 不需要在线升级时在反向代理阻断该端点 |
| 多 worker | 内存 ticket、限速桶、活动回合和终端注册表不会跨 worker 共享 | 使用默认单 worker 部署 |

另见项目根目录的 `SECURITY.md`。
