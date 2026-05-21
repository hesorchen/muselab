# 模型 Providers

> [English](providers.md)

muselab 以 **Claude Agent SDK** 作为唯一 chat 后端。非 Claude 模型通过
per-session env override 把 SDK 指向 vendor 的 Anthropic 兼容端点。
**所有 provider 都获得完整 agent loop**——不只是 chat。无 proxy，无协议翻译。

| Provider | 启用方式 | 工具调用 | 备注 |
|---|---|---|---|
| **Anthropic Claude**（Opus / Sonnet / Haiku） | `claude login` 一次 | ✅ | 复用 Pro / Max OAuth，无需 API key，无 token 计费 |
| **DeepSeek**（V4 Pro / V4 Flash / Chat / Reasoner） | Settings 填 `DEEPSEEK_API_KEY` | ✅ | 对话场景比 Claude 便宜约 10× |
| **智谱 GLM**（GLM-5 / GLM-5 Air / GLM-4.7 / 4 Plus） | `ZHIPUAI_API_KEY` | ✅ | bigmodel.cn 提供免费额度 |
| **MiniMax**（M2.7 / M2.7 Highspeed / M2.5） | `MINIMAX_API_KEY` | ✅ | M2.7 默认返回 thinking block |

## 对话中切换模型

Dropdown → 确认对话框 → 自动新建 session。这是为了规避跨 vendor 的 thinking
signature 漂移——一家厂商签名过的 thinking block 直接送给另一家厂商会静默崩坏。

每条 assistant 消息存储自己的 `model` 字段，所以即使页面 reload 重渲整段对话，
badge 也保持准确。

## 新增 provider

`backend/endpoints.py` 加 3 行就够。具体 diff 和 per-session
`CLAUDE_CONFIG_DIR` 隔离的理由见 [add-provider_zh.md](add-provider_zh.md)。
