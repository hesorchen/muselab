# TODO — muselab

> 状态：核心可用，开源前所有 P0 / P1 / P2 已清。剩 launch readiness（demo gif / 截图 / release tag / awesome lists / launch post）。
> 排序原则：开源前必修 → 强化亮点 → 体验打磨。

最后更新：2026-05-22（P0/P1/P2 launch readiness 清单全清）

---

## 📈 现状速览

- **182 tests passing**（pytest，含 frontend lint；e2e 模块默认 skip）
- **多模型**：Claude (Pro OAuth) + DeepSeek + GLM + MiniMax
- **三个 OS 一键安装**（systemd / launchd / Task Scheduler）+ `doctor` / `intake` 工具
- **multi-arch Docker** 镜像通过 GH Actions 自动发到 `ghcr.io/hesorchen/muselab`
- **多会话 tab**（VS Code 风格）：tab 条 / 右键菜单 / 双击改名 / 后台流式不断 / 持久化 / mobile 长 ⋮ kebab
- **移动端响应式**：< 900px 折叠成 3-tab 单 pane；`100dvh` 修 iOS 地址栏挡输入框
- **完整 Agent SDK 能力**：MCP / Skills（7 个预置）/ Subagent / plan / ImageBlock / partial streaming
- **首次进入引导**：检测 CLAUDE.md / archive 状态，给针对性 onboarding

---

## ✅ 已完成（重大冲刺记录）

### 2026-05-16 凌晨：核心 SDK 能力 + 三方 provider + 开源物料

| 类 | 完成 |
|---|------|
| Agent SDK | Permission UI / ask_user_question MCP / Compact + Seed / Per-message model badge / 7 preset skills / MCP CRUD UI / Skill discovery / TodoWrite + Task + ExitPlanMode + MCP/Skill render |
| 第三方 provider | DeepSeek / GLM / MiniMax 路由（含 OAuth 抢权 bug 修：CLAUDE_CONFIG_DIR 隔离）+ vendor probe endpoint |
| 文件 | 黑名单+sniff 替代白名单 / symlink escape 防护 / 上传 100MB 限+扩展黑名单 / sensitive 文件名上传拒 |
| 开源物料 | LICENSE / SECURITY / CONTRIBUTING / 3 个 Issue templates / PR template / GH Actions CI（pytest + ruff + docker multi-arch publish）|
| 安装器 | install-{linux,macos,windows} 三脚本 + 7 问 intake + archive 骨架（health/work/money/people/notes/archives）|

### 2026-05-17 下半场：multi-tab + mobile 适配 + 缓存升级

| 类 | 完成 |
|---|------|
| Multi-tab chat | VS Code 风格 tab 条 / inline 双击改名 / 中键关 / 右键 context menu / 历史 picker / `openTabIds` 持久化 |
| 后台流式 | per-tab `tabState[id]`（messages / es / streaming / sessionUsage）；切走继续接收，回来看完整结果 |
| 头像 | assistant 用当前 mascot SVG；user 用通用 glyph |
| 浏览器标题 | document.title 跟随当前 session 名，流式时加 ● |
| 缓存策略 | `index.html` 渲染时把 `/static/X` 改成 `/static/X?v=<mtime>`；带 `?v=` 长缓存 immutable，不带的 no-cache 兜底 |
| Mobile | `100vh` → `100dvh` 避免 iOS Safari 地址栏挡输入框；touch-action: manipulation 去 tap delay；⋮ kebab 替代 right-click |
| ROOT=$HOME | 解锁 guard，agent 反正能写 FS；SENSITIVE_NAMES 补 `.bash_history` 等防 token 泄露场景 |
| CLI 兜底 | "Session ID already in use" 错误时 fallback 到 `resume=` 让用户消息能发出去 |
| Sanity test | scan app.js 重复方法定义（防 closeTab 那种沉默 shadow）|
| Undo toast | 关 tab 后 5s 内可点"撤销"恢复（按原 index）|
| 键盘快捷键 | Ctrl+T / Ctrl+W / Ctrl+Tab / Ctrl+1..9 / Esc |
| 拖动重排 | HTML5 drag&drop 改 tab 顺序，2px accent 指示落点 |
| mdRender 节流 | 流式中 ≥80ms coalesce；done/error/cancelled/close 强制 flush |
| e2e 脚手架 | tests/e2e/ 用 pytest-playwright 草稿；RUN_E2E=1 启用，详见 e2e/README.md |

