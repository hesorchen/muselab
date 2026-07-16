# Codex Gateway

> [English](codex-gateway.md)

muselab 通过**本地 Anthropic 兼容网关**支持 Codex 后端模型。网关是一个 sidecar 进程：muselab 仍然只和 Claude Agent SDK 以及 Anthropic Messages API 形状交互；sidecar 负责把请求转换到用户自己的 Codex/OpenAI 后端，再把响应转换回来。

muselab **不保存 Codex OAuth 凭据**，也**不直接调用 OpenAI 原生接口**。

截至 2026-07-16，已验证的兼容基线是 CLIProxyAPI `v7.2.80`、Claude Agent
SDK `0.2.120` 以及其内置 Claude CLI `2.1.211`。新版 CLIProxyAPI 对这条
链路相关的 Codex 缓存 token 统计、reasoning effort、工具调用回放和
Anthropic 响应转换都有修复。

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
| 模型 | `codex:gpt-5.6-sol`、`codex:gpt-5.6-terra`、`codex:gpt-5.6-luna`、`codex:gpt-5.5`、`codex:gpt-5.4`、`codex:gpt-5.4-mini`、`codex:gpt-5.3-codex-spark` |

`codex:` 前缀只供 muselab 内部路由使用。发给网关前会被剥掉，所以 muselab 里的 `codex:gpt-5.6-sol` 到网关侧会变成 `gpt-5.6-sol`。Codex Gateway 也会在 muselab 里打开按会话设置的 reasoning `effort` 下拉；muselab 通过 Claude Agent SDK 透传所选值，sidecar 负责把它映射到 Codex/OpenAI 后端的推理强度参数。

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
  → Anthropic Messages API 请求（model: codex:gpt-5.6-sol）
  → muselab 剥掉 codex: 前缀（model: gpt-5.6-sol）
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

`usage-statistics-enabled` 刻意保留为 `false`。CLIProxyAPI `v7.2.80` 虽然
提供 `/v0/management/usage-queue` 请求明细，但它要求打开管理接口、只短暂
保留数据，而且读取会弹出记录。它适合作为专用采集器的事件流，不适合直接
充当个人持久用量看板的真相源。

另一个 `/v0/management/api-key-usage` 也不是 Codex token 计数器：它只统计
上游 `api_key` 凭据在内存中的成功/失败次数，而 Codex 通常使用 OAuth；返回
结果的 map key 还会包含上游 key 本身。因此 muselab 不读取、更不会透出它。

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
- 接受 muselab 发送的 `x-api-key` 和 / 或 `Authorization: Bearer`；
- 支持 Claude Agent SDK 发出的 reasoning `effort` 字段，并至少把 `low`、`medium`、`high`、`max` 映射到 Codex/OpenAI 后端等价的推理强度控制。

如果普通聊天可用但工具调用失败，说明该 gateway 仍是 chat-only，不能作为完整 muselab agent 支持来宣传。

## 用量与额度统计

muselab 把两种口径明确分开：

- **Gateway 专属流量**：现有用量看板聚合 Claude Agent SDK 会话写下的逐条
  token 统计。它可持久化、能按模型查看，并且只统计真正经过 muselab 的
  请求。CLIProxyAPI `v7.2.63+` 改进了会流入这里的 cache write/read 字段。
- **Codex 账户全局用量与额度**：打开用量看板时，
  `scripts/codex-quota-refresh.py` 会启动本机 Codex app-server，调用只读的
  `account/rateLimits/read` 与 `account/usage/read` JSON-RPC 方法，直接获得
  当前额度桶、每日 token 和累计 token。它不会发送 prompt、不会消耗模型
  turn，也不会读取 OAuth 文件。旧版 Codex 不支持这些 RPC 时，才退回读取
  已存在的本地额度日志快照。

