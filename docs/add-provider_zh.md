# 向 muselab 接入新模型提供商

> [English](add-provider.md)

muselab 不绑定 Claude。只要厂商提供 **Anthropic Messages API 兼容端点**，即可直接接入，**3 步、3 行配置、约 5 分钟**完成。Claude SDK 的全部能力（Read/Edit/Bash/Grep/MCP/Skills/CLAUDE.md 自动加载）均可跨厂使用。

## 前提：确认厂商提供 Anthropic 兼容端点

在厂商文档中搜索 "Anthropic compatible"、"anthropic-compatible" 或 "/anthropic"。2026 年起，国内主流大模型厂商大多已支持。目前已知情况：

| 厂商 | Anthropic 端点 | 状态 |
|------|---------------|------|
| DeepSeek | `https://api.deepseek.com/anthropic` | ✅ 内置 |
| 智谱 GLM | `https://open.bigmodel.cn/api/anthropic` | ✅ 内置 |
| MiniMax | `https://api.minimaxi.com/anthropic` | ✅ 内置 |
| Kimi（月之暗面）| `https://api.moonshot.ai/anthropic` | ✅ 内置 |
| Qwen（DashScope）| `https://dashscope-intl.aliyuncs.com/apps/anthropic` | ✅ 内置 |
| 小米 MiMo | `https://api.xiaomimimo.com/anthropic` | ✅ 内置 |
| 豆包（字节火山）| 仅 `Doubao-Seed-Code` 兼容 | 🟡 暂未内置 |

**未提供 Anthropic 端点的厂商**暂不支持。可推动厂商发布兼容端点，或使用 [claude-code-router](https://github.com/musistudio/claude-code-router) 进行协议转换（存在功能损耗，需额外进程）。

---

## 接入步骤

### 1. 在 `backend/endpoints.py` 中添加一条配置项

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

也可通过界面中的设置弹窗填入（推荐方式，会自动写入文件并刷新 `os.environ`）。

### 3. 重启服务

```bash
# Docker
docker compose restart

# 或原生
# kill 旧 uvicorn 进程, 重新 uv run uvicorn ...
```

完成。浏览器的**模型下拉菜单**将出现 "Qwen" 分组，选中即可立即使用对话和工具调用功能。

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

**关键点：** muselab 的业务代码无需任何改动，SDK 也无需感知此重定向。环境变量覆盖在每次 `get_client(session_id, model, ...)` 调用时，通过 `ClaudeAgentOptions(env=...)` 传入底层 claude CLI 子进程。

---

## 测试新提供商

```bash
# 1. 验证端点可达
curl https://你的厂商.com/anthropic/v1/messages -X POST \
  -H "Authorization: Bearer sk-xxx" \
  -H "Content-Type: application/json" \
  -d '{"model": "你的模型", "messages": [{"role":"user","content":"hi"}], "max_tokens": 50}'

# 2. 在 muselab UI 里选这个模型，发条消息
# 3. 检查是否触发工具调用（让它 "Read README.md"）
```

若**对话正常但工具调用失败**，通常是厂商的 Anthropic 兼容端点尚未实现工具调用功能。可向厂商提交问题反馈，或暂时将其作为纯对话模型使用。

---

## 已知注意事项

### DeepSeek 思考模式

DeepSeek 推理模型（`deepseek-reasoner`）要求在下一轮对话中回传 `reasoning_content`。SDK 使用 Anthropic 协议时，该映射由 DeepSeek 自身的端点处理。若端点行为变化导致上下文丢失，临时方案是关闭思考模式或切换至对话模型。

### Pro OAuth 不受影响

仅配置项前缀匹配的模型才会应用环境变量覆盖。Claude 模型（`claude-*`）不经过覆盖，继续使用 `claude login` 的 OAuth 凭据，不产生 API 费用。

### 补充测试

接入新提供商时，请在 `tests/test_endpoints.py` 中添加对应测试：

```python
@pytest.mark.parametrize("model,expected_host", [
    ("qwen-3.6-max", "dashscope.aliyuncs.com"),   # 你的厂
])
def test_provider_routing_correct(monkeypatch, model, expected_host):
    ep = _reload_endpoints(monkeypatch, {})
    assert expected_host in ep.lookup(model).base_url
```

执行 `make test` 确认无回归。

---

## 常见问题

**Q：厂商需要先充值才能使用？**
A：是的。muselab 不负责账单管理。仅 Pro OAuth 使用订阅包含的免费配额。

**Q：同一个会话可以跨厂商连续对话吗？**
A：可以。会话与模型无关，切换下拉菜单即时生效，历史记录保留。跨厂商切换后，新模型无法访问另一家厂商内部的 `tool_use` 上下文，纯文本对话不受影响。

**Q：配置项中 `prefix` 和 `models` 是否重复？**
A：`prefix` 供分发器匹配路由使用；`models` 是界面下拉菜单显示的具体型号列表。`models` 中的每个值均须以 `prefix` 开头。

**Q：修改配置项后是否需要重启？**
A：需要。配置项是 Python 模块级常量，热重载属于独立的工程问题。

**Q：想实现厂商间智能路由（如 plan 任务走 Sonnet、代码任务走 DeepSeek）？**
A：不建议在 muselab 内部实现。[claude-code-router](https://github.com/musistudio/claude-code-router) 是处理此类需求的合适工具。muselab 的设计原则是"精简层+用户自主选择模型"。
