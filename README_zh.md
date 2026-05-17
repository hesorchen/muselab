# muselab

### 见见 **Muse** —— 真的认识你的 AI 助理。
*muselab 是 Muse 居住的自托管"工坊"，跟你的档案为邻。*

> ⚡ ~4.4 k 行 · 不用 npm · 不用 bundler · 1 GB 内存的 VPS 跑得动
> 🚀 一条 `docker compose up`，搞定
> 🧠 跑在 **Anthropic 官方 Claude Agent SDK** 上——跟 Claude Code 同一个引擎，
> MCP / Skills / Subagents / CLAUDE.md 在所有模型上都可用。

[English → README.md](README.md) · [更新日志](CHANGELOG.md) · [加新模型](docs/add-provider.md) · [安全](SECURITY.md)

---

## 为什么用 muselab

| | |
|---|---|
| 🏛 **常驻型设计** | 不是 IDE 边栏。muselab 常驻在你的档案旁——体检报告、职业笔记、投资记录、论文——帮你读它、写它、推理它。 |
| ⚡ **真·轻量** | ~1.2 k 行 Python + ~3.2 k 行原生 HTML / JS / CSS。无 bundler，无 npm。一下午能读完每一行。 |
| 🚀 **部署极简** | Docker Compose 3 行命令从克隆到上线。原生安装（uv）也就多一行。 |
| 🧠 **完整 Agent 能力** | Chat 后端只用 Claude Agent SDK——MCP servers / Skills / Subagents / CLAUDE.md 自动加载 / Plan mode / 工具调用全都开箱即用。非 Claude 模型走厂商 Anthropic 兼容端点，**不用协议翻译代理**。 |

## 它是什么

浏览器里一页三栏（~100 MB 常驻）：

- 📁 **文件区**——档案树：列表 / 多文件 tab 预览 / 拖拽上传 / 全文搜索 / CodeMirror 语法高亮编辑
- 💬 **Chat**——流式 agent，自带工具（Read / Edit / Bash / Glob / Grep / WebFetch / TodoWrite / Task / MCP），会话持久化到磁盘，对话中途可切模型
- ⚙ **设置**——UI 配置 provider、主题色、**界面语言（中文 / English）**、默认选项，不用手改 `.env`

## 快速开始

### 一键安装——开机自启 · 默认仅 localhost

选你的系统。脚本会装依赖、生成 `.env`（含随机 token + 你指定的档案目录）、
注册开机自启项——电脑关机重启 Muse 自动回来。

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
claude login   # 一次性，给 Anthropic 模型用；非 Claude 厂商只需要 API key

# macOS  ——用户级 LaunchAgent
bash scripts/install-macos.sh

# Linux  ——用户级 systemd
bash scripts/install-linux.sh

# Windows ——任务计划程序（PowerShell）
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

然后浏览器打开 `http://localhost:8765`，从 `.env` 复制 token 登录。

#### 笔记本关机重启之后呢？

| OS | 重启 → 登录回去 | 重启 → 不登录（合盖、ssh-only……）|
|----|----------------|----------------------------------|
| **macOS** | ✓ 自动起 | n/a（Mac 重启总会登录）|
| **Linux** | ✓ 自动起 | ✗ 需要 `sudo loginctl enable-linger $USER` 才能跨登出 / 不登录的重启 |
| **Windows** | ✓ 自动起 | n/a（Task Scheduler 触发器就是「At Logon」）|

**普通笔记本场景**（重启后会登录回去）——**三个系统都自动 work**，
脚本注册了自启项，剩下交给 OS。

**台式 / mini PC 场景**（Linux，平时可能根本不登录）——开 linger：
```bash
sudo loginctl enable-linger $USER
```
Linux 安装脚本最后会检测并提示这一条，如果还没启用的话。

各 OS 详细指引（验证 / 重启 / 看日志 / 开放 LAN / 卸载）：
[macOS](docs/install-macos.md) · [Linux](docs/install-linux.md) · [Windows](docs/install-windows.md)。

### 进阶——Docker / 手动

<details>
<summary>Docker — GHCR 预构建镜像（一行起）</summary>

```bash
# 多架构预构建镜像（amd64 + arm64），latest = main 分支头
docker run -d --name muselab \
  -p 8765:8765 \
  -e MUSELAB_TOKEN=$(openssl rand -hex 32) \
  -v $HOME/muselab-archive:/root/muselab-archive \
  -e MUSELAB_ROOT=/root/muselab-archive \
  -v $HOME/.claude:/root/.claude \
  ghcr.io/hesorchen/muselab:latest
```

