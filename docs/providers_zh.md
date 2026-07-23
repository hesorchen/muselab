# 模型提供商

> [English](providers.md)

muselab 以 **Claude Agent SDK** 作为唯一对话后端。非 Claude 模型通过会话级环境变量覆盖，将 SDK 指向各厂商的 Anthropic 兼容端点。**所有提供商均获得完整 agent loop**——而非仅限对话功能。muselab 自身不实现 OpenAI 原生协议；如果后端不是 Anthropic 兼容端点，需要在前面放一层网关。

| 提供商 | 启用方式 | 工具调用 | 在哪里拿 key |
|---|---|---|---|
| **Anthropic Claude** | 执行一次 `claude login` | ✅ | 复用 Pro / Max OAuth，无需 API key |
| **DeepSeek** | 在设置中填入 `DEEPSEEK_API_KEY` | ✅ | platform.deepseek.com |
| **智谱 GLM** | `ZHIPUAI_API_KEY` | ✅ | bigmodel.cn |
| **MiniMax**（国内／国际） | `MINIMAX_API_KEY`／`MINIMAX_INTL_API_KEY` | ✅ | minimaxi.com／minimax.io |
| **Kimi** | `MOONSHOT_API_KEY` | ✅ | platform.moonshot.cn |
| **Qwen**（国内／国际） | `DASHSCOPE_API_KEY` | ✅ | dashscope.console.aliyun.com |
| **小米 MiMo** | `XIAOMI_MIMO_API_KEY` | ✅ | platform.xiaomimimo.com |
| **百度千帆** | `QIANFAN_API_KEY` | ✅ | console.bce.baidu.com/qianfan；Anthropic 兼容路径需要 IAM access token |
| **Codex Gateway**（本地 sidecar） | `CODEX_GATEWAY_API_KEY` | ✅* | 用户自备运行在 `127.0.0.1` 的 Anthropic 兼容网关；见 [codex-gateway_zh.md](codex-gateway_zh.md) |
| **OpenAI**（Anthropic 兼容通路） | 当前内置项复用 `ZHIPUAI_API_KEY` 及其兼容端点 | ✅* | 由部署方提供兼容端点；muselab 不直连 OpenAI 原生 Chat/Responses API |
| **自定义提供商** | 在 Settings → Providers 中填写端点、前缀、模型和 key | 取决于端点 | 任意 Anthropic Messages 兼容服务 |

\* 工具调用取决于网关是否正确转换 Anthropic `tool_use` / `tool_result`。

各家具体型号与可用性以 UI 下拉为准——来源是当前生效的 catalog（内置默认、覆盖和自定义项），更新频率比本表高。这里刻意不维护容易过时的型号数量。

## 生图

Composer 里的图片按钮不是聊天 provider。`MUSELAB_IMAGE_PROVIDER=auto` 时，
如果配置了 `OPENAI_IMAGE_API_KEY`（或 `OPENAI_API_KEY`），会走 OpenAI Image
API。本机 Codex `$imagegen` 通路必须显式 opt-in：设置
`MUSELAB_IMAGE_PROVIDER=codex_imagegen` 与 `CODEX_IMAGEGEN_ENABLED=true` 后，
才会调用已登录的 `codex` CLI。也可以设置 `MUSELAB_IMAGE_PROVIDER=openai`
强制使用 OpenAI-compatible 通路。

生成结果会作为普通 muselab 图片附件暂存，因此可预览、画笔标注，并加入当前聊天发送。
生图请求会作为后台任务执行，并保留在生图历史里，刷新页面也不会丢掉已完成结果。
Codex imagegen 通路只适合 localhost 单用户部署；不要把带本机 Codex 能力的 muselab
实例暴露到公网。

## 对话中切换模型

当前会话已有消息时，下拉菜单弹确认 → fork 一个使用新模型的新会话，原会话保留在历史。空会话则直接原地切换（不 fork）。fork 是为了避免跨厂商的思考签名漂移——将一家厂商签名的思考块直接发送给另一家厂商会导致难以排查的错误。

每条助手消息存储各自的 `model` 字段，因此即使页面刷新并重新渲染整段对话，模型标识也保持准确。

## 新增提供商

在 **Settings → Providers** 里新增一条即可 —— 填端点、前缀、模型列表和 key，保存即时生效，无需重启。也可作为内置默认写进 `backend/endpoints.py` 的 `CATALOG`（面向贡献者）。完整步骤、两条路径的区别及会话级 `CLAUDE_CONFIG_DIR` 隔离的设计原因，见 [add-provider_zh.md](add-provider_zh.md)。