### 2026-05-17 上半场：UX 大改 + bug 清扫

| 类 | 完成 |
|---|------|
| Onboarding | CLAUDE.md 加载状态 chip / 首次进入 3 态卡片（无 claude_md / archive 空 / ready 含 skill chips）/ 登录页改造（mascot + tagline + 错误时给 .env 路径建议）|
| 模型切换 | confirm modal "切换模型需要新建会话" → 创建新 session 跳过去；空 session silent 切；label 美化（claude-haiku-4-5-20251001 → "Haiku 4.5"）|
| Session | 智能命名跳过 hi/你好/slash 命令；Settings 加"清理空会话"按钮 |
| 视觉 | cost badge 人话化（$0.0001 → 0.01¢）；vendor key 测试 inline ✅/❌+ 错误建议；context meter 常驻；pending bubble 显示 elapsed time；mascot streaming 时静止 |
| 移动端 | < 900px 折叠 tab 布局 + 底部 nav；自动切 tab |
| /help | 含斜杠命令 + 键盘快捷键 + 三栏说明 + 文档链接 |
| 安装器 | 修 `local esc` bash 顶层错；去掉 `--quiet`；Windows utf8 编码；30s retry loop；archive 可写探测；Windows launcher.cmd 避 escape hell |
| 工具 | `scripts/doctor.{sh,ps1}` 一键自检 / `scripts/intake.{sh,ps1}` 重跑档案 intake |
| Bug | runCompact race（streaming 中点压缩 → 用错内容）/ MiniMax base URL（minimax.io → minimaxi.com）/ chinese .md 误判为二进制 / etc |
| README | hero 重写按竞品分析 A 方案：「Reuse your Claude Pro / Max seat from a browser」|

---

## 🟡 还能做的（按价值）

### Launch readiness checklist（2026-05-21 评估）

> 按"是否会显著影响第一周 traffic / 转化"排序。🔴 必做、🟡 强烈建议、🟢 可发布后补。
> 拆成"你自己做"和"我能做"两栏，前者多是 GitHub UI 操作 + 个人账号动作。

#### 🔴 必做（缺一项就显著掉转化）

**你自己做**：

- [ ] **Repo About + topics**（GitHub repo settings）
  - description 一句话（复用 README tagline）
  - 10 个 topics：`claude` `anthropic` `claude-agent-sdk` `mcp` `self-hosted` `llm` `ai-assistant` `personal-archive` `deepseek` `pwa`
- [ ] **第一个 release tag (v0.1.0)**
  - `git tag v0.1.0 && git push --tags`
  - GitHub Releases UI 里手写 highlights（用 CHANGELOG 总结）
- [ ] **Repo Social Preview 图**（1280×640 png，repo settings → Social preview）
  - 决定 HN / Twitter / 微信 卡片缩略图样子
- [ ] **Launch post 一组草稿**：Show HN / r/selfhosted / r/LocalLLaMA / V2EX 各一份
  - 标题 + 第一段决定 60% traffic；写完互不相同
  - HN 投放时间：美东周二/周三 凌晨 5-7 点
- [ ] **Awesome lists 提 PR**：awesome-claude / awesome-self-hosted / awesome-llm-apps / awesome-mcp

**我能做**：

- [x] **THIRD_PARTY_LICENSES.md** ✅ 2026-05-22 — 已建，列了 Alpine/marked/DOMPurify/highlight.js/KaTeX/CodeMirror + 后端 deps
- [ ] **Demo gif**（30 秒：三栏 + onboarding 卡片 + 切模型 + skill chip 触发 + context meter 警告 + 压缩流程）
  - VPS 没桌面，**需要笔记本上录**（macOS QuickTime + gifski / Linux peek / Windows ScreenToGif）
- [ ] **首页截图 hero PNG**（chat 满状态 + onboarding 卡片 + 手机 PWA 各一张）
- [x] **CHANGELOG.md v0.1.0 entry** ✅ 2026-05-22 — 已切 [Unreleased] → [0.1.0] - 2026-05-22 + 结构化 release notes

#### 🟡 强烈建议（前两周补也来得及）

**你自己做**：

