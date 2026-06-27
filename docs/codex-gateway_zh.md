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
| Endpoint | `http://127.0.0.1:8766/anthropic` |
| Env key | `CODEX_GATEWAY_API_KEY` |
| Base URL override | `CODEX_GATEWAY_BASE_URL` |
| 内部前缀 | `codex:` |
| 模型 | `codex:gpt-5-codex`、`codex:gpt-5`、`codex:gpt-5-mini` |

`codex:` 前缀只供 muselab 内部路由使用。发给网关前会被剥掉，所以 muselab 里的 `codex:gpt-5-codex` 到网关侧会变成 `gpt-5-codex`。

## 启用方式

1. 在本机启动 Codex Gateway，并只监听 loopback：

   ```bash
   # 仅为示例 —— 请使用你信任的 gateway 实现。
   codex-gateway --host 127.0.0.1 --port 8766
   ```

2. 在 `.env` 里放一个高强度本地 token：

   ```bash
   CODEX_GATEWAY_API_KEY=replace-with-a-random-local-token
   # 如果你的 gateway 使用不同端口，可覆盖：
   # CODEX_GATEWAY_BASE_URL=http://127.0.0.1:8766/anthropic
   ```

3. 如果是手动编辑 `.env`，重启 muselab；如果在 **Settings → Providers → Codex Gateway** 里粘贴 key，则无需重启。

4. 在聊天模型下拉里选择 `codex:*` 模型。

## 网关要求

sidecar 至少要实现 Anthropic Messages API 中 agent loop 需要的部分：

- `POST /v1/messages`，或配置的 base URL 下等价路径；
- Anthropic SSE 事件形状的文本流式输出；
- `tool_use` 与 `tool_result` 往返；
- auth、quota、invalid model、network failure 等错误的 Anthropic 风格响应；
- 接受 muselab 发送的 `x-api-key` 和 / 或 `Authorization: Bearer`。

如果普通聊天可用但工具调用失败，说明该 gateway 仍是 chat-only，不能作为完整 muselab agent 支持来宣传。

## 上下文窗口说明

muselab 的上下文仪表会把内置 Codex Gateway 模型按 400K context 处理，这与 OpenAI 公开的 GPT-5 / GPT-5 mini / GPT-5-Codex model card 一致（128K max output；GPT-5-Codex 在 gateway 背后走 Responses API）。

如果实际运行仍报 `input exceeds the context window`，通常说明 gateway 转换层、所选后端模型或账号档位的有效窗口更小。此时可以新开会话、压缩历史，或切到上下文窗口已确认更大的模型 / gateway 路径。

## 安全模型

- 默认只监听 `127.0.0.1`。
- 即使在 loopback 上也要求 token。
- 日志不要打印 `Authorization`、`x-api-key`、OAuth access token、refresh token、cookie 或原始 Codex auth 文件。
- 不要提交 gateway 运行态文件。`.env`、`.codex/`、`.cli-proxy-muselab/`、`.muselab/codex-gateway/`、日志和 provider overrides 都是本地状态。
- 如果要暴露到 localhost 之外，必须放到 HTTPS 和反向代理后面，并使用高熵 token。

## 为什么不做 OpenAI/Codex 原生支持？

muselab 的架构不变量是只有一套 agent runtime：Claude Agent SDK。工具执行、MCP、Skills、权限、流式事件和 transcript 都由这套 runtime 负责。OpenAI/Codex 原生接口的 message、streaming、tool 和 error 形状都不同。直接支持它们意味着在 muselab 内部维护第二套 agent runtime。把转换边界放在 gateway 上，可以保持 muselab 简洁，同时在有兼容 adapter 时接入 Codex 后端模型。
