# TODO — muselab

> 目标：成为有亮点的开源项目。
> 排序原则：开源前必修 → 强化亮点 → 体验打磨。

---

## 🌙 今晚冲刺（2026-05-16 → 17 凌晨，全部完成）

> 14 / 14 ✅。122 passed (pytest)。下一项见 P0 开源前必修 / 顶部下一步建议。

### P0 — 必做 ✅
- [x] **A. smoke test 现状** — alpine guard 就位；90→122 测试全过
- [x] **B1. Permission request UI** — `backend/permission_request.py` + 前端 Allow/Deny/Always 按钮；10 test
- [x] **C. 默认 CLAUDE.md 预设** — `scripts/templates/default-CLAUDE.md`（4 persona）+ 3 OS install 脚本 hook + `docs/personalize-claude-md.md`
- [x] **E1. MCP — Settings UI 管理** — `/api/settings/mcp` CRUD + 前端面板 + 10 test
- [x] **F1. Skill — 重新启用 + 加载本地** — disallow 里去掉 Skill；`setting_sources=["user","project","local"]` + `skills="all"`

### P1 ✅
- [x] **F2. Skill — 预置 7 个** — `skills/{web-search,markdown-formatter,mermaid-helper,code-reviewer,citation-formatter,task-decomposer,summary-distiller}/SKILL.md` + `skills/README.md`
- [x] **E2. MCP — 预置升级** — `mcp.json.example` 加 `sequential-thinking` + `time` + description 字段；3 OS install 检 npx/uvx
- [x] **B2. TodoWrite 专用 UI** — chat 内 checkbox 任务列表 + 状态徽章

### P2 ✅
- [x] **B3. Subagent (Task) 进度可见** — Task tool 卡片：subagent_type tag + description + 折叠 prompt
- [x] **B4. ExitPlanMode UI** — plan 卡片：markdown 渲染
- [x] **E3. MCP tool 渲染 polish** — `mcp__` 前缀去掉 + 🔌 icon + server 边色
- [x] **F3. Skill Settings UI** — `/api/settings/skills` + 前端列表（project / user scope）；6 test
- [x] **F4. Skill 调用渲染** — 🧩 icon + 橙色边
- [x] **D. 爆款项目调研** — `docs/competitive-analysis.md`（8 对标 + 3 周路线图 + README hero A/B）

### P3 ✅
- [x] **B5. ImageBlock 输入** — `POST /api/chat/upload-image`（10MB 限 + TTL）+ 前端粘贴/拖拽/选择 + thumbnail；6 test

---

## 🎯 项目定位

**一句话**：~2000 行可读 web 入口，浏览器里用 Pro 订阅跟 Claude 对话管你的档案。

**差异化亮点**：
1. 复用 Pro/Max 订阅（不要 API key，省 $20-100/月）
2. ~2000 行，单人可读完
3. 无 npm / 无 webpack（clone 即跑）
4. 专为个人档案 / 知识库设计

---

## ✅ 已完成

### 核心功能
- [x] 三栏 UI：文件树 / 预览 / chat
- [x] Markdown / HTML / SVG（沙盒）/ 图片 / PDF / 代码 预览
- [x] 文件树折叠展开 + 展开状态记忆
- [x] 文件名搜索 + **全文搜索（纯 Python，跨平台无 grep 依赖）**
- [x] 上传 / 下载 / 删除 / 新建目录 / **重命名**
- [x] **拖拽上传**到任意目录节点
- [x] **右键菜单**：预览 / @引用 / 复制路径 / 下载 / 重命名 / 删除
- [x] **CodeMirror 5 编辑器**：14 种语言高亮 + 行号 + 括号匹配 + 主题跟随
- [x] @ 文件引用（chat 输入框 @ 触发文件选择）
- [x] 消息复制 + 滚动停留 + 跳到最新
- [x] 代码块语法高亮（highlight.js，主题随明暗切换）

### Session 与 SDK 能力
- [x] 多会话切换（SDK `session_id` + `resume`）
- [x] 服务端持久化（`sessions/index.json` + per-session JSON）
- [x] 模型切换（Sonnet / Haiku / Opus）
- [x] 权限模式（bypass / acceptEdits / default / plan）
- [x] thinking blocks 显示开关
- [x] 累计成本显示 + per-message cost
- [x] Stop / reset 当前会话
- [x] **MCP 服务器支持**（mcp.json 一键启用 4 个官方 server）

