# 向 muselab 接入新模型提供商

> [English](add-provider.md)

muselab 不限于 Claude。只要模型厂商提供 **Anthropic Messages API 兼容端点**，即可直接接入。**推荐路径是在 Settings 界面里填一条 provider —— 无需改代码、无需重启。** Claude SDK 的全部能力（Read/Edit/Bash/Grep/MCP/Skills/CLAUDE.md 自动加载）均可跨厂使用。

## 前提：确认厂商提供 Anthropic 兼容端点

在厂商文档中搜索 "Anthropic compatible"、"anthropic-compatible" 或 "/anthropic"。2026 年起，国内主流大模型厂商大多已支持。目前已知情况：

| 厂商 | Anthropic 端点 | 状态 |
|------|---------------|------|
| DeepSeek | `https://api.deepseek.com/anthropic` | ✅ 内置 |
| 智谱 GLM | `https://open.bigmodel.cn/api/anthropic` | ✅ 内置 |
| MiniMax | `https://api.minimaxi.com/anthropic` | ✅ 内置 |
| Kimi（月之暗面）| `https://api.moonshot.cn/anthropic` | ✅ 内置 |
| Qwen（DashScope）| `https://dashscope.aliyuncs.com/apps/anthropic`（国内默认；国际站走 `dashscope-intl.aliyuncs.com`）| ✅ 内置 |
| 小米 MiMo | `https://api.xiaomimimo.com/anthropic` | ✅ 内置 |
| 百度千帆 | `https://qianfan.baidubce.com/anthropic` | ✅ 内置 |
| Codex Gateway | `http://127.0.0.1:8317` | ✅ 内置本地网关预设 |

**未提供 Anthropic 端点的厂商**暂不支持。可以联系厂商发布兼容端点，或使用 [claude-code-router](https://github.com/musistudio/claude-code-router) 进行协议转换（存在功能损失，需额外进程）。

对于 Codex/OpenAI 后端模型，内置的 **Codex Gateway** 预设假设用户自备一个运行在本机的 sidecar，并在 `http://127.0.0.1:8317` 暴露 Anthropic Messages 兼容端点。muselab 不读取 Codex OAuth 文件，也不直接调用 OpenAI 原生接口。见 [codex-gateway_zh.md](codex-gateway_zh.md)。

---

## 两种接入方式

按目标选择：

- **路径 A（推荐，面向使用者）**：在 Settings 界面新增 provider。即时生效，无需改代码、无需重启。
- **路径 B（面向贡献者）**：在 `backend/endpoints.py` 里加内置默认项，让所有用户开箱即用 —— 适合提 PR 时使用。

### 路径 A：在 Settings 里新增（推荐）

1. 打开 **Settings → Providers**，点「新增 provider」。
2. 填四个字段：
   - **Endpoint**：厂商的 Anthropic 兼容端点（如 `https://api.acme.com/anthropic`）
   - **Prefix**：模型名前缀（如 `acme-`），dispatcher 据此路由
   - **Models**：模型 id 列表，每个都须以 prefix 开头（如 `acme-large`、`acme-small`）
   - **API key**：可在同一表单里填，也可稍后单独填
3. 保存。**模型下拉菜单立即出现该分组，即时可用** —— 无需重启。

配置写入项目根目录的 `provider_overrides.json`（与 `mcp.json` 同级）；API key 写入 `.env`，并实时刷新 `os.environ`。两者都会在下次启动时自动加载。env key 名由后端自动分配，无需手动处理。

> 编辑内置厂商（改端点 / 模型列表）或删除，同样在这里完成；删掉的内置项可一键「恢复默认」。

### 路径 B：加内置默认项（面向贡献者）

想把一个 provider 作为 muselab 的内置默认（让所有人开箱即用），在 `CATALOG` 里追加一条：

```python
# backend/endpoints.py — CATALOG 元组中追加。下面是虚构厂商示例，
# 替换成你自己的值即可。`CATALOG` 里现有的 DeepSeek / GLM / MiniMax 等
# 是真实的可工作样本，可对照参考。
Provider(
    prefix="acme-",                              # 模型名前缀（dispatcher 用）
    base_url="https://api.acme.com/anthropic",   # 厂商 Anthropic 兼容端点
    env_key="ACME_API_KEY",                      # 对应的 .env key
    display="Acme",                              # UI 分组名
    models=(
        ("acme-large", "Large"),                 # (模型 id, UI 显示名)
        ("acme-small", "Small"),
    ),
),
```

`CATALOG` 是 Python 模块级常量，**改完需重启服务**才能生效：

```bash
# Docker
docker compose restart

# 或原生：kill 旧 uvicorn 进程，重新 uv run uvicorn ...
```

随后把 API key 填进 `.env`（或在 Settings 里填），即可在模型下拉看到新分组。

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
                          → 设置 ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY
                            （同时镜像写入 ANTHROPIC_AUTH_TOKEN，
                            兼容接受 Bearer 而非 x-api-key 的厂商），
                            并把 CLAUDE_CONFIG_DIR 指向隔离目录，
                            防止 CLI 回退到 Pro OAuth
                          → SDK 以为自己在和 Anthropic 通信，实际打到厂商端点
                          → 厂商端点把 Anthropic 协议转换成自己的协议，返回时翻回来
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

### Pro OAuth 不受影响

仅配置项前缀匹配的模型才会应用环境变量覆盖。Claude 模型（`claude-*`）不经过覆盖，继续使用 `claude login` 的 OAuth 凭据，不产生 API 费用。

### 补充测试

通过路径 B 内置新提供商（提 PR）时，请在 `tests/test_endpoints.py` 中添加对应测试：

```python
@pytest.mark.parametrize("model,expected_host", [
    ("qwen3-max", "dashscope.aliyuncs.com"),   # 你的厂
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
A：不能。如果当前会话已有消息，切换模型会弹确认 → fork 出一个用新模型的新会话；原会话保留在历史。空会话允许原地切换。这是为了避开跨厂商的思考签名漂移和 `tool_use` 上下文不可访问 —— 详见 [providers_zh.md](providers_zh.md) 的「对话中切换模型」段。

**Q：配置项中 `prefix` 和 `models` 是否重复？**
A：`prefix` 供分发器匹配路由使用；`models` 是界面下拉菜单显示的具体型号列表。`models` 中的每个值均须以 `prefix` 开头。

**Q：新增 / 修改 provider 后需要重启吗？**
A：看走哪条路径。**Settings 里的改动即时生效，不用重启** —— 覆盖项存在 `provider_overrides.json`，按文件变更热加载；API key 写入 `.env` 后实时刷新 `os.environ`。只有**直接编辑 `backend/endpoints.py` 源码**才需要重启，因为 `CATALOG` 是 Python 模块级常量。

**Q：想实现厂商间智能路由（如 plan 任务走 Sonnet、代码任务走 DeepSeek）？**
A：不建议在 muselab 内部实现。[claude-code-router](https://github.com/musistudio/claude-code-router) 是处理此类需求的合适工具。muselab 的设计原则是「精简层 + 用户自主选择模型」。
