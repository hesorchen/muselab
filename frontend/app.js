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
    // login
    "login.sub": "Meet Muse — an AI that actually knows you.",
    "login.token_placeholder": "MUSELAB_TOKEN",
    "login.go": "进入",
    "login.err": "Token 错误",
    // settings modal
    "set.title": "设置",
    "set.sec.provider": "Provider API Keys",
    "set.sec.appearance": "外观",
    "set.sec.defaults": "新会话默认",
    "set.sec.model_params": "模型参数",
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
    "login.sub": "Meet Muse — an AI that actually knows you.",
    "login.token_placeholder": "MUSELAB_TOKEN",
    "login.go": "Enter",
    "login.err": "Bad token",
    "set.title": "Settings",
    "set.sec.provider": "Provider API keys",
    "set.sec.appearance": "Appearance",
    "set.sec.defaults": "New-session defaults",
    "set.sec.model_params": "Model parameters",
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
    stats: { total_cost_usd: 0, total_messages: 0, total_input_tokens: 0, total_output_tokens: 0 },
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
      // 模型切换给视觉反馈（避免用户不确定是否切了）
      this.$watch("model", (newM, oldM) => {
        if (!oldM || newM === oldM) return;
        // 防 Alpine / 浏览器 select 重复触发：1.5s 内对同一目标 model 只 toast 一次。
        if (this._lastModelToastFor === newM) return;
        this._lastModelToastFor = newM;
        setTimeout(() => {
          if (this._lastModelToastFor === newM) this._lastModelToastFor = null;
        }, 1500);
        const meta = this.availableModels.find(m => m.model === newM);
        const label = meta ? `${meta.group} · ${meta.label}` : newM;
        this.toast(this.t("toast.model_switched", { label }), "info", 2500);
        this.savePrefs();
      });
      this._lastModelToastFor = null;
      const t = localStorage.getItem("muselab_token");
      if (t) {
        this.token = t; this.authed = true;
        this.loadPrefs();
        this.loadRoot();
        this.initSessions();
        this.fetchStats();
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
    mdRender(text) {
      if (!text) return "";
      const raw = window.marked ? window.marked.parse(text) : text;
      if (!window.DOMPurify) return raw;
      return window.DOMPurify.sanitize(raw, {
        USE_PROFILES: { html: true },
        FORBID_TAGS: ["style", "iframe", "form", "object", "embed"],
        FORBID_ATTR: ["style", "formaction"],
      });
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
        DEEPSEEK_API_KEY: "deepseek_api_key",
        ZHIPUAI_API_KEY: "zhipuai_api_key",
        MINIMAX_API_KEY: "minimax_api_key",
        MOONSHOT_API_KEY: "moonshot_api_key",
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
    },
    autoGrow(ta) {
      // 撑高到内容 + 上限（避免无限增长把 chat 区挤没）
      ta.style.height = "auto";
      const max = 240;   // px
      ta.style.height = Math.min(ta.scrollHeight, max) + "px";
    },

    onChatInput(ev) {
      const ta = ev.target;
      const pos = ta.selectionStart;
      const text = this.input.slice(0, pos);
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
      if (!text || this.streaming || !this.currentId) return;
      this.messages.push({ role: "user", text });
      this.input = "";
      // 发送后 textarea 重置高度
      this.$nextTick(() => { if (this.$refs.chatInput) this.autoGrow(this.$refs.chatInput); });
      this.mentionShow = false;
      this.streaming = true;
      this.atBottom = true;
      this.scrollToBottom(true);

      const url = "/api/chat/stream"
        + "?prompt=" + encodeURIComponent(text)
        + "&session_id=" + encodeURIComponent(this.currentId)
        + "&model=" + encodeURIComponent(this.model)
        + "&permission=" + encodeURIComponent(this.permission)
        + "&show_thinking=" + (this.showThinking ? "true" : "false")
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
        this.messages.push({ role: "tool_use", name: d.name, summary: d.summary });
        this.scrollToBottom(false);
      });
      es.addEventListener("tool_result", ev => {
        const d = JSON.parse(ev.data);
        this.messages.push({ role: "tool_result", preview: d.preview, truncated: d.truncated, is_error: d.is_error });
        this.scrollToBottom(false);
      });
      es.addEventListener("done", ev => {
        const d = JSON.parse(ev.data);
        if (d.total_cost_usd != null && curIdx !== -1) {
          this.messages[curIdx].cost = "$" + d.total_cost_usd.toFixed(4);
        }
        if (d.stats) this.stats = d.stats;
        es.close(); this.streaming = false; this.es = null;
        this.refreshSessions();
        this.$nextTick(() => this.highlightCode(".chat-body"));
      });
      es.addEventListener("error", ev => {
        try {
          const d = JSON.parse(ev.data);
          this.toast("Claude 出错：" + d.error, "error", 6000);
        } catch { this.toast("流式连接失败", "error"); }
        es.close(); this.streaming = false; this.es = null;
      });
      es.addEventListener("cancelled", () => {
        this.toast("已中断", "warn", 2000);
        es.close(); this.streaming = false; this.es = null;
      });
      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) { this.streaming = false; this.es = null; }
      };
    },
    stop() {
      if (this.es) { this.es.close(); this.es = null; }
      this.streaming = false;
      fetch("/api/chat/reset?token=" + encodeURIComponent(this.token) + "&session_id=" + encodeURIComponent(this.currentId),
            { method: "POST" });
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
