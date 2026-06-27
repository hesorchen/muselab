# 配置参考

> [English](configuration.md)

所有设置都在仓库的 `.env` 文件里。安装器会创建它；你可以手动编辑，或在应用内的 **设置** 面板里改大部分值（设置面板会热更新 `.env` *并同时* 更新运行中的进程 —— 无需重启）。手动改 `.env` **需要** 重启，因为进程只在启动时读一次文件。

起始模板见 `.env.example`。

## 鉴权

muselab 是单用户的。一个 token 保护整个 Web UI 和每一次 API 调用。

- token 是 `.env` 里的 `MUSELAB_TOKEN`。用 `grep MUSELAB_TOKEN .env` 找它。
- 浏览器以 `X-Auth-Token` header 发送（缓存在 `localStorage["muselab_token"]`）；链接也接受 `?token=` 查询参数。
- 至少 16 个字符 —— 否则后端拒绝启动。安装器用 `openssl rand -hex 32` 生成随机 token。

## 核心设置

| 变量 | 作用 | 默认 | 必填 |
|---|---|---|---|
| `MUSELAB_TOKEN` | Web UI / API 鉴权 token | 随机（安装器生成） | **是** —— ≥16 字符 |
| `MUSELAB_ROOT` | 归档目录的绝对路径（原生部署） | —— | **是**（原生部署） |
| `MUSELAB_HOST` | uvicorn 绑定的网络接口 | `127.0.0.1` | 否 |
| `MUSELAB_PORT` | 监听端口 | `8765` | 否 |
| `MUSELAB_MODEL` | 新会话的默认模型 id | 未设置 | 否 —— **建议留空**，让 UI 自动选你配的第一个 provider |

> `MUSELAB_ROOT` 不能是裸系统路径（`/`、`/etc`、`/home`、`/var` 等）；后端会拒绝，避免把整块磁盘交给 agent。

## Provider 密钥

至少配一个。Anthropic 走 `claude login`（Pro/Max OAuth），无需密钥。其余都是 API key。这些也能在设置面板里配。

| Provider | API-key 环境变量 | 默认 base URL | base URL 覆盖 |
|---|---|---|---|
| Anthropic（Claude） | `ANTHROPIC_API_KEY`（或 `claude login`） | api.anthropic.com | —— |
| DeepSeek | `DEEPSEEK_API_KEY` | api.deepseek.com/anthropic | `DEEPSEEK_BASE_URL` |
| 智谱 GLM | `ZHIPUAI_API_KEY` | open.bigmodel.cn/api/anthropic | `ZHIPUAI_BASE_URL` |
| MiniMax（国内） | `MINIMAX_API_KEY` | api.minimaxi.com/anthropic | `MINIMAX_BASE_URL` |
| MiniMax（国际） | `MINIMAX_INTL_API_KEY` | api.minimax.io/anthropic | —— |
| Kimi / Moonshot | `MOONSHOT_API_KEY` | api.moonshot.cn/anthropic | `MOONSHOT_BASE_URL` |
| Qwen / DashScope | `DASHSCOPE_API_KEY` | dashscope.aliyuncs.com/apps/anthropic | `DASHSCOPE_BASE_URL` |
| 小米 MiMo | `XIAOMI_MIMO_API_KEY` | api.xiaomimimo.com/anthropic | `XIAOMI_MIMO_BASE_URL` |
| 百度 ERNIE（千帆） | `QIANFAN_API_KEY` | qianfan.baidubce.com/anthropic | `QIANFAN_BASE_URL` |
| Codex Gateway | `CODEX_GATEWAY_API_KEY` | 127.0.0.1:8317 | `CODEX_GATEWAY_BASE_URL` |

注意：
- **MiniMax 国内与国际用不同的密钥。** `minimaxi.com` 的 key 在 `minimax.io` 上会 401，反之亦然 —— 配与你账户匹配的那个。
- **Qwen** 国内与国际端点共用 `DASHSCOPE_API_KEY`；国际变体在 UI 里按模型选择。
- 你自己在设置里添加的 provider，密钥名为 `MUSELAB_PROVIDER_<SLUG>_API_KEY`。
- **Codex Gateway** 是本地 Anthropic 兼容 sidecar。这里的 token 只用于 gateway；muselab 不保存 Codex OAuth 凭据。
- **生图** 与聊天 provider 分离。默认 `MUSELAB_IMAGE_PROVIDER=auto`：如果配置了
  `OPENAI_IMAGE_API_KEY`（或复用 `OPENAI_API_KEY`）就走 OpenAI Image API。本机
  Codex `$imagegen` 必须显式 opt-in：仅在可信 localhost 实例上设置
  `MUSELAB_IMAGE_PROVIDER=codex_imagegen` 与 `CODEX_IMAGEGEN_ENABLED=true`。如果你的
  本地网关暴露 OpenAI-compatible image endpoint，可把 `OPENAI_IMAGE_BASE_URL` 指向
  对应 `/v1`，并使用 `openai` provider。