### UI 系统
- [x] Lucide SVG 图标（统一 stroke 风格）
- [x] 设计 token（color/spacing/typography）
- [x] **亮/暗主题切换**（跟随系统 + localStorage 持久化）
- [x] Toast 通知 替代 alert
- [x] Modal 对话框 替代 confirm/prompt
- [x] 动画过渡（msg-in / modal-in / toast-in / ctx-in）
- [x] 空状态设计
- [x] 全局 SVG 默认 stroke=currentColor（修按钮不可见 bug）

### 跨平台 / 部署
- [x] Pure Python 全文搜索（Windows 友好）
- [x] **Dockerfile**（多阶段 + 非 root + healthcheck + 预装 MCP runtime）
- [x] **docker-compose.yml**（env_file + 3 volume + 强制 unset API key）
- [x] .dockerignore
- [x] README 跨平台 quick start（Docker / Native 两条）
- [x] systemd / launchd / Windows Service 部署指引
- [x] **英文 README 重写**（专业，含 vs 同类对比表）

### 安全
- [x] 路径穿越防护（`safe_resolve`）
- [x] 敏感文件名硬阻（`.env*` / `.pem` / `id_rsa` / `credentials*` 等）
- [x] **MUSELAB_ROOT 黑名单**（禁 `/` / `/etc` / `/home` / `$HOME` 等）
- [x] **MUSELAB_TOKEN 最小长度 16 校验**
- [x] **XSS 防护**：marked 输出全过 DOMPurify
- [x] **HTML/SVG 预览沙盒**：iframe `sandbox=""` + 强 CSP
- [x] 启动时自动 unset `ANTHROPIC_API_KEY` 强制走 Pro OAuth
- [x] `Content-Disposition: inline` 显式标注，防浏览器默认下载

### 已修 Bug
- [x] 隐藏 iframe/img 触发 raw URL 请求 → md/txt 被下载（改条件 src 绑定）
- [x] Alpine `<template x-if>` 多个嵌套报错（统一用 x-show + 条件 src）
- [x] SVG 默认 fill=black 看不见（全局 stroke=currentColor）
- [x] HTML 文件被强制 attachment 下载（改回沙盒 iframe 预览）

---

## 🔴 P0 — 开源前必修

### 安全（剩余）
- [ ] **Token 不能放 URL 查询串**：SSE / `/raw` / `/download` 都暴露在 nginx access log。改 HttpOnly cookie + CSRF token 或短时效 signed token（hmac）
- [ ] **多用户支持**：username/password hash + role；每用户独立 ROOT 或 sandbox
- [ ] **上传大小 / 类型限制**：默认 100MB；可执行黑名单
- [ ] **路径校验**：symlink 跟随风险（当前 resolve 跟随 symlink，可能越界）
- [ ] **速率限制**：登录失败次数 / API 调用频率

### 开源物料
- [ ] **LICENSE 文件**（MIT）
- [ ] **SECURITY.md**（README 已引用但文件不存在）
- [ ] **README_zh.md**（英文 README 已引用但文件不存在）
- [ ] **demo gif**：录三栏 + @文件 + 工具调用 全流程
- [ ] **首页截图**：高质量 PNG 放 README 顶部
- [ ] **CONTRIBUTING.md**：开发约定 + PR 流程
- [ ] **Issue / PR 模板**（.github/ISSUE_TEMPLATE/）
- [ ] **CHANGELOG.md**（Keep a Changelog 格式）

### 基础质量
- [ ] **pytest 测试**：文件 CRUD + auth + session + grep + 敏感文件阻断 + 路径穿越
- [ ] **GitHub Actions**：ruff lint + pytest + Docker build & push
- [ ] **Docker 镜像发布**：GitHub Container Registry 自动构建 multi-arch（amd64 + arm64）
- [ ] **Docker 镜像本地实测**（Dockerfile 当前未亲自 build 验证）

---

## 🟡 P1 — 强化核心亮点

### 复用 Pro/Max 订阅
- [ ] **配额可视化**：调 claude.ai 接口拉本周期用量（看是否有官方 API）
- [ ] **预算告警**：累计 cost 超阈值（如 $1/天）弹通知