- [ ] **GitHub Discussions enable**（settings → Features），open 一个 welcome thread
- [ ] **Twitter / X launch tweet**：@Anthropic + Claude 团队，配 demo gif
- [ ] **`good first issue` 标签**：从 TODO.md 挑 3-5 个低门槛（新 provider catalog / 文档扩展 / 小 UI fix）开 issue 打标签

**我能做**：

- [ ] **Hosted 只读 demo**（Cloudflare tunnel + VPS 套 read-only token + 限速）
- [x] **README 顶部 hero 美化** ✅ 2026-05-22 — badges 排版统一 + Self-hosted 徽章；缩略图待 demo gif 出来再嵌

#### 🟢 Nice to have（发布后再做）

- [ ] **GitHub Pages 落地页** `hesorchen.github.io/muselab`
- [x] **Dependabot config** ✅ — `.github/dependabot.yml` 已有，uv + github-actions 周扫
- [ ] **release-please CI**（自动 bump version + 生成 changelog）
- [ ] **Container image scan**（trivy / grype 加进 CI）
- [x] **"Multi-user 不支持"显式说明** ✅ 2026-05-22 — README + docs/comparison.md 已加
- [ ] **Discord / TG 群**（< 500 star 前没人会进，先不做）

#### 📣 发布渠道 cheat sheet

