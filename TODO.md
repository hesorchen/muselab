# TODO — muselab

> 状态：核心可用，开源前所有 P0 / P1 已清。剩 demo gif 和小幅打磨。
> 排序原则：开源前必修 → 强化亮点 → 体验打磨。

最后更新：2026-05-17

---

## 📈 现状速览

- **148 tests passing**（pytest）
- **多模型**：Claude (Pro OAuth) + DeepSeek + GLM + MiniMax
- **三个 OS 一键安装**（systemd / launchd / Task Scheduler）+ `doctor` / `intake` 工具
- **multi-arch Docker** 镜像通过 GH Actions 自动发到 `ghcr.io/hesorchen/muselab`
- **移动端响应式**：< 900px 折叠成 3-tab 单 pane
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

### 2026-05-17：UX 大改 + bug 清扫

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

### 真正会影响开源声量的（你自己做）

- [ ] **Demo gif**（30 秒：三栏 + onboarding 卡片 + 切模型 + skill chip 触发 + context meter 警告 + 压缩流程）
  - VPS 没桌面，**需要笔记本上录**
  - 建议工具：macOS QuickTime + gifski；Linux peek；Windows ScreenToGif
- [ ] **首页截图 hero PNG**（chat 满状态 + onboarding 卡片各一张）
- [ ] **Hosted 只读 demo**（Cloudflare tunnel / 你 VPS 套个 read-only token）
- [ ] **HN / Reddit / V2EX 发布**：标题用 README hero 那句 + 30s gif

### 代码层小幅打磨（我能做）

- [ ] **Sessions 搜索**：list > 20 时加搜索框 + 按日期分组（今天 / 本周 / 更早）
- [ ] **会话导出**：右键 session → 下载 markdown
- [ ] **消息重发 / 编辑**：用户消息上鼠标悬停显示编辑按钮
- [ ] **全局快捷键**：Ctrl+K 文件搜索 / Ctrl+/ 聚焦输入框 / Ctrl+B 折叠侧栏
- [ ] **Mobile Safari 100vh bug**：用 `100dvh` 兜底（输入框被浏览器底栏遮的问题）
- [ ] **CodeMirror Ctrl+S 保存快捷键** + auto-save 草稿
- [ ] **进度可视化**：调 claude.ai 接口拉本周期 Pro/Max 用量（看是否有非官方 API）

### 进阶能力（按需）

- [ ] **多目录 archive**：SDK `add_dirs` 暴露多 root
- [ ] **标签系统**：给文件/目录打 tag，Muse 按 tag 检索
- [ ] **预算告警 webhook**：cost 超额发邮件 / 推 Telegram
- [ ] **定时任务**：让 Muse 每周整理 health/ 生成周报
- [ ] **目录级 sub-prompt**：进入 health/ Muse 自动切到"健康助理"风格
- [ ] **全文搜索升级**：可选 ripgrep / SQLite FTS5 后端

---

## 🚫 明确不做（已决策）

- ❌ **多用户支持** — muselab 定位单人自部署。要多人用就跑多实例
- ❌ **Token 走 cookie + CSRF** — 单用户场景 query token 风险可接受
- ❌ **OpenAI 兼容路由器** — 强依赖 Anthropic Messages 协议（厂商有兼容端点的才接）
- ❌ **plugin store / marketplace** — LobeChat 已经占了。muselab 的差异化是个人档案不是通用 plugin
- ❌ **本地 LLM (Ollama) 优先** — OpenWebUI 占了。Muse 跟 Anthropic SDK 绑死换不了

---

## 🐛 已知 Bug / 技术债

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

## 🎯 项目定位（不再变）

**一句话**：在浏览器里复用你的 Claude Pro / Max 订阅，让 Muse 跟你的真实档案对话。

**4 个差异化亮点**（按重要性排）：

1. **复用 Pro/Max 订阅** — 省 $20-100/月 vs 按 API 计费
2. **专为个人档案设计** — health / career / money / people / notes / archives 6 个预置子目录 + universal CLAUDE.md 模板
3. **~4.4 k 行可读完** — 无 npm / 无 webpack / 单人项目质感
4. **完整 Agent SDK 能力**：MCP / Skills / Subagent / plan / 图片 / PDF / 流式 partial — 跟 Claude Code 同源