接入列表之外的 Anthropic 兼容端点，见 [add-provider_zh.md](add-provider_zh.md)。

## 可选调优

全部可选；未设置时用合理默认。

| 变量 | 作用 | 默认 |
|---|---|---|
| `MUSELAB_PROMPT_CACHE_TTL` | Claude prompt 缓存 TTL（`1h` / `5m` / 空=CLI 默认） | `1h` |
| `MUSELAB_BUDGET_USD` | 月度软预算 —— 仅 UI 角标提示，不硬性中断 | `0`（关闭） |
| `MUSELAB_MAX_UPLOAD_MB` | 单次上传大小上限（MiB） | `100` |
| `MUSELAB_IMAGE_PROVIDER` | Composer 生图后端（`auto`、`openai`、`codex_imagegen`） | `auto` |
| `OPENAI_IMAGE_API_KEY` | Composer GPT Image 工具使用的 API key | 未设置 |
| `OPENAI_IMAGE_BASE_URL` | 生图使用的 OpenAI-compatible `/v1` base URL | `https://api.openai.com/v1` |
| `MUSELAB_IMAGE_GENERATION_TIMEOUT` | 生图超时时间（秒） | `180` |
| `CODEX_IMAGEGEN_ENABLED` | `MUSELAB_IMAGE_PROVIDER=codex_imagegen` 或 `auto` 无图片 API key 时，是否允许本机 Codex `$imagegen` | `false` |
| `CODEX_IMAGEGEN_TIMEOUT_SECONDS` | 本机 Codex 生图超时时间（秒） | `300` |
| `MUSELAB_MAX_TURNS` | 每会话最大回合数（0 = 不限） | `0` |
| `MUSELAB_THINKING_BUDGET` | 扩展思考 token 预算（0 = 关） | `10000` |
| `MUSELAB_CLIENT_POOL_CAP` | 保活的 SDK client 池大小 | `3` |
| `MUSELAB_DISABLED_PROVIDERS` | 要隐藏的 provider 模型 id（逗号分隔） | 空 |
| `MUSELAB_DISABLE_SKILLS` | 关闭内置 skills（`1`/`true`） | 关 |
| `MUSELAB_PRUNE_EMPTY_SESSIONS` | 自动删除无消息的空会话（`true`） | `false` |
| `MUSELAB_TRASH_TTL_DAYS` | 软删除文件在 `.muselab-dustbin/` 保留天数（0 = 永久） | `30` |
| `MUSELAB_VAPID_SUBJECT` | Web-push VAPID `sub` 声明（一个 `mailto:`） | `mailto:noreply@muselab.dev` |
| `MUSELAB_DEFAULT_PERMISSION` | 默认权限模式 | `bypassPermissions` |

> VAPID **密钥** 不是环境变量 —— 它们在磁盘上生成于 `<archive>/.muselab/vapid.json`。只有上面的 subject 可配置。

## 仅 Docker

由 `docker-compose.yml` 读取，后端**不读**：

| 变量 | 作用 | 默认 |
|---|---|---|
| `ARCHIVE_DIR` | 挂载到容器 `/data` 的宿主机目录 | `./data` |
| `CLAUDE_HOME` | 宿主机 `~/.claude`（OAuth 凭证）路径 | `${HOME}/.claude` |
| `MUSELAB_BIND` | 发布端口绑定的宿主机接口 | `127.0.0.1` |

## 仅安装期

由安装脚本读取，运行中的后端**不读**：

| 变量 | 作用 | 默认 |
|---|---|---|
| `MUSELAB_NONINTERACTIVE` | 全取默认值，跳过所有交互 | `0` |
| `MUSELAB_LOCALE` | intake 引导 + 预置 `CLAUDE.md` 的语言 | 自动（`LANG`） |

运行中的后端从 `LANG` / `LC_ALL` 判断语言，而非 `MUSELAB_LOCALE`。

## 把 muselab 暴露到 localhost 之外

`MUSELAB_HOST`（以及 Docker 的 `MUSELAB_BIND`）默认 `127.0.0.1` 是一道安全底线：公网与你的归档之间唯一的屏障就是那个 token。若把任一项设为 `0.0.0.0`，请在前面架一层带 HTTPS 的反向代理 —— 见 [手机端 / HTTPS](mobile_zh.md) 与 `scripts/setup-https.sh`。
