# 模型提供商

> [English](providers.md)

muselab 以 **Claude Agent SDK** 作为唯一对话后端。非 Claude 模型通过会话级环境变量覆盖，将 SDK 指向各厂商的 Anthropic 兼容端点。**所有提供商均获得完整 agent loop**——而非仅限对话功能。无代理，无协议转换。

| 提供商 | 启用方式 | 工具调用 | 备注 |
|---|---|---|---|
| **Anthropic Claude**（Opus / Sonnet / Haiku） | 执行一次 `claude login` | ✅ | 复用 Pro / Max OAuth，无需 API key，无按令牌计费 |
| **DeepSeek**（V4 Pro / V4 Flash / Chat / Reasoner） | 在设置中填入 `DEEPSEEK_API_KEY` | ✅ | 对话场景费用约为 Claude 的十分之一 |
| **智谱 GLM**（GLM-5 / GLM-5 Air / GLM-4.7 / 4 Plus） | `ZHIPUAI_API_KEY` | ✅ | bigmodel.cn 提供免费额度 |
| **MiniMax**（M2.7 / M2.7 Highspeed / M2.5） | `MINIMAX_API_KEY` | ✅ | M2.7 默认返回思考块 |
| **Kimi**（K2 / K2.5 / K2.6 / K2 Thinking） | `MOONSHOT_API_KEY` | ✅ | platform.moonshot.cn |
| **Qwen**（Qwen3 Max / Plus / Flash / Coder） | `DASHSCOPE_API_KEY` | ✅ | dashscope.console.aliyun.com |
| **小米 MiMo**（V2.5 Pro / V2 Flash） | `XIAOMI_MIMO_API_KEY` | ✅ | platform.xiaomimimo.com（公测）|

## 对话中切换模型

下拉菜单 → 确认弹窗 → 自动新建会话。此设计是为了避免跨厂商的思考签名漂移——将一家厂商签名的思考块直接发送给另一家厂商会导致静默错误。

每条助手消息存储各自的 `model` 字段，因此即使页面刷新并重新渲染整段对话，模型标识也保持准确。

## 新增提供商

在 `backend/endpoints.py` 中添加 3 行配置即可。具体差异说明及会话级 `CLAUDE_CONFIG_DIR` 隔离的设计原因，见 [add-provider_zh.md](add-provider_zh.md)。
