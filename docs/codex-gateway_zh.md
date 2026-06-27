# Codex Gateway

> [English](codex-gateway.md)

muselab 通过**本地 Anthropic 兼容网关**支持 Codex 后端模型。网关是一个 sidecar 进程：muselab 仍然只和 Claude Agent SDK 以及 Anthropic Messages API 形状交互；sidecar 负责把请求转换到用户自己的 Codex/OpenAI 后端，再把响应转换回来。

muselab **不保存 Codex OAuth 凭据**，也**不直接调用 OpenAI 原生接口**。

```text
muselab → Claude Agent SDK → Anthropic Messages 请求
        → 127.0.0.1 上的 Codex Gateway
        → 用户自己已认证的 Codex/OpenAI 后端
```

## 内置内容

模型 catalog 里已经包含一个默认关闭的 provider 预设：

| 字段 | 默认值 |
|---|---|
| Provider | `Codex Gateway` |
| Endpoint | `http://127.0.0.1:8317` |
| Env key | `CODEX_GATEWAY_API_KEY` |
| Base URL override | `CODEX_GATEWAY_BASE_URL` |
| 内部前缀 | `codex:` |
| 模型 | `codex:gpt-5.5`、`codex:gpt-5.4`、`codex:gpt-5.4-mini`、`codex:gpt-5.3-codex-spark` |

`codex:` 前缀只供 muselab 内部路由使用。发给网关前会被剥掉，所以 muselab 里的 `codex:gpt-5.5` 到网关侧会变成 `gpt-5.5`。

## 启用方式

1. 从 muselab 推荐的 CLIProxyAPI 配置开始：

   ```bash
   mkdir -p ~/.cli-proxy-muselab
   cp examples/cli-proxy-muselab.config.yaml ~/.cli-proxy-muselab/config.yaml
   ```

2. 编辑 `~/.cli-proxy-muselab/config.yaml`：

   - 把 `replace-with-a-random-local-token` 换成高强度本地 token；
   - 除非你明确希望 proxy 额外增加本地冷却窗口，否则保留 `disable-cooling: true` 和 `session-affinity: false`。

3. 在本机启动 CLIProxyAPI，并只监听 loopback：

   ```bash
   cli-proxy-api -config ~/.cli-proxy-muselab/config.yaml
   ```

4. 在 `.env` 里放同一个 gateway token：

   ```bash
   CODEX_GATEWAY_API_KEY=replace-with-a-random-local-token
   # 如果你的 gateway 使用不同端口，可覆盖：
   # CODEX_GATEWAY_BASE_URL=http://127.0.0.1:8317
   ```

5. 如果是手动编辑 `.env`，重启 muselab；如果在 **Settings → Providers → Codex Gateway** 里粘贴 key，则无需重启。

6. 在聊天模型下拉里选择 `codex:*` 模型。

推荐的 CLIProxyAPI 模板关闭了 proxy 自己的 auth/model cooldown 调度。这样可以避免上游失败后被本地 proxy 额外放大成黑窗期，体验更接近直接使用 Codex app/CLI。它不能绕过真实的上游额度限制或模型级 429。

## 参考实现方案：CLIProxyAPI sidecar

muselab 采用的参考方案是把 **CLIProxyAPI** 放在 muselab 旁边作为本地 sidecar：

```text
浏览器
  → muselab 后端
  → Claude Agent SDK
  → Anthropic Messages API 请求（model: codex:gpt-5.5）
  → muselab 剥掉 codex: 前缀（model: gpt-5.5）
  → http://127.0.0.1:8317/v1/messages
  → CLIProxyAPI
  → 用户已登录 / 已授权的 Codex 后端
```

这套方案的边界是：

- **muselab 负责**：provider catalog、模型下拉、会话级 base URL / api key 注入、工具调用和 transcript 仍由 Claude Agent SDK 驱动。
- **CLIProxyAPI 负责**：保存和使用 Codex 侧认证、把 Anthropic Messages 请求转换到 Codex/OpenAI 后端、把流式响应和错误再转换回 Anthropic 形状。
- **用户负责**：在本机运行 sidecar，并把同一个本地 token 同时写进 `~/.cli-proxy-muselab/config.yaml` 和 muselab 的 `CODEX_GATEWAY_API_KEY`。