**Hacker News（HN）= [news.ycombinator.com](https://news.ycombinator.com)**

| 维度 | 说明 |
|------|------|
| 是什么 | Y Combinator 运营的极简 tech news 聚合站 |
| 受众 | 北美 / 欧洲技术圈 + 独立开发者 + startup 圈 + OSS 维护者——**自部署工具的核心受众** |
| 规模 | 日活 ~200-300 万；上首页 top 30 一次 ≈ 数万曝光 + 1-3k 新 star |
| 形式 | 纯文本标题 + 链接 + comments，无图无视频。**标题决定一切** |
| muselab 适配度 | 高——HN 极爱 self-hosted / 复用 paid subscription / no build step / vanilla HTML 这些标签 |

**Show HN 格式**：自己作品标题前缀 `Show HN:`，例：

```
Show HN: Muselab – Self-hosted web UI for Claude Agent SDK with your own files
```

⚠ **作者必须在评论区全程在线 2-4 小时**回答问题、收 bug 反馈。发完就跑会被社区记恨。

**上首页机制**：

1. 提交后进 `/newest` 队列（按时间倒序，几分钟被新内容刷下去）
2. 前 30 分钟收到 5-10 个 upvote → 算法给一次 front page "试推"
3. 上 front page 后 vote / 评论速率持续涨 → 维持几小时，曝光呈指数累积
4. **黄金时段**：美东周二 / 周三早 5-9 点（北京下午 5-9 点）——欧洲已起床、美东刚到办公室、亚洲下班刷手机
5. 错过黄金时段 → 曝光差 3-5 倍。**周末发是浪费**

**多渠道发布顺序**：

| 渠道 | 受众 | 发布时机 |
|------|------|----------|
| 🥇 HN（Show HN） | 全球技术圈 | 周二 / 周三 北京 17:00 |
| 🥈 Reddit r/selfhosted | 自部署玩家 | HN 当天工作日中午（错峰）|
| 🥈 Reddit r/LocalLLaMA | 本地 LLM 玩家 | 同上，错开几小时 |
| 🥉 V2EX（节点：分享创造） | 中文技术圈 | 国内白天，HN 之后或当天 |
| 配合 | Twitter / X | HN 上首页后立刻发，配 gif 引流 |

**逻辑**：HN 先发，如果上 front page 当天 Reddit / V2EX 会自然 pickup；如果 HN 没爆 Reddit 还能独立尝试。**不要全平台同时撒**——精力分散在评论区。

**HN 投放 checklist**（发那一刻前 30 分钟内）：

- [ ] 标题斟酌过 5 遍以上（短句 / 一句话讲清独特性 / 不要 "Introducing X"）
- [ ] README hero 段读起来没错别字 + gif 已嵌入
- [ ] 自己已登录 HN 在浏览器 tab 开着 comments 页面
- [ ] 接下来 2-4 小时无其他安排（女友 / 会议 / 通勤都得避开）
- [ ] 预先想好 5 个最可能被问的问题，答案打草稿（"why not LobeChat?" / "vs claude-code-ui?" / "security model?" / "how is the archive different from RAG?" / "do you handle multi-user?"）

### 代码层小幅打磨（我能做）

- [x] **Sessions 搜索 + 按日期分组** ✅ 2026-05-22 — 搜索框已有；分组扩到 今天 / 昨天 / 7d / 30d / 更早
- [x] **会话导出**：右键 session → 下载 markdown ✅ — `/api/chat/sessions/{sid}/export` + `menuExportMarkdown` 已实现
- [ ] **消息重发 / 编辑**：用户消息上鼠标悬停显示编辑按钮
- [x] **Mobile Safari 100vh bug**：~~用 `100dvh` 兜底~~ ✅ 2026-05-17
- [x] **Chat 多会话 tab**：~~VS Code 风格~~ ✅ 2026-05-17
- [x] **Tab 持久化**：~~刷新保留 openTabIds / 预览 tab~~ ✅ 2026-05-17
- [x] **全局快捷键**：~~Ctrl+T / Ctrl+W / Ctrl+Tab / Ctrl+1..9 / Esc~~ ✅ 2026-05-18（Ctrl+K 文件搜索 + Ctrl+/ 输入框聚焦 留作未来）
- [x] **拖动 tab 重排序**：~~HTML5 native drag~~ ✅ 2026-05-18（mobile 仍留待未来）
- [x] **关 tab undo toast**：~~5s 内可恢复~~ ✅ 2026-05-18
- [x] **mdRender 流式节流**：~~done/error/cancelled/close 强制 flush~~ ✅ 2026-05-18（80ms coalesce）
- [x] **前端 e2e 测试**（playwright headless）：~~脚手架就位~~ ✅ 2026-05-18（默认 skip；首次启用见 tests/e2e/README.md）
- [x] **CodeMirror Ctrl+S 保存快捷键** ✅ 2026-05-22 — 已绑到 extraKeys；草稿 auto-save 留待未来
- [ ] **进度可视化**：调 claude.ai 接口拉本周期 Pro/Max 用量（看是否有非官方 API）

### 长时运行加固（2026-05-21 业界方案调研后）

> 目标：让 muselab 能稳定跑数小时～数天，autonomous loop / 长任务场景不丢状态。
> 路线选择：A（SDK 自身能力）已用足，C（systemd）+ 应用层 in-flight 持久化最划算。B（Temporal/Restate/LangGraph）单用户场景 overkill，不做。

- [x] **systemd unit 加固** ✅ 2026-05-21
  - `~/.config/systemd/user/muselab.service` 已加 `StartLimitIntervalSec=300` / `StartLimitBurst=5` / `MemoryHigh=2G` / `MemoryMax=4G` / `TasksMax=4096`
  - `Restart=on-failure` 保持不变（手动 stop 不应自动拉）
  - `Type=notify + WatchdogSec` 暂未上，等真出"假死"再加
  - daemon-reload 已执行；新限制在下次 restart 后应用到 running process
- [x] **in-flight turn 持久化** ✅ 2026-05-21
  - 选 (b) UI 提示（不自动续）
  - backend：sidecar 写到 `sessions/active_turns/<sid>.json`（不是 `.muselab/`），用 settings.atomic_write_text；开 turn 时写，正常 / 异常 / timeout 终止时删
  - backend：模块导入时一次性 `_scan_interrupted_turns_at_startup()`，结果存内存 dict；新 turn 启动时 auto-dismiss 同 sid 的旧条目
  - backend：`GET /api/chat/interrupted-turns` 返回列表 + `POST /api/chat/interrupted-turns/{sid}/dismiss`
  - frontend：`_bootApp()` 末尾 fire-and-forget `_checkInterruptedTurns()`，每个中断 turn 一个 warn toast（含 `[打开]` action + 自动 POST dismiss）
  - 不持久化 `last_event_ts`（每 token 一次写盘太贵；起始时间已够 UX 判断）

> 调研来源（查询日期 2026-05-21）：anthropics/claude-code GH #32062 / #10856；anthropics/claude-agent-sdk-python #772；oneuptime 2026-03 systemd watchdog 系列；zylos.ai 2026-02 durable execution 综述；LangGraph PostgresSaver 2026 文档。Temporal/Restate/DBOS/Inngest/LangGraph 五个 durable runtime 单用户场景下 ROI 不够，不引入。

### 进阶能力（按需）

- [ ] **多目录 archive**：SDK `add_dirs` 暴露多 root
- [ ] **标签系统**：给文件/目录打 tag，Muse 按 tag 检索
- [ ] **预算告警 webhook**：cost 超额发邮件 / 推 Telegram
- [ ] **定时任务**：让 Muse 每周整理 health/ 生成周报
- [ ] **目录级 sub-prompt**：进入 health/ Muse 自动切到"健康助理"风格
- [ ] **全文搜索升级**：可选 ripgrep / SQLite FTS5 后端

---

### 全栈扫荡审计（2026-05-21）

> 本次"找一切可优化点"扫荡，确定的已开干（见 CHANGELOG 2026-05-21 块），剩下的列在这里待你 review。

#### 待你拍板（需要决策才能继续）

- [ ] **bubble 长消息宽度不对称（user vs muse）** — 已在已知 Bug 里列出，仍需拍板 (A) / (B) / (C)
- [ ] **`Restart=on-failure` → `Restart=always`?** — 当前手动 `systemctl stop` 后服务不会自动拉。如果你希望 systemctl stop 之外的任何退出都自动恢复（包括 ctrl+c 调试），切到 always。我没改，怕意外干扰你的调试流程。

#### 我有疑虑、留给你 review 后再动

- [ ] **`Type=notify + WatchdogSec=60s` 改造** — 检测"假死"（事件循环阻塞 / 死锁）。需要 Python 端加 `sdnotify` 周期 ping。当前没遇到真"假死"现象，过度工程；先观察 systemd 加固后 1-2 周再决定。
- [x] **`/api/log/client-error` 加 rate limit** ✅ 2026-05-22 — 每 IP 30/min，超额返回 `rate_limited:true` 不写 stderr；test_client_error_rate_limited 锁住
- [ ] **`sessions/index.json` 高并发写损坏防护** — 已在已知 Bug 里。当前 atomic_write_text 防半写，但两个 muselab 进程同时写会丢更新。考虑 fcntl 文件锁（POSIX，跨平台需测 Windows）或迁 SQLite。单用户单进程下不触发；如果未来支持 multi-instance 再考虑。
- [ ] **list_sessions cache 用 mtime + size 而非 TTL** — 当前 2s TTL 兜底外部 JSONL 变化。更精确的做法：跟踪 `sessions/` 目录 + `~/.claude/projects/<root>/` 的 mtime，变化才重新计算。但 cross-platform 文件 watch 复杂，当前 TTL 已经把 list 调用 dedupe 到 0ms，收益边际。
- [ ] **Frontend bundle 拆分** — app.js 已 279 kB。按 muselab "纯 HTML / 无构建" 哲学不该拆。但如果首屏 LCP 成问题，可以考虑：异步加载 CodeMirror / highlight.js / DOMPurify（这些都是 vendored 大块），首屏只加载 chat 必需。需要先量化首屏体验问题再动。
- [ ] **Service worker cache 策略复核** — 当前 sw.js 只做 push notification，没做静态资源缓存。如果 LCP / 重复访问性能是问题再加；自托管局域网下没意义。

#### 我能做、可能值得做的（按 ROI 排）

- [x] **`/api/log/client-error` 加最小 IP rate limit** ✅ 2026-05-22 — 已做
- [ ] **`sdk_list_sessions` 内部 profile** — 还有 ~150ms 的 cold 路径。看 SDK 在做什么，能不能复用一次 directory walk。需要读 SDK 源码。
- [ ] **首屏 LCP 量化** — 用 chrome devtools performance tab 量首次进入的关键指标，证明是否需要优化。先量化再优化。
- [ ] **CSP 部分启用**：`script-src 'self' 'unsafe-inline' 'unsafe-eval'` 阻止外部 script 注入但保留 Alpine.js 内联事件。比"完全不设 CSP"安全一点点。但 Alpine 的 `x-on:`/`@click` 走的不是 inline script 协议，需要测试。
- [ ] **Frontend "无障碍审计"** — 用 axe-core 跑一遍，输出 a11y 问题列表。muselab 是个人工具不强追求 WCAG，但开源后可能有 a11y 工具用户。

## 🚫 明确不做（已决策）

- ❌ **多用户支持** — muselab 定位单人自部署。要多人用就跑多实例
- ❌ **Token 走 cookie + CSRF** — 单用户场景 query token 风险可接受
- ❌ **OpenAI 兼容路由器** — 强依赖 Anthropic Messages 协议（厂商有兼容端点的才接）
- ❌ **plugin store / marketplace** — LobeChat 已经占了。muselab 的差异化是个人档案不是通用 plugin
- ❌ **本地 LLM (Ollama) 优先** — OpenWebUI 占了。Muse 跟 Anthropic SDK 绑死换不了

---

## 🐛 已知 Bug / 技术债

- [ ] **bubble 长消息宽度不对称（user vs muse）— 2026-05-20**
  - 现状：bubble 用 `fit-content + max-width: calc(100% - 40px)`。muse markdown 内是连续 `<p>` 块（长段无 forced break），max-content 撑到 cap；user 文本里的 `\n`（pre-wrap）或 `<br>` 是 forced line break，max-content = 最长那一行宽 → 多行 user 消息永远比 muse 长消息窄
  - 三选项：(A) 接受 trade-off（建议用户少手动换行）；(B) user bubble 改 `width: calc(100% - 40px)` 撑横条，短消息也满宽；(C) 用 JS 把 user 文本 push 进 markdown 渲染 pipeline，soft-break 不形成 forced break
  - 用户暂未拍板。需要拍板：你想要 (A) / (B) / (C)?
- [ ] `sessions/index.json` 高并发写入可能损坏（加文件锁 / 改 SQLite）
- [ ] 大文件 read 接口无流式（>2MB 直接 413）— 主要影响 archive 里大 log 预览
- [ ] CodeMirror 切文件需手动退出编辑再进入（不会自动加载新内容）
- [ ] thinking blocks 显示后切换 session 可能残留（小概率，主要影响 ✅ 已用过/再开同 session）
- [ ] iframe HTML 预览偶发 `sandbox=""` 跟 `allow-scripts` 切换不及时（多 tab 场景）

---

## 📌 决策待办

- [ ] **首发渠道** & 时间：HN「Show HN」/ Reddit r/selfhosted / V2EX / X / 微信圈子 — 看你想触达哪类人群
- [ ] **README badge 加哪些**：CI 状态 / docker pulls / license / GitHub stars
- [ ] **要不要主动联系 Anthropic** 报备项目（避免被认为 abuse Pro OAuth）

---

## 🎯 项目定位（2026-05-21 校准）

**一句话**：muselab 是个开源 self-hosted Web UI，让 Muse（基于 Claude Agent SDK）跟你的真实 archive 对话；多端同步，archive / 配置 / credentials 不通过任何 SaaS。

**4 个核心能力**（按"用户能拿到啥"排）：

1. **完整的个人上下文**——`MUSELAB_ROOT` 指向你的 archive；6 个预置子目录（`health / work / money / people / notes / archives`）+ CLAUDE.md 自加载，Muse 默认就懂你
2. **完整 Agent 能力**——基于 Claude Agent SDK（业界最完整的开源 agent runtime）：MCP / Skills / Subagent / plan / tool use / 流式 partial
3. **优秀基础模型 + 多模型路由**——同一 agent loop 接入 Claude / DeepSeek / GLM / MiniMax，零 vendor 切换成本
4. **多端同步 + 无中转**——自部署 server 让手机/电脑/平板共享 sessions；archive / 配置 / credentials 不通过任何 SaaS；模型厂商只看到你授权的对话内容

**额外卖点（不是核心能力，但是用户的决策点）**：

- **复用 Pro/Max 订阅**——通过 Anthropic 官方 OAuth 路径（`~/.claude/.credentials.json`），相比 API key 按量计费可省 $20-100/月。但 OAuth 本身是 Anthropic 推的，不是 muselab 的技术壁垒；竞品想接也能接
- **无构建链**——前端 vanilla HTML + Alpine.js，没 npm / webpack；自部署敢真用、敢改

**澄清（避免外宣 overclaim）**：

- "Claude Agent SDK 是 Anthropic 写的"——muselab 的贡献是把它在浏览器里铺平 + 多 tab + 多模型路由 + 多端同步，不是发明 agent loop
- "数据不出本机"只指 archive / sessions / 配置 / credentials；你发给模型的对话内容（包括 Muse 替你读的文件内容）**会**到 vendor 那里
- 跟 Claude.ai / ChatGPT 的区别：没有 SaaS 中间商持有/审阅/训练你的全部历史，仅有 vendor 看到你当下授权的那一轮对话
