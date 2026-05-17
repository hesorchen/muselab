// ==========================================================================
// i18n — 中英双语字符串表。新增条目时两边都要加，否则 t() 回退到 key 暴露缺漏。
// ==========================================================================
const STRINGS = {
  zh: {
    // panes / titles
    "pane.files": "Files",
    "pane.preview": "Preview",
    "pane.chat": "Muse",
    // sidebar / toggles
    "btn.hide_left": "隐藏文件区",  "btn.show_left": "显示文件区",
    "btn.hide_right": "隐藏 Muse", "btn.show_right": "显示 Muse",
    "btn.show_hidden": "显示隐藏文件", "btn.hide_hidden": "不显示隐藏文件",
    "btn.refresh": "刷新",
    "btn.upload": "上传到当前目录",
    "btn.new_file": "新建文件",
    "btn.new_dir": "新建子目录",
    "btn.search": "全文搜索",
    "btn.theme_light": "切到浅色", "btn.theme_dark": "切到深色",
    "btn.settings": "设置",
    "btn.logout": "退出",
    "btn.stop": "停止",
    "btn.send": "发送",
    "btn.save": "保存",
    "btn.cancel": "取消",
    "btn.confirm": "确定",
    "btn.edit": "编辑",
    "btn.delete": "删除",
    "btn.rename": "重命名",
    "btn.download": "下载",
    "btn.copy_path": "复制路径",
    "btn.preview": "预览",
    "btn.at_mention": "@ 引用到 chat",
    "btn.new_session": "新会话",
    "btn.edit_prompt": "编辑系统提示词",
    "btn.close": "关闭",
    // file pane / search
    "files.empty": "目录为空，拖文件进来或点 + 新建",
    "files.empty_search": "没有匹配",
    "files.searching": "搜索中…",
    "files.search_more": "（结果已截断，请细化关键词）",
    "files.search_placeholder": "搜索文件名和内容…",
    // chat
    "chat.placeholder": "和 Muse 聊点什么…（@ 引用文件，Shift+Enter 换行）",
    "chat.thinking": "Muse 正在思考…",
    "chat.empty_tip1": "输入消息按",
    "chat.empty_tip2": " 把文件递给 Muse",
    "chat.empty_tip3": "换模型不丢历史",
    "chat.runs_on": "runs on",
    "chat.no_session": "还没选会话，点左下「+ 新会话」开始",
    "chat.session_prompt": "本会话系统提示词（叠加在 muselab 默认 + CLAUDE.md 之上）",
    "chat.attach_files": "已附带文件（点 × 移除）",
    // preview empty state
    "empty.preview_tagline": "Meet Muse — an AI that actually knows you.",
    "empty.preview_tip1": "从左侧选文件 — Muse 看得见",
    "empty.preview_tip2": " 在右侧把文件递给 Muse",
    "empty.preview_tip3": "右键文件查看更多操作",
    "preview.action_new_note": "新建笔记",
    "preview.action_upload": "上传文档",
    "preview.new_note_title": "新建 markdown 笔记",
    "preview.new_note_body": "文件名（默认放 archive 根目录；可写 health/foo.md 等子路径）",
    // login
    "splash.greet": "正在唤醒 Muse...",
    "splash.slow": "还在启动，请稍等（首次加载会久一点）",
    "conn.reconnecting": "🔄 muselab 失联中 — 正在重试...",
    "conn.reconnected": "✓ 已重连",
    "login.sub": "你的私人 AI 助理，同时记得你的所有维度。",
    "login.tagline": "muse · lab",
    "login.token_placeholder": "MUSELAB_TOKEN",
    "login.go": "进入",
    "login.err": "Token 错误",
    "login.hint_on_err": "Token 在 muselab 安装目录的 .env 文件里（找 MUSELAB_TOKEN=）。第一次用？跑 scripts/install-{linux,macos,windows}",
    // settings modal
    "set.title": "设置",
    "set.sec.provider": "Provider API Keys",
    "set.sec.appearance": "外观",
    "set.sec.defaults": "新会话默认",
    "set.sec.model_params": "模型参数",
    "set.sec.sessions": "会话",
    "set.sessions.cleanup_label": "空会话清理",
    "set.sessions.cleanup_btn": "清理空会话",
    "set.sessions.cleanup_confirm_title": "清理空会话",
    "set.sessions.cleanup_confirm_body": "删除所有 0 条消息的会话（未发过任何消息的占位项）。无法撤销。",
    "set.sessions.cleanup_done": "已删除 {n} 个空会话",
    "set.probe": "测试",
    "set.probe_tip": "向 vendor 真发一条 ping，看 key 是否真的有效",
    "set.probe_running": "正在测试 key...",
    "set.probe_ok": "key 有效，端点通",
    "set.probe_failed": "测试失败",
    "set.probe_hint_401": "key 无效，去 vendor 控制台查",
    "set.probe_hint_403": "可能需要在 vendor 开通对应服务",
    "set.probe_hint_429": "限流了，等一会再试",
    "set.sec.mcp": "MCP 工具服务器",
    "set.mcp.hint": "MCP 让 Muse 调用外部工具（fetch、time、sequential-thinking 等）。开关控制是否加载；删除会移除配置。",
    "set.mcp.empty": "尚未配置 MCP server。点击下方添加，或从预设安装。",
    "set.mcp.disabled": "已禁用",
    "set.mcp.toggle_title": "启用 / 禁用此 server",
    "set.mcp.delete": "删除",
    "set.mcp.presets": "预设：",
    "set.mcp.add": "添加 MCP server",
    "set.mcp.name": "名字",
    "set.mcp.command": "命令",
    "set.mcp.args": "参数（空格分隔）",
    "set.mcp.added": "已添加",
    "set.mcp.deleted": "已删除",
    "set.mcp.installed": "已从预设安装",
    "set.mcp.toggle_saved": "已切换",
    "set.mcp.save_failed": "保存失败",
    "set.mcp.delete_failed": "删除失败",
    "set.mcp.name_command_required": "名字和命令都不能为空",
    "set.sec.skills": "Skills（模型可发现的能力包）",
    "set.skills.hint": "Muse 启动时从 ~/.claude/skills 和 muselab/skills 加载。模型按任务自动调用，无需手动启用。",
    "set.skills.empty": "未发现任何 skill。在 ~/.claude/skills 或本项目 skills/ 下新增 SKILL.md 即可。",
    "slash.hint": "↑↓ 选 · Enter / Tab 选中 · Esc 关",
    "slash.none": "没有匹配的命令",
    "slash.help_title": "muselab 帮助",
    "help.sec_slash": "斜杠命令",
    "help.sec_keys": "键盘快捷键",
    "help.keys_list": "- `Enter` — 发送消息\n- `Shift+Enter` — 换行\n- `@` — 引用 archive 里的文件\n- `/` — 斜杠命令面板\n- `↑ ↓` — 在 @ / `/` 面板里选项\n- `Esc` — 关闭面板 / modal / 中止流式\n- `Ctrl+S` — 保存编辑中的文件（CodeMirror 内）",
    "help.sec_layout": "三栏布局",
    "help.layout_list": "- **左**：archive 文件树 + 全文搜索（右键文件看更多动作）\n- **中**：预览 / 编辑当前文件（md / 代码 / 图片 / PDF / HTML 沙盒）\n- **右**：聊 Muse（顶部 chip 显示 CLAUDE.md / MCP / cost / 模型）",
    "help.docs_link": "完整文档",
    "slash.cleared": "已清空当前会话",
    "slash.failed": "命令执行失败",
    "slash.compact_ok": "已创建压缩会话 — 输入框已预填请求语，回车发送",
    "slash.compact_prompt": "请把上一个会话的所有内容压缩成结构化的简短摘要，保留：关键事实 / 已做决定 / 待办 / 引用过的具体文件路径。摘要将作为新会话的起点。",
    "slash.model_list_title": "可选模型（点 dropdown 切换更直观）",
    "slash.model_unknown": "找不到模型：{id}",
    "slash.model_switched": "已切到 {id}",
    "slash.resume_list_title": "最近 10 个会话（带 ID 前缀和消息数）",
    "slash.resume_no_match": "没找到匹配的会话",
    "slash.resumed": "已跳到「{name}」",
    "slash.cost_title": "当前用量",
    "slash.unknown": "未知命令：/{cmd}（输入 /help 查看全部）",
    "cost.total": "总成本",
    "cost.in_out": "输入/输出 tokens",
    "cost.cache_hit": "缓存命中",
    "cost.budget": "预算",
    "cost.no_budget": "未设预算（可在 Settings 加 MUSELAB_BUDGET_USD）",
    "cost.context": "当前会话 context",
    "cost.budget_warn": "预算告警：已用 {pct}% / ${usd}",
    "ctx.title": "当前会话 context tokens",
    "ctx.claude_md_tip": "Muse 已加载 {root}/CLAUDE.md（上次修改 {date}）",
    "ctx.no_claude_md": "{root} 里没有 CLAUDE.md — Muse 不认识你，点击查看怎么配",
    "ctx.no_claude_md_title": "Muse 还不认识你",
    "ctx.no_claude_md_body": "你的 archive 在：\n  {root}\n\nMuse 启动时会读 {root}/CLAUDE.md 作为「关于你」的档案。现在没有这个文件，所以 Muse 只能用默认行为，不能基于你的真实情况给建议。\n\n建议：\n  1. 跑 scripts/install-{linux,macos,windows} 里的 intake（推荐）\n  2. 或手动复制：scripts/templates/default-CLAUDE.md → {root}/CLAUDE.md，按提示填\n\n详见 docs/personalize-claude-md.md",
    "onboard.no_provider_title": "🔑 还没配置模型，无法对话",
    "onboard.no_provider_body": "Muse 需要至少一个模型 provider。可选：\n  • Claude（Pro/Max 订阅）：终端跑 `claude login` 一次（免费配额）\n  • Claude（API 按量）：去 console.anthropic.com 拿 key 填到 Settings\n  • DeepSeek / GLM / MiniMax：去对应控制台拿 API key 填到 Settings",
    "onboard.no_provider_cta": "打开 Settings",
    "onboard.no_claude_md_title": "👋 Muse 还不认识你",
    "onboard.no_claude_md_body": "Muse 是一个整体助理 — 同时帮你管身体、做的事、钱、关心的人、生活。在它能给针对性建议之前，需要你先做一份 3 分钟的档案。",
    "onboard.no_claude_md_cta": "查看怎么设置",
    "onboard.empty_archive_title": "📂 你的 archive 里还没有文档",
    "onboard.empty_archive_body": "把真实材料放进对应的子目录，Muse 才能基于它们给建议。下面是已建好的目录：",
    "onboard.dir_health": "体检报告 / 补剂方案 / 训练记录",
    "onboard.dir_work":   "在做的事的产物（学业 / 简历 / 作品集 / 项目）",
    "onboard.dir_money":  "预算 / 持仓 / 学贷 / 保单",
    "onboard.dir_people": "关心的人的资料",
    "onboard.dir_notes":  "学习笔记 / 灵感 / 日记",
    "onboard.ready_title": "💡 试试问 Muse",
    "onboard.q_health":   "看看我的健康档案，有什么需要注意的",
    "onboard.q_work":     "帮我看看简历 / 学业材料",
    "onboard.q_money":    "看一下我的财务状况，给建议",
    "onboard.q_people":   "我关心的人最近有哪些需要跟进的",
    "onboard.q_overview": "我的 archive 里有什么？整理一下给我看",
    "onboard.skill_hint": "或试试这些 skill：",
    "ctx.cache_tip": "缓存命中率 — 越高越省钱。Anthropic prompt cache 在 5 分钟内复用上次的系统提示词 + 上下文。",
    "ctx.normal": "上下文 {used}K / {limit}K · {pct}% · 缓存 {cached}K",
    "ctx.warn":   "上下文用了 {pct}%（{used}K / {limit}K · 缓存 {cached}K）· 长对话变贵，可压缩",
    "ctx.danger": "上下文已用 {pct}%（{used}K / {limit}K · 缓存 {cached}K）· 即将截断 · 立即压缩",
    "ctx.compact_btn": "压缩历史",
    "ctx.tip_line1": "每次回答都把整段历史送给模型 → token 越多越贵越慢",
    "ctx.tip_line2": "压缩 = Muse 总结上文 → 新会话只带摘要起步，省钱也变快",
    "ctx.compact_confirm_title": "压缩这个会话",
    "ctx.compact_confirm_body": "Muse 会先让模型把全部历史总结一段，然后用这段摘要新建一个会话作为起点。原会话不动，可以随时切回。",
    "ctx.compact_summarize_prompt": "请把这个会话的全部要点压缩成结构化摘要：①事实背景 ②已做决定 ③待办或下一步 ④引用过的具体文件路径。要让一个第一次看到摘要的人能凭它继续对话。",
    "ctx.compact_step1": "Step 1/2: 让模型总结上文...",
    "ctx.compact_done": "压缩完成，已切到新会话",
    "ctx.compact_wait_streaming": "Muse 还在回复中，等这一轮结束再压缩",
    "ctx.compact_empty": "当前会话还没有 Muse 的回复，没什么可压缩的",
    "ctx.compact_timeout": "压缩超时（60s）— Muse 可能在处理长历史，再试一次",
    "ctx.compact_no_reply": "模型没返回摘要 — 检查 Muse 是不是出错了，再重试",
    "model.switch_title": "切换模型",
    "model.switch_body": "切换到 {label} 需要新建会话。确认吗？",
    "model.switch_new": "新建并切换",
    "model.new_session_ok": "已新建 {label} 会话",
    "img.attach": "粘贴 / 拖拽 / 选择图片",
    "attach.title": "粘贴 / 拖拽 / 选择图片或文档（PDF、md、txt、json、代码……）",
    "attach.bad_type": "不支持的文件类型",
    "img.remove": "移除",
    "img.bad_type": "不支持的图片格式（png/jpg/gif/webp）",
    "img.too_big": "图片超过 10MB",
    "img.upload_failed": "图片上传失败",
    "img.wait_upload": "等图片上传完再发送",
    "todo.title": "任务清单",
    "todo.doing": "进行中",
    "subagent.label": "派子 agent",
    "subagent.prompt_toggle": "展开 prompt",
    "plan.title": "Muse 提出的计划",
    "set.sec.lang": "语言",
    "set.label.lang": "界面语言",
    "set.label.theme": "主题",
    "set.label.accent": "主题色",
    "set.label.default_model": "默认模型",
    "set.label.default_permission": "默认权限",
    "set.label.show_thinking_default": "默认显示 thinking 块",
    "set.label.thinking_budget": "思考预算（tokens）",
    "set.label.max_turns": "最多工具回合",
    "set.lang.zh": "中文", "set.lang.en": "English",
    "set.theme.light": "浅色", "set.theme.dark": "深色",
    "set.key_set": "已配置", "set.key_unset": "未配置",
    "set.placeholder_key": "输入 API Key 并保存",
    // toasts
    "toast.saved": "已保存",
    "toast.save_failed": "保存失败",
    "toast.deleted": "已删除",
    "toast.delete_failed": "删除失败",
    "toast.copied": "已复制",
    "toast.uploaded": "上传完成",
    "toast.upload_failed": "上传失败",
    "toast.renamed": "重命名完成",
    "toast.rename_failed": "重命名失败",
    "toast.created": "已创建",
    "toast.token_required": "请填 Token",
    "toast.model_switched": "已切到 {label}，下条消息立刻用新模型（不用新建会话）",
    "toast.muse_back": "Muse 回来啦",
    "toast.mention_added": "已把 {path} 递给 Muse",
    "toast.lang_switched": "已切换到中文",
    "ask.title": "Muse 在等你选",
    "ask.submitted": "已提交",
    "ask.multi": "可多选",
    "ask.submit": "提交",
    "ask.unanswered": "还有问题没选",
    "ask.submit_failed": "提交答案失败",
    "perm.title": "Muse 想用一个工具",
    "perm.allow": "允许",
    "perm.deny": "拒绝",
    "perm.always": "本次会话总是允许",
    "perm.decision_allow": "已允许",
    "perm.decision_deny": "已拒绝",
    "perm.decision_always": "已加白",
    "perm.submit_failed": "提交决定失败",
    // modal generic
    "modal.confirm_delete": "确认删除 {name}？此操作不可恢复。",
    "modal.input_required": "请输入内容",
    "modal.dirty_save": "当前文件有未保存改动，先保存吗？",
  },
  en: {
    "pane.files": "Files",
    "pane.preview": "Preview",
    "pane.chat": "Muse",
    "btn.hide_left": "Hide files",  "btn.show_left": "Show files",
    "btn.hide_right": "Hide Muse",  "btn.show_right": "Show Muse",
    "btn.show_hidden": "Show hidden files", "btn.hide_hidden": "Hide hidden files",
    "btn.refresh": "Refresh",
    "btn.upload": "Upload to current dir",
    "btn.new_file": "New file",
    "btn.new_dir": "New folder",
    "btn.search": "Full-text search",
    "btn.theme_light": "Light mode", "btn.theme_dark": "Dark mode",
    "btn.settings": "Settings",
    "btn.logout": "Log out",
    "btn.stop": "Stop",
    "btn.send": "Send",
    "btn.save": "Save",
    "btn.cancel": "Cancel",
    "btn.confirm": "OK",
    "btn.edit": "Edit",
    "btn.delete": "Delete",
    "btn.rename": "Rename",
    "btn.download": "Download",
    "btn.copy_path": "Copy path",
    "btn.preview": "Preview",
    "btn.at_mention": "@-mention in chat",
    "btn.new_session": "New session",
    "btn.edit_prompt": "Edit system prompt",
    "btn.close": "Close",
    "files.empty": "Empty folder — drop files here or click + to create",
    "files.empty_search": "No matches",
    "files.searching": "Searching…",
    "files.search_more": "(results truncated — refine keywords)",
    "files.search_placeholder": "Search names and contents…",
    "chat.placeholder": "Talk to Muse… (@ for files, Shift+Enter for newline)",
    "chat.thinking": "Muse is thinking…",
    "chat.empty_tip1": "Press",
    "chat.empty_tip2": " hand a file to Muse",
    "chat.empty_tip3": "Switch model anytime — history kept",
    "chat.runs_on": "runs on",
    "chat.no_session": "No session selected — click \"+ New\" at bottom-left",
    "chat.session_prompt": "Per-session system prompt (layered above muselab default + CLAUDE.md)",
    "chat.attach_files": "Attached files (× to remove)",
    "empty.preview_tagline": "Meet Muse — an AI that actually knows you.",
    "empty.preview_tip1": "Pick a file on the left — Muse can see it",
    "empty.preview_tip2": " hand a file to Muse on the right",
    "empty.preview_tip3": "Right-click for more actions",
    "preview.action_new_note": "New note",
    "preview.action_upload": "Upload",
    "preview.new_note_title": "New markdown note",
    "preview.new_note_body": "Filename (defaults to archive root; you can write health/foo.md etc.)",
    "splash.greet": "Waking up Muse…",
    "splash.slow": "Still loading — first boot takes a moment",
    "conn.reconnecting": "🔄 muselab is unreachable — retrying…",
    "conn.reconnected": "✓ Reconnected",
    "login.sub": "Your personal AI assistant — knows every dimension of your life.",
    "login.tagline": "muse · lab",
    "login.token_placeholder": "MUSELAB_TOKEN",
    "login.go": "Enter",
    "login.err": "Bad token",
    "login.hint_on_err": "Token lives in the .env file at your muselab install dir (look for MUSELAB_TOKEN=). First time? Run scripts/install-{linux,macos,windows}",
    "set.title": "Settings",
    "set.sec.provider": "Provider API keys",
    "set.sec.appearance": "Appearance",
    "set.sec.defaults": "New-session defaults",
    "set.sec.model_params": "Model parameters",
    "set.sec.sessions": "Sessions",
    "set.sessions.cleanup_label": "Empty sessions",
    "set.sessions.cleanup_btn": "Clean up empty",
    "set.sessions.cleanup_confirm_title": "Clean up empty sessions",
    "set.sessions.cleanup_confirm_body": "Delete every session with 0 messages (abandoned placeholders). Not undoable.",
    "set.sessions.cleanup_done": "Removed {n} empty sessions",
    "set.probe": "Test",
    "set.probe_tip": "Ping the vendor endpoint with the configured key",
    "set.probe_running": "Testing key…",
    "set.probe_ok": "key works, endpoint reachable",
    "set.probe_failed": "Test failed",
    "set.probe_hint_401": "key is invalid — check vendor console",
    "set.probe_hint_403": "may need to enable that service on vendor side",
    "set.probe_hint_429": "rate-limited, retry shortly",
    "set.sec.mcp": "MCP tool servers",
    "set.mcp.hint": "MCP lets Muse call external tools (fetch, time, sequential-thinking, …). The switch enables / disables; delete removes config entirely.",
    "set.mcp.empty": "No MCP servers configured. Add one below or install from presets.",
    "set.mcp.disabled": "disabled",
    "set.mcp.toggle_title": "Enable / disable this server",
    "set.mcp.delete": "Delete",
    "set.mcp.presets": "Presets:",
    "set.mcp.add": "Add MCP server",
    "set.mcp.name": "Name",
    "set.mcp.command": "Command",
    "set.mcp.args": "Args (space-separated)",
    "set.mcp.added": "Added",
    "set.mcp.deleted": "Deleted",
    "set.mcp.installed": "Installed from preset",
    "set.mcp.toggle_saved": "Toggled",
    "set.mcp.save_failed": "Save failed",
    "set.mcp.delete_failed": "Delete failed",
    "set.mcp.name_command_required": "Both name and command required",
    "set.sec.skills": "Skills (model-discoverable capability packs)",
    "set.skills.hint": "Muse loads skills from ~/.claude/skills and muselab/skills at startup. The model picks them automatically by task; no manual enable/disable.",
    "set.skills.empty": "No skills discovered. Add a SKILL.md under ~/.claude/skills or this project's skills/.",
    "slash.hint": "↑↓ pick · Enter / Tab to choose · Esc close",
    "slash.none": "no matching command",
    "slash.help_title": "muselab help",
    "help.sec_slash": "Slash commands",
    "help.sec_keys": "Keyboard shortcuts",
    "help.keys_list": "- `Enter` — send message\n- `Shift+Enter` — newline\n- `@` — reference a file from archive\n- `/` — slash command palette\n- `↑ ↓` — navigate @ / `/` popup\n- `Esc` — close popup / modal / cancel streaming\n- `Ctrl+S` — save in CodeMirror editor",
    "help.sec_layout": "Three-column layout",
    "help.layout_list": "- **Left**: archive file tree + full-text search (right-click for more)\n- **Middle**: preview / edit current file (md / code / image / PDF / sandboxed HTML)\n- **Right**: chat with Muse (top chips show CLAUDE.md / MCP / cost / model)",
    "help.docs_link": "Full docs",
    "slash.cleared": "Cleared current session",
    "slash.failed": "Command failed",
    "slash.compact_ok": "New compact session created — request is pre-filled, hit enter to send",
    "slash.compact_prompt": "Summarize the prior conversation into a concise structured note. Preserve: key facts / decisions made / open todos / specific file paths referenced. This summary becomes the seed of the new session.",
    "slash.model_list_title": "Available models (use the dropdown for an easier switch)",
    "slash.model_unknown": "Unknown model: {id}",
    "slash.model_switched": "Switched to {id}",
    "slash.resume_list_title": "Last 10 sessions (with ID prefix + message count)",
    "slash.resume_no_match": "No matching session",
    "slash.resumed": "Jumped to \"{name}\"",
    "slash.cost_title": "Current usage",
    "slash.unknown": "Unknown command: /{cmd} (type /help to list all)",
    "cost.total": "Total cost",
    "cost.in_out": "Input / output tokens",
    "cost.cache_hit": "Cache hit",
    "cost.budget": "Budget",
    "cost.no_budget": "No budget set (add MUSELAB_BUDGET_USD in Settings)",
    "cost.context": "Current session context",
    "cost.budget_warn": "Budget warning: used {pct}% of ${usd}",
    "ctx.title": "Current session context tokens",
    "ctx.claude_md_tip": "Muse loaded {root}/CLAUDE.md (last modified {date})",
    "ctx.no_claude_md": "No CLAUDE.md in {root} — Muse doesn't know you; click to learn how to set up",
    "ctx.no_claude_md_title": "Muse doesn't know you yet",
    "ctx.no_claude_md_body": "Your archive lives at:\n  {root}\n\nMuse reads {root}/CLAUDE.md on startup as your profile. Without it, Muse uses default behavior and can't tailor advice to your real situation.\n\nTo set up:\n  1. Run scripts/install-{linux,macos,windows} (with intake — recommended)\n  2. Or copy scripts/templates/default-CLAUDE.md → {root}/CLAUDE.md and edit\n\nDetails: docs/personalize-claude-md.md",
    "onboard.no_provider_title": "🔑 No model configured — can't chat yet",
    "onboard.no_provider_body": "Muse needs at least one provider. Either:\n  • Claude (Pro/Max subscription): run `claude login` in terminal (free quota)\n  • Claude (pay-per-use API): grab a key from console.anthropic.com, paste into Settings\n  • DeepSeek / GLM / MiniMax: grab a key from their console, paste into Settings",
    "onboard.no_provider_cta": "Open Settings",
    "onboard.no_claude_md_title": "👋 Muse doesn't know you yet",
    "onboard.no_claude_md_body": "Muse is one assistant for your whole life — body, work, money, people, daily stuff. Before it can give tailored advice, it needs a 3-minute intake of your profile.",
    "onboard.no_claude_md_cta": "Show me how",
    "onboard.empty_archive_title": "📂 No documents in your archive yet",
    "onboard.empty_archive_body": "Drop real material into the right subdir so Muse can work with it. These are ready for you:",
    "onboard.dir_health": "checkup reports / supplement plan / training log",
    "onboard.dir_work":   "what you spend time on (school / resume / portfolio / projects)",
    "onboard.dir_money":  "budget / positions / loans / insurance",
    "onboard.dir_people": "people you care about",
    "onboard.dir_notes":  "study notes / ideas / journal",
    "onboard.ready_title": "💡 Try asking Muse",
    "onboard.q_health":   "Look at my health files and tell me what to watch",
    "onboard.q_work":     "Help me review my resume / school materials",
    "onboard.q_money":    "Take a look at my finances and suggest changes",
    "onboard.q_people":   "Who do I need to follow up with lately?",
    "onboard.q_overview": "What's in my archive? Give me a tour",
    "onboard.skill_hint": "Or try a skill:",
    "ctx.cache_tip": "Cache hit rate — higher is cheaper. Anthropic prompt cache reuses the last system prompt + context within 5 minutes.",
    "ctx.normal": "Context {used}K / {limit}K · {pct}% · cached {cached}K",
    "ctx.warn":   "Context {pct}% ({used}K / {limit}K · cached {cached}K) · Long chats get expensive — compact?",
    "ctx.danger": "Context {pct}% ({used}K / {limit}K · cached {cached}K) · About to truncate — compact now",
    "ctx.compact_btn": "Compact",
    "ctx.tip_line1": "Every reply sends the full history to the model — more tokens means slower and pricier",
    "ctx.tip_line2": "Compact = Muse summarizes the past, new session starts from that summary",
    "ctx.compact_confirm_title": "Compact this session",
    "ctx.compact_confirm_body": "Muse will summarize the full history first, then create a new session seeded with that summary. The original is preserved.",
    "ctx.compact_summarize_prompt": "Compress this entire conversation into a structured summary: (1) facts / background (2) decisions made (3) open todos or next steps (4) specific file paths referenced. Make it complete enough that someone reading only the summary can pick up where we left off.",
    "ctx.compact_step1": "Step 1/2: Asking the model to summarize…",
    "ctx.compact_done": "Compacted — jumped to the new session",
    "ctx.compact_wait_streaming": "Muse is still replying — wait for this turn to finish",
    "ctx.compact_empty": "No assistant replies yet in this session — nothing to compact",
    "ctx.compact_timeout": "Compact timed out (60s) — Muse may be processing a long history, try again",
    "ctx.compact_no_reply": "Model didn't return a summary — check for errors and retry",
    "model.switch_title": "Switch model",
    "model.switch_body": "Switching to {label} needs a new session. Confirm?",
    "model.switch_new": "Create & switch",
    "model.new_session_ok": "New session with {label}",
    "img.attach": "Attach image (paste / drag / pick)",
    "attach.title": "Attach image or document (PDF / md / txt / json / source…)",
    "attach.bad_type": "Unsupported file type",
    "img.remove": "Remove",
    "img.bad_type": "Unsupported image type (png/jpg/gif/webp)",
    "img.too_big": "Image exceeds 10 MB",
    "img.upload_failed": "Image upload failed",
    "img.wait_upload": "Wait for images to finish uploading",
    "todo.title": "Task list",
    "todo.doing": "in progress",
    "subagent.label": "Subagent dispatched",
    "subagent.prompt_toggle": "show prompt",
    "plan.title": "Plan from Muse",
    "set.sec.lang": "Language",
    "set.label.lang": "Interface language",
    "set.label.theme": "Theme",
    "set.label.accent": "Accent color",
    "set.label.default_model": "Default model",
    "set.label.default_permission": "Default permission",
    "set.label.show_thinking_default": "Show thinking block by default",
    "set.label.thinking_budget": "Thinking budget (tokens)",
    "set.label.max_turns": "Max tool turns",
    "set.lang.zh": "中文", "set.lang.en": "English",
    "set.theme.light": "Light", "set.theme.dark": "Dark",
    "set.key_set": "Configured", "set.key_unset": "Not set",
    "set.placeholder_key": "Paste API key and save",
    "toast.saved": "Saved",
    "toast.save_failed": "Save failed",
    "toast.deleted": "Deleted",
    "toast.delete_failed": "Delete failed",
    "toast.copied": "Copied",
    "toast.uploaded": "Upload complete",
    "toast.upload_failed": "Upload failed",
    "toast.renamed": "Renamed",
    "toast.rename_failed": "Rename failed",
    "toast.created": "Created",
    "toast.token_required": "Token required",
    "toast.model_switched": "Switched to {label} — next message uses the new model (no new session needed)",
    "toast.muse_back": "Muse is back",
    "toast.mention_added": "Handed {path} to Muse",
    "toast.lang_switched": "Switched to English",
    "ask.title": "Muse needs your input",
    "ask.submitted": "Submitted",
    "ask.multi": "multi-select",
    "ask.submit": "Submit",
    "ask.unanswered": "Some questions still unanswered",
    "ask.submit_failed": "Submit failed",
    "perm.title": "Muse wants to use a tool",
    "perm.allow": "Allow",
    "perm.deny": "Deny",
    "perm.always": "Always allow (this session)",
    "perm.decision_allow": "Allowed",
    "perm.decision_deny": "Denied",
    "perm.decision_always": "Whitelisted",
    "perm.submit_failed": "Decision failed",
    "modal.confirm_delete": "Delete {name}? This cannot be undone.",
    "modal.input_required": "Input required",
    "modal.dirty_save": "Unsaved changes — save first?",
  },
};

