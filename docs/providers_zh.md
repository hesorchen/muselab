# 模型提供商

> [English](providers.md)

muselab 以 **Claude Agent SDK** 作为唯一对话后端。非 Claude 模型通过会话级环境变量覆盖，将 SDK 指向各厂商的 Anthropic 兼容端点。**所有提供商均获得完整 agent loop**——而非仅限对话功能。无代理，无协议转换。

| 提供商 | 启用方式 | 工具调用 | 在哪里拿 key |
|---|---|---|---|
| **Anthropic Claude**（Opus / Sonnet / Haiku） | 执行一次 `claude login` | ✅ | 复用 Pro / Max OAuth，无需 API key，无按令牌计费 |
| **DeepSeek**（V4 系列） | 在设置中填入 `DEEPSEEK_API_KEY` | ✅ | platform.deepseek.com |
| **智谱 GLM**（GLM 5 / 5 Air / 5.1 / 4.7 / 4 Plus） | `ZHIPUAI_API_KEY` | ✅ | bigmodel.cn（提供免费额度） |
| **MiniMax**（M2.1 / M2.5 / M2.7 + 各自 Highspeed；国际站走 `MINIMAX_INTL_API_KEY`） | `MINIMAX_API_KEY` | ✅ | minimaxi.com（国内）/ minimax.io（国际）— 默认返回思考块 |
| **Kimi**（K2 / K2.5 / K2.6 / K2 Thinking） | `MOONSHOT_API_KEY` | ✅ | platform.moonshot.cn |
| **Qwen**（Qwen3 / 3.5 / 3.6 系列 —— Max / Plus / Flash / Coder；国际站同一把 key） | `DASHSCOPE_API_KEY` | ✅ | dashscope.console.aliyun.com — 国内 + 国际共用一把 key，仅延迟差异 |
| **小米 MiMo**（V2.5 Pro / V2.5 / V2 Flash） | `XIAOMI_MIMO_API_KEY` | ✅ | platform.xiaomimimo.com（公测） |
| **百度千帆**（ERNIE 4 / 4.5 / 5 系列 + X1 推理 + 千帆托管的 DeepSeek V3.2） | `QIANFAN_API_KEY` | ✅ | console.bce.baidu.com/qianfan — Anthropic 兼容路径使用 `sk-xxx` 风格 key，不是 IAM AK/SK |

各家具体型号以 UI 下拉为准 —— 来源是 `backend/endpoints.py` 的 `CATALOG`，更新频率比本表高。

## 对话中切换模型

当前会话已有消息时，下拉菜单弹确认 → fork 一个使用新模型的新会话，原会话保留在历史。空会话则直接原地切换（不 fork）。fork 是为了避免跨厂商的思考签名漂移——将一家厂商签名的思考块直接发送给另一家厂商会导致静默错误。

每条助手消息存储各自的 `model` 字段，因此即使页面刷新并重新渲染整段对话，模型标识也保持准确。

## 新增提供商

在 `backend/endpoints.py` 的 `CATALOG` 中添加一条 `Provider` 配置即可。具体差异说明及会话级 `CLAUDE_CONFIG_DIR` 隔离的设计原因，见 [add-provider_zh.md](add-provider_zh.md)。