本机 Codex CLI 与 CLIProxyAPI 可能登录的是不同账户。因此界面会明确标成
**本机 Codex 账户**，不会把它冒充为另一个 Gateway 身份的权威额度。

## 上下文窗口说明

CLIProxyAPI `v7.2.80` 会在 `GET /v1/models?client_version` 暴露当前 Codex
client 模型目录。muselab 会读取并短时缓存每个模型的 `context_window`、
`max_context_window` 和可选的 `effective_context_window_percent`，不再沿用
Claude CLI 与 Codex 无关的 200K 默认值。目录未给百分比时，按 Codex client
默认策略使用 95% 的有效输入容量。

圆环和 compact 使用的有效窗口按以下优先级决定：

1. 显式环境变量：`MUSELAB_CONTEXT_LIMIT_CODEX_GPT_5_6_SOL`、`MUSELAB_CONTEXT_LIMIT_CODEX_GPT_5_5`、`CODEX_GATEWAY_CONTEXT_LIMIT`、`MUSELAB_THIRD_PARTY_CONTEXT_LIMIT`；
2. CLIProxyAPI Codex 目录的 `context_window`（默认取 95% effective）；
3. 旧版 gateway 通用 `/v1/models` 暴露的能力字段；
4. 内置 raw-window fallback：GPT-5.6 为 372K，GPT-5.5/5.4 为 272K，
   GPT-5.3 Codex Spark 为 128K，同样按 95% effective 计算。

`max_context_window` 只作为“可配置上限”展示，不会悄悄放大当前分母。创建
Codex SDK client 时，muselab 还会通过 `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 把
解析出的有效窗口注入 Claude CLI。因此 Claude CLI `/context`、原生自动压缩、
muselab 发送前预压缩、拆分弹窗和底部圆环现在共用同一个分母。

底部圆环会把两部分精度分别标明：

- 正常完成一轮后，用量来自 gateway/provider 响应，标记为“精确”；原生
  `/compact` 后、下一次 provider 响应到来前，只能读取 live SDK 计数，此时
  会明确标为“估算”；
- 窗口会标记来源是 gateway 目录、人工配置、SDK 还是内置 fallback；只有
  内置 fallback 会标为估算。

发送新消息前，muselab 会先调用 SDK 的上下文统计；如果接近 effective window，会优先执行 Claude Code 原生 `/compact`，再发送用户消息。这样比等一轮回复成功后的事后 compact 更早，能减少 gateway 在请求入口直接报 `input exceeds the context window` 的概率。

如果实际运行仍报 `input exceeds the context window`，通常说明 gateway 转换层、所选后端模型或账号档位的有效窗口更小，或当前会话已经超过到连 `/compact` 也无法进入模型。此时可以新开会话、手动降低 `CODEX_GATEWAY_CONTEXT_LIMIT`、压缩历史，或切到上下文窗口已确认更大的模型 / gateway 路径。

## 安全模型

- 默认只监听 `127.0.0.1`。
- 即使在 loopback 上也要求 token。
- 日志不要打印 `Authorization`、`x-api-key`、OAuth access token、refresh token、cookie 或原始 Codex auth 文件。
- 不要提交 gateway 运行态文件。`.env`、`.codex/`、`.cli-proxy-muselab/`、`.muselab/codex-gateway/`、日志和 provider overrides 都是本地状态。
- 如果要暴露到 localhost 之外，必须放到 HTTPS 和反向代理后面，并使用高熵 token。

## 为什么不做 OpenAI/Codex 原生支持？

muselab 的架构不变量是只有一套 agent runtime：Claude Agent SDK。工具执行、MCP、Skills、权限、流式事件和 transcript 都由这套 runtime 负责。OpenAI/Codex 原生接口的 message、streaming、tool 和 error 形状都不同。直接支持它们意味着在 muselab 内部维护第二套 agent runtime。把转换边界放在 gateway 上，可以保持 muselab 简洁，同时在有兼容 adapter 时接入 Codex 后端模型。