也可以钉版本：`ghcr.io/hesorchen/muselab:1.2.3` / `:1.2` / `:sha-abc1234`。

</details>

<details>
<summary>Docker Compose（喜欢 compose 体感的话）</summary>

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
cp .env.example .env && $EDITOR .env   # 填 MUSELAB_TOKEN、ARCHIVE_DIR
claude login                            # 宿主机做，容器复用 OAuth
docker compose up -d
```

`docker-compose.yml` 默认 `restart: unless-stopped`——宿主机重启容器自动起，
不需要额外的自启配置。

</details>

<details>
<summary>原生 uv（不挂服务）</summary>

适合手动在终端跑——开发 / 调试 / 临时用。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cd muselab && uv sync
cp .env.example .env && $EDITOR .env
claude login
uv run python -m backend.main          # 按 .env 里的 MUSELAB_HOST:MUSELAB_PORT 绑定
```

</details>

## 接任意模型

muselab 后端只用 Claude Agent SDK，每次请求按 model 前缀给 SDK 设环境变量，
路由到厂商的 Anthropic 兼容端点。**所有 provider 都享有完整 agent 循环**——
不是"纯聊天"，不需要起代理进程，没有翻译损耗。

| 厂商 | 怎么启用 | 工具调用 | 备注 |
|---|---|---|---|
| **Anthropic Claude**（Sonnet/Haiku/Opus）| `claude login` 一次 | ✅ | 复用 Pro/Max 订阅 OAuth——不用 API key，不按 token 付费 |
| **DeepSeek**（V4 Pro / V4 Flash / R1 / Chat）| 在 `.env` 或 Settings UI 设 `DEEPSEEK_API_KEY` | ✅ | 对话场景比 Claude 便宜约 10× |
| **智谱 GLM**（GLM-5 / GLM-4-Plus）| `ZHIPUAI_API_KEY` | ✅ | |
| **MiniMax**（M2.7）| `MINIMAX_API_KEY` | ✅ | |

对话中切模型一个下拉的事，历史不丢。
加新 provider 只要 **`endpoints.py` 加 3 行**——见 [docs/add-provider.md](docs/add-provider.md)。

## 一天里的 muselab

```
早上  | 让 Claude 总结 archives/papers/this-week/ 里的新论文
     | 切到 DeepSeek 把总结翻译成英文
     |
中午  | 拖一个 PDF 到 investment/research/
     | chat 里 @investment/portfolio.md，Claude 按你 CLAUDE.md 里的
     |   投资规则给再平衡建议
     |
晚上  | 浏览器里直接编辑 health/training-log.md（CodeMirror）
     | Ctrl+S 保存，toast 提示已写盘
     | 让 Claude 找过去 3 个月训练记录的趋势
```

`~/.claude/CLAUDE.md`（你全局规则）+ `<档案根>/CLAUDE.md`（按目录规则）
**自动加载，对所有模型生效**。换 DeepSeek 也守你的规矩。

## 60 秒架构图

```
┌────────────────────────────────────────────────────────────┐
│ 浏览器：~3200 行原生 HTML + Alpine.js + CSS                 │
│   ┌──────────┬─────────────────┬───────────────────────┐   │
│   │ 文件树    │  预览 + 多 tab   │  chat + 多模型切换      │   │
│   └──────────┴─────────────────┴───────────────────────┘   │
└────────────────────────────────────────────────────────────┘
                              │
                              ▼  HTTP / SSE
┌────────────────────────────────────────────────────────────┐
│ 后端：FastAPI（~1200 行）                                    │
│   ┌──────────────────────┐   ┌─────────────────────────┐   │
│   │ /api/files/*         │   │ /api/chat/*             │   │
│   │   safe_resolve       │   │   ClaudeSDKClient 池    │   │
│   │   read/write/grep    │   │   按 (session, model)   │   │
│   │   敏感文件硬阻        │   │   model 前缀分流         │   │
│   └──────────────────────┘   └─────────────────────────┘   │
└────────────────────────────────────────────────────────────┘
                              │
        claude-*  ┌───────────┴───────────────┐  其他（deepseek-/glm-/...）
                  ▼                           ▼  （按请求 env 覆盖）
        api.anthropic.com               api.deepseek.com/anthropic
        （Pro OAuth）                    api.minimaxi.com/anthropic
                                        open.bigmodel.cn/api/anthropic
                                        api.moonshot.cn/anthropic
```

