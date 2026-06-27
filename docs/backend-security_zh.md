# 安全模型

> [English](backend-security.md)

本页介绍 muselab 的安全架构：鉴权 token 如何在系统中流转、文件系统隔离层的作用机制、哪些设置可以在运行时修改而哪些不可以、第三方模型提供商如何与 Anthropic 账户隔离，以及默认的网络策略。漏洞报告政策和运维加固清单请参阅 [../SECURITY.md](../SECURITY.md)。

---

## 威胁模型

muselab 是一个**单用户、自托管、优先本地访问**的应用。它没有用户账户系统，没有基于角色的访问控制（RBAC），也不支持多租户。持有 `MUSELAB_TOKEN` 的人可以读取、写入、上传和删除 `MUSELAB_ROOT` 下的任意文件，并且能够驱动一个以 `permission_mode="bypassPermissions"` 和 `cwd=MUSELAB_ROOT` 运行的 Claude Agent SDK 会话。实际含义是：token 泄露等同于获得了归档目录范围内的远程 shell 访问权限。这是设计上的权衡取舍——muselab 是 AI 归档管理器，不是沙箱。缓解措施属于运维层面：以专用的低权限用户运行、使用足够长且随机的 token、在前面放一个 TLS 反向代理、永远不要将 8765 端口暴露到公网。

---

## 鉴权

### Token 来源与最短长度