// Preset accent colors offered in Settings. Each entry has bilingual names; the
// UI tooltip picks the right side via `lang`.
const ACCENT_PRESETS = [
  { name: { zh: "默认蓝", en: "Classic blue" }, value: "#6093ff" },
  { name: { zh: "紫罗兰", en: "Violet" },        value: "#a78bfa" },
  { name: { zh: "翠绿",   en: "Emerald" },       value: "#34d399" },
  { name: { zh: "暖橙",   en: "Warm orange" },   value: "#fb923c" },
  { name: { zh: "玫红",   en: "Rose" },          value: "#f472b6" },
  { name: { zh: "石板灰", en: "Slate" },         value: "#94a3b8" },
];

// Editable file extensions (matches backend TEXT_EXT). Kept outside the reactive
// component so Alpine doesn't try to wrap the Set in a Proxy.
const EDITABLE_EXT = new Set([
  "md", "markdown", "txt", "html", "htm", "json", "yaml", "yml",
  "py", "js", "ts", "tsx", "jsx", "mjs", "css", "scss", "less",
  "sh", "bash", "zsh", "toml", "ini", "cfg", "csv", "xml", "log",
  "sql", "rs", "go", "java", "cpp", "c", "h", "hpp", "rb", "php",
  "lua", "kt", "swift", "vue", "svelte", "tex", "rst", "env",
  "dockerfile", "makefile", "conf", "properties", "gitignore",
  "containerfile", "rakefile", "gemfile", "vagrantfile",
  "license", "licence", "readme", "changelog",
]);