### 个人档案定位
- [ ] **多目录管理**：SDK `add_dirs` 把 multiple roots 都暴露
- [ ] **标签系统**：给文件/目录打 tag（health/career/finance），Claude 按 tag 检索
- [ ] **目录级 system prompt**：进入 `health/` 切到"健康助理"prompt
- [ ] **全文搜索升级**：可选 ripgrep / SQLite FTS5 后端（保留 Python fallback）
- [ ] **MCP 配置 UI**：在 Settings 面板可视化勾选启用 MCP server，不用手改 mcp.json

---

## 🟢 P2 — 用户体验

### chat
- [ ] **消息引用回复**（quote 上文重发）
- [ ] **消息编辑重发**（用户消息可改）
- [ ] **会话置顶 / 分组 / 搜索**
- [ ] **会话导出**（markdown）

### 文件树
- [ ] **拖拽移动**（拖文件到另一目录）
- [ ] **多选 + 批量操作**

### 编辑器
- [ ] **预览 / 编辑分屏**（不切换，左编辑右预览）
- [ ] **图片粘贴上传**（编辑器粘贴图片自动上传 + 插入 markdown）
- [ ] **Ctrl+S 保存快捷键**（CodeMirror 已就绪）
- [ ] **CodeMirror auto-save 草稿**（断电不丢）

### 通用
- [ ] **全局快捷键**：`Ctrl+K` 文件搜索、`Ctrl+/` 聚焦聊天、`Ctrl+B` 折叠侧栏
- [ ] **移动端响应式**（折叠为单栏 + tab 切换）
- [ ] **i18n**：至少中英双语

---

## 🔵 P3 — 进阶能力

- [ ] **DeepSeek / 第三方模型**：通过 [claude-code-router](https://github.com/musistudio/claude-code-router) 用 `ANTHROPIC_BASE_URL` 走代理（已调研）
- [ ] **定时任务**：让 Claude 每周整理一次 health/ 目录生成周报
- [ ] **多 agent 预设**：保存不同 system_prompt（健康/投资/面试官）
- [ ] **历史会话备份恢复**（导出 .zip / 导入）
- [ ] **Webhook 通知**：撞 cost 阈值、长任务完成时推送
- [ ] **跨设备同步**：通过 git 自动同步 sessions/ 到私有 repo

---

## ⚪ P4 — 营销 / 发布

- [ ] **landing page**：GitHub Pages 一页式
- [ ] **配套博客**：
  - 复用 Claude Pro 省 API 费的实战
  - 100MB web 知识库自托管全套
  - 为什么不用 VSCode
- [ ] **HackerNews / V2EX / Reddit 发布**
- [ ] **README badges**（CI / version / docker pulls / license）

---

## 🐛 已知 Bug / 技术债

- [ ] `nohup ... &` 在某些 shell 启动方式偶发 exit 144（需 setsid + bash -c 包裹）
- [ ] `sessions/index.json` 高并发写入可能损坏（加文件锁 / 改 SQLite）
- [ ] 大文件 read 接口无流式（>2MB 直接 413）
- [ ] thinking blocks 显示后切换 session 可能残留
- [ ] 多用户化后 `_stats` 全局共享（不区分用户）
- [ ] CodeMirror 切文件需手动退出编辑再进入（不会自动加载新内容）

---

## 📌 决策待办

- [ ] **国际化优先级**：英文 README 已完成，中文版要不要补
- [ ] **要不要做 hosted SaaS 版本**
- [ ] **目标用户画像**：自用 vs Claude Pro 用户群（影响 P0+P4 力度）
- [ ] **首发渠道**：HN / Reddit / V2EX / Twitter 哪个

---

## 🎯 下一步建议（按 ROI 排）

1. **Docker 镜像本地实测**（P0，30 分钟）— 没实测就上 README 是赌
2. **LICENSE + SECURITY.md + README_zh.md**（P0，1 小时）— README 已经引用，文件不存在很糙
3. **pytest 几个 happy path**（P0，1-2 小时）— GitHub 上没 CI 没人信
4. **Token 安全**（P0，半天）— signed cookie + CSRF
5. **demo gif**（P0，30 分钟）— 没动图没人理
6. **MCP 配置 UI**（P1，半天）— 把 MCP 这个亮点真正可视化

---

最后更新：2026-05-17
