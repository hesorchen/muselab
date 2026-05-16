# 加新 LLM provider 到 muselab

muselab 不锁 Claude，只要厂商提供 **Anthropic Messages API 兼容端点**，就能直连，
**3 步、3 行配置、~5 分钟** 完成。所有 Claude SDK 的能力（Read/Edit/Bash/Grep/MCP/
Skills/CLAUDE.md auto-load）都自动跨厂可用。

## 前提：确认厂商有 Anthropic 兼容端点

去厂商文档搜 "Anthropic compatible" / "anthropic-compatible" / "/anthropic"。
2026 年起，国产 LLM 大多支持，目前已知：

| 厂商 | Anthropic 端点 | 状态 |
|------|---------------|------|
| DeepSeek | `https://api.deepseek.com/anthropic` | ✅ 内置 |
| 智谱 GLM | `https://open.bigmodel.cn/api/anthropic` | ✅ 内置 |
| MiniMax | `https://api.minimax.io/anthropic` | ✅ 内置 |
| Kimi (Moonshot) | `https://api.moonshot.cn/anthropic` | ✅ 内置 |
| 小米 MiMo | – | ❌ 暂只支持 OpenAI 协议 |
| Qwen | – | ❌ 暂只支持 OpenAI 协议 |

**没 Anthropic 端点的厂商**目前不支持。可以推动厂商出兼容端点，或考虑用
[claude-code-router](https://github.com/musistudio/claude-code-router) 做协议翻译
（损耗大、需要额外进程）。

---

## 加新 provider 的 3 步

### 1. 在 `backend/endpoints.py` 加一条 catalog

```python
# backend/endpoints.py — CATALOG 元组中追加
Provider(
    prefix="qwen-",                                    # 模型名前缀（dispatcher 用）
    base_url="https://dashscope.aliyuncs.com/anthropic",  # 厂商 Anthropic 端点
    env_key="DASHSCOPE_API_KEY",                       # 对应的 .env key
    display="Qwen",                                    # UI 分组名
    models=("qwen-3.6-max", "qwen-3.6-plus"),          # 暴露在模型下拉里的具体型号
),
```

### 2. 在 `.env` 里加 API key

```bash
echo "DASHSCOPE_API_KEY=sk-xxx" >> .env
```

或通过 Settings modal UI 填入（更安全，会自动写入并刷新 `os.environ`）。

### 3. 重启服务

```bash
# Docker
docker compose restart

# 或原生
# kill 旧 uvicorn 进程, 重新 uv run uvicorn ...
```

完成。浏览器**模型下拉**会自动多出 "Qwen" 分组。选了立刻能聊 + 用工具。

---

## 工作原理

```
muselab 收到 chat 请求
  ↓
chat.py 看 model 前缀
  ├── claude-*  → ClaudeSDKClient (无 env override)
  │              → 默认走 Anthropic API + 你的 Pro OAuth 凭据
  │
  └── 匹配 catalog 的前缀 → ClaudeSDKClient (env override)
                          → 替换 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN
                          → SDK 以为还在跟 Anthropic 说话，实际打到厂商端点
                          → 厂商端点把 Anthropic 协议翻成自己的，返回时翻回来
```

**关键点**：muselab 一行业务代码都不用改，SDK 也不用知道。env override 在每次
`get_client(session_id, model, ...)` 调用时通过 `ClaudeAgentOptions(env=...)`
传给底层 claude CLI 子进程。

---

## 测试新 provider

```bash
# 1. 验证 endpoint 可达
curl https://你的厂商.com/anthropic/v1/messages -X POST \
  -H "Authorization: Bearer sk-xxx" \
  -H "Content-Type: application/json" \
  -d '{"model": "你的模型", "messages": [{"role":"user","content":"hi"}], "max_tokens": 50}'

# 2. 在 muselab UI 里选这个模型，发条消息
# 3. 检查是否触发工具调用（让它 "Read README.md"）
```

如果**对话能通但工具不行**，多半是厂商的 Anthropic 兼容端点没实现 tool use。
可向厂商提 issue，或暂时只把它当"纯对话"用。

---

## 已知踩坑

### DeepSeek thinking 模式

DeepSeek 推理模型（`deepseek-reasoner`）要求把 `reasoning_content` 在下一轮回传。
SDK 走 Anthropic 协议时，这个映射由 DeepSeek 自家端点处理；如果未来端点行为变化
导致丢上下文，临时方案是关掉 thinking 或切到 chat 模型。

### Pro OAuth 不会被影响

只有 catalog 命中前缀的模型才会被 env override。Claude 模型 (`claude-*`) **不会**走
override，继续用 `claude login` 的 OAuth → 不付 API key 费。

### 测试当然要补

加新 provider 时，请在 `tests/test_endpoints.py` 里加：

```python
@pytest.mark.parametrize("model,expected_host", [
    ("qwen-3.6-max", "dashscope.aliyuncs.com"),   # 你的厂
])
def test_provider_routing_correct(monkeypatch, model, expected_host):
    ep = _reload_endpoints(monkeypatch, {})
    assert expected_host in ep.lookup(model).base_url
```

跑 `make test` 保证没破回归。

---

## FAQ

**Q: 厂商要先充值才能用？**  
A: 是。muselab 不替你管账。Pro OAuth 才走订阅免费配额。

**Q: 能让一个 session 跨厂连续聊吗？**  
A: 可以。session 是 model-agnostic，切下拉就生效，历史不丢。但跨厂时新模型看不到
对方厂特有的内部 tool_use 上下文，纯文字对话没问题。

**Q: catalog 里 `prefix` 和 `models` 重不重？**  
A: `prefix` 是 dispatcher 匹配用的；`models` 是 UI 下拉显示的具体型号。`models`
里每个值必须以 `prefix` 开头。

**Q: 改 catalog 后要不要重启？**  
A: 要。它是 Python 模块的常量，热重载是另一个工程问题。

**Q: 想做厂商间智能路由（如 plan task 走 Sonnet、code task 走 DeepSeek）？**  
A: 不建议在 muselab 里做。该用 [claude-code-router](https://github.com/musistudio/claude-code-router)
独立处理。muselab 的设计哲学是"thin layer + 用户自己选模型"。