function portal() {
  return {
    // ===== auth =====
    authed: false, tokenInput: "", token: "", loginErr: "",
    // App-readiness layers:
    //   appReady=false → full-screen splash (initial load / hard refresh).
    //     Cleared once contextInfo + sessions list have arrived OR after
    //     a hard 8s timeout (avoid splashing forever on backend dead).
    //   connState: 'ok' | 'reconnecting' | 'reconnected'
    //     drives the slim top banner shown when fetches start failing
    //     (backend restarted, network blipped). 'reconnected' briefly
    //     flashes green then auto-clears.
    appReady: false,
    splashHint: "",          // "still warming up..." appears after 3s
    connState: "ok",
    _connFails: 0,
    _connHeartbeat: null,
    _splashHintTimer: null,
    _splashHardTimeout: null,

    // ===== file tree =====
    visible: [], expanded: new Set(), childCache: {},
    selected: "",
    dragOver: "",
    searchQ: "", searchMode: false, searching: false,
    searchHits: [], searchTruncated: false,
    grepHits: [], grepTruncated: false,

    // ===== preview =====
    previewMode: "", rawText: "", renderedMd: "", previewLang: "plaintext",
    editing: false, editText: "",
    cmStatus: { line: 1, col: 1, sel: 0, lines: 0, chars: 0, mode: "plaintext", dirty: false },
    tabs: [],   // open file tabs: [{path, name}]

    // ===== chat =====
    sessions: [], currentId: "",
    messages: [],
    model: "claude-sonnet-4-6",
    permission: "bypassPermissions",
    showThinking: false,
    input: "", streaming: false, es: null,
    // 锁定当前在跑的那条请求用的模型——dropdown 切到别的，pending bubble 不能跟着变。
    streamingModel: "",
    // Elapsed seconds since send() — pending bubble shows it after 1s so the
    // user knows the system isn't stuck.
    streamElapsed: 0,
    _streamTimer: null,
    _streamStartedAt: 0,
    pendingImages: [],    // [{id, mime, preview (data URL), uploading, error}]
    pendingDocs: [],      // [{id, name, kind: 'pdf'|'text', uploading, error}]
    dragHover: false,

    // What Muse can see — populated from /api/chat/context-info on login.
    // Drives the onboarding hints (claude_md chip, "drop a doc here" cards).
    contextInfo: {
      archive_root: "",
      claude_md_exists: false,
      claude_md_lines: 0,
      claude_md_mtime: 0,
      archive_empty: true,
      subdir_present: {},
      has_claude_oauth: false,
      third_party_configured: [],
      has_any_provider: false,
    },

    // ===== slash commands =====
    slashShow: false,
    slashIdx: 0,
    slashAnchor: -1,      // input position where the leading '/' is
    SLASH_CMDS: [
      { name: "help",    desc: { zh: "查看所有可用斜杠命令", en: "List all slash commands" } },
      { name: "clear",   desc: { zh: "清空当前会话", en: "Reset / clear current session" } },
      { name: "compact", desc: { zh: "压缩历史 — 把上下文摘要成新会话", en: "Compact: summarize history into a new session" } },
      { name: "model",   desc: { zh: "/model <id> 切换模型，留空看可选项", en: "/model <id> — switch model (no arg = list)" } },
      { name: "resume",  desc: { zh: "/resume <名字> 跳到名字匹配的旧会话", en: "/resume <name> — jump to a session by name" } },
      { name: "cost",    desc: { zh: "显示当前用量 / 预算 / 缓存命中率", en: "Show current usage / budget / cache hit rate" } },
      { name: "config",  desc: { zh: "打开 Settings 面板", en: "Open Settings panel" } },
      { name: "stop",    desc: { zh: "中断当前流式响应", en: "Stop the current streaming reply" } },
    ],
    // Per-session context meter snapshot, updated on every SSE `done` event
    sessionUsage: { input_tokens: 0, output_tokens: 0,
                     cache_read_tokens: 0, cache_creation_tokens: 0,
                     context_limit: 128000, context_used: 0, context_used_pct: 0 },
    stats: { total_cost_usd: 0, total_messages: 0, total_input_tokens: 0,
              total_output_tokens: 0, total_cache_read_tokens: 0,
              total_cache_creation_tokens: 0, cache_hit_pct: 0,
              budget_usd: 0, budget_used_pct: 0 },
    mcp: { configured: false, servers: [] },
    availableModels: [],   // from /api/chat/providers
    atBottom: true,
    theme: "dark",
    accent: "#6093ff",
    ACCENT_PRESETS,

    // ===== i18n =====
    lang: "zh",
    STRINGS,

    // ===== Muse mascot =====
    // 九缪斯（Nine Muses of Greek mythology）。视觉仍是抽象几何，名字承载典故：
    // 每个缪斯对应一种艺术 / 学科，几何形象选有意义关联的（如 Urania 天文 → orbit 行星）。
    MASCOTS: [
      { id: "hex",      greek: "Calliope",    zhName: "卡利俄佩",       domain: { zh: "史诗", en: "Epic poetry" } },
      { id: "bars",     greek: "Clio",        zhName: "克利俄",         domain: { zh: "历史", en: "History" } },
      { id: "lens",     greek: "Erato",       zhName: "厄拉托",         domain: { zh: "情诗", en: "Love poetry" } },
      { id: "wave",     greek: "Euterpe",     zhName: "欧忒耳佩",       domain: { zh: "音乐", en: "Music" } },
      { id: "crescent", greek: "Melpomene",   zhName: "墨尔波墨涅",     domain: { zh: "悲剧", en: "Tragedy" } },
      { id: "halo",     greek: "Polyhymnia",  zhName: "波吕许谟尼亚",   domain: { zh: "圣诗", en: "Sacred hymns" } },
      { id: "trio",     greek: "Terpsichore", zhName: "忒耳普西科瑞",   domain: { zh: "舞蹈", en: "Dance" } },
      { id: "spark",    greek: "Thalia",      zhName: "塔利亚",         domain: { zh: "喜剧", en: "Comedy" } },
      { id: "orbit",    greek: "Urania",      zhName: "乌拉尼亚",       domain: { zh: "天文", en: "Astronomy" } },
    ],
    mascotIdx: 0,
    mascotGreet: false,

    leftOpen: true,
    rightOpen: true,
    leftWidth: 280,
    rightWidth: 440,
    showHidden: false,

    // Mobile: viewport < 900px collapses the 3 panes into a single visible
    // tab. Default "chat" since that's the primary action; auto-switches to
    // "preview" when user opens a file, and "chat" when they @-mention one.
    mobileTab: "chat",

    // ===== @ mention =====
    mentionShow: false, mentionResults: [], mentionIdx: 0, mentionAnchor: -1,

    // ===== toast / modal / ctx menu =====
    toasts: [], _toastId: 0,
    modal: { show: false, title: "", body: "", input: null, confirm: null, cancel: null, okText: "", cancelText: "", danger: false },
    ctxMenu: { show: false, x: 0, y: 0, node: null },

    // ===== settings =====
    settings: {
      show: false,
      providers: [],
      draftKeys: {},
      draftDefaults: { model: "", permission: "", show_thinking: false },
      draftParams: { thinking_budget: 4000, max_turns: 0 },
      // MCP server list (loaded from /api/settings/mcp)
      mcpServers: [],
      mcpExamples: [],
      mcpDraft: { show: false, name: "", command: "", argsStr: "" },
      skills: [],   // discovered skill list (read-only browse)
      probeResults: {},   // env_key -> {ok, text} from last "Test" click
    },

    // Per-provider help hints rendered under the API-key input. Anthropic
    // gets the most because it has two valid paths (Pro OAuth or API key);
    // others are just a link to where to get the key.
    PROVIDER_HELP: {
      ANTHROPIC_API_KEY: {
        url: "https://console.anthropic.com/settings/keys",
        zh: "两种路径：(1) Pro/Max 订阅 → 终端跑 `claude login`（免费配额，无需在此填）；(2) API 按量付费 → 去 console.anthropic.com 拿 key 填这里。两个都配 → CLI 自动用 Pro，不会重复扣费。",
        en: "Two paths: (1) Pro/Max subscription → run `claude login` in terminal (free quota, leave this blank); (2) pay-per-use API → grab a key from console.anthropic.com and paste it here. With both configured, CLI prefers Pro automatically — you won't double-bill.",
      },
      DEEPSEEK_API_KEY: {
        url: "https://platform.deepseek.com/api_keys",
        zh: "去 platform.deepseek.com 控制台创建 API key（注册送 5 元额度）。",
        en: "Create an API key at platform.deepseek.com (free trial credit on signup).",
      },
      ZHIPUAI_API_KEY: {
        url: "https://open.bigmodel.cn/usercenter/apikeys",
        zh: "去 open.bigmodel.cn 控制台创建 API key。注意是国内站，不是 zhipuai.com.cn。",
        en: "Create an API key at open.bigmodel.cn (China mainland site).",
      },
      MINIMAX_API_KEY: {
        url: "https://platform.minimaxi.com/user-center/basic-information/interface-key",
        zh: "去 platform.minimaxi.com（国内站）创建 API key。注意是 minimaxi.com 不是 minimax.io（后者是海外站，用同 key 401）。",
        en: "Create an API key at platform.minimaxi.com (the .com - .io is overseas and rejects the same key).",
      },
    },

    _pendingExpanded: null,

    // ===== init =====
    onGlobalKeyDown(ev) {
      // Ctrl/Cmd+S → 保存（编辑模式下）；Esc → 关 modal/menu/停止流式
      if ((ev.ctrlKey || ev.metaKey) && ev.key === "s") {
        if (this.editing && this.selected) {
          ev.preventDefault();
          this.saveEdit();
        }
        return;
      }
      if (ev.key === "Escape") {
        if (this.mentionShow) { this.mentionShow = false; return; }
        if (this.ctxMenu.show) { this.ctxMenu.show = false; return; }
        if (this.settings.show) { this.settings.show = false; return; }
        if (this.modal.show && this.modal.cancel) { this.modal.cancel(); return; }
        if (this.editing) { this.editing = false; return; }   // 退出编辑
        if (this.streaming) { this.stop(); return; }          // 停止流式
      }
    },

    init() {
      // 全局快捷键（绑在 document，避免每个 textarea 单独处理）
      document.addEventListener("keydown", e => this.onGlobalKeyDown(e));
      // 一次性迁移旧 localStorage key（portal_* → muselab_*），让现有用户无感升级
      for (const [oldK, newK] of [
        ["portal_token", "muselab_token"],
        ["portal_prefs", "muselab_prefs"],
        ["portal_theme", "muselab_theme"],
        ["portal_chat", "muselab_chat"],
      ]) {
        const v = localStorage.getItem(oldK);
        if (v != null && localStorage.getItem(newK) == null) {
          localStorage.setItem(newK, v);
        }
        localStorage.removeItem(oldK);
      }
      this.initTheme();
      this.initLang();
      this.initMascot();
      this.configureMarked();
      this.$watch("editing", v => v ? this.mountCM() : this.unmountCM());
      this.$watch("rightOpen", v => { if (v) this.greetMascot(this.t("toast.muse_back")); });
      // 编辑模式下切换文件时，重新挂载 CM 加载新文件内容
      this.$watch("selected", () => { if (this.editing) { this.unmountCM(); this.mountCM(); } });
      // 注意：之前这里挂过 `$watch("model", ...)` 自动 toast「模型已切」。
      // 但 dropdown 的 x-model 是 onchange 之前就把 this.model 写新值——
      // watch 会比 onModelChange() 的 confirm modal 先 fire，让用户看到"已
      // 切换"toast 之后才弹"是否新建会话？"。删掉 watch，让 onModelChange()
      // 作为唯一的视觉反馈源（成功 PATCH / 成功新建后才 toast）。
      const t = localStorage.getItem("muselab_token");
      if (t) {
        this.token = t; this.authed = true;
        this._bootApp();
      } else {
        // No token saved → skip splash, jump straight to login.
        this.appReady = true;
      }
    },

    // First-load splash + initial fetch sequence. Sets appReady=true once
    // contextInfo + sessions both come back, OR after 8s hard timeout (so
    // a dead backend doesn't leave the user on a splash forever — we surface
    // the issue via the reconnect banner instead).
    async _bootApp() {
      // Splash hint after 3s ("still warming up...")
      this._splashHintTimer = setTimeout(() => {
        this.splashHint = this.t("splash.slow");
      }, 3000);
      // Hard timeout — if 8s pass without a successful fetch, drop splash
      // and let the reconnect banner take over.
      this._splashHardTimeout = setTimeout(() => {
        if (!this.appReady) {
          this.appReady = true;
          this.connState = "reconnecting";
        }
      }, 8000);

      this.loadPrefs();
      this.loadRoot();
      this.initSessions();
      this.fetchStats();
      // Block readiness on context-info (the most important one for the
      // onboarding cards). Others come along in parallel.
      try {
        await this.fetchContextInfo();
        this._markReady();
      } catch (e) {
        // Will retry via heartbeat
      }
      this._startHeartbeat();
    },

    _markReady() {
      if (this.appReady) return;
      this.appReady = true;
      clearTimeout(this._splashHintTimer);
      clearTimeout(this._splashHardTimeout);
      this.splashHint = "";
    },

    // 10s heartbeat — pings /api/meta. If 2 consecutive fails, flag reconnecting;
    // when one comes back, flash "reconnected" then auto-clear.
    _startHeartbeat() {
      if (this._connHeartbeat) clearInterval(this._connHeartbeat);
      this._connHeartbeat = setInterval(() => this._pingHealth(), 10_000);
    },
    async _pingHealth() {
      try {
        const r = await fetch("/api/meta", { headers: this.hdr() });
        if (!r.ok) throw new Error("status " + r.status);
        // Healthy
        if (this.connState === "reconnecting") {
          this.connState = "reconnected";
          // After a 1.5s flash of green, drop back to silent ok.
          setTimeout(() => {
            if (this.connState === "reconnected") this.connState = "ok";
          }, 1500);
          // Also refresh sessions / context — they may be stale post-restart
          this.refreshSessions();
          this.fetchContextInfo();
        } else {
          this.connState = "ok";
        }
        this._connFails = 0;
      } catch (e) {
        this._connFails++;
        if (this._connFails >= 2) this.connState = "reconnecting";
        // Splash → if we never managed to ready up, force ready so user sees
        // the banner (otherwise they stare at splash with no feedback).
        if (!this.appReady) this._markReady();
      }
    },

    _cm: null,
    cmMode(path) {
      if (!path) return "text/plain";
      const ext = path.split(".").pop().toLowerCase();
      const map = {
        md: "markdown", markdown: "markdown",
        py: "python",
        js: "javascript", mjs: "javascript", jsx: "javascript",
        ts: "text/typescript", tsx: "text/typescript",
        json: "application/json",
        html: "htmlmixed", htm: "htmlmixed",
        xml: "xml", svg: "xml",
        css: "css", scss: "css", less: "css",
        yaml: "yaml", yml: "yaml",
        sh: "shell", bash: "shell", zsh: "shell",
        go: "go",
        rs: "rust",
        c: "text/x-csrc", h: "text/x-csrc",
        cpp: "text/x-c++src", hpp: "text/x-c++src",
        java: "text/x-java",
      };
      return map[ext] || "text/plain";
    },
    mountCM() {
      this.$nextTick(() => {
        if (!window.CodeMirror) { console.warn("[muselab] CodeMirror not loaded"); return; }
        const host = this.$refs.cmHost;
        if (!host) { console.warn("[muselab] no cmHost ref"); return; }
        host.innerHTML = "";
        const modeStr = this.cmMode(this.selected);
        try {
          const cm = window.CodeMirror(host, {
            value: String(this.editText || ""),
            mode: modeStr,
            lineNumbers: true,
            lineWrapping: true,
            tabSize: 2,
            indentUnit: 2,
            theme: this.theme === "light" ? "default" : "material-darker",
          });
          // Initial status
          this.cmStatus = {
            line: 1, col: 1, sel: 0,
            lines: cm.lineCount(),
            chars: cm.getValue().length,
            mode: this.shortMode(modeStr),
            dirty: false,
          };
          const updateStatus = () => {
            const c = cm.getCursor();
            const sel = cm.getSelection().length;
            this.cmStatus = {
              line: c.line + 1, col: c.ch + 1, sel,
              lines: cm.lineCount(),
              chars: cm.getValue().length,
              mode: this.shortMode(modeStr),
              dirty: cm.getValue() !== String(this.rawText || ""),
            };
          };
          cm.on("change", () => { this.editText = cm.getValue(); updateStatus(); });
          cm.on("cursorActivity", updateStatus);
          window.__muselab_cm = cm;
          setTimeout(() => { cm.refresh(); updateStatus(); }, 50);
        } catch (e) {
          console.error("[muselab] CodeMirror init failed:", e);
          this.toast("编辑器初始化失败：" + e.message, "error", 6000);
          host.innerHTML = '<textarea style="width:100%;height:100%;padding:14px;background:var(--c-bg-0);color:var(--c-fg-0);border:0;font:13px ui-monospace,monospace;resize:none"></textarea>';
          const ta = host.querySelector("textarea");
          ta.value = this.editText;
          ta.addEventListener("input", () => { this.editText = ta.value; });
        }
      });
    },
    shortMode(mode) {
      // CM 内部 mode 名标准化成显示用短名
      if (!mode) return "text";
      if (mode === "text/plain") return "text";
      if (mode === "htmlmixed") return "html";
      if (mode.includes("/")) return mode.split("/").pop().replace(/^x-/, "");
      return mode;
    },
    unmountCM() {
      const host = this.$refs.cmHost;
      if (host) host.innerHTML = "";
      window.__muselab_cm = null;
    },

    initTheme() {
      const saved = localStorage.getItem("muselab_theme");
      if (saved === "light" || saved === "dark") {
        this.theme = saved;
      } else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) {
        this.theme = "light";
      }
      const savedAccent = localStorage.getItem("muselab_accent");
      if (savedAccent) this.accent = savedAccent;
      this.applyTheme();
      this.applyAccent();
    },
    applyTheme() {
      document.documentElement.setAttribute("data-theme", this.theme);
      const link = document.getElementById("hljs-theme");
      if (link) {
        link.href = this.theme === "light"
          ? "/static/vendor/highlight-theme-light.css"
          : "/static/vendor/highlight-theme.css";
      }
    },
    applyAccent() {
      // 主色 + 派生色（hover / soft 半透明 / 文字色用浅化 mix 实现）
      const r = document.documentElement.style;
      r.setProperty("--c-accent", this.accent);
      r.setProperty("--c-accent-hover", this._shade(this.accent, this.theme === "light" ? -15 : 12));
      r.setProperty("--c-accent-soft", this._withAlpha(this.accent, this.theme === "light" ? 0.10 : 0.14));
      r.setProperty("--c-accent-fg", this.theme === "light"
        ? this._shade(this.accent, -25)
        : this._shade(this.accent, 25));
    },
    setAccent(color) {
      this.accent = color;
      localStorage.setItem("muselab_accent", color);
      this.applyAccent();
      if (this.MASCOTS) this.applyFavicon();  // favicon 跟主题色同步
    },

    // ===== i18n =====
    initLang() {
      const saved = localStorage.getItem("muselab_lang");
      if (saved === "zh" || saved === "en") this.lang = saved;
      else this.lang = (navigator.language || "zh").toLowerCase().startsWith("en") ? "en" : "zh";
      document.documentElement.lang = this.lang;
    },
    setLang(lang) {
      if (lang !== "zh" && lang !== "en") return;
      this.lang = lang;
      localStorage.setItem("muselab_lang", lang);
      document.documentElement.lang = lang;
      this.toast(this.t("toast.lang_switched"), "success", 1500);
    },
    // t("key.path", {var: "x"}) — 简单变量插值；缺 key 时回退到 key 本身（方便发现遗漏）
    t(key, vars) {
      const table = STRINGS[this.lang] || STRINGS.zh;
      let s = table[key];
      if (s == null) s = (STRINGS.zh[key] != null ? STRINGS.zh[key] : key);
      if (vars) {
        for (const k in vars) s = s.split("{" + k + "}").join(vars[k]);
      }
      return s;
    },

    // ===== Muse mascot =====
    initMascot() {
      // 按"日期+小时"哈希选形象——同一小时内稳定，跨小时变化（不会刷新一次换一次）
      const seed = new Date().toISOString().slice(0, 13);
      let h = 5381;
      for (let i = 0; i < seed.length; i++) h = ((h << 5) + h + seed.charCodeAt(i)) | 0;
      this.mascotIdx = Math.abs(h) % this.MASCOTS.length;
      this.applyFavicon();
      setTimeout(() => this.greetMascot(this.mascotLabel()), 400);
    },
    mascot() { return this.MASCOTS[this.mascotIdx]; },
    mascotHref() { return "#m-" + this.mascot().id; },
    // 显示文案：英文界面 "Muse · Urania · Astronomy"；中文界面 "Muse · 乌拉尼亚 · 天文"（保留希腊名作 hint）
    mascotLabel() {
      const m = this.mascot();
      if (this.lang === "zh") return `Muse · ${m.zhName}（${m.greek}）· ${m.domain.zh}`;
      return `Muse · ${m.greek} · ${m.domain.en}`;
    },
    mascotShortLabel() {
      const m = this.mascot();
      return this.lang === "zh" ? `${m.zhName} · ${m.domain.zh}` : `${m.greek} · ${m.domain.en}`;
    },
    cycleMascot() {
      this.mascotIdx = (this.mascotIdx + 1) % this.MASCOTS.length;
      this.applyFavicon();
      this.greetMascot(this.mascotLabel());
    },
    // 把当前 mascot 渲染成 data:image/svg+xml favicon，跟着主题色走
    applyFavicon() {
      const id = this.mascot().id;
      // 重新声明每个 mascot 的 SVG body —— defs 在 document 里通过 <use> 引用，但 favicon
      // data URL 是独立文档，必须把图形内嵌。集中在这里维护成 lookup。
      const SHAPES = {
        hex:      '<path d="M12 3 L20 7.5 L20 16.5 L12 21 L4 16.5 L4 7.5 Z"/>',
        bars:     '<line x1="4" y1="7" x2="20" y2="7"/><line x1="7" y1="12" x2="17" y2="12"/><line x1="10" y1="17" x2="14" y2="17"/>',
        lens:     '<circle cx="9" cy="12" r="6"/><circle cx="15" cy="12" r="6"/>',
        wave:     '<circle cx="12" cy="12" r="9"/><path d="M5 12 Q 8.5 6 12 12 T 19 12"/>',
        crescent: '<path d="M16 3 A 9 9 0 1 0 16 21 A 7 7 0 1 1 16 3 Z"/>',
        halo:     '<circle cx="12" cy="14" r="5"/><path d="M5 8 A 7 4 0 0 1 19 8"/>',
        trio:     '<circle cx="12" cy="6" r="2" fill="currentColor"/><circle cx="6" cy="17" r="2" fill="currentColor"/><circle cx="18" cy="17" r="2" fill="currentColor"/>',
        spark:    '<line x1="12" y1="3" x2="12" y2="21"/><line x1="3" y1="12" x2="21" y2="12"/><circle cx="12" cy="12" r="2" fill="currentColor"/>',
        orbit:    '<circle cx="11" cy="13" r="5"/><circle cx="18.5" cy="6" r="1.6" fill="currentColor"/>',
      };
      const color = this.accent || "#6093ff";
      const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="color:${color}">${SHAPES[id] || SHAPES.orbit}</svg>`;
      const url = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
      let link = document.querySelector('link[rel="icon"]');
      if (!link) {
        link = document.createElement("link");
        link.rel = "icon";
        document.head.appendChild(link);
      }
      link.type = "image/svg+xml";
      link.href = url;
    },
    greetMascot(msg) {
      // 去重：同一条 msg 在 1.5s 内重复调用只 toast 一次（Alpine $watch 在某些场景会双触发，
      // 比如 rightOpen 既被 loadPrefs 写又被点击 toggle 时的 render 顺序）。
      const now = Date.now();
      if (msg && this._lastGreetMsg === msg && now - this._lastGreetAt < 1500) {
        return;
      }
      this._lastGreetMsg = msg;
      this._lastGreetAt = now;
      this.mascotGreet = true;
      if (msg) this.toast(msg, "info", 1400);
      clearTimeout(this._mascotT);
      this._mascotT = setTimeout(() => { this.mascotGreet = false; }, 900);
    },
    toggleTheme() {
      this.theme = this.theme === "light" ? "dark" : "light";
      this.applyTheme();
      this.applyAccent();   // 派生色对深浅敏感，重算
      localStorage.setItem("muselab_theme", this.theme);
      if (window.__muselab_cm) window.__muselab_cm.setOption("theme", this.theme === "light" ? "default" : "material-darker");
    },

    // 色彩小工具
    _withAlpha(hex, alpha) {
      const { r, g, b } = this._hex2rgb(hex);
      return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    },
    _shade(hex, percent) {
      // percent 正数变亮，负数变暗，幅度 0-100
      const { r, g, b } = this._hex2rgb(hex);
      const adj = v => Math.max(0, Math.min(255, Math.round(v + (255 - v) * percent / 100) - (percent < 0 ? Math.round(v * -percent / 100) : 0)));
      const a = (v) => percent >= 0 ? Math.round(v + (255 - v) * percent / 100) : Math.round(v * (1 + percent / 100));
      const cap = v => Math.max(0, Math.min(255, v));
      return "#" + [cap(a(r)), cap(a(g)), cap(a(b))].map(x => x.toString(16).padStart(2, "0")).join("");
    },
    _hex2rgb(hex) {
      const h = hex.replace("#", "");
      const v = h.length === 3 ? h.split("").map(c => c + c).join("") : h;
      return { r: parseInt(v.slice(0, 2), 16), g: parseInt(v.slice(2, 4), 16), b: parseInt(v.slice(4, 6), 16) };
    },

    configureMarked() {
      // marked v13 removed the `highlight` option; we post-process rendered HTML
      // via highlightCode() instead. Nothing to configure here for now.
    },

    // Render markdown -> sanitized HTML. All markdown rendering MUST go through
    // here; passing raw `marked.parse(...)` to x-html opens XSS via untrusted
    // file content / Claude responses containing <script>, on*, javascript: etc.
    // ===== attachment helpers (images + docs) =====
    // Classify a file by mime/extension to decide which preview chip to show
    // and (cosmetically) what kind label to display. Server has the
    // authoritative classification — we just guess for the chip.
    _classifyFile(file) {
      const m = (file.type || "").toLowerCase();
      const name = (file.name || "").toLowerCase();
      if (m.startsWith("image/")) return "image";
      if (m === "application/pdf" || name.endsWith(".pdf")) return "pdf";
      const textMimes = ["text/", "application/json", "application/xml",
                          "application/yaml", "application/x-yaml",
                          "application/toml"];
      if (textMimes.some(p => m.startsWith(p) || m === p)) return "text";
      const textExts = [".md", ".markdown", ".txt", ".csv", ".json", ".yaml",
                         ".yml", ".toml", ".py", ".sh", ".js", ".ts", ".tsx",
                         ".jsx", ".html", ".css", ".xml", ".log", ".ini",
                         ".conf", ".cfg"];
      if (textExts.some(ext => name.endsWith(ext))) return "text";
      return "unknown";
    },
    async _attachFile(file) {
      if (file.size > 10 * 1024 * 1024) {
        this.toast(this.t("img.too_big"), "warn", 2500);
        return;
      }
      const kind = this._classifyFile(file);
      if (kind === "unknown") {
        this.toast(this.t("attach.bad_type") + ": " + file.name, "warn", 3500);
        return;
      }

      let entry;
      if (kind === "image") {
        const preview = await new Promise((res, rej) => {
          const fr = new FileReader();
          fr.onload = () => res(fr.result);
          fr.onerror = rej;
          fr.readAsDataURL(file);
        });
        entry = { id: null, mime: file.type, preview,
                  uploading: true, error: false };
        this.pendingImages.push(entry);
      } else {
        entry = { id: null, name: file.name, kind,
                  uploading: true, error: false };
        this.pendingDocs.push(entry);
      }

      const fd = new FormData();
      fd.append("file", file);
      try {
        const r = await fetch("/api/chat/upload-image", {
          method: "POST", headers: this.hdr(), body: fd,
        });
        if (!r.ok) {
          entry.error = true; entry.uploading = false;
          this.toast(this.t("img.upload_failed") + ": " + await r.text(),
                      "error", 4000);
          return;
        }
        const d = await r.json();
        entry.id = d.id; entry.uploading = false;
        // Server's classification wins for kind label.
        if (d.kind && entry.kind) entry.kind = d.kind;
      } catch (e) {
        entry.error = true; entry.uploading = false;
        this.toast(this.t("img.upload_failed"), "error", 3000);
      }
    },
    async onAttachPicked(ev) {
      const files = Array.from(ev.target.files || []);
      ev.target.value = "";
      for (const f of files) await this._attachFile(f);
    },
    async onAttachDrop(ev) {
      const files = Array.from((ev.dataTransfer && ev.dataTransfer.files) || []);
      for (const f of files) await this._attachFile(f);
    },
    async onImagePaste(ev) {
      // Only handle pasted image data; let normal text paste through.
      const items = (ev.clipboardData && ev.clipboardData.items) || [];
      const files = [];
      for (const it of items) {
        if (it.kind === "file") {
          const f = it.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length) {
        ev.preventDefault();
        for (const f of files) await this._attachFile(f);
      }
    },
    removePendingImage(i) { this.pendingImages.splice(i, 1); },
    removePendingDoc(i) { this.pendingDocs.splice(i, 1); },

    // Alias for use in inline x-html (shorter name reads better in markup).
    renderMd(text) { return this.mdRender(text); },
    // Friendly label for a model id — falls back to the raw id if not in catalog.
    // Used by the bubble badge so old messages keep showing their original model
    // (deepseek / glm / claude variants) instead of just the long id.
    modelLabel(id) {
      if (!id) return "";
      const meta = (this.availableModels || []).find(m => m.model === id);
      return meta ? meta.label : id;
    },
    // Render mcp__<server>__<tool> nicely: drop the mcp__ prefix, replace __ with " · "
    renderToolName(name) {
      if (!name) return "";
      if (name.startsWith("mcp__")) {
        return name.slice(5).split("__").join(" · ");
      }
      return name;
    },

    mdRender(text) {
      if (!text) return "";
      const raw = window.marked ? window.marked.parse(text) : text;
      if (!window.DOMPurify) return raw;
      const safe = window.DOMPurify.sanitize(raw, {
        USE_PROFILES: { html: true, mathMl: true },          // KaTeX may emit MathML
        FORBID_TAGS: ["style", "iframe", "form", "object", "embed"],
        FORBID_ATTR: ["style", "formaction"],
        ADD_ATTR: ["aria-hidden"],                            // KaTeX uses these
      });
      // Math: render $...$ / $$...$$ via KaTeX auto-render. KaTeX runs after
      // DOMPurify (its output is trusted vendor HTML, no need to re-sanitize).
      if (window.renderMathInElement && window.katex) {
        const tmp = document.createElement("div");
        tmp.innerHTML = safe;
        try {
          window.renderMathInElement(tmp, {
            delimiters: [
              { left: "$$", right: "$$", display: true },
              { left: "$",  right: "$",  display: false },
              { left: "\\(", right: "\\)", display: false },
              { left: "\\[", right: "\\]", display: true },
            ],
            throwOnError: false,
            ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
          });
        } catch (e) { /* malformed math falls through as plain text */ }
        return tmp.innerHTML;
      }
      return safe;
    },

    async login() {
      this.loginErr = "";
      this.token = this.tokenInput.trim();
      try {
        const r = await fetch("/api/files/list?path=", { headers: this.hdr() });
        if (!r.ok) throw new Error("token 错误");
        localStorage.setItem("muselab_token", this.token);
        this.authed = true;
        this.loadPrefs();
        await this.loadRoot();
        await this.initSessions();
        this.fetchStats();
      } catch (e) { this.loginErr = e.message; }
    },

    logout() {
      localStorage.removeItem("muselab_token");
      location.reload();
    },

    hdr() { return { "X-Auth-Token": this.token }; },

    // ===== toast =====
    toast(msg, type = "info", timeout = 3000) {
      const id = ++this._toastId;
      this.toasts.push({ id, msg, type });
      if (timeout) setTimeout(() => this.dismissToast(id), timeout);
    },
    dismissToast(id) { this.toasts = this.toasts.filter(t => t.id !== id); },

    // ===== modal =====
    confirm({ title, body = "", okText, cancelText, danger = false }) {
      title = title || this.t("btn.confirm");
      okText = okText || this.t("btn.confirm");
      cancelText = cancelText || this.t("btn.cancel");
      return new Promise((resolve) => {
        this.modal = {
          show: true, title, body, input: null,
          okText, cancelText, danger,
          confirm: () => { this.modal.show = false; resolve(true); },
          cancel: () => { this.modal.show = false; resolve(false); },
        };
      });
    },
    prompt({ title, body = "", placeholder = "", value = "", okText, cancelText }) {
      title = title || (this.lang === "zh" ? "输入" : "Input");
      okText = okText || this.t("btn.confirm");
      cancelText = cancelText || this.t("btn.cancel");
      return new Promise((resolve) => {
        this.modal = {
          show: true, title, body, input: value,
          okText, cancelText, danger: false,
          confirm: () => { const v = this.modal.input; this.modal.show = false; resolve(v); },
          cancel: () => { this.modal.show = false; resolve(null); },
        };
        this.$nextTick(() => { if (this.$refs.modalInput) this.$refs.modalInput.focus(); });
      });
    },

    // ===== prefs =====
    savePrefs() {
      localStorage.setItem("muselab_prefs", JSON.stringify({
        model: this.model, permission: this.permission,
        showThinking: this.showThinking, currentId: this.currentId,
        expanded: Array.from(this.expanded),
        leftOpen: this.leftOpen, rightOpen: this.rightOpen,
        leftWidth: this.leftWidth, rightWidth: this.rightWidth,
        showHidden: this.showHidden,
      }));
    },
    loadPrefs() {
      try {
        const p = JSON.parse(localStorage.getItem("muselab_prefs") || "{}");
        if (p.model) this.model = p.model;
        if (p.permission) this.permission = p.permission;
        if (typeof p.showThinking === "boolean") this.showThinking = p.showThinking;
        if (typeof p.leftOpen === "boolean") this.leftOpen = p.leftOpen;
        if (typeof p.rightOpen === "boolean") this.rightOpen = p.rightOpen;
        if (typeof p.leftWidth === "number") this.leftWidth = p.leftWidth;
        if (typeof p.rightWidth === "number") this.rightWidth = p.rightWidth;
        if (typeof p.showHidden === "boolean") this.showHidden = p.showHidden;
        if (p.currentId) this.currentId = p.currentId;
        this._pendingExpanded = p.expanded || [];
      } catch {}
    },

    async fetchContextInfo() {
      try {
        const r = await fetch("/api/chat/context-info", { headers: this.hdr() });
        if (r.ok) {
          this.contextInfo = await r.json();
          // First successful load: if the user hasn't configured any provider,
          // pop the Settings drawer so they can fix it before trying to chat.
          // _providerCheckDone gate ensures we don't re-pop on heartbeat
          // reconnects or polling refreshes.
          if (!this._providerCheckDone) {
            this._providerCheckDone = true;
            if (!this.contextInfo.has_any_provider && !this.settings.show) {
              this.openSettings();
            }
          }
        }
      } catch (e) { /* non-fatal */ }
    },

    async fetchStats() {
      try {
        const r = await fetch("/api/chat/usage", { headers: this.hdr() });
        if (r.ok) {
          const d = await r.json();
          this.stats = { ...this.stats, total_cost_usd: d.total_cost_usd, total_messages: d.total_messages };
        }
      } catch {}
      try {
        const r = await fetch("/api/chat/mcp", { headers: this.hdr() });
        if (r.ok) this.mcp = await r.json();
      } catch {}
      try {
        const r = await fetch("/api/chat/providers", { headers: this.hdr() });
        if (r.ok) this.availableModels = (await r.json()).models || [];
      } catch {}
    },

    // Model switch:
    //   - empty session: PATCH model in place (no point in creating an empty fork)
    //   - session with messages: confirm modal "切换模型需要新建会话" —
    //     confirm → create new session with chosen model, jump to it.
    //     cancel  → revert dropdown.
    async onModelChange() {
      const newM = this.model;
      if (!this.currentId) return;
      const cur = this.sessions.find(s => s.id === this.currentId);
      const oldM = cur ? cur.model : "";
      if (newM === oldM) return;

      // Decide empty vs has-messages from the PERSISTED message_count, not
      // this.messages.length. The in-memory array can be temporarily empty
      // during a race (session metadata loaded before messages stream in),
      // which would wrongly take the silent path on a session that actually
      // has history.
      const persistedCount = (cur && typeof cur.message_count === "number")
        ? cur.message_count
        : this.messages.length;

      // Empty session — switch in place (no point creating an empty fork).
      // Still toast so the user gets visual confirmation the switch happened.
      if (persistedCount === 0) {
        try {
          const r = await fetch("/api/chat/sessions/" + this.currentId, {
            method: "PATCH",
            headers: { ...this.hdr(), "Content-Type": "application/json" },
            body: JSON.stringify({ model: newM }),
          });
          if (!r.ok) {
            this.model = oldM;
            this.toast(this.t("slash.failed"), "error");
            return;
          }
          await this.refreshSessions();
          this.savePrefs();
          const label = this.modelLabel(newM);
          this.toast(this.lang === "zh"
            ? `已切到 ${label}（空会话，无需新建）`
            : `Switched to ${label} (empty session, no fork needed)`,
            "success", 1800);
        } catch (e) {
          this.model = oldM;
          this.toast(this.t("slash.failed"), "error");
        }
        return;
      }

      // Session has history — confirm + create new.
      const label = this.modelLabel(newM);
      const ok = await this.confirm({
        title: this.t("model.switch_title"),
        body: this.t("model.switch_body", { label }),
        okText: this.t("model.switch_new"),
      });
      if (!ok) {
        this.model = oldM;     // revert dropdown
        return;
      }
      try {
        const r = await fetch("/api/chat/sessions", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ name: "", model: newM }),
        });
        if (!r.ok) {
          this.model = oldM;
          this.toast(this.t("slash.failed"), "error");
          return;
        }
        const meta = await r.json();
        await this.refreshSessions();
        this.currentId = meta.id;
        await this.loadSession(meta.id);
        this.savePrefs();
        this.toast(this.t("model.new_session_ok", { label }), "success", 2000);
      } catch (e) {
        this.model = oldM;
        this.toast(this.t("slash.failed"), "error");
      }
    },

    modelGroups() {
      const map = {};
      for (const m of this.availableModels) {
        if (!map[m.group]) map[m.group] = { name: m.group, items: [] };
        map[m.group].items.push(m);
      }
      return Object.values(map);
    },

    currentModelLabel() {
      const m = this.availableModels.find(x => x.model === this.model);
      if (m) return m.label;
      // fallback：直接显示 model id
      return this.model || "AI";
    },

    // ===== sessions =====
    async initSessions() {
      await this.refreshSessions();
      if (!this.sessions.length) {
        const s = await this.newSession();
        this.currentId = s.id;
      } else if (!this.sessions.find(x => x.id === this.currentId)) {
        this.currentId = this.sessions[0].id;
      }
      await this.loadSession(this.currentId);
      this.savePrefs();
    },
    async refreshSessions() {
      const r = await fetch("/api/chat/sessions", { headers: this.hdr() });
      if (r.ok) this.sessions = (await r.json()).sessions;
    },
    async newSession() {
      const r = await fetch("/api/chat/sessions", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ name: "", model: this.model }),
      });
      const meta = await r.json();
      await this.refreshSessions();
      this.currentId = meta.id;
      this.messages = [];
      this.savePrefs();
      this.toast(this.t("toast.created"), "success");
      return meta;
    },
    async switchSession() { this.savePrefs(); await this.loadSession(this.currentId); },
    async loadSession(sid) {
      if (!sid) return;
      const r = await fetch("/api/chat/sessions/" + sid, { headers: this.hdr() });
      if (!r.ok) { this.messages = []; return; }
      const s = await r.json();
      // 用 sid 拼 key，确保切 session 时 Alpine 重新挂载所有节点
      this.messages = (s.messages || []).map((m, i) => {
        const out = { ...m, _k: sid + "-" + i };
        if (m.role === "assistant" && m.text) out.html = this.mdRender(m.text);
        return out;
      });
      if (s.model) this.model = s.model;
      this.atBottom = true;
      this.scrollToBottom(true);
      this.$nextTick(() => this.highlightCode(".chat-body"));
      // Refresh per-session context meter — won't have anything yet if the
      // process restarted, but will once a new turn fires.
      try {
        const ur = await fetch(`/api/chat/usage/${sid}?model=${encodeURIComponent(this.model || "")}`,
                                 { headers: this.hdr() });
        if (ur.ok) this.sessionUsage = await ur.json();
      } catch (e) { /* non-fatal */ }
    },
    async renameSession() {
      const cur = this.sessions.find(x => x.id === this.currentId);
      if (!cur) return;
      const name = await this.prompt({ title: "重命名会话", value: cur.name });
      if (!name) return;
      const r = await fetch("/api/chat/sessions/" + cur.id, {
        method: "PATCH",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (r.ok) { await this.refreshSessions(); this.toast(this.t("toast.renamed"), "success"); }
    },

    async editSessionPrompt() {
      const cur = this.sessions.find(x => x.id === this.currentId);
      if (!cur) return;
      // 取最新（含 system_prompt）
      const r0 = await fetch("/api/chat/sessions/" + cur.id, { headers: this.hdr() });
      const full = r0.ok ? await r0.json() : { system_prompt: "" };
      const prompt = await this.prompt({
        title: "本会话 system prompt（留空 = 用默认）",
        body: "会拼在 muselab 默认 system prompt 前。改后下一条消息生效。",
        value: full.system_prompt || "",
      });
      if (prompt === null) return;
      const r = await fetch("/api/chat/sessions/" + cur.id, {
        method: "PATCH",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ system_prompt: prompt }),
      });
      if (r.ok) this.toast(this.t("toast.saved"), "success");
    },

    // ===== settings modal =====
    async openSettings() {
      const r = await fetch("/api/settings", { headers: this.hdr() });
      if (!r.ok) { this.toast("无法加载设置", "error"); return; }
      const d = await r.json();
      this.settings.providers = d.providers;
      this.settings.draftKeys = Object.fromEntries(d.providers.map(p => [p.env_key, ""]));
      this.settings.draftDefaults = { ...d.defaults };
      this.settings.draftParams = { ...d.params };
      this.settings.show = true;
      // Load MCP + Skill list in parallel — non-fatal if either fails.
      this.refreshMcpList();
      this.refreshSkillList();
    },
    async refreshSkillList() {
      try {
        const r = await fetch("/api/settings/skills", { headers: this.hdr() });
        if (!r.ok) return;
        const d = await r.json();
        this.settings.skills = d.skills || [];
      } catch (e) { /* silent */ }
    },
    async refreshMcpList() {
      try {
        const r = await fetch("/api/settings/mcp", { headers: this.hdr() });
        if (!r.ok) return;
        const d = await r.json();
        this.settings.mcpServers = d.servers || [];
        this.settings.mcpExamples = d.examples || [];
      } catch (e) { /* silent — UI shows empty state */ }
    },
    async toggleMcp(name, disabled) {
      const r = await fetch(`/api/settings/mcp/${encodeURIComponent(name)}/toggle`, {
        method: "PATCH",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ disabled }),
      });
      if (r.ok) {
        this.toast(this.t("set.mcp.toggle_saved"), "success", 1500);
        this.refreshMcpList();
      } else {
        this.toast(this.t("set.mcp.save_failed"), "error", 3000);
      }
    },
    async deleteMcp(name) {
      const ok = await this.confirm({
        title: this.t("set.mcp.delete"),
        body: this.lang === "zh"
          ? `确定删除 MCP server「${name}」？`
          : `Delete MCP server "${name}"?`,
        danger: true,
        okText: this.t("set.mcp.delete"),
      });
      if (!ok) return;
      const r = await fetch(`/api/settings/mcp/${encodeURIComponent(name)}`, {
        method: "DELETE", headers: this.hdr(),
      });
      if (r.ok) {
        this.toast(this.t("set.mcp.deleted"), "success", 1500);
        this.refreshMcpList();
      } else {
        this.toast(this.t("set.mcp.delete_failed"), "error", 3000);
      }
    },
    async addMcpFromDraft() {
      const d = this.settings.mcpDraft;
      const name = (d.name || "").trim();
      const command = (d.command || "").trim();
      if (!name || !command) {
        this.toast(this.t("set.mcp.name_command_required"), "warn", 2500);
        return;
      }
      const args = (d.argsStr || "").trim().split(/\s+/).filter(Boolean);
      const r = await fetch(`/api/settings/mcp/${encodeURIComponent(name)}`, {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ name, command, args, env: {}, disabled: false }),
      });
      if (r.ok) {
        this.toast(this.t("set.mcp.added"), "success", 1500);
        this.settings.mcpDraft = { show: false, name: "", command: "", argsStr: "" };
        this.refreshMcpList();
      } else {
        this.toast(this.t("set.mcp.save_failed"), "error", 3000);
      }
    },
    // Provider key self-test — hits the vendor's anthropic-compatible endpoint
    // with the configured key and reports back. Useful when user gets 401 and
    // doesn't want to paste keys to debug.
    async probeProvider(envKey) {
      // Pick a representative model id for this env key.
      const ENV_TO_MODEL = {
        DEEPSEEK_API_KEY: "deepseek-v4-flash",
        ZHIPUAI_API_KEY:  "glm-4.7",
        MINIMAX_API_KEY:  "minimax-m2.7",
      };
      const m = ENV_TO_MODEL[envKey];
      if (!m) return;
      this.settings.probeResults[envKey] = { ok: null, text: this.t("set.probe_running") };
      try {
        const r = await fetch(`/api/chat/probe/${encodeURIComponent(m)}`,
                                 { headers: this.hdr() });
        const d = await r.json();
        if (d.ok) {
          this.settings.probeResults[envKey] = {
            ok: true,
            text: `${this.t("set.probe_ok")} · ${d.key_hint}`,
          };
        } else {
          const status = d.status ? `HTTP ${d.status}` : (d.reason || "error");
          // Extract a single-line vendor message if there is one
          let detail = "";
          try {
            const ex = d.vendor_response_excerpt ?
              JSON.parse(d.vendor_response_excerpt) : null;
            detail = ex?.error?.message || ex?.error?.type || "";
          } catch { detail = (d.vendor_response_excerpt || "").slice(0, 120); }
          // Tack on a hint based on common error shapes
          let hint = "";
          if (d.status === 401) hint = " · " + this.t("set.probe_hint_401");
          else if (d.status === 403) hint = " · " + this.t("set.probe_hint_403");
          else if (d.status === 429) hint = " · " + this.t("set.probe_hint_429");
          this.settings.probeResults[envKey] = {
            ok: false,
            text: `${status}: ${detail || "—"}${hint}`,
          };
        }
      } catch (e) {
        this.settings.probeResults[envKey] = {
          ok: false, text: this.t("set.probe_failed") + ": " + e.message,
        };
      }
    },

    async cleanupEmptySessions() {
      const ok = await this.confirm({
        title: this.t("set.sessions.cleanup_confirm_title"),
        body:  this.t("set.sessions.cleanup_confirm_body"),
        okText: this.t("set.sessions.cleanup_btn"),
        danger: true,
      });
      if (!ok) return;
      const r = await fetch("/api/chat/sessions", { method: "DELETE", headers: this.hdr() });
      if (!r.ok) { this.toast(this.t("slash.failed"), "error"); return; }
      const d = await r.json();
      await this.refreshSessions();
      this.toast(this.t("set.sessions.cleanup_done", { n: d.count }), "success", 2500);
      // If current session was among removed ones, jump to first remaining or fresh
      if (!this.sessions.find(s => s.id === this.currentId)) {
        if (this.sessions.length) {
          this.currentId = this.sessions[0].id;
          await this.loadSession(this.currentId);
        } else {
          await this.newSession();
        }
      }
    },

    async installMcpPreset(ex) {
      const r = await fetch(`/api/settings/mcp/${encodeURIComponent(ex.name)}`, {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({
          name: ex.name, command: ex.command, args: ex.args || [],
          env: ex.env || {}, disabled: false,
        }),
      });
      if (r.ok) {
        this.toast(this.t("set.mcp.installed"), "success", 1500);
        this.refreshMcpList();
      } else {
        this.toast(this.t("set.mcp.save_failed"), "error", 3000);
      }
    },
    async saveSettings() {
      const body = {
        default_model: this.settings.draftDefaults.model,
        default_permission: this.settings.draftDefaults.permission,
        default_show_thinking: this.settings.draftDefaults.show_thinking,
        thinking_budget: this.settings.draftParams.thinking_budget,
        max_turns: this.settings.draftParams.max_turns,
      };
      // 字段名按后端转 snake_case
      const k2f = {
        ANTHROPIC_API_KEY: "anthropic_api_key",
        DEEPSEEK_API_KEY: "deepseek_api_key",
        ZHIPUAI_API_KEY: "zhipuai_api_key",
        MINIMAX_API_KEY: "minimax_api_key",
      };
      for (const [envK, field] of Object.entries(k2f)) {
        const v = this.settings.draftKeys[envK];
        if (v && v.trim()) body[field] = v.trim();
      }
      const r = await fetch("/api/settings", {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (r.ok) {
        const d = await r.json();
        this.settings.show = false;
        this.toast(`已保存 ${d.updated.length} 项设置`, "success");
        // 刷新可用 provider 列表
        const r2 = await fetch("/api/chat/providers", { headers: this.hdr() });
        if (r2.ok) this.availableModels = (await r2.json()).models || [];
        // 也刷新 contextInfo — has_any_provider 变了，否则 "no provider" 卡片不消失
        this.fetchContextInfo();
      } else {
        this.toast("保存失败：" + (await r.text()), "error", 5000);
      }
    },
    async deleteSession() {
      const cur = this.sessions.find(x => x.id === this.currentId);
      if (!cur) return;
      const ok = await this.confirm({ title: "删除会话", body: `确定删除「${cur.name}」？此操作不可恢复。`, danger: true, okText: "删除" });
      if (!ok) return;
      await fetch("/api/chat/sessions/" + cur.id, { method: "DELETE", headers: this.hdr() });
      await this.refreshSessions();
      if (this.sessions.length === 0) { const s = await this.newSession(); this.currentId = s.id; }
      else { this.currentId = this.sessions[0].id; }
      await this.loadSession(this.currentId);
      this.savePrefs();
      this.toast(this.t("toast.deleted"), "success");
    },

    // ===== file tree =====
    async loadRoot() {
      this.childCache = {};
      const children = await this.fetchChildren("");
      this.visible = children.map(c => ({ ...c, depth: 0 }));
      this.expanded = new Set();
      const want = this._pendingExpanded || [];
      this._pendingExpanded = null;
      for (const p of want.sort((a, b) => a.length - b.length)) {
        const node = this.visible.find(n => n.path === p);
        if (node && node.is_dir) await this.expand(node);
      }
    },
    reloadTree() {
      this._pendingExpanded = Array.from(this.expanded);
      this.childCache = {};
      this.loadRoot();
    },
    async fetchChildren(path) {
      if (this.childCache[path]) return this.childCache[path];
      const url = "/api/files/list?path=" + encodeURIComponent(path)
        + (this.showHidden ? "&show_hidden=true" : "");
      const r = await fetch(url, { headers: this.hdr() });
      if (!r.ok) return [];
      const d = await r.json();
      this.childCache[path] = d.entries;
      if (d.truncated) {
        this.toast(`/${path || ""} 条目过多，仅显示前 ${d.entries.length} 条`, "warn", 3500);
      }
      return d.entries;
    },
    toggleHidden() {
      this.showHidden = !this.showHidden;
      this.savePrefs();
      this.reloadTree();
      this.toast(this.showHidden ? "显示隐藏文件" : "已隐藏 .* 文件", "info", 1500);
    },
    async onNodeClick(n) {
      if (n.is_dir) {
        if (this.expanded.has(n.path)) this.collapse(n);
        else await this.expand(n);
        this.savePrefs();
      } else {
        await this.openFile(n);
      }
    },
    async expand(n) {
      const children = await this.fetchChildren(n.path);
      const idx = this.visible.findIndex(x => x.path === n.path);
      if (idx < 0) return;
      const items = children.map(c => ({ ...c, depth: n.depth + 1 }));
      this.visible.splice(idx + 1, 0, ...items);
      this.expanded.add(n.path);
      this.expanded = new Set(this.expanded);
    },
    collapse(n) {
      const idx = this.visible.findIndex(x => x.path === n.path);
      if (idx < 0) return;
      let end = idx + 1;
      while (end < this.visible.length && this.visible[end].depth > n.depth) end++;
      this.visible.splice(idx + 1, end - idx - 1);
      for (const p of Array.from(this.expanded)) {
        if (p === n.path || p.startsWith(n.path + "/")) this.expanded.delete(p);
      }
      this.expanded = new Set(this.expanded);
    },
    // ===== context menu =====
    openCtxMenu(ev, n) {
      // Clamp to viewport so menu doesn't overflow.
      const MENU_W = 200, MENU_H = 280;
      const x = Math.min(ev.clientX, window.innerWidth - MENU_W - 8);
      const y = Math.min(ev.clientY, window.innerHeight - MENU_H - 8);
      this.ctxMenu = { show: true, x, y, node: n };
    },
    async ctxAction(action) {
      const n = this.ctxMenu.node;
      this.ctxMenu.show = false;
      if (!n) return;
      switch (action) {
        case "open":
          if (!n.is_dir) await this.openFile(n);
          break;
        case "mention":
          this.insertFileMention(n.path);
          break;
        case "copyPath":
          await navigator.clipboard?.writeText(n.path);
          this.toast(this.t("toast.copied") + ": " + n.path, "success", 1500);
          break;
        case "download":
          if (!n.is_dir) window.open(this.downloadUrl(n.path), "_blank");
          break;
        case "rename":
          await this.doRename(n);
          break;
        case "delete":
          await this.doDelete(n);
          break;
        case "newFile":
          await this.doNewFile(n);
          break;
        case "newDir":
          await this.doNewDir(n);
          break;
        case "upload":
          this._ctxUploadDir = n.path;
          this.$refs.ctxUpload.click();
          break;
      }
    },
    async doNewFile(dirNode) {
      const name = await this.prompt({
        title: "新建文件", body: `在 /${dirNode.path} 下：`,
        value: "new.md",
      });
      if (!name) return;
      const path = dirNode.path ? `${dirNode.path}/${name}` : name;
      const r = await fetch("/api/files/write", {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ path, content: "" }),
      });
      if (r.ok) {
        delete this.childCache[dirNode.path];
        this.reloadTree();
        this.toast(`已创建 ${name}`, "success");
        // 自动打开编辑
        await this.openFile({ path, name });
        this.editing = true;
      } else this.toast("创建失败：" + (await r.text()), "error");
    },
    async doNewDir(dirNode) {
      const name = await this.prompt({
        title: "新建子目录", body: `在 /${dirNode.path} 下：`,
        value: "",
      });
      if (!name) return;
      const path = dirNode.path ? `${dirNode.path}/${name}` : name;
      const r = await fetch("/api/files/mkdir", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      if (r.ok) {
        delete this.childCache[dirNode.path];
        this.reloadTree();
        this.toast(`已创建 ${name}/`, "success");
      } else this.toast("失败：" + (await r.text()), "error");
    },
    _ctxUploadDir: "",
    async ctxUploadHandler(ev) {
      const file = ev.target.files[0];
      if (!file) return;
      await this.uploadFileTo(this._ctxUploadDir, file);
      ev.target.value = "";
      this._ctxUploadDir = "";
    },
    async doRename(n) {
      const newName = await this.prompt({
        title: "重命名", body: `当前路径：${n.path}`,
        value: n.name,
      });
      if (!newName || newName === n.name) return;
      const parent = n.path.split("/").slice(0, -1).join("/");
      const newPath = parent ? `${parent}/${newName}` : newName;
      const r = await fetch("/api/files/rename", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ src: n.path, dst: newPath }),
      });
      if (r.ok) {
        if (this.selected === n.path) this.selected = newPath;
        delete this.childCache[parent];
        this.reloadTree();
        this.toast(this.t("toast.renamed"), "success");
      } else this.toast("重命名失败：" + (await r.text()), "error");
    },
    async doDelete(n) {
      const ok = await this.confirm({
        title: "删除", body: `删除 ${n.name}？` + (n.is_dir ? "（仅可删除空目录）" : ""),
        danger: true, okText: "删除",
      });
      if (!ok) return;
      const r = await fetch("/api/files/delete", {
        method: "DELETE",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ path: n.path }),
      });
      if (r.ok) {
        // 同步 tabs：删了的文件如果在 tabs 也清掉
        this.tabs = this.tabs.filter(t => t.path !== n.path);
        if (this.selected === n.path) { this.selected = ""; this.previewMode = ""; }
        const parent = n.path.split("/").slice(0, -1).join("/");
        delete this.childCache[parent];
        this.reloadTree();
        this.toast(this.t("toast.deleted"), "success");
      } else this.toast("删除失败：" + (await r.text()), "error");
    },
    async openFile(n) {
      // multi-tab：第一次打开就推进 tabs；已存在则切换
      if (!this.tabs.find(t => t.path === n.path)) {
        this.tabs.push({ path: n.path, name: n.name || n.path.split("/").pop() });
      }
      this.selected = n.path;
      this.editing = false;
      // Mobile: opening a file should jump to the preview pane (otherwise
      // the user is still on `files` tab and sees nothing change).
      if (window.innerWidth <= 900) this.mobileTab = "preview";
      const name = n.name || n.path.split("/").pop();
      const ext = name.split(".").pop().toLowerCase();
      if (["md", "markdown"].includes(ext)) {
        this.previewMode = "md";
        const r = await fetch("/api/files/read?path=" + encodeURIComponent(n.path), { headers: this.hdr() });
        if (r.ok) {
          this.rawText = await r.text();
          this.renderedMd = this.mdRender(this.rawText);
          this.$nextTick(() => this.highlightCode(".markdown"));
        }
      } else if (["html", "htm"].includes(ext)) {
        // Render via sandboxed iframe (backend sends strict CSP + sandbox token).
        this.previewMode = "html";
      }
      else if (["png", "jpg", "jpeg", "gif", "webp", "ico", "bmp"].includes(ext)) this.previewMode = "img";
      else if (ext === "pdf") this.previewMode = "pdf";
      else {
        const r = await fetch("/api/files/read?path=" + encodeURIComponent(n.path), { headers: this.hdr() });
        if (r.ok) {
          this.previewMode = "text";
          this.rawText = await r.text();
          this.previewLang = this.hljsLang(n.path);
          // 强制重新高亮：删 dataset.hl 让 highlightCode 重新跑
          this.$nextTick(() => {
            document.querySelectorAll(".text code").forEach(el => { delete el.dataset.hl; });
            this.highlightCode(".text");
          });
        }
        else this.previewMode = "unsupported";
      }
    },
    hljsLang(path) {
      if (!path) return "plaintext";
      const name = path.split("/").pop().toLowerCase();
      // No-extension files mapped by name
      const noExt = {
        dockerfile: "dockerfile", containerfile: "dockerfile",
        makefile: "makefile",
        rakefile: "ruby", gemfile: "ruby",
        vagrantfile: "ruby", brewfile: "ruby",
      };
      if (noExt[name]) return noExt[name];
      const ext = name.includes(".") ? name.split(".").pop() : "";
      const map = {
        md: "markdown", markdown: "markdown",
        py: "python", pyi: "python",
        js: "javascript", mjs: "javascript", cjs: "javascript",
        jsx: "javascript", ts: "typescript", tsx: "typescript",
        cpp: "cpp", "c++": "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp",
        c: "c", h: "c", m: "objectivec",
        rs: "rust", go: "go",
        java: "java", kt: "kotlin", scala: "scala",
        rb: "ruby", php: "php", swift: "swift", lua: "lua",
        sh: "bash", bash: "bash", zsh: "bash", fish: "bash",
        ps1: "powershell",
        sql: "sql", graphql: "graphql",
        html: "xml", htm: "xml", xml: "xml", svg: "xml",
        css: "css", scss: "scss", less: "less",
        json: "json", yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini",
        env: "bash", conf: "ini",
        log: "accesslog",
        vue: "xml", svelte: "xml",
        proto: "protobuf",
      };
      return map[ext] || "plaintext";
    },
    async openByPath(path) { await this.openFile({ path, name: path.split("/").pop() }); },

    async switchTab(path) {
      // 不再 push（已在 tabs 里），只是切换 selected 并重新加载内容
      await this.openFile({ path, name: path.split("/").pop() });
    },
    closeTab(path) {
      const idx = this.tabs.findIndex(t => t.path === path);
      if (idx < 0) return;
      this.tabs.splice(idx, 1);
      if (this.selected !== path) return;
      // 关掉的是当前 tab，切到旁边
      if (this.tabs.length === 0) {
        this.selected = "";
        this.previewMode = "";
        this.rawText = "";
        this.renderedMd = "";
        this.editing = false;
      } else {
        const next = this.tabs[Math.min(idx, this.tabs.length - 1)];
        this.openByPath(next.path);
      }
    },

    rawUrl(p) { return "/api/files/raw?path=" + encodeURIComponent(p) + "&token=" + encodeURIComponent(this.token); },
    downloadUrl(p) { return "/api/files/download?path=" + encodeURIComponent(p) + "&token=" + encodeURIComponent(this.token); },

    iconRef(n) {
      if (n.is_dir) return "#i-folder";
      const name = n.name || n.path.split("/").pop() || "";
      const ext = name.split(".").pop().toLowerCase();
      if (["md", "markdown", "txt", "rst"].includes(ext)) return "#i-file-text";
      if (["html", "htm"].includes(ext)) return "#i-globe";
      if (["png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp"].includes(ext)) return "#i-image";
      if (["py", "js", "ts", "go", "rs", "java", "cpp", "c", "sh", "json", "yaml", "yml", "toml"].includes(ext)) return "#i-code";
      return "#i-file";
    },
    fmtSize(n) {
      if (n < 1024) return n + "B";
      if (n < 1024 * 1024) return (n / 1024).toFixed(1) + "K";
      return (n / 1024 / 1024).toFixed(1) + "M";
    },
    highlightCode(root) {
      if (!window.hljs) { console.warn("[muselab] hljs not loaded"); return; }
      document.querySelectorAll(root + " code").forEach(el => {
        // hljs.highlightElement refuses to re-highlight already-highlighted
        // elements. So always go through highlight() directly and replace HTML.
        const text = el.textContent;
        const m = el.className.match(/language-([\w+#-]+)/);
        const lang = m && m[1];
        try {
          const r = (lang && window.hljs.getLanguage(lang))
            ? window.hljs.highlight(text, { language: lang, ignoreIllegals: true })
            : window.hljs.highlightAuto(text);
          el.innerHTML = r.value;
          el.classList.add("hljs");
        } catch (e) { console.warn("[muselab] highlight failed:", e); }
      });
    },

    // ===== search =====
    async doSearch() {
      const q = this.searchQ.trim();
      if (q.length < 2) { this.clearSearch(); return; }
      this.searchMode = true;
      this.searching = true;
      const [a, b] = await Promise.all([
        fetch("/api/files/search?q=" + encodeURIComponent(q), { headers: this.hdr() }).then(r => r.ok ? r.json() : { entries: [] }),
        fetch("/api/files/grep?q=" + encodeURIComponent(q), { headers: this.hdr() }).then(r => r.ok ? r.json() : { hits: [] }),
      ]);
      this.searchHits = a.entries || [];
      this.searchTruncated = !!a.truncated;
      this.grepHits = b.hits || [];
      this.grepTruncated = !!b.truncated;
      this.searching = false;
    },
    clearSearch() {
      this.searchQ = ""; this.searchMode = false; this.searching = false;
      this.searchHits = []; this.grepHits = []; this.searchTruncated = false; this.grepTruncated = false;
    },
    async onSearchClick(n) {
      if (n.is_dir) { this.clearSearch(); await this.expandPath(n.path); }
      else { await this.openFile(n); }
    },
    async expandPath(path) {
      const parts = path.split("/");
      let acc = "";
      for (let i = 0; i < parts.length; i++) {
        acc = acc ? acc + "/" + parts[i] : parts[i];
        const node = this.visible.find(x => x.path === acc);
        if (node && node.is_dir && !this.expanded.has(acc)) await this.expand(node);
      }
    },

    // ===== upload / drag-drop / mkdir =====
    async upload(ev) {
      const file = ev.target.files[0];
      if (!file) return;
      await this.uploadFileTo("", file);
      ev.target.value = "";
    },
    async uploadFileTo(dirPath, file) {
      const fd = new FormData();
      fd.append("path", dirPath);
      fd.append("file", file);
      const r = await fetch("/api/files/upload", { method: "POST", headers: this.hdr(), body: fd });
      if (r.ok) {
        delete this.childCache[dirPath];
        this.reloadTree();
        this.toast(`已上传 ${file.name} 到 /${dirPath || ""}`, "success");
      } else this.toast("上传失败：" + (await r.text()), "error");
    },
    async onDrop(ev, n) {
      this.dragOver = "";
      if (!n.is_dir) return;
      const files = ev.dataTransfer?.files || [];
      if (!files.length) return;
      for (const f of files) await this.uploadFileTo(n.path, f);
    },
    async mkdirPrompt() {
      const name = await this.prompt({
        title: "新建目录",
        body: "输入相对根的路径，例如 archives/2026",
        placeholder: "archives/2026",
      });
      if (!name) return;
      const r = await fetch("/api/files/mkdir", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ path: name }),
      });
      if (r.ok) { this.reloadTree(); this.toast(this.t("toast.created"), "success"); }
      else this.toast("失败：" + (await r.text()), "error");
    },

    // ===== edit =====
    isEditable(path) {
      if (!path) return false;
      const name = path.split("/").pop().toLowerCase();
      const ext = name.includes(".") ? name.split(".").pop() : name;
      return EDITABLE_EXT.has(ext);
    },

    layoutStyle() {
      // 动态算 template，匹配实际渲染的元素数。否则 x-show 隐藏 resizer 时
      // 元素被移出 grid，剩余 children 错位填入空闲 column，导致右 resizer
      // 拿到 1fr 宽 → 鼠标 hover 它整片变成 accent 色。
      const cols = [];
      if (this.leftOpen) cols.push(this.leftWidth + "px", "4px");
      cols.push("1fr");
      if (this.rightOpen) cols.push("4px", this.rightWidth + "px");
      return { gridTemplateColumns: cols.join(" ") };
    },
    startResize(which, ev) {
      ev.preventDefault();
      const startX = ev.clientX;
      const startW = which === "left" ? this.leftWidth : this.rightWidth;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      ev.target.classList.add("active");
      const onMove = (e) => {
        const delta = which === "left" ? (e.clientX - startX) : (startX - e.clientX);
        const w = Math.max(180, Math.min(700, startW + delta));
        if (which === "left") this.leftWidth = w;
        else this.rightWidth = w;
      };
      const onUp = () => {
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        ev.target.classList.remove("active");
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        this.savePrefs();
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },

    async toggleEdit() {
      if (this.editing) { this.editing = false; return; }
      // 进入编辑：确保 rawText 已加载（html/img/pdf 走 raw 模式时没 fetch 文本）
      if (!this.rawText || this.previewMode === "html" || this.previewMode === "pdf" || this.previewMode === "img") {
        const r = await fetch("/api/files/read?path=" + encodeURIComponent(this.selected), { headers: this.hdr() });
        if (!r.ok) {
          this.toast("无法读取文件（可能是二进制或太大）：" + (await r.text()), "error", 5000);
          return;
        }
        this.rawText = await r.text();
      }
      this.editText = this.rawText;
      this.editing = true;
    },
    async saveEdit() {
      const r = await fetch("/api/files/write", {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ path: this.selected, content: this.editText }),
      });
      if (r.ok) {
        this.rawText = this.editText;
        if (this.previewMode === "md") {
          this.renderedMd = this.mdRender(this.rawText);
          this.$nextTick(() => this.highlightCode(".markdown"));
        }
        this.editing = false;
        this.toast(this.t("toast.saved"), "success");
      } else this.toast("保存失败：" + (await r.text()), "error");
    },

    // ===== @ mention =====
    insertFileMention(path) {
      const mention = "@" + path + " ";
      this.input = (this.input || "") + (this.input && !this.input.endsWith(" ") ? " " : "") + mention;
      if (this.$refs.chatInput) this.$refs.chatInput.focus();
      this.toast(this.t("toast.mention_added", { path }), "success", 1500);
      // Mobile: @ mention is a chat-side action, jump to the chat pane
      if (window.innerWidth <= 900) this.mobileTab = "chat";
    },
    autoGrow(ta) {
      // 撑高到内容 + 上限（避免无限增长把 chat 区挤没）
      ta.style.height = "auto";
      const max = 240;   // px
      ta.style.height = Math.min(ta.scrollHeight, max) + "px";
    },

    // ===== slash commands =====
    slashResults: [],   // filled by onChatInput
    _navPop(delta) {
      // shared up/down handler for either @ mention or / slash popup
      if (this.slashShow) {
        if (delta < 0) this.slashIdx = Math.max(0, this.slashIdx - 1);
        else this.slashIdx = Math.min(this.slashResults.length - 1, this.slashIdx + 1);
        return true;
      }
      if (this.mentionShow) {
        if (delta < 0) this.mentionIdx = Math.max(0, this.mentionIdx - 1);
        else this.mentionIdx = Math.min(this.mentionResults.length - 1, this.mentionIdx + 1);
        return true;
      }
      return false;
    },
    pickSlash(i) {
      const c = this.slashResults[i];
      if (!c) return;
      // Replace current input with the canonical form so user sees what's submitted
      this.input = "/" + c.name + (c.name === "model" || c.name === "resume" ? " " : "");
      this.slashShow = false;
      if (this.$refs.chatInput) this.$refs.chatInput.focus();
      // For commands with NO argument needed, auto-execute on selection
      if (!["model", "resume"].includes(c.name)) {
        this._runSlash(c.name, "");
        this.input = "";
      }
    },

    async _runSlash(cmd, arg) {
      arg = (arg || "").trim();
      switch (cmd) {
        case "help": {
          const cmds = this.SLASH_CMDS
            .map(c => `**/${c.name}** — ${c.desc[this.lang] || c.desc.zh}`)
            .join("\n");
          const md = [
            `## ${this.t("slash.help_title")}`,
            "",
            `### ${this.t("help.sec_slash")}`,
            cmds,
            "",
            `### ${this.t("help.sec_keys")}`,
            this.t("help.keys_list"),
            "",
            `### ${this.t("help.sec_layout")}`,
            this.t("help.layout_list"),
            "",
            `${this.t("help.docs_link")} → [docs/personalize-claude-md.md](docs/personalize-claude-md.md)`,
          ].join("\n");
          this._injectAssistantNote(md);
          return;
        }
        case "clear": {
          if (!this.currentId) return;
          await fetch(`/api/chat/reset?token=${encodeURIComponent(this.token)}&session_id=${encodeURIComponent(this.currentId)}`,
                       { method: "POST" });
          await fetch(`/api/chat/sessions/${this.currentId}`, { method: "DELETE", headers: this.hdr() });
          await this.refreshSessions();
          this.messages = [];
          // start a fresh session so the dropdown isn't empty
          await this.newSession();
          this.toast(this.t("slash.cleared"), "success", 1500);
          return;
        }
        case "compact": {
          if (!this.currentId) return;
          const r = await fetch(`/api/chat/sessions/${this.currentId}/compact`,
                                  { method: "POST", headers: this.hdr() });
          if (!r.ok) { this.toast(this.t("slash.failed"), "error"); return; }
          const meta = await r.json();
          await this.refreshSessions();
          this.currentId = meta.id;
          await this.loadSession(meta.id);
          // Pre-fill input with the compact prompt — user reviews then sends
          this.input = this.t("slash.compact_prompt");
          this.toast(this.t("slash.compact_ok"), "success", 2500);
          return;
        }
        case "model": {
          if (!arg) {
            const list = (this.availableModels || []).map(m => `- ${m.group} · **${m.model}**`).join("\n");
            this._injectAssistantNote(this.t("slash.model_list_title") + "\n\n" + list);
            return;
          }
          const found = (this.availableModels || []).find(m => m.model === arg);
          if (!found) { this.toast(this.t("slash.model_unknown", { id: arg }), "warn", 3000); return; }
          this.model = arg;
          this.toast(this.t("slash.model_switched", { id: arg }), "success", 1500);
          return;
        }
        case "resume": {
          if (!arg) {
            const list = (this.sessions || []).slice(0, 10)
              .map(s => `- **${s.name}** (${s.message_count}, ${s.id.slice(0,8)})`).join("\n");
            this._injectAssistantNote(this.t("slash.resume_list_title") + "\n\n" + list);
            return;
          }
          const q = arg.toLowerCase();
          const hit = (this.sessions || []).find(s =>
            s.id.startsWith(arg) || s.name.toLowerCase().includes(q));
          if (!hit) { this.toast(this.t("slash.resume_no_match"), "warn", 2000); return; }
          this.currentId = hit.id;
          await this.loadSession(hit.id);
          this.toast(this.t("slash.resumed", { name: hit.name }), "success", 1500);
          return;
        }
        case "cost": {
          await this.fetchStats();
          const s = this.stats;
          const lines = [
            `**${this.t("slash.cost_title")}**`,
            `- ${this.t("cost.total")}: $${s.total_cost_usd.toFixed(4)}`,
            `- ${this.t("cost.in_out")}: ${s.total_input_tokens.toLocaleString()} in / ${s.total_output_tokens.toLocaleString()} out`,
            `- ${this.t("cost.cache_hit")}: ${s.cache_hit_pct}% (${s.total_cache_read_tokens.toLocaleString()} cached read)`,
            s.budget_usd > 0
              ? `- ${this.t("cost.budget")}: $${s.budget_usd} (${s.budget_used_pct}% used)`
              : `- ${this.t("cost.no_budget")}`,
            `- ${this.t("cost.context")}: ${(this.sessionUsage.input_tokens/1000).toFixed(1)}K / ${(this.sessionUsage.context_limit/1000).toFixed(0)}K (${this.sessionUsage.context_used_pct}%)`,
          ];
          this._injectAssistantNote(lines.join("\n"));
          return;
        }
        case "config": this.openSettings(); return;
        case "stop":   if (this.streaming) this.stop(); return;
        default:
          this.toast(this.t("slash.unknown", { cmd }), "warn", 2000);
      }
    },

    // Inject a synthetic assistant bubble (markdown rendered) for slash output.
    // Not persisted — slash output is ephemeral, doesn't pollute session history.
    _injectAssistantNote(md) {
      this.messages.push({
        role: "assistant", text: md, html: this.mdRender(md),
        cost: "", model: "muselab", _ephemeral: true,
      });
      this.scrollToBottom(true);
    },

    // Suggest a few subdirs the user could fill in first, based on what's
    // missing. Order: health → work → money → people → notes (most common
    // first for a personal-archive use case).
    onboardingSubdirs() {
      const sp = this.contextInfo.subdir_present || {};
      const hints = {
        health: this.t("onboard.dir_health"),
        work:   this.t("onboard.dir_work"),
        money:  this.t("onboard.dir_money"),
        people: this.t("onboard.dir_people"),
        notes:  this.t("onboard.dir_notes"),
      };
      return ["health", "work", "money", "people", "notes"]
        .filter(k => sp[k])     // only show subdirs that actually exist
        .map(k => ({ name: k, hint: hints[k] }));
    },

    // Suggested first questions when the user has set things up but hasn't
    // chatted yet. Tailored a bit to what data they've dropped in.
    // Skill chips for the onboarding card — give a short, friendly example
    // prompt that triggers each known skill (matches the 7 presets in skills/).
    SKILL_TRIGGERS: [
      { name: "web-search",         label_zh: "查时效数据",  label_en: "live web fact",     prompt_zh: "查一下今天 USD 兑 CNY 的汇率，给出处" },
      { name: "markdown-formatter", label_zh: "整理 markdown", label_en: "clean markdown",   prompt_zh: "帮我把这段 markdown 整理一下" },
      { name: "mermaid-helper",     label_zh: "画架构图",    label_en: "draw a diagram",   prompt_zh: "画一张三栏 web 应用的架构图" },
      { name: "code-reviewer",      label_zh: "code review", label_en: "code review",      prompt_zh: "帮我 review 这段代码：" },
      { name: "citation-formatter", label_zh: "格式化引用",  label_en: "format a citation", prompt_zh: "把 DOI 10.1038/nature12345 格式化为 APA" },
      { name: "task-decomposer",    label_zh: "拆任务",      label_en: "decompose a goal", prompt_zh: "帮我把「6 个月内换工作」拆成可执行任务" },
      { name: "summary-distiller",  label_zh: "长文摘要",    label_en: "summarize",         prompt_zh: "总结这篇文章的要点" },
    ],

    skillSuggestions() {
      const loaded = new Set(this.settings.skills.map(s => s.name));
      const lang = this.lang;
      return this.SKILL_TRIGGERS
        .filter(t => loaded.has(t.name))
        .map(t => ({
          name: t.name,
          label: lang === "zh" ? t.label_zh : t.label_en,
          prompt: t.prompt_zh,   // prompt always Chinese (Muse responds in user's lang)
          description: lang === "zh" ? "触发 skill: " + t.name : "Triggers skill: " + t.name,
        }))
        .slice(0, 6);
    },

    onboardingPrompts() {
      const sp = this.contextInfo.subdir_present || {};
      const ps = [];
      if (sp.health) ps.push(this.t("onboard.q_health"));
      if (sp.work)   ps.push(this.t("onboard.q_work"));
      if (sp.money)  ps.push(this.t("onboard.q_money"));
      if (sp.people) ps.push(this.t("onboard.q_people"));
      ps.push(this.t("onboard.q_overview"));
      return ps.slice(0, 4);
    },

    async quickNewNote() {
      const name = await this.prompt({
        title: this.t("preview.new_note_title"),
        body: this.t("preview.new_note_body"),
        value: "untitled.md",
      });
      if (!name) return;
      const trimmed = name.trim();
      if (!trimmed) return;
      // Create empty file at archive root
      const r = await fetch("/api/files/write", {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ path: trimmed, content: "# " + trimmed.replace(/\.md$/, "") + "\n\n" }),
      });
      if (!r.ok) { this.toast(this.t("slash.failed"), "error"); return; }
      await this.loadRoot();
      await this.openFile({ path: trimmed, name: trimmed });
      this.editing = true;
      this.toast(this.t("toast.saved"), "success", 1200);
    },

    useSuggestedPrompt(q) {
      this.input = q;
      if (this.$refs.chatInput) {
        this.$refs.chatInput.focus();
        this.autoGrow(this.$refs.chatInput);
      }
    },

    claudeMdChipTitle() {
      const i = this.contextInfo;
      if (!i.claude_md_exists) {
        return this.t("ctx.no_claude_md", { root: i.archive_root });
      }
      const d = i.claude_md_mtime ? new Date(i.claude_md_mtime * 1000).toLocaleDateString() : "";
      return this.t("ctx.claude_md_tip", { root: i.archive_root, date: d });
    },
    openClaudeMdHelp() {
      this.modal = {
        show: true,
        title: this.t("ctx.no_claude_md_title"),
        body: this.t("ctx.no_claude_md_body", { root: this.contextInfo.archive_root }),
        input: null, danger: false,
        okText: this.t("btn.confirm"),
        confirm: () => { this.modal.show = false; },
        cancel: () => { this.modal.show = false; },
      };
    },

    // Pretty-print a USD amount for the cost badge.
    //   0          → "$0"
    //   0.0023     → "0.23¢"   (cents form for sub-dollar)
    //   0.45       → "45¢"
    //   1.234      → "$1.23"
    //   12.34      → "$12.34"
    fmtCost(usd) {
      if (!usd || usd < 0) return "$0";
      if (usd < 0.01) {
        const c = usd * 100;
        return (c < 0.1 ? c.toFixed(2) : c.toFixed(1)) + "¢";
      }
      if (usd < 1) return Math.round(usd * 100) + "¢";
      return "$" + usd.toFixed(2);
    },

    // Header badge: show accumulated input/output tokens instead of $.
    // 1.2K / 350 format — concise, intuitive (in / out). Use M for ≥1M.
    fmtTokens(n) {
      n = n || 0;
      if (n < 1000) return n.toString();
      if (n < 1_000_000) return (n / 1000).toFixed(n < 10_000 ? 1 : 0) + "K";
      return (n / 1_000_000).toFixed(2) + "M";
    },
    tokenBadgeText() {
      const s = this.stats;
      return `↓ ${this.fmtTokens(s.total_input_tokens)} · ↑ ${this.fmtTokens(s.total_output_tokens)}`;
    },

    costBadgeTitle() {
      const s = this.stats;
      const parts = [
        `${s.total_input_tokens.toLocaleString()} in / ${s.total_output_tokens.toLocaleString()} out  ·  ${s.total_messages} msg`,
        `cache hit ${s.cache_hit_pct}%  ·  cache_read ${s.total_cache_read_tokens.toLocaleString()}`,
        `${this.t("cost.total")}: $${s.total_cost_usd.toFixed(4)}`,
      ];
      if (s.budget_usd > 0) parts.push(`${this.t("cost.budget")} ${s.budget_used_pct}% of $${s.budget_usd}`);
      return parts.join("\n");
    },
    ctxMeterTitle() {
      const u = this.sessionUsage;
      return `${this.t("ctx.tip_line1")}\n` +
             `${u.input_tokens.toLocaleString()} / ${u.context_limit.toLocaleString()} tokens (${u.context_used_pct}%)\n\n` +
             `${this.t("ctx.tip_line2")}`;
    },
    ctxMeterLabel() {
      const pct = this.sessionUsage.context_used_pct;
      // Real context = input + cache_read + cache_creation. Backend sends
      // pre-computed `context_used`; fall back to summing if absent.
      const usedTokens = (this.sessionUsage.context_used != null)
        ? this.sessionUsage.context_used
        : (this.sessionUsage.input_tokens || 0)
          + (this.sessionUsage.cache_read_tokens || 0)
          + (this.sessionUsage.cache_creation_tokens || 0);
      const cachedTokens = (this.sessionUsage.cache_read_tokens || 0)
                         + (this.sessionUsage.cache_creation_tokens || 0);
      const usedK = (usedTokens / 1000).toFixed(1);
      const cachedK = (cachedTokens / 1000).toFixed(1);
      const limitK = (this.sessionUsage.context_limit / 1000).toFixed(0);
      const args = { used: usedK, limit: limitK, pct, cached: cachedK };
      if (pct >= 90) return this.t("ctx.danger", args);
      if (pct >= 70) return this.t("ctx.warn",   args);
      return this.t("ctx.normal", args);
    },
    // Real compact: a) make sure the OLD session has been summarized in chat,
    // b) fork it, c) the fork inherits the summary as starting context.
    // Easier path: just send a /compact instruction to the CURRENT session that
    // asks the model to produce a self-contained summary, which the user can
    // copy / use as basis. The "true" compact is a feature of the underlying
    // CLI we don't have direct API for, so we implement it as a synthesized
    // summarize-and-fork workflow.
    async runCompact() {
      if (!this.currentId) return;
      // Guard: don't run while a turn is already in flight. send() would be a
      // no-op (input never sent), then the wait-loop would sample the OLD
      // turn's last assistant message — and we'd fork using the wrong content.
      if (this.streaming) {
        this.toast(this.t("ctx.compact_wait_streaming"), "warn", 2500);
        return;
      }
      // And: need at least one assistant message to compact (an empty session
      // has nothing to summarize).
      const hasContent = this.messages.some(m => m.role === "assistant" && m.text);
      if (!hasContent) {
        this.toast(this.t("ctx.compact_empty"), "warn", 2500);
        return;
      }

      const ok = await this.confirm({
        title: this.t("ctx.compact_confirm_title"),
        body: this.t("ctx.compact_confirm_body"),
        okText: this.t("ctx.compact_btn"),
      });
      if (!ok) return;

      // Snapshot the latest assistant message BEFORE we send the summary
      // prompt — we'll diff against it to grab the NEW (summary) reply only.
      const preCount = this.messages.filter(m => m.role === "assistant" && m.text).length;

      // Step 1: tell the CURRENT session to produce a self-contained summary.
      this.input = this.t("ctx.compact_summarize_prompt");
      this.toast(this.t("ctx.compact_step1"), "info", 2500);
      await this.send();
      // Wait for streaming to actually end (with a 60s timeout — Opus on a
      // long history can take 30s+).
      await new Promise((resolve, reject) => {
        const started = Date.now();
        const check = () => {
          if (!this.streaming) return resolve();
          if (Date.now() - started > 60_000) return reject(new Error("compact timeout"));
          setTimeout(check, 200);
        };
        check();
      }).catch(e => {
        this.toast(this.t("ctx.compact_timeout"), "error", 5000);
        throw e;
      });

      // Step 2: grab the NEW assistant message (the summary). Use the count
      // delta to ensure we don't accidentally read an old one if streaming
      // returned 0 content.
      const asstMsgs = this.messages.filter(m => m.role === "assistant" && m.text);
      if (asstMsgs.length <= preCount) {
        this.toast(this.t("ctx.compact_no_reply"), "error", 4000);
        return;
      }
      const summary = asstMsgs[asstMsgs.length - 1].text;

      const r = await fetch(`/api/chat/sessions/${this.currentId}/compact`,
                              { method: "POST", headers: this.hdr() });
      if (!r.ok) { this.toast(this.t("slash.failed"), "error"); return; }
      const meta = await r.json();
      // Seed the new session with the summary as a persisted user message so
      // subsequent turns have it as established context.
      await fetch(`/api/chat/sessions/${meta.id}/seed`, {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ summary }),
      });
      await this.refreshSessions();
      this.currentId = meta.id;
      await this.loadSession(meta.id);
      this.toast(this.t("ctx.compact_done"), "success", 3000);
    },

    onChatInput(ev) {
      const ta = ev.target;
      const pos = ta.selectionStart;
      const text = this.input.slice(0, pos);

      // Slash command palette — only when input starts with '/' (no leading space).
      if (text.startsWith("/")) {
        const q = text.slice(1).toLowerCase();
        // Hide once user typed a space (means they're past the command name)
        if (/\s/.test(q)) { this.slashShow = false; }
        else {
          this.slashResults = this.SLASH_CMDS.filter(c => c.name.startsWith(q));
          this.slashIdx = 0;
          this.slashShow = this.slashResults.length > 0;
          this.slashAnchor = 0;
        }
        this.mentionShow = false;
        return;
      } else {
        this.slashShow = false;
      }

      const at = text.lastIndexOf("@");
      if (at < 0 || (at > 0 && /\S/.test(text[at - 1]))) { this.mentionShow = false; return; }
      const query = text.slice(at + 1);
      if (/\s/.test(query)) { this.mentionShow = false; return; }
      this.mentionAnchor = at;
      this.fetchMention(query);
    },
    async fetchMention(q) {
      if (q.length === 0) {
        this.mentionResults = (await this.fetchChildren("")).slice(0, 8);
      } else {
        const r = await fetch("/api/files/search?q=" + encodeURIComponent(q) + "&limit=15", { headers: this.hdr() });
        const d = r.ok ? await r.json() : { entries: [] };
        this.mentionResults = d.entries.filter(e => !e.is_dir).slice(0, 12);
      }
      this.mentionIdx = 0;
      this.mentionShow = true;
    },
    pickMention(i) {
      const idx = (i ?? this.mentionIdx);
      const item = this.mentionResults[idx];
      if (!item) return;
      const ta = this.$refs.chatInput;
      const before = this.input.slice(0, this.mentionAnchor);
      const after = this.input.slice(ta.selectionStart);
      this.input = before + "@" + item.path + " " + after;
      this.mentionShow = false;
      this.$nextTick(() => {
        const newPos = (before + "@" + item.path + " ").length;
        ta.setSelectionRange(newPos, newPos);
        ta.focus();
      });
    },

    // ===== chat =====
    onEnter(ev) {
      // 中文 / 日文 输入法在选词阶段也会触发 Enter (keyCode=229 / isComposing=true)。
      // 那时不应该当成"发送"，让 IME 自己处理。
      if (ev.isComposing || ev.keyCode === 229) return;
      if (this.mentionShow) { this.pickMention(); return; }
      if (ev.shiftKey) { this.input += "\n"; return; }
      this.send();
    },
    onChatScroll() {
      const el = this.$refs.chatBody;
      if (!el) return;
      this.atBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 80;
    },
    scrollToBottom(force) {
      this.$nextTick(() => {
        const el = this.$refs.chatBody;
        if (!el) return;
        if (force || this.atBottom) {
          el.scrollTop = el.scrollHeight;
          this.atBottom = true;
        }
      });
    },

    async send() {
      const text = this.input.trim();
      // Slash command: intercept BEFORE hitting the SDK. /word or /word arg
      if (text.startsWith("/") && !this.streaming) {
        const m = text.match(/^\/(\w+)(?:\s+(.*))?$/);
        if (m) {
          this.input = "";
          this.$nextTick(() => { if (this.$refs.chatInput) this.autoGrow(this.$refs.chatInput); });
          await this._runSlash(m[1], m[2] || "");
          return;
        }
      }
      const readyImages = this.pendingImages.filter(im => im.id && !im.error);
      const readyDocs = this.pendingDocs.filter(d => d.id && !d.error);
      if ((!text && !readyImages.length && !readyDocs.length)
          || this.streaming || !this.currentId) return;
      // If anything still uploading, wait.
      if (this.pendingImages.some(im => im.uploading)
          || this.pendingDocs.some(d => d.uploading)) {
        this.toast(this.t("img.wait_upload"), "warn", 2000);
        return;
      }
      this.messages.push({
        role: "user", text,
        images: readyImages.map(im => ({ preview: im.preview })),
        docs: readyDocs.map(d => ({ name: d.name, kind: d.kind })),
      });
      // Single id-list for both kinds — backend dispatches by stored kind.
      const attachIds = [
        ...readyImages.map(im => im.id),
        ...readyDocs.map(d => d.id),
      ];
      this.pendingImages = [];
      this.pendingDocs = [];
      this.input = "";
      // 发送后 textarea 重置高度
      this.$nextTick(() => { if (this.$refs.chatInput) this.autoGrow(this.$refs.chatInput); });
      this.mentionShow = false;
      this.streaming = true;
      this.streamingModel = this.model;   // 锁定 — pending bubble 用它，不跟着 dropdown
      // Tick elapsed time so user sees "thinking · 3.2s" when first token is slow.
      this._streamStartedAt = Date.now();
      this.streamElapsed = 0;
      if (this._streamTimer) clearInterval(this._streamTimer);
      this._streamTimer = setInterval(() => {
        this.streamElapsed = (Date.now() - this._streamStartedAt) / 1000;
      }, 200);
      this.atBottom = true;
      this.scrollToBottom(true);

      const url = "/api/chat/stream"
        + "?prompt=" + encodeURIComponent(text)
        + "&session_id=" + encodeURIComponent(this.currentId)
        + "&model=" + encodeURIComponent(this.model)
        + "&permission=" + encodeURIComponent(this.permission)
        + "&show_thinking=" + (this.showThinking ? "true" : "false")
        + (attachIds.length ? "&image_ids=" + encodeURIComponent(attachIds.join(",")) : "")
        + "&token=" + encodeURIComponent(this.token);
      const es = new EventSource(url);
      this.es = es;

      // Active assistant bubble pointer (-1 = none). Text events open / extend
      // it; tool/thinking events close it so subsequent text starts a fresh
      // bubble — preserves the actual event order visually.
      let curIdx = -1;
      let acc = "";
      const modelForBubble = this.model;   // 锁定本次消息用的 model（避免中途切换造成 badge 错位）
      const openAsst = () => {
        if (curIdx !== -1) return;
        this.messages.push({ role: "assistant", text: "", html: "", cost: "", model: modelForBubble });
        curIdx = this.messages.length - 1;
        acc = "";
      };
      const closeAsst = () => { curIdx = -1; acc = ""; };

      es.addEventListener("text", ev => {
        const d = JSON.parse(ev.data);
        openAsst();
        acc += d.text;
        this.messages[curIdx].text = acc;
        this.messages[curIdx].html = this.mdRender(acc);
        this.scrollToBottom(false);
      });
      es.addEventListener("thinking", ev => {
        if (!this.showThinking) return;
        closeAsst();
        const d = JSON.parse(ev.data);
        this.messages.push({ role: "thinking", text: d.text });
        this.scrollToBottom(false);
      });
      es.addEventListener("tool_use", ev => {
        closeAsst();
        const d = JSON.parse(ev.data);
        const msg = { role: "tool_use", name: d.name, summary: d.summary };
        // Carry through structured payloads for dedicated UIs
        if (d.todos != null) msg.todos = d.todos;
        if (d.task != null) msg.task = d.task;
        if (d.plan != null) msg.plan = d.plan;
        this.messages.push(msg);
        this.scrollToBottom(false);
      });
      es.addEventListener("tool_result", ev => {
        const d = JSON.parse(ev.data);
        this.messages.push({ role: "tool_result", preview: d.preview, truncated: d.truncated, is_error: d.is_error });
        this.scrollToBottom(false);
      });
      // ask_user_question: Muse 用 mcp__muselab__ask_user_question 工具问选项。
      // 在 chat 里插入特殊 bubble，每个 option 一个按钮。点击 → POST 答案 → 工具
      // handler 解 future → 模型继续。pendingQId / pendingAnswers 用于 multiSelect 收集中间态。
      es.addEventListener("ask_user_question", ev => {
        closeAsst();
        const d = JSON.parse(ev.data);
        this.messages.push({
          role: "ask_user_question",
          id: d.id,
          questions: d.questions,
          pendingAnswers: {},   // multi-select intermediate; single click submits immediately
          submitted: false,
        });
        this.scrollToBottom(false);
      });
      // permission_request: muselab's can_use_tool bridge. Shows Allow / Deny /
      // Always-allow buttons; click POSTs decision, backend resolves the SDK callback.
      es.addEventListener("permission_request", ev => {
        closeAsst();
        const d = JSON.parse(ev.data);
        this.messages.push({
          role: "permission_request",
          id: d.id,
          tool: d.tool,
          summary: d.summary,
          resolved: false,
          decision: null,
        });
        this.scrollToBottom(false);
      });
      const _stopTimer = () => {
        if (this._streamTimer) { clearInterval(this._streamTimer); this._streamTimer = null; }
        this.streamElapsed = 0;
      };
      es.addEventListener("done", ev => {
        const d = JSON.parse(ev.data);
        if (d.total_cost_usd != null && curIdx !== -1) {
          this.messages[curIdx].cost = "$" + d.total_cost_usd.toFixed(4);
        }
        if (d.stats) this.stats = { ...this.stats, ...d.stats };
        if (d.session_usage) this.sessionUsage = d.session_usage;
        // Budget warn on every turn that crosses the threshold (don't spam if already over)
        if (d.budget_usd > 0 && d.budget_used_pct >= 90 && !this._budgetWarned) {
          this._budgetWarned = true;
          this.toast(this.t("cost.budget_warn", { pct: d.budget_used_pct, usd: d.budget_usd }),
                      "warn", 5000);
        }
        es.close(); this.streaming = false; this.es = null; _stopTimer();
        this.refreshSessions();
        this.$nextTick(() => this.highlightCode(".chat-body"));
      });
      es.addEventListener("error", ev => {
        try {
          const d = JSON.parse(ev.data);
          this.toast("Claude 出错：" + d.error, "error", 6000);
        } catch { this.toast("流式连接失败", "error"); }
        es.close(); this.streaming = false; this.es = null; _stopTimer();
      });
      es.addEventListener("cancelled", () => {
        this.toast("已中断", "warn", 2000);
        es.close(); this.streaming = false; this.es = null; _stopTimer();
      });
      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) { this.streaming = false; this.es = null; _stopTimer(); }
      };
    },
    stop() {
      if (this.es) { this.es.close(); this.es = null; }
      this.streaming = false;
      fetch("/api/chat/reset?token=" + encodeURIComponent(this.token) + "&session_id=" + encodeURIComponent(this.currentId),
            { method: "POST" });
    },

    // ====== ask_user_question UI helpers ======
    // Single-select: user clicks an option → submit immediately.
    pickAskOption(msg, qIdx, optionLabel) {
      if (msg.submitted) return;
      const q = msg.questions[qIdx];
      msg.pendingAnswers[q.question] = optionLabel;
      // If single-select AND all questions answered → submit
      if (!q.multiSelect && this._allAskQuestionsAnswered(msg)) {
        this.submitAskAnswers(msg);
      }
    },
    // Multi-select: user toggles a checkbox; submitted via the "提交" button.
    toggleAskOption(msg, qIdx, optionLabel) {
      if (msg.submitted) return;
      const q = msg.questions[qIdx];
      const key = q.question;
      const cur = msg.pendingAnswers[key];
      const arr = Array.isArray(cur) ? cur.slice() : [];
      const i = arr.indexOf(optionLabel);
      if (i >= 0) arr.splice(i, 1); else arr.push(optionLabel);
      msg.pendingAnswers[key] = arr;
    },
    isAskOptionPicked(msg, qIdx, optionLabel) {
      const q = msg.questions[qIdx];
      const cur = msg.pendingAnswers[q.question];
      if (q.multiSelect) return Array.isArray(cur) && cur.includes(optionLabel);
      return cur === optionLabel;
    },
    _allAskQuestionsAnswered(msg) {
      return msg.questions.every(q => {
        const v = msg.pendingAnswers[q.question];
        if (q.multiSelect) return Array.isArray(v) && v.length > 0;
        return v != null;
      });
    },
    async submitAskAnswers(msg) {
      if (msg.submitted) return;
      if (!this._allAskQuestionsAnswered(msg)) {
        this.toast(this.t("ask.unanswered"), "warn", 2000);
        return;
      }
      msg.submitted = true;
      try {
        const r = await fetch(
          `/api/chat/answer/${encodeURIComponent(this.currentId)}/${encodeURIComponent(msg.id)}`,
          {
            method: "POST",
            headers: { ...this.hdr(), "Content-Type": "application/json" },
            body: JSON.stringify({ answers: msg.pendingAnswers }),
          },
        );
        if (!r.ok) {
          msg.submitted = false;
          this.toast(this.t("ask.submit_failed"), "error", 3000);
        }
      } catch (e) {
        msg.submitted = false;
        this.toast(this.t("ask.submit_failed"), "error", 3000);
      }
    },
    // ====== permission_request helpers ======
    async decidePermission(msg, decision) {
      if (msg.resolved) return;
      msg.resolved = true;
      msg.decision = decision;
      try {
        const r = await fetch(
          `/api/chat/permission/${encodeURIComponent(this.currentId)}/${encodeURIComponent(msg.id)}`,
          {
            method: "POST",
            headers: { ...this.hdr(), "Content-Type": "application/json" },
            body: JSON.stringify({ decision }),
          },
        );
        if (!r.ok) {
          msg.resolved = false;
          msg.decision = null;
          this.toast(this.t("perm.submit_failed"), "error", 3000);
        }
      } catch (e) {
        msg.resolved = false;
        msg.decision = null;
        this.toast(this.t("perm.submit_failed"), "error", 3000);
      }
    },

    copyMsg(m) {
      const text = m.text || "";
      navigator.clipboard?.writeText(text).then(
        () => this.toast(this.t("toast.copied"), "success", 1500),
        () => this.toast("复制失败（需要 HTTPS）", "error")
      );
    },

    escape(s) {
      return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    },
  };
}