`examples/cli-proxy-muselab.config.yaml` 是 muselab 推荐的最小参考配置。它刻意做了这些选择：

| 配置 | 推荐值 | 原因 |
|---|---|---|
| `host` | `127.0.0.1` | 只允许本机访问，避免把本地 Codex 能力暴露到公网 |
| `port` | `8317` | 对应 muselab 内置默认 `CODEX_GATEWAY_BASE_URL` |
| `api-keys` | 用户自设高强度 token | 即使只监听 loopback，也避免本机其它进程无鉴权调用 |
| `disable-cooling` | `true` | 不让 proxy 额外制造本地冷却黑窗期 |
| `session-affinity` | `false` | 默认不把 muselab 会话绑定到某个 credential |
| `logging-to-file` | `false` | 降低把 prompt / token / 上游错误落盘的风险 |
| `remote-management.allow-remote` | `false` | 禁止远程管理面板 |

这个 sidecar **不会由 muselab 自动安装或自动启动**。如果你希望开机自启，可以自己用 systemd / launchd / supervisor 管理 `cli-proxy-api -config ~/.cli-proxy-muselab/config.yaml`，但不要把 Codex OAuth 文件或 gateway 日志提交进仓库。

### Docker 注意事项

如果 muselab 跑在 Docker 里，`http://127.0.0.1:8317` 指的是**容器内部**，不是宿主机。可选做法：

- 把 gateway 也放进同一个 compose/network，然后把 `CODEX_GATEWAY_BASE_URL` 指到 gateway service 名；
- 或让容器访问宿主机 gateway，例如使用 `host.docker.internal`（Linux 可能还需要额外 host-gateway 配置）。

不要直接把 gateway 绑定到 `0.0.0.0` 暴露公网。确实需要跨机器访问时，必须放在 HTTPS / 反向代理 / 防火墙后面，并使用高熵 token。

## 网关要求

sidecar 至少要实现 Anthropic Messages API 中 agent loop 需要的部分：

- `POST /v1/messages`，或配置的 base URL 下等价路径；
- Anthropic SSE 事件形状的文本流式输出；
- `tool_use` 与 `tool_result` 往返；
- auth、quota、invalid model、network failure 等错误的 Anthropic 风格响应；
- 接受 muselab 发送的 `x-api-key` 和 / 或 `Authorization: Bearer`。

如果普通聊天可用但工具调用失败，说明该 gateway 仍是 chat-only，不能作为完整 muselab agent 支持来宣传。

## 上下文窗口说明

muselab 的上下文仪表会把内置 Codex Gateway 模型按 400K context 处理。gateway 仍可能根据后端模型和账号档位执行不同的有效窗口。

如果实际运行仍报 `input exceeds the context window`，通常说明 gateway 转换层、所选后端模型或账号档位的有效窗口更小。此时可以新开会话、压缩历史，或切到上下文窗口已确认更大的模型 / gateway 路径。

## 安全模型

- 默认只监听 `127.0.0.1`。
- 即使在 loopback 上也要求 token。
- 日志不要打印 `Authorization`、`x-api-key`、OAuth access token、refresh token、cookie 或原始 Codex auth 文件。
- 不要提交 gateway 运行态文件。`.env`、`.codex/`、`.cli-proxy-muselab/`、`.muselab/codex-gateway/`、日志和 provider overrides 都是本地状态。
- 如果要暴露到 localhost 之外，必须放到 HTTPS 和反向代理后面，并使用高熵 token。

## 为什么不做 OpenAI/Codex 原生支持？

muselab 的架构不变量是只有一套 agent runtime：Claude Agent SDK。工具执行、MCP、Skills、权限、流式事件和 transcript 都由这套 runtime 负责。OpenAI/Codex 原生接口的 message、streaming、tool 和 error 形状都不同。直接支持它们意味着在 muselab 内部维护第二套 agent runtime。把转换边界放在 gateway 上，可以保持 muselab 简洁，同时在有兼容 adapter 时接入 Codex 后端模型。