`MUSELAB_TOKEN` 在模块导入时从环境变量读取（向后兼容已废弃的 `PORTAL_TOKEN`），位于 [`backend/settings.py:L195`](../backend/settings.py#L195)。启动时强制要求**最短 16 字符**：如果 token 不存在或长度不足 16 字符，服务器会抛出 `RuntimeError` 并拒绝启动（[`backend/settings.py:L229-L235`](../backend/settings.py#L229-L235)）。

### 恒定时间比较

所有 token 校验使用 [`hmac.compare_digest()`](../backend/auth.py#L7-L19) 而非 Python 的 `==` 运算符。Python 的字符串相等判断在遇到第一个不匹配字节时就会短路，通过局域网响应时间可以泄露已匹配的前缀长度。`hmac.compare_digest` 的运行时间与两个输入中较长者的长度成正比，与首个差异位置无关（[`backend/auth.py:L7-L19`](../backend/auth.py#L7-L19)）。

### 三种依赖变体

三个 FastAPI 依赖项处理不同的传输约束（[`backend/auth.py:L22-L54`](../backend/auth.py#L22-L54)）：

| 依赖项 | 使用方 | Token 接受来源 |
|--------|--------|---------------|
| `require_token` | 大多数 `/api/files/*`、`/api/settings/*`、`/api/meta`、`/api/presence` | 仅 `X-Auth-Token` header |
| `require_token_query` | `/api/files/raw`、`/api/files/download` | 仅 `?token=` 查询参数 |
| `require_token_header_or_query` | Chat SSE 流（`/api/chat/stream`） | header 或 `?token=` 均可 |

查询参数变体的存在是因为浏览器无法为 `<iframe src>` 与 `EventSource` 连接发送自定义 header。需要鉴权的图片预览会用 `X-Auth-Token` fetch 图片字节，再渲染为 blob URL，因此不会把全局 token 放进图片 URL。`fetch()` 调用可以使用 header 且优先这样做；查询参数路径作为 iframe 下载和复制链接的备用方案保留（[`backend/auth.py:L33-L54`](../backend/auth.py#L33-L54)）。

### 无需鉴权的路由

以下路由无需 token：

| 路由 | 原因 |
|------|------|
| `GET /api/health` | Docker / k8s / Caddy `health_uri` 存活探针（[`backend/main.py:L529-L535`](../backend/main.py#L529-L535)） |
| `POST /api/log/client-error` | 在鉴权建立前捕获浏览器错误；每 IP 限速 30 次/分钟（[`backend/main.py:L582-L622`](../backend/main.py#L582-L622)） |
| `GET /`、`/static/*`、`/sw.js`、`/robots.txt`、`/static/assets/manifest.webmanifest` | HTML 外壳、前端资源、PWA 清单文件 —— 不含私人数据 |

---

## 文件系统隔离

muselab Files API 的每个文件操作在进行任何文件系统调用前都经过 `safe_resolve()`。该函数屏蔽路径穿越（`../../etc/passwd`）、符号链接逃逸（通过解析目标路径并检查其是否在 `ROOT` 下）、NUL 字节注入，以及对形如凭据的文件名（`.env*`、`id_rsa`、`*.pem`、`credentials.json` 及 30 余种其他模式）的访问——即使持有有效 token 也不例外。`MUSELAB_ROOT` 本身禁止指向系统路径（`/`、`/etc`、`/root`、`/home`、`/var`、`/usr`、`/boot`），在启动时强制检查。

完整细节，包括 `SENSITIVE_NAMES` 和 `SENSITIVE_SUFFIX` 的完整屏蔽列表以及回收站还原的 `allow_sensitive=True` 例外情况，请参阅 [backend-files_zh.md](backend-files_zh.md)（见 `safe_resolve` 章节）。

---

## 设置接口

### `PUT /api/settings` 可修改的内容

设置写入接口（[`backend/api_settings.py:L275-L404`](../backend/api_settings.py#L275-L404)）接受严格类型化的 `SettingsIn` 请求体，并在写入 `.env` 前应用**字段名白名单**。可写字段如下：

| 字段 | 写入的环境变量 |
|------|--------------|
| `anthropic_api_key` / `deepseek_api_key` / `zhipuai_api_key` / `minimax_api_key` | 对应的 `*_API_KEY` 变量 |
| `provider_keys`（dict） | 提供商目录 `env_key` 集合中的任意名称，或匹配 `^MUSELAB_PROVIDER_[A-Z0-9_]+_API_KEY$` 的名称 |
| `default_model` | `MUSELAB_DEFAULT_MODEL` + `MUSELAB_MODEL`（保持同步） |
| `default_permission` | `MUSELAB_DEFAULT_PERMISSION` |
| `provider_disabled` | `MUSELAB_DISABLED_PROVIDERS` |

不在白名单中的字段会被**静默丢弃** —— `PATH`、`MUSELAB_TOKEN`、`MUSELAB_ROOT` 以及任何任意环境变量都无法通过此接口写入（[`backend/api_settings.py:L309-L311`](../backend/api_settings.py#L309-L311)）。

### 永远无法通过 API 修改的内容

- `MUSELAB_TOKEN` —— 不在任何白名单中；在已认证的会话内修改鉴权 token 会产生权限提升面。
- `MUSELAB_ROOT` —— 运行时修改根目录可能静默地将文件操作重定向到意外位置。
- `PATH` 或其他任何不在白名单中的进程环境变量。

### 脱敏值拒绝写入

`GET /api/settings` 返回的提供商 API key 经过脱敏处理（`first4•••last4`，使用 U+2022 BULLET 符号）。向 `PUT /api/settings` 提交的任何包含 `•` 的值都会被**拒绝而不写入**，防止前端 bug 将脱敏后的展示值意外回传并覆盖真实 key（[`backend/api_settings.py:L319-L324`](../backend/api_settings.py#L319-L324)）。

### `.env` 原子重写

设置变更通过 `tempfile.mkstemp` + `os.replace` 原子写入（[`backend/api_settings.py:L163-L173`](../backend/api_settings.py#L163-L173)）。写入前会从值中去除 CR/LF 字符，防止换行注入攻击——攻击者可能通过在值中插入换行，在下次 `load_dotenv` 时将一个值拆分成多行 `KEY=VALUE`（[`backend/api_settings.py:L129-L133`](../backend/api_settings.py#L129-L133)）。文件写入后立即在进程内更新 `os.environ`，使变更无需重启即可生效。

---

## 第三方模型的计费隔离

当 muselab 将会话路由到第三方提供商（DeepSeek、GLM、MiniMax、Kimi、Qwen、MiMo、千帆）时，会构建一个**最小白名单环境**，并以**完全替换**（而非合并）的方式传给 Claude CLI 子进程（[`backend/endpoints.py:L851-L930`](../backend/endpoints.py#L851-L930)）。

替换后的环境精确包含以下内容：

```
ANTHROPIC_BASE_URL       = <厂商端点>
ANTHROPIC_API_KEY        = <厂商 key>      # x-api-key header
ANTHROPIC_AUTH_TOKEN     = <厂商 key>      # 双保险 Bearer
CLAUDE_CODE_OAUTH_TOKEN  = ""              # 终止 OAuth 回退
CLAUDE_OAUTH_TOKEN       = ""              # 终止 OAuth 回退
CLAUDE_CONFIG_DIR        = <隔离的临时目录>
```

再加上一份简短的进程基础变量白名单（`PATH`、`HOME`、`USER`、locale、代理、TLS CA 变量）—— 其他一概不传。

**`CLAUDE_CONFIG_DIR` 隔离为何能防止 Anthropic 被静默计费。** Claude CLI 优先使用 `~/.claude/.credentials.json`（Pro OAuth）而非 `ANTHROPIC_API_KEY`。如果不隔离，一个 DeepSeek 会话会把 Claude OAuth token 发到 `api.deepseek.com`，收到 401 后静默回退到 `api.anthropic.com`——将费用计到你的 Claude Pro 账户。将 `CLAUDE_CONFIG_DIR` 指向一个不含凭据文件的、每用户独立的临时目录（`/tmp/muselab-vendor-cli-config-<uid>/`），强制 CLI 使用注入的 API key。该目录下任何泄露的凭据文件在每次调用时都会被删除（[`backend/endpoints.py:L879-L887`](../backend/endpoints.py#L879-L887)）。

**最小白名单环境为何能防止 key 外泄。** CLI 子进程以 `bypassPermissions` 运行且能访问互联网。如果继承完整的父进程环境，`MUSELAB_TOKEN` 和每个提供商的 `*_API_KEY` 都会暴露给 agent，后者可能通过 Bash 工具调用（`echo $MUSELAB_TOKEN`）将其外泄。白名单确保子进程只能看到连接该厂商所必需的信息（[`backend/endpoints.py:L895-L910`](../backend/endpoints.py#L895-L910)）。

提供商目录和模型解析细节另见 [routing_zh.md](routing_zh.md)。

---

## 网络策略

**绑定地址。** muselab 默认绑定到 `127.0.0.1`（仅本地回环）。[`backend/settings.py:L206-L209`](../backend/settings.py#L206-L209) 中的注释明确指出，对于默认的单用户安装场景，绑定到 LAN 地址是一个陷阱。仅在有 TLS 终止代理的 LAN / VPS / Docker 场景下，才通过 `.env` 中的 `MUSELAB_HOST` 覆盖为 `0.0.0.0`。

**响应头。** `_security_headers` 中间件（[`backend/main.py:L299-L331`](../backend/main.py#L299-L331)）通过 `setdefault` 为每个响应附加以下三个 header（接口自己设置的 header 优先级更高）：

| Header | 值 | 用途 |
|--------|-----|------|
| `X-Content-Type-Options` | `nosniff` | 防止对文件预览进行 MIME 嗅探 |
| `Referrer-Policy` | `same-origin` | 防止跨域导航时通过 `Referer` 泄露 token |
| `X-Frame-Options` | `SAMEORIGIN` | 阻止外部网站将 UI 嵌入 iframe |

**不设全局 CSP。** UI 使用了 Alpine.js 内联指令（`x-on:`、`@click`、`:class`）和多个内联 `<script>` 标签。严格 CSP 需要为每个请求生成 nonce 或允许 eval，维护成本对单用户应用来说不划算。通过 `/api/files/raw` 提供的 HTML/SVG 文件*会*获得一个针对该响应的严格 CSP（[`backend/files.py:L694-L704`](../backend/files.py#L694-L704)）。

**不内置 HSTS。** `Strict-Transport-Security` 只在 HTTPS 下有意义。muselab 通常在 `127.0.0.1` 上以明文运行；在明文 localhost 上启用 HSTS 会让反向代理配置产生混乱。运维人员应在反向代理层设置 HSTS。

**反向代理日志注意事项。** muselab 自身的访问日志会通过 `_TokenFilter`（[`backend/main.py:L23-L62`](../backend/main.py#L23-L62)）从 URL 中去除 `token=` 参数，但反向代理会记录原始 URL。请配置你的代理对 `token` 查询参数进行脱敏处理——nginx 和 Caddy 的示例见 [../SECURITY.md](../SECURITY.md)。

---

## 已知局限

| 局限 | 影响 | 缓解措施 |
|------|------|----------|
| **单用户，无 RBAC** | 持有 token 即可完全访问归档；无法按用户或目录划分权限 | 仅为一名受信任的用户运行；将 token 视同 root 凭据 |
| **大多数接口无每请求限速** | 有效 token 可以洪泛服务器；上传大小有上限（100 MB），但请求频率没有 | 若暴露给多个用户，在前面放置带全局限速的 Caddy 或 nginx（参考 [SECURITY.md](../SECURITY.md)） |
| **升级接口在设计上是 token 门控的 RCE** | `POST /api/settings/upgrade` 会运行 `uv` 和 `npm` 子进程；包安装会执行任意脚本（[`backend/api_settings.py:L1367-L1388`](../backend/api_settings.py#L1367-L1388)） | token 本就授予了等效的访问权限；包名是固定字面量，非用户输入。若要消除该攻击面，在反向代理处屏蔽 `POST /api/settings/upgrade` |
| **不支持多 worker** | 限速桶（`_CLIENT_ERR_BUCKETS`）仅在进程内；多 worker 部署会静默绕过限制（[`backend/main.py:L554-L556`](../backend/main.py#L554-L556)） | 使用单 worker 部署（默认设置） |
| **Token 出现在反向代理日志中** | SSE 和下载接口使用 `?token=` 查询参数；muselab 本地会去除，但上游代理会记录原始 URL | 配置代理日志格式脱敏 `token` 字段 —— 参考 [../SECURITY.md](../SECURITY.md) 中的 nginx 和 Caddy 示例 |

---

*相关页面：[configuration_zh.md](configuration_zh.md) · [routing_zh.md](routing_zh.md) · [backend-files_zh.md](backend-files_zh.md) · [../SECURITY.md](../SECURITY.md)*