**无 bundler，无 build 步骤**。改个文件，刷新页面，完事。

## 跟同类对比

|  | muselab | claudecodeui | code-server + Cline | Obsidian + AI | Claude Code CLI |
|---|---|---|---|---|---|
| 主要目的 | 档案 + AI chat | 多 CLI agent IDE | 浏览器 VS Code + AI | 本地知识库 | 终端编码 agent |
| 自托管 | ✅ | ✅ | ✅ | ❌ 仅本地 | ❌ |
| 浏览器访问 | ✅ | ✅ | ✅ | ❌ | ❌ |
| HTML/PDF/图片预览 | ✅ 一流 | ⚠️ | ✅ 插件 | ⚠️ 插件 | ❌ |
| **所有 agent 能力对任意模型可用** | ✅ | ⚠️ 主 Claude | varies | varies | ✅ 仅 Claude |
| 代码量 | ~4.4 k | 几万 | 几十万 | 闭源 | 闭源 |
| 安装命令数 | 3 | 多 | docker compose（重）| 一键 | brew/npm |

要 IDE 全套 → claudecodeui 或 code-server。muselab 的卖点完全相反：
**最小可读的档案 + AI 表面，给每个模型 Claude 全套 agent 能力。**

## 安全

⚠️ **`MUSELAB_TOKEN` 泄漏 ≈ 对 `MUSELAB_ROOT` 的 shell 权限**。chat 默认
`permission_mode="bypassPermissions"`——Claude 可读写该 root 下任意文件，不再 per-call 确认。

代码层已做的：

- `MUSELAB_ROOT` 黑名单：`/` `/etc` `/root` `/home` `/var` `/usr` `/boot` `$HOME` 全拒
- `MUSELAB_TOKEN` 强制 ≥ 16 字符
- 路径穿越防护（`safe_resolve`）
- 敏感文件名硬阻：`.env*` / `id_rsa` / `*.pem` / `credentials*` 等——带 token 也读不到
- XSS 防护：所有 markdown 过 DOMPurify
- HTML/SVG 预览跑在 `iframe sandbox=""` + 严格 CSP

运维层你要做的：

- 用非特权用户跑（不是 `root`，也不是你的登录用户）
- `MUSELAB_ROOT` 指向专门目录
- Token 长且随机，怀疑泄漏立即轮换
- 超出 LAN 范围必配 HTTPS + nginx basic auth

详见 [SECURITY.md](SECURITY.md)。

## 关于名字

muselab 这个名字来自希腊神话的**九位缪斯**——艺术与学问的灵感女神。**Muse**
是住在里面的 AI，**muselab** 是她工作的开放工坊。

每次会话按当前小时哈希挑一位缪斯出场（同小时稳定，跨小时自然轮转），慢慢能遇齐九位：

| 缪斯 | 中译 | 掌管 | 几何形象 |
|---|---|---|---|
| Calliope | 卡利俄佩 | 史诗 | 六边形（Hex）|
| Clio | 克利俄 | 历史 | 层叠横线（Bars）|
| Erato | 厄拉托 | 情诗 | vesica piscis（Lens）|
| Euterpe | 欧忒耳佩 | 音乐 | 正弦波（Wave）|
| Melpomene | 墨尔波墨涅 | 悲剧 | 残月（Crescent）|
| Polyhymnia | 波吕许谟尼亚 | 圣诗 | 光环（Halo）|
| Terpsichore | 忒耳普西科瑞 | 舞蹈 | 三美神舞步（Trio）|
| Thalia | 塔利亚 | 喜剧 | 灵光一现（Spark）|
| Urania | 乌拉尼亚 | 天文 | 行星 + 卫星（Orbit）|

点 chat 顶栏的小图形可以手动切换；favicon 也跟着变——浏览器 tab 默默替你
带着今天这位缪斯。

## 项目状态

Pre-1.0，个人项目。我每天用。PR 欢迎；维护者保留**拒绝任何会让代码超过"一下午能读完"**功能的权利。

路线图 + 已知问题见 [TODO.md](TODO.md)。

## License

[MIT](LICENSE)
