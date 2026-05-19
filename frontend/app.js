// ==========================================================================
// i18n — dictionary is loaded by /static/i18n/index.js (plain <script> tag in
// index.html, before app.js). Kept out of this file so the file stays focused
// on app logic and editing translations doesn't require diffing 470 lines.
// Falls back to an empty dict if the i18n script failed to load — t() then
// returns the key, making the breakage visible instead of silently broken.
// ==========================================================================
const STRINGS = (typeof window !== "undefined" && window.MUSELAB_STRINGS)
                  || { zh: {}, en: {} };


// Static UI data lives in /static/data/constants.js (loaded by index.html
// before this file). These aliases keep the existing references in this file
// working without code changes elsewhere.
const ACCENT_PRESETS = window.MUSELAB_ACCENT_PRESETS || [];
const EDITABLE_EXT = window.MUSELAB_EDITABLE_EXT || new Set();

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
    // Drag-and-drop visual state for the preview pane.
    previewDragHover: false,
    // Right-click context menu on a preview tab. { path, x, y } when open.
    previewTabCtxMenu: null,
    // Image lightbox (click chat-bubble image to enlarge).
    lightbox: { show: false, src: "" },
    // Whether the "Open files" sidebar section is collapsed. Persists to
    // localStorage via savePrefs/loadPrefs so the user's preference sticks.
    openFilesCollapsed: false,
    // User-override height (px). null = auto: 1 file → 1 row, 2 → 2, ...,
    // capped at 5 rows + header. Once the user drags the splitter we set a
    // concrete pixel value and stop auto-fitting.
    openFilesHeight: null,
    previewMode: "", rawText: "", renderedMd: "", previewLang: "plaintext",
    // xlsx preview state. previewMode==='xlsx' uses xlsxSheets (array of
    // {name, rows, rows_truncated, cols_truncated}). xlsxActive picks the
    // sheet tab. xlsxLimits carries the server's row/col caps for the UI
    // hint when truncation happens.
    xlsxSheets: [], xlsxActive: "", xlsxLimits: null, xlsxSheetsTruncated: false,
    // Bumped whenever an assistant tool_use edits a file. Used as a cache
    // buster on iframe / read URLs so the preview reflects the new content
    // without the user needing to manually refresh the page.
    previewVersion: 0,
    // Compact orchestration: _compacting marks the window where the CLI is
    // busy summarising history; user messages typed during that window queue
    // up and dispatch when compact finishes.
    _compacting: false,
    _compactQueue: [],
    // SDK get_context_usage() breakdown popup. Shows per-category token
    // counts (system prompt / tools / memory files / messages / mcp / skills)
    // so the user can see which slice is using their context window.
    ctxBreakdown: { show: false, loading: false, data: null, error: "" },
    editing: false, editText: "",
    cmStatus: { line: 1, col: 1, sel: 0, lines: 0, chars: 0, mode: "plaintext", dirty: false },
    tabs: [],   // open file tabs: [{path, name}]

    // ===== chat =====
    sessions: [], currentId: "",
    // True while loadSession(currentId) is in flight. UI uses this to swap
    // the brand-empty placeholder for a shimmer skeleton, so users don't
    // see "Muse · Calliope / empty chat" for the second a big session
    // takes to fetch.
    messagesLoading: false,
    // Tab strip — VS Code-Claude style. `openTabIds` is the visible order; each
    // entry is a session id (also present in `sessions`). currentId is the
    // active tab. Tabs can be opened from the session picker, closed via × on
    // the tab, or created by the "+ new" button.
    openTabIds: [],
    renamingTabId: "",   // session id whose name is currently being inline-edited
    renameDraft: "",     // current value of the inline rename <input>
    tabCtxMenu: null,    // {id, x, y} for the right-click tab menu, or null
                          // (kept separate from the file-tree's `ctxMenu` which
                          //  has a different shape — overlapping names crash
                          //  Alpine when one side reads .show on the other's null)
    // Per-tab runtime state. Keyed by session id. Each entry is a "snapshot"
    // of {messages, sessionUsage, streaming, es, streamingModel, ...} that the
    // active tab mirrors into root state (this.messages, this.es, etc.) via
    // _activateTabState(). Background tabs' stream callbacks write to their
    // own tabState[sid] so switching away doesn't lose / mis-route events.
    // The active tab's `this.messages` and `tabState[currentId].messages` are
    // the SAME array reference — we mutate in place, never replace.
    tabState: {},
    messages: [],
    model: "claude-sonnet-4-6",
    permission: "bypassPermissions",
    // Always render thinking blocks. Toggle removed 2026-05-19 — adaptive
    // thinking was causing invisible mid-reply stalls when hidden; now we
    // always enable thinking on the backend AND always display it.
    showThinking: true,
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
      has_anthropic_api: false,
      third_party_configured: [],
      has_any_provider: false,
      // Guard so onboarding chips ("⚠ 未配档案" / "no provider") don't
      // flash to the user while the first contextInfo fetch is still in
      // flight. UI conditions check `_fetched && !X` rather than `!X`.
      _fetched: false,
    },

    // ===== slash commands =====
    slashShow: false,
    slashIdx: 0,
    slashAnchor: -1,      // input position where the leading '/' is
    // Defined in /static/data/constants.js (window.MUSELAB_SLASH_CMDS). Kept
    // as a component property so `this.SLASH_CMDS` references throughout the
    // file keep working without changes.
    SLASH_CMDS: window.MUSELAB_SLASH_CMDS || [],
    // Per-session context meter snapshot, updated on every SSE `done` event
    sessionUsage: { input_tokens: 0, output_tokens: 0,
                     cache_read_tokens: 0, cache_creation_tokens: 0,
                     context_limit: 0, context_used: 0, context_used_pct: 0 },
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
      draftDefaults: { model: "", permission: "" },
      draftParams: { thinking_budget: 4000, max_turns: 0 },
      // MCP server list (loaded from /api/settings/mcp)
      mcpServers: [],
      mcpExamples: [],
      mcpDraft: { show: false, name: "", command: "", argsStr: "" },
      skills: [],   // discovered skill list (read-only browse)
      probeResults: {},   // env_key -> {ok, text} from last "Test" click
      // Versions + upgrade — populated by loadVersions(), set by runUpgrade()
      versions: null,
      versionsLoading: false,
      upgradeRunning: false,
      upgradeResult: null,
    },

    // Session picker dropdown open state (replaces native <select> so each
    // row can have an inline delete button).
    sessionPickerOpen: false,

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
      // Chat-tab keybindings (Ctrl/Cmd as the modifier; Mac users get Cmd):
      //   Ctrl+T          new chat tab
      //   Ctrl+W          close current chat tab
      //   Ctrl+Tab        next tab    (Shift+Tab = previous)
      //   Ctrl+1..9       jump to Nth tab
      // We hijack Ctrl+T and Ctrl+W from the browser. The user is inside a
      // single-page web app — we own these. (Mobile Safari ignores them.)
      if ((ev.ctrlKey || ev.metaKey) && !ev.altKey) {
        if (ev.key === "t" || ev.key === "T") {
          ev.preventDefault();
          this.newSession();
          return;
        }
        if (ev.key === "w" || ev.key === "W") {
          if (this.currentId) {
            ev.preventDefault();
            this.closeChatTab(this.currentId);
          }
          return;
        }
        if (ev.key === "Tab") {
          if (!this.openTabIds.length) return;
          ev.preventDefault();
          const cur = Math.max(0, this.openTabIds.indexOf(this.currentId));
          const next = ev.shiftKey
            ? (cur - 1 + this.openTabIds.length) % this.openTabIds.length
            : (cur + 1) % this.openTabIds.length;
          this.activateTab(this.openTabIds[next]);
          return;
        }
        // Ctrl+1..9
        if (/^[1-9]$/.test(ev.key)) {
          const i = parseInt(ev.key, 10) - 1;
          if (i < this.openTabIds.length) {
            ev.preventDefault();
            this.activateTab(this.openTabIds[i]);
          }
          return;
        }
      }
      if (ev.key === "Escape") {
        if (this.mentionShow) { this.mentionShow = false; return; }
        if (this.ctxMenu.show) { this.ctxMenu.show = false; return; }
        if (this.tabCtxMenu) { this.closeTabMenu(); return; }
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
      this._initMobileKeyboardWatch();
      // Global error capture — when alpine's "Cannot read properties of
      // undefined (reading 'after')" fires we want the FULL story (msg,
      // file, line, stack) printed in one block so the user can copy it
      // in a single paste. The minified alpine stack is useless on its
      // own; pair this with the dev (unminified) bundle for real names.
      window.addEventListener("error", (ev) => {
        if (!ev || !ev.error) return;
        const msg = ev.error.message || String(ev.error);
        if (!msg.includes("after")) return;
        console.group("%c[muselab DEBUG] alpine .after error", "color: #f87171; font-weight: bold");
        console.error("message:", msg);
        console.error("file:", ev.filename, "line:", ev.lineno, "col:", ev.colno);
        console.error("stack:", ev.error.stack);
        console.error("currentId:", this.currentId,
                      "messages.length:", (this.messages || []).length,
                      "sessions.length:", (this.sessions || []).length,
                      "previewTabCtxMenu:", JSON.stringify(this.previewTabCtxMenu),
                      "ctxBreakdown:", JSON.stringify(this.ctxBreakdown));
        console.groupEnd();
      });
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

    // Mobile keyboard handling. iOS Safari (and some Android browsers) overlay
    // the virtual keyboard ABOVE the layout instead of resizing it — without
    // intervention, the chat-input + bottom tab bar end up hidden behind the
    // keyboard and the user can't tap "send". We watch visualViewport for
    // height changes and (a) flag the body so CSS can hide the bottom tab
    // bar, (b) expose --kb-inset so the chat-input can lift above the
    // keyboard. The viewport meta `interactive-widget=resizes-content` does
    // this natively on modern browsers; this is the fallback path.
    _initMobileKeyboardWatch() {
      if (!window.visualViewport) return;
      const vv = window.visualViewport;
      const update = () => {
        const inset = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
        // Anything > 80px likely means the on-screen keyboard is up (small
        // values are OS-chrome / address-bar transitions, not the keyboard).
        const kbOpen = inset > 80;
        const wasOpen = document.body.classList.contains("kb-open");
        if (kbOpen) {
          document.body.classList.add("kb-open");
          document.documentElement.style.setProperty("--kb-inset", inset + "px");
        } else {
          document.body.classList.remove("kb-open");
          document.documentElement.style.setProperty("--kb-inset", "0px");
        }
        // Keyboard open/close shrinks/grows chat-body. If the user was
        // already at the bottom, the new viewport leaves the latest
        // message stranded mid-screen — re-pin to bottom. Use rAF so
        // the browser has done layout pass for the new height. We pass
        // force=false because scrollToBottom honors `atBottom`, so a
        // user mid-history won't get yanked.
        if (kbOpen !== wasOpen) {
          requestAnimationFrame(() => this.scrollToBottom(false));
        }
      };
      vv.addEventListener("resize", update);
      vv.addEventListener("scroll", update);
      update();
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
      // First-run hint — surface key shortcuts so the user doesn't have to
      // hunt for them. Flagged in localStorage so it only fires once. Short
      // delay lets the splash clear first.
      if (!localStorage.getItem("muselab_seen_help")) {
        setTimeout(() => {
          this.toast(
            this.lang === "zh"
              ? "Tip：输入 / 看斜杠命令，@ 引用文件，↑ 回滚上一条"
              : "Tip: type / for slash commands, @ to reference files, ↑ to recall last message",
            "info", 7000);
          localStorage.setItem("muselab_seen_help", "1");
        }, 1500);
      }
      // Same preview-file restore that login() does — covers the
      // already-authed boot path (page refresh with saved token).
      if (this._pendingPreviewSelected) {
        const path = this._pendingPreviewSelected;
        this._pendingPreviewSelected = null;
        this.openFile({ path, name: path.split("/").pop() })
            .catch(() => { /* file gone — silent */ });
      }
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
      // Reactive system-theme tracking: when the OS flips between light/dark
      // and the user hasn't explicitly overridden, follow along. Once they
      // toggle muselab's theme manually (writes muselab_theme), this listener
      // becomes a no-op for them.
      if (window.matchMedia) {
        const mq = window.matchMedia("(prefers-color-scheme: dark)");
        const onSysFlip = (ev) => {
          if (localStorage.getItem("muselab_theme")) return;
          this.theme = ev.matches ? "dark" : "light";
          this.applyTheme();
        };
        if (mq.addEventListener) mq.addEventListener("change", onSysFlip);
        else if (mq.addListener) mq.addListener(onSysFlip);   // legacy Safari
      }
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
    // Short label shown inside the user-side message avatar. Muselab has no
    // identity layer (single-user, token-auth), so we just stamp "我" / "U"
    // by language. Cheap, requires no extra SVG asset.
    userAvatarText() { return this.lang === "zh" ? "我" : "U"; },
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
    // Normalize a model-emitted path into something openByPathToasted can hand
    // to /api/files/list. Handles three things the model commonly does wrong:
    //   - absolute path under ROOT  →  strip ROOT prefix
    //   - "~/..." path              →  return "" (we don't know HOME-vs-ROOT)
    //   - path prefixed by ROOT's basename (e.g. "claude_space/health/x.md"
    //     when ROOT itself is /home/u/claude_space) → strip the duplicate
    // Returns "" for paths we can't safely open (would 403 / 404 on backend).
    _normalizeArchivePath(p) {
      if (!p) return "";
      const root = (this.contextInfo && this.contextInfo.archive_root) || "";
      if (p.startsWith("/")) {
        if (root && (p === root || p.startsWith(root + "/"))) {
          return p.slice(root.length).replace(/^\/+/, "");
        }
        return "";
      }
      if (p.startsWith("~/")) return "";
      // Model often writes "claude_space/foo/bar.md" thinking the archive root
      // is the parent of claude_space. Strip the basename of ROOT if it's the
      // first segment and stripping leaves a non-empty remainder.
      if (root) {
        const base = root.split("/").pop();
        if (base && p.startsWith(base + "/")) {
          return p.slice(base.length + 1);
        }
      }
      return p;
    },

    // For file-centric tools (Read / Edit / Write / NotebookEdit), extract the
    // file path from the tool input so the bubble can render it as a clickable
    // .file-link instead of plain summary text. Returns "" when the tool is
    // not file-centric or the path is empty/system-path we wouldn't open.
    toolFilePath(m) {
      if (!m || m.role !== "tool_use") return "";
      const FILE_TOOLS = new Set(["Read", "Edit", "Write", "NotebookEdit", "MultiEdit"]);
      if (!FILE_TOOLS.has(m.name)) return "";
      const inp = m.input || {};
      const path = inp.file_path || inp.notebook_path || "";
      return this._normalizeArchivePath(path);
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
      // Streaming-friendly preprocess: close any unclosed ``` or ~~~ fenced
      // code blocks before handing to marked. Without this, while the model
      // is mid-codeblock the parser sees `<lang>\n<content>` with no closer
      // and either drops everything past the opening fence or returns an
      // empty render — the user perceives this as "Muse stalled mid-reply,
      // then dumped the rest on completion". Patches a *copy* fed to
      // marked; the source `text` stays the truth. Both fence kinds covered;
      // already-balanced text is untouched.
      let parseInput = text;
      const tripleCount = (text.match(/```/g) || []).length;
      if (tripleCount % 2 === 1) parseInput += "\n```";
      const tildeCount = (text.match(/~~~/g) || []).length;
      if (tildeCount % 2 === 1) parseInput += "\n~~~";
      // marked occasionally throws on partial markdown mid-stream (unclosed
      // fenced block, half-typed table row, etc). Catch and fall through to
      // escaped raw text so the bubble keeps showing SOMETHING instead of
      // briefly clearing while the next chunk arrives.
      let raw;
      try {
        raw = window.marked ? window.marked.parse(parseInput) : parseInput;
      } catch (e) {
        raw = "<pre>" + this.escape(text) + "</pre>";
      }
      if (!window.DOMPurify) return raw;
      const safe = window.DOMPurify.sanitize(raw, {
        USE_PROFILES: { html: true, mathMl: true },          // KaTeX may emit MathML
        FORBID_TAGS: ["style", "iframe", "form", "object", "embed"],
        FORBID_ATTR: ["style", "formaction"],
        ADD_ATTR: ["aria-hidden"],                            // KaTeX uses these
      });
      // If sanitize returned a string MUCH shorter than the input text (e.g.
      // partial code-block syntax tripped the parser and everything past it
      // got stripped), fall back to a plain-pre rendering so we don't display
      // a half-empty bubble mid-stream. Threshold is heuristic — only kicks
      // in on dramatic loss.
      if (safe.length < text.length * 0.25 && text.length > 80) {
        return "<pre>" + this.escape(text) + "</pre>";
      }
      // Math: render $...$ / $$...$$ via KaTeX auto-render. KaTeX runs after
      // DOMPurify (its output is trusted vendor HTML, no need to re-sanitize).
      const tmp = document.createElement("div");
      tmp.innerHTML = safe;
      if (window.renderMathInElement && window.katex) {
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
      }
      this._linkifyFilePaths(tmp);
      return tmp.innerHTML;
    },

    // Path-shaped strings inside inline <code> become clickable `.file-link`
    // anchors. Click is handled by chat-body delegation -> openByPathToasted.
    // Aggressive match (anything ending in .ext) — 404 on click degrades to a
    // toast, no need to be conservative here. Skips fenced code (<pre><code>)
    // since those are usually whole-file snippets, not references.
    _linkifyFilePaths(rootEl) {
      // path/with/slashes.ext  OR  bare.ext  (+ optional :line[:col])
      // Ext must START with a letter to avoid eating version strings like 1.2.3.
      // \p{L}\p{N} (unicode flag) so Chinese/Japanese filenames also match.
      const RE = /^([\p{L}\p{N}_@./~+-]+\.[A-Za-z][A-Za-z0-9]{0,9})(?::(\d+))?(?::\d+)?$/u;
      const toRel = (p) => this._normalizeArchivePath(p);
      // 1) inline <code> whose text looks like a path
      const codes = rootEl.querySelectorAll("code");
      for (const code of codes) {
        if (code.closest("pre")) continue;
        if (code.querySelector("a")) continue;
        const raw = (code.textContent || "").trim();
        if (!raw || raw.length > 200) continue;
        const m = raw.match(RE);
        if (!m) continue;
        const path = toRel(m[1]);
        if (!path) continue;
        const a = document.createElement("a");
        a.className = "file-link";
        a.href = "#";
        a.dataset.path = path;
        if (m[2]) a.dataset.line = m[2];
        a.textContent = raw;
        code.textContent = "";
        code.appendChild(a);
        code.classList.add("has-file-link");
      }
      // 2) markdown links — [label](path/to/file.md) — marked renders as <a>.
      // Convert to .file-link if href is relative (no protocol) and matches the
      // path regex; otherwise leave the anchor alone (real URLs stay clickable).
      const anchors = rootEl.querySelectorAll("a[href]");
      for (const a of anchors) {
        if (a.classList.contains("file-link")) continue;
        let href = a.getAttribute("href") || "";
        if (!href || href.startsWith("#")) continue;
        if (/^[a-z]+:/i.test(href)) continue;          // http: / https: / mailto: / etc.
        // marked / the model may URL-encode the href (e.g. Chinese filenames
        // come through as %E8%B5%84...). Decode so the regex + backend list
        // lookup operate on the raw UTF-8 form.
        try { href = decodeURIComponent(href); } catch (_) { /* malformed → leave as-is */ }
        const m = href.match(RE);
        if (!m) continue;
        const path = toRel(m[1]);
        if (!path) continue;
        a.classList.add("file-link");
        a.setAttribute("href", "#");
        a.dataset.path = path;
        if (m[2]) a.dataset.line = m[2];
      }
    },

    // Delegated click handler on the chat body for `.file-link` anchors
    // produced by _linkifyFilePaths or the tool-bubble template.
    onChatClick(ev) {
      const a = ev.target.closest && ev.target.closest("a[href]");
      if (!a) return;
      // .file-link → open via archive preview
      if (a.classList.contains("file-link")) {
        ev.preventDefault();
        const path = a.dataset.path || "";
        if (path) this.openByPathToasted(path);
        return;
      }
      // Safety net: any other anchor with a relative href shouldn't trigger
      // a same-origin navigation (which would unload the SPA). Try to treat
      // it as a file path; fall back to a toast if we can't parse.
      let href = a.getAttribute("href") || "";
      if (!href || href.startsWith("#")) return;
      if (/^[a-z]+:/i.test(href)) return;             // real protocol → let browser handle
      try { href = decodeURIComponent(href); } catch (_) { /* malformed → leave as-is */ }
      ev.preventDefault();
      // Try ROOT-relative normalization first (absolute under ROOT, or ROOT
      // basename duplicated as prefix). If that returns "" (e.g. /etc/passwd),
      // fall back to the raw href minus leading slash so the user at least
      // gets a "not found" toast instead of silent navigation.
      let p = this._normalizeArchivePath(href);
      if (!p) p = href.replace(/^\/+/, "");
      this.openByPathToasted(p);
    },

    // openFile silently no-ops on 404 (designed for tree clicks where the
    // entry came from the API). For chat-link clicks the path comes from
    // model output and may not exist — surface the failure as a toast.
    async openByPathToasted(path) {
      // HEAD-equivalent check via list on the parent dir is fragile (binary
      // files, images etc. don't go through /api/files/read). Just delegate
      // to openFile and let it set previewMode='unsupported' / pdf / img,
      // but pre-check existence with a cheap list on the parent so we can
      // toast cleanly when the path is fabricated.
      const parent = path.includes("/") ? path.split("/").slice(0, -1).join("/") : "";
      const name = path.split("/").pop();
      try {
        const r = await fetch("/api/files/list?path=" + encodeURIComponent(parent),
          { headers: this.hdr() });
        if (!r.ok) {
          this.toast(this.lang === "zh" ? `文件不存在：${path}` : `Not found: ${path}`, "warn");
          return;
        }
        const d = await r.json();
        const hit = (d.entries || []).find(e => e.name === name);
        if (!hit) {
          this.toast(this.lang === "zh" ? `文件不存在：${path}` : `Not found: ${path}`, "warn");
          return;
        }
        if (hit.is_dir) {
          this.toast(this.lang === "zh" ? `这是目录：${path}` : `Is a directory: ${path}`, "warn");
          return;
        }
        await this.openFile({ path, name });
      } catch (e) {
        this.toast(this.lang === "zh" ? `打开失败：${path}` : `Open failed: ${path}`, "warn");
      }
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
        // Restore the preview file the user was looking at before refresh.
        // openFile is idempotent on tabs[] (won't duplicate); if the file
        // no longer exists we silently no-op (no toast — refresh restoration).
        if (this._pendingPreviewSelected) {
          const path = this._pendingPreviewSelected;
          this._pendingPreviewSelected = null;
          this.openFile({ path, name: path.split("/").pop() })
              .catch(() => { /* file went away — nothing to do */ });
        }
      } catch (e) { this.loginErr = e.message; }
    },

    logout() {
      localStorage.removeItem("muselab_token");
      location.reload();
    },

    hdr() { return { "X-Auth-Token": this.token }; },

    // ===== unified fetch wrapper =====
    // Consolidates the ~60 hand-written fetch calls. Auto-attaches token
    // header, JSON-encodes bodies, decodes JSON / text response, and returns
    // a shape the caller can destructure regardless of success. On non-OK
    // response or network failure, returns { ok: false, status, error } —
    // callers that want auto-toast use api(..., { toastError: true }).
    //
    // Signature:
    //   api(path, opts?)              — GET by default
    //   api(path, { method, json })   — JSON POST/PUT/PATCH with auto serialize
    //   api(path, { method, body })   — raw body (FormData / string / blob)
    //   api(path, { method, query })  — object → ?k=v&... appended to path
    //   api(path, { headers })        — merged on top of token header
    //   api(path, { responseType })   — "json" (default) | "text" | "blob"
    //   api(path, { toastError })     — true → on failure pop an error toast
    async api(path, opts = {}) {
      const method = (opts.method || "GET").toUpperCase();
      const headers = { ...this.hdr(), ...(opts.headers || {}) };
      let body = opts.body;
      if (opts.json !== undefined) {
        headers["Content-Type"] = headers["Content-Type"] || "application/json";
        body = JSON.stringify(opts.json);
      }
      let url = path;
      if (opts.query) {
        const qs = new URLSearchParams(
          Object.entries(opts.query).filter(([_, v]) => v != null && v !== "")
        ).toString();
        if (qs) url += (url.includes("?") ? "&" : "?") + qs;
      }
      let r;
      try {
        r = await fetch(url, { method, headers, body });
      } catch (e) {
        if (opts.toastError) this.toast(
          (this.lang === "zh" ? "网络错误：" : "Network error: ") + e.message,
          "error", 4000);
        return { ok: false, status: 0, error: e.message };
      }
      const rt = opts.responseType || "json";
      let data;
      try {
        if (rt === "json") data = await r.json();
        else if (rt === "text") data = await r.text();
        else if (rt === "blob") data = await r.blob();
        else data = null;
      } catch {
        data = null;
      }
      if (!r.ok) {
        if (opts.toastError) {
          const msg = (data && data.detail) || r.statusText || `HTTP ${r.status}`;
          this.toast(
            (this.lang === "zh" ? "请求失败：" : "Request failed: ") + msg,
            "error", 4000);
        }
        return { ok: false, status: r.status, data, error: (data && data.detail) || r.statusText };
      }
      return { ok: true, status: r.status, data };
    },

    // ===== toast =====
    // `action` is optional: { label: "撤销", onClick: () => {...} } — renders
    // a button inside the toast. Clicking it runs onClick and dismisses.
    toast(msg, type = "info", timeout = 3000, action = null) {
      const id = ++this._toastId;
      this.toasts.push({ id, msg, type, action });
      if (timeout) setTimeout(() => this.dismissToast(id), timeout);
    },
    dismissToast(id) { this.toasts = this.toasts.filter(t => t.id !== id); },
    runToastAction(t) {
      try { t.action && t.action.onClick && t.action.onClick(); }
      finally { this.dismissToast(t.id); }
    },

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
      // Preview-pane state (tabs, selected) persists too so a refresh restores
      // the exact files the user was looking at — matches the chat-tab strip's
      // behavior via openTabIds.
      localStorage.setItem("muselab_prefs", JSON.stringify({
        model: this.model, permission: this.permission,
        showThinking: this.showThinking, currentId: this.currentId,
        openTabIds: this.openTabIds,
        previewTabs: this.tabs.map(t => ({ path: t.path, name: t.name })),
        previewSelected: this.selected,
        expanded: Array.from(this.expanded),
        leftOpen: this.leftOpen, rightOpen: this.rightOpen,
        leftWidth: this.leftWidth, rightWidth: this.rightWidth,
        showHidden: this.showHidden,
        openFilesCollapsed: this.openFilesCollapsed,
        openFilesHeight: this.openFilesHeight,
      }));
    },
    loadPrefs() {
      try {
        const p = JSON.parse(localStorage.getItem("muselab_prefs") || "{}");
        if (p.model) this.model = p.model;
        if (p.permission) this.permission = p.permission;
        // showThinking is now always-on; ignore any persisted `false` from
        // pre-2026-05-19 prefs. See app.js:102.
        if (typeof p.leftOpen === "boolean") this.leftOpen = p.leftOpen;
        if (typeof p.rightOpen === "boolean") this.rightOpen = p.rightOpen;
        if (typeof p.leftWidth === "number") this.leftWidth = p.leftWidth;
        if (typeof p.rightWidth === "number") this.rightWidth = p.rightWidth;
        if (typeof p.showHidden === "boolean") this.showHidden = p.showHidden;
        if (p.currentId) this.currentId = p.currentId;
        if (Array.isArray(p.openTabIds)) this.openTabIds = p.openTabIds;
        // Preview tabs — restore the strip; the actual content fetch happens
        // lazily when the user clicks back to one (or via restorePreviewSelected
        // which runs once after login).
        if (Array.isArray(p.previewTabs)) this.tabs = p.previewTabs;
        if (typeof p.previewSelected === "string") this._pendingPreviewSelected = p.previewSelected;
        if (typeof p.openFilesCollapsed === "boolean") this.openFilesCollapsed = p.openFilesCollapsed;
        // null = auto-fit; only restore an explicit user override.
        if (typeof p.openFilesHeight === "number" && p.openFilesHeight > 60) {
          this.openFilesHeight = p.openFilesHeight;
        } else if (p.openFilesHeight === null) {
          this.openFilesHeight = null;
        }
        this._pendingExpanded = p.expanded || [];
      } catch {}
    },

    async fetchContextInfo() {
      const { ok, data } = await this.api("/api/chat/context-info");
      if (!ok || !data) return;
      data._fetched = true;
      this.contextInfo = data;
      // First successful load: if the user hasn't configured any provider,
      // pop the Settings drawer so they can fix it before trying to chat.
      // _providerCheckDone gate ensures we don't re-pop on heartbeat
      // reconnects or polling refreshes.
      if (!this._providerCheckDone) {
        this._providerCheckDone = true;
        if (!data.has_any_provider && !this.settings.show) {
          this.openSettings();
        }
      }
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
        if (r.ok) {
          this.availableModels = (await r.json()).models || [];
          this._rebindModelSelect();
        }
      } catch {}
    },

    // Backwards-compat wrapper — call _rebindSelect("model") directly.
    async _rebindModelSelect() { await this._rebindSelect("model"); },

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
        // Model-fork creates a brand-new session — wire it up as a tab the
        // same way newSession() does (tabState + openTabIds + activate).
        this.currentId = meta.id;
        const newSt = this._ensureTabState(meta.id);
        newSt.messages.length = 0;
        newSt._loaded = true;
        this._activateTabState(meta.id);
        if (!this.openTabIds.includes(meta.id)) this.openTabIds.push(meta.id);
        this._fetchTabUsage(meta.id);
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
    // A fresh per-tab state slot. Object refs (messages, sessionUsage) live
    // forever — we mutate in place so Alpine's reactivity stays bound.
    _blankTabState() {
      return {
        messages: [],
        sessionUsage: { input_tokens: 0, output_tokens: 0,
                         cache_read_tokens: 0, cache_creation_tokens: 0,
                         context_limit: 0, context_used: 0, context_used_pct: 0 },
        streaming: false,
        es: null,
        streamingModel: "",
        streamElapsed: 0,
        _streamTimer: null,
        _streamStartedAt: 0,
        _loaded: false,   // set true after first loadSession populates messages
      };
    },
    _ensureTabState(id) {
      if (!this.tabState[id]) this.tabState[id] = this._blankTabState();
      return this.tabState[id];
    },
    // Pull the per-session context meter (input/output tokens, limit, %)
    // from the backend and merge it into tabState[sid].sessionUsage. Limit
    // is model-specific (opus/sonnet/haiku → 200k, others → 128k default),
    // so we MUST refresh whenever the active model could differ from what
    // last produced the cached numbers — that includes brand-new sessions
    // which would otherwise sit at the _blankTabState default of 128k.
    async _fetchTabUsage(sid) {
      if (!sid) return;
      const st = this._ensureTabState(sid);
      // Prefer the model that's currently bound to the session on the server
      // (sessions list metadata); fall back to root this.model if absent.
      const sessMeta = this.sessions.find(s => s.id === sid);
      const model = (sessMeta && sessMeta.model) || this.model || "";
      try {
        const ur = await fetch(`/api/chat/usage/${sid}?model=${encodeURIComponent(model)}`,
                                 { headers: this.hdr() });
        if (!ur.ok) return;
        const u = await ur.json();
        Object.assign(st.sessionUsage, u);
        if (sid === this.currentId) this.sessionUsage = st.sessionUsage;
      } catch (e) { /* non-fatal */ }
    },

    // Mirror this tab's state into root fields so the UI sees it. Object refs
    // (messages, sessionUsage) are shared — mutations from anywhere reflect.
    // Primitives (streaming, es, ...) must be copied; they get re-synced as
    // the active stream progresses.
    _activateTabState(id) {
      const st = this._ensureTabState(id);
      this.messages = st.messages;
      this.sessionUsage = st.sessionUsage;
      this.streaming = st.streaming;
      this.es = st.es;
      this.streamingModel = st.streamingModel;
      this.streamElapsed = st.streamElapsed;
      this._streamTimer = st._streamTimer;
      this._streamStartedAt = st._streamStartedAt;
      // Tab cache may hold an out-of-date sessionUsage (e.g. backend table
      // updated since we last polled). Fire-and-forget a re-fetch so the
      // meter reflects current truth without blocking the UI swap.
      this._fetchTabUsage(id);
    },

    async initSessions() {
      await this.refreshSessions();
      if (!this.sessions.length) {
        const s = await this.newSession();
        this.currentId = s.id;
      } else if (!this.sessions.find(x => x.id === this.currentId)) {
        this.currentId = this.sessions[0].id;
      }
      // Reconcile openTabIds (restored from prefs) with what still exists on
      // the server: drop tabs whose session was deleted, then ensure currentId
      // is in the list. Other tabs are lazy-loaded on first switch.
      const validIds = new Set(this.sessions.map(s => s.id));
      this.openTabIds = (this.openTabIds || []).filter(id => validIds.has(id));
      if (!this.openTabIds.includes(this.currentId)) {
        this.openTabIds.push(this.currentId);
      }
      this._activateTabState(this.currentId);
      const st = this._ensureTabState(this.currentId);
      if (!st._loaded) {
        await this.loadSession(this.currentId);
        st._loaded = true;
      }
      this.savePrefs();
    },
    async refreshSessions() {
      const { ok, data } = await this.api("/api/chat/sessions");
      if (!ok) return;
      const raw = (data && data.sessions) || [];
      // Defensive: drop any entry without a usable id. Alpine x-for :key
      // bindings (session-picker, history popup) use `s.id`; an undefined
      // key crashes alpine morph with "Cannot read properties of undefined
      // (reading 'after')".
      this.sessions = raw.filter(s => s && typeof s.id === "string" && s.id);
      // <select x-model="currentId"> needs a tickle to sync display when
      // sessions populate (same Alpine-x-model-on-dynamic-options race).
      await this._rebindSelect("currentId");
    },

    // Generic select-rebind tickle (model + currentId share this). Flipping
    // to '' then back across two ticks forces Alpine to re-evaluate x-model.
    async _rebindSelect(field) {
      const cur = this[field];
      if (!cur) return;
      await this.$nextTick();
      this[field] = "";
      await this.$nextTick();
      this[field] = cur;
    },
    async newSession() {
      // No longer stops streams in OTHER tabs — each tab has its own ES in
      // tabState[id].es. The new session starts fresh in its own tab.
      const r = await fetch("/api/chat/sessions", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ name: "", model: this.model }),
      });
      const meta = await r.json();
      await this.refreshSessions();
      this.currentId = meta.id;
      // Initialize a fresh tabState slot for this brand-new session; mark
      // _loaded so switchSession won't try to fetch (it's empty by design).
      const st = this._ensureTabState(meta.id);
      st.messages.length = 0;
      st._loaded = true;
      this._activateTabState(meta.id);
      if (!this.openTabIds.includes(meta.id)) this.openTabIds.push(meta.id);
      // Fetch the correct context_limit for this session's model so the meter
      // doesn't sit at the 128k default from _blankTabState (opus/sonnet → 200k).
      this._fetchTabUsage(meta.id);
      this.savePrefs();
      this.toast(this.t("toast.created"), "success");
      return meta;
    },

    // ===== tabs =====
    // Switch to (and if needed open) a tab. Used by the picker dropdown to
    // promote a history session into a tab.
    async openTab(id, makeCurrent = true) {
      if (!this.openTabIds.includes(id)) this.openTabIds.push(id);
      if (makeCurrent && id !== this.currentId) {
        this.currentId = id;
        await this.switchSession();
      }
      this.savePrefs();
    },

    // Close a tab. If it was active, hop to a neighbor; if the strip would be
    // empty, create a fresh session so the user always has somewhere to type.
    // Also closes any in-flight stream for the closed tab and drops its
    // tabState entry — leaving it around would leak EventSources.
    // NOTE: do NOT rename to closeTab — that name is taken by the file-preview
    // tab strip's closer (see line ~2640). JS object literals: later definition
    // wins, so when this was named closeTab, file-preview's overrode ours and
    // every × click in the chat tab strip silently no-op'd.
    async closeChatTab(id, ev) {
      if (ev && ev.stopPropagation) ev.stopPropagation();
      const idx = this.openTabIds.indexOf(id);
      if (idx < 0) return;
      const wasActive = this.currentId === id;
      const st = this.tabState[id];
      // Don't offer undo for a tab whose stream is in flight — we'd have to
      // re-attach to a live EventSource which gets hairy fast. Tearing it
      // down and silently swallowing the in-flight reply is the lesser evil
      // (and the user clicked × explicitly).
      const wasStreaming = !!(st && st.streaming);
      if (st) {
        if (st.es) { try { st.es.close(); } catch {} }
        if (st._streamTimer) clearInterval(st._streamTimer);
      }
      this.openTabIds.splice(idx, 1);
      if (wasActive) {
        if (this.openTabIds.length) {
          const nextIdx = Math.min(idx, this.openTabIds.length - 1);
          this.currentId = this.openTabIds[nextIdx];
          await this.switchSession();
        } else {
          await this.newSession();
        }
      }
      if (this.tabState[id]) delete this.tabState[id];
      this.savePrefs();
      // Undo: re-add to openTabIds at the original index, switch back. The
      // tab state will rehydrate lazily via switchSession → loadSession (we
      // don't preserve the in-memory messages snapshot — they'll be fetched
      // from the server's authoritative session JSONL).
      if (!wasStreaming) {
        const sessionName = (this.sessions.find(s => s.id === id) || {}).name
                             || (this.lang === "zh" ? "会话" : "session");
        this.toast(
          this.lang === "zh" ? `已关闭「${sessionName}」` : `Closed "${sessionName}"`,
          "info", 5000,
          {
            label: this.lang === "zh" ? "撤销" : "Undo",
            onClick: async () => {
              if (this.openTabIds.includes(id)) return;
              this.openTabIds.splice(Math.min(idx, this.openTabIds.length), 0, id);
              this.currentId = id;
              await this.switchSession();
              this.savePrefs();
            },
          }
        );
      }
    },

    // Inline rename — tab name -> <input>. Enter saves, Esc cancels, blur saves.
    startRenameTab(id) {
      const s = this.sessions.find(x => x.id === id);
      if (!s) return;
      this.renamingTabId = id;
      this.renameDraft = s.name || "";
      this.$nextTick(() => {
        // x-show keeps every tab's <input> mounted — scope the selector to
        // THIS tab's data-tid so we focus the right one.
        const el = document.querySelector(
          `.chat-tab-rename-input[data-tid="${CSS.escape(id)}"]`);
        if (el) { el.focus(); el.select(); }
      });
    },
    async commitRenameTab() {
      const id = this.renamingTabId;
      const name = (this.renameDraft || "").trim();
      this.renamingTabId = "";
      const draft = this.renameDraft;
      this.renameDraft = "";
      if (!id || !name) return;
      const cur = this.sessions.find(x => x.id === id);
      if (!cur || cur.name === name) return;
      const r = await fetch("/api/chat/sessions/" + id, {
        method: "PATCH",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (r.ok) { await this.refreshSessions(); this.toast(this.t("toast.renamed"), "success"); }
      else { this.toast(this.lang === "zh" ? "重命名失败" : "Rename failed", "error"); }
    },
    cancelRenameTab() { this.renamingTabId = ""; this.renameDraft = ""; },

    // Defensive helpers used by inline templates — keep them tiny so Alpine
    // never has to re-parse complex expressions on every reactive tick.
    isTabStreaming(tid) {
      const st = this.tabState[tid];
      return !!(st && st.streaming);
    },
    tabCtxMenuStyle() {
      const m = this.tabCtxMenu;
      return m ? `left:${m.x}px; top:${m.y}px` : "";
    },
    tabTitle(tid) {
      const s = this.sessions.find(x => x.id === tid);
      return s ? (s.name || "") : "";
    },
    // <title> driver — wired via x-effect on the root #app element. Re-runs
    // whenever any read reactive (currentId / sessions / streaming) changes.
    pageTitle() {
      const cur = this.sessions.find(s => s.id === this.currentId);
      const name = (cur && cur.name) || "";
      const prefix = this.streaming ? "● " : "";
      return name ? `${prefix}${name} · muselab` : "muselab — Meet Muse";
    },
    // ===== thinking / tool_result collapse =====
    // Default-collapse historical blocks; the currently-streaming last block
    // stays expanded. User clicks override either way.
    // Storage: top-level reactive _expandedMsgs map (keyed by uuid or _k).
    // Previously stored as `m._userExpanded` on the message object directly,
    // but Alpine v3 doesn't deep-wrap array elements — direct property set
    // didn't trigger re-render. Top-level prop + spread-assign does.
    _expandedMsgs: {},
    _msgKey(i, m) {
      if (!m) return "";
      return m.uuid || m._k || ("m-" + i);
    },
    isMsgExpanded(i, m) {
      if (!m) return true;
      const k = this._msgKey(i, m);
      if (k in this._expandedMsgs) return this._expandedMsgs[k];
      // Default: only the actively-streaming last block is expanded.
      const msgs = this.messages || [];
      return !!this.streaming && i === msgs.length - 1;
    },
    toggleMsgExpanded(m, i) {
      if (!m) return;
      const idx = (i ?? (this.messages || []).indexOf(m));
      const k = this._msgKey(idx, m);
      const cur = this.isMsgExpanded(idx, m);
      // Spread-assign so Alpine sees the replacement and re-evaluates.
      this._expandedMsgs = { ...this._expandedMsgs, [k]: !cur };
    },
    toolResultClass(i, m) {
      let cls = "tool-result";
      if (m && m.is_error) cls += " err";
      if (!this.isMsgExpanded(i, m)) cls += " collapsed";
      return cls;
    },
    toolResultSummary(m) {
      const text = (m && m.preview) || "";
      const lines = text.split("\n").length;
      return lines + (this.lang === "zh" ? " 行输出" : " lines");
    },
    thinkingClass(i, m) {
      return this.isMsgExpanded(i, m) ? "thinking" : "thinking collapsed";
    },
    thinkingPreview(m) {
      const text = (m && m.text) || "";
      const firstLine = text.split("\n")[0] || "";
      const trimmed = firstLine.slice(0, 80);
      return trimmed + (text.length > 80 ? "…" : "");
    },
    async _refreshCtxMeter() {
      // Pull SDK ContextUsageResponse via /context-breakdown so the meter
      // shows post-compact (or any other out-of-band) state without waiting
      // for the next stream's 'done' event.
      if (!this.currentId) return;
      const { ok, data } = await this.api(
        `/api/chat/context-breakdown/${this.currentId}`);
      if (!ok || !data) return;
      const used = Math.max(0, Number(data.totalTokens || 0));
      const maxT = Math.max(0, Number(data.maxTokens
                                       || this.sessionUsage.context_limit
                                       || 200000));
      this.sessionUsage = {
        ...this.sessionUsage,
        context_used: used,
        context_limit: maxT,
        context_used_pct: maxT
          ? Math.round(used / maxT * 1000) / 10
          : 0,
      };
    },

    async showCtxBreakdown() {
      if (!this.currentId) return;
      this.ctxBreakdown = { show: true, loading: true, data: null, error: "" };
      const { ok, data, error, status } = await this.api(
        `/api/chat/context-breakdown/${this.currentId}`);
      this.ctxBreakdown.loading = false;
      if (ok && data) {
        this.ctxBreakdown.data = data;
      } else {
        // 409 = no live client yet (session hasn't streamed a turn).
        this.ctxBreakdown.error = status === 409
          ? (this.lang === "zh"
              ? "需要先发一条消息才能查 breakdown（SDK 要求 live client）"
              : "Send a message first — SDK breakdown needs a live client")
          : (error || (this.lang === "zh" ? "查询失败" : "Fetch failed"));
      }
    },

    ctxRingTitle() {
      const u = this.sessionUsage || {};
      const used = u.context_used || 0;
      const limit = u.context_limit || 0;
      const pct = u.context_used_pct || 0;
      if (this._compacting) {
        return this.lang === "zh"
          ? `📦 压缩进行中，已排队 ${this._compactQueue.length} 条`
          : `📦 Compact in progress (${this._compactQueue.length} queued)`;
      }
      if (!limit) return this.lang === "zh" ? "上下文 …" : "Context …";
      const used_s = (used / 1000).toFixed(1) + "K";
      const limit_s = limit >= 1_000_000
        ? (limit / 1_000_000).toFixed(0) + "M"
        : (limit / 1000).toFixed(0) + "K";
      const meta = (this.availableModels || []).find(m => m.model === this.model);
      const modelLabel = meta ? meta.label : this.model;
      const hint = this.lang === "zh"
        ? "（点击压缩 · 右键看拆分）"
        : "(click to compact · right-click for breakdown)";
      return `${used_s} / ${limit_s} (${pct}%) · ${modelLabel}\n${hint}`;
    },
    compactStatusLabel() {
      // Single method instead of an inline template-literal expression in
      // x-text — Alpine error handling for templated attribute expressions
      // is brittle (a thrown evaluation can corrupt reactive state and
      // surface as "Cannot read properties of undefined (reading 'after')"
      // when the next morph/transition runs). Centralising here keeps the
      // expression in real JS where defensive guards are normal.
      if (!this._compacting) {
        try { return this.ctxMeterLabel(); }
        catch { return ""; }
      }
      const q = (this._compactQueue && this._compactQueue.length) || 0;
      if (this.lang === "zh") {
        return q ? `📦 压缩中… 消息队列 ${q}` : "📦 压缩中…";
      }
      return q ? `📦 Compacting… queued ${q}` : "📦 Compacting…";
    },

    activateTab(tid) {
      if (tid === this.currentId) return;
      this.currentId = tid;
      this.switchSession();
      // Scroll the newly-active tab into view — when the strip overflows
      // horizontally (many sessions open), keyboard shortcuts / programmatic
      // activation would otherwise leave the active tab hidden off-screen.
      this.$nextTick(() => this._scrollTabIntoView(tid));
    },
    _scrollTabIntoView(tid) {
      const strip = document.querySelector(".chat-tabs-list");
      if (!strip) return;
      const tab = strip.querySelector(`.chat-tab[data-tid="${tid}"]`)
                  || Array.from(strip.querySelectorAll(".chat-tab"))[
                       this.openTabIds.indexOf(tid)];
      if (!tab) return;
      // `inline: nearest` preserves vertical scroll, only scrolls horizontally
      // if the tab isn't already visible. `block: nearest` likewise vertical.
      tab.scrollIntoView({ inline: "nearest", block: "nearest" });
    },
    onTabAuxClick(ev, tid) {
      // 1 = middle-click — close the tab.
      if (ev.button === 1) this.closeChatTab(tid);
    },

    // Long-press handlers used to live here. Removed: they ate mobile taps
    // (touchstart→timer→touchend→cleared, but the synthetic click after a
    // long-press window collided with @click.outside on the menu and
    // sometimes blocked legitimate taps on history rows). Mobile users get
    // the same actions via the inline ⋮ kebab button on each tab / row.
    onChatTabsWheel(ev) {
      // Horizontal scroll the tab strip via the vertical wheel — like editors do.
      if (ev.deltaY !== 0) ev.currentTarget.scrollLeft += ev.deltaY;
    },

    // ===== drag-to-reorder chat tabs (desktop only — HTML5 drag-and-drop) =====
    // Mobile would need a touch-based fallback; keeping scope tight for now.
    // We track which tab is being dragged in `_draggingTabId` and which tab
    // the mouse is currently over in `tabDragOverId` (drives a visual hint).
    _draggingTabId: "",
    tabDragOverId: "",
    onTabDragStart(ev, tid) {
      this._draggingTabId = tid;
      // dataTransfer must be set for Firefox to fire drag events at all.
      try {
        ev.dataTransfer.effectAllowed = "move";
        ev.dataTransfer.setData("text/plain", tid);
      } catch (_) {}
    },
    onTabDragOver(ev, tid) {
      if (!this._draggingTabId || tid === this._draggingTabId) return;
      ev.dataTransfer.dropEffect = "move";
      this.tabDragOverId = tid;
    },
    onTabDragLeave(tid) {
      if (this.tabDragOverId === tid) this.tabDragOverId = "";
    },
    onTabDrop(ev, tid) {
      const src = this._draggingTabId;
      this._draggingTabId = "";
      this.tabDragOverId = "";
      if (!src || src === tid) return;
      const from = this.openTabIds.indexOf(src);
      const to = this.openTabIds.indexOf(tid);
      if (from < 0 || to < 0) return;
      this.openTabIds.splice(from, 1);
      this.openTabIds.splice(to, 0, src);
      this.savePrefs();
    },
    onTabDragEnd() {
      this._draggingTabId = "";
      this.tabDragOverId = "";
    },

    // ===== drag-to-reorder preview tabs (mirrors chat-tab drag, but operates
    // on `tabs` array instead of openTabIds and on file paths as the id).
    _draggingPreviewTabPath: "",
    previewDragOverPath: "",
    showPreviewTabMenu(ev, path) {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (ev && ev.stopPropagation) ev.stopPropagation();
      const cx = (ev && ev.clientX) || 100;
      const cy = (ev && ev.clientY) || 100;
      // Overlay catches outside-clicks reliably; no need to defer mount.
      this.previewTabCtxMenu = {
        path,
        x: Math.min(cx, window.innerWidth - 220),
        y: Math.min(cy, window.innerHeight - 280),
      };
    },
    previewTabCtxMenuStyle() {
      if (!this.previewTabCtxMenu) return "";
      return `position: fixed; top: ${this.previewTabCtxMenu.y}px; left: ${this.previewTabCtxMenu.x}px;`;
    },
    async previewTabMenuAction(action) {
      const m = this.previewTabCtxMenu;
      if (!m) return;
      const path = m.path;
      this.previewTabCtxMenu = null;
      switch (action) {
        case "close":
          this.closeTab(path);
          break;
        case "closeOthers":
          this.tabs = this.tabs.filter(t => t.path === path);
          if (this.selected !== path) await this.switchTab(path);
          this.savePrefs();
          break;
        case "closeRight": {
          const idx = this.tabs.findIndex(t => t.path === path);
          if (idx >= 0) this.tabs = this.tabs.slice(0, idx + 1);
          this.savePrefs();
          break;
        }
        case "closeAll":
          this.tabs = []; this.selected = ""; this.savePrefs();
          break;
        case "reveal":
          await this.revealInTree(path);
          break;
        case "mention":
          this.insertFileMention(path);
          break;
        case "copyPath":
          navigator.clipboard?.writeText(path).then(
            () => this.toast(this.t("toast.copied") + ": " + path, "success", 1500),
            () => this.toast("复制失败（需要 HTTPS）", "error"));
          break;
      }
    },

    async onPreviewDrop(ev) {
      this.previewDragHover = false;
      const files = (ev.dataTransfer && ev.dataTransfer.files) || [];
      if (!files.length) return;
      // Upload each file to ROOT (or the current preview dir if we can
      // determine one). uploadFileTo handles toasts + tree refresh.
      for (const f of files) {
        try { await this.uploadFileTo("", f); } catch {}
      }
    },
    onPreviewTabDragStart(ev, path) {
      this._draggingPreviewTabPath = path;
      try {
        ev.dataTransfer.effectAllowed = "move";
        ev.dataTransfer.setData("text/plain", path);
      } catch (_) {}
    },
    onPreviewTabDragOver(ev, path) {
      if (!this._draggingPreviewTabPath
          || path === this._draggingPreviewTabPath) return;
      ev.dataTransfer.dropEffect = "move";
      this.previewDragOverPath = path;
    },
    onPreviewTabDragLeave(path) {
      if (this.previewDragOverPath === path) this.previewDragOverPath = "";
    },
    onPreviewTabDrop(ev, path) {
      const src = this._draggingPreviewTabPath;
      this._draggingPreviewTabPath = "";
      this.previewDragOverPath = "";
      if (!src || src === path) return;
      const from = this.tabs.findIndex(t => t.path === src);
      const to = this.tabs.findIndex(t => t.path === path);
      if (from < 0 || to < 0) return;
      const [moved] = this.tabs.splice(from, 1);
      this.tabs.splice(to, 0, moved);
      this.savePrefs();
    },
    onPreviewTabDragEnd() {
      this._draggingPreviewTabPath = "";
      this.previewDragOverPath = "";
    },
    historyRowClass(sid) {
      return { active: sid === this.currentId, open: this.openTabIds.includes(sid) };
    },
    // The history picker popup escapes its container via position: fixed
    // (the parent .chat-tabs has overflow-x: auto which forces overflow-y to
    // also clip — an absolute-positioned popup gets cut off). We compute the
    // viewport-anchored position from the 📁 button's bounding rect at click
    // time so the popup floats just below it.
    historyPickerStyle: "",
    sessionPickerSearch: "",
    filteredSessions() {
      const q = (this.sessionPickerSearch || "").trim().toLowerCase();
      if (!q) return this.sessions;
      return this.sessions.filter(s =>
        (s.name && s.name.toLowerCase().includes(q))
        || (s.first_prompt && s.first_prompt.toLowerCase().includes(q))
      );
    },
    toggleHistoryPicker(ev) {
      if (this.sessionPickerOpen) { this.sessionPickerOpen = false; return; }
      const btn = ev && ev.currentTarget;
      const rect = btn ? btn.getBoundingClientRect() : null;
      if (rect) {
        const popW = Math.min(320, window.innerWidth - 16);
        // Right-align under the button, but stay inside the viewport edges.
        let left = Math.round(rect.right - popW);
        if (left < 8) left = 8;
        const top = Math.round(rect.bottom + 4);
        this.historyPickerStyle =
          `position: fixed; top: ${top}px; left: ${left}px; width: ${popW}px;`;
      } else {
        this.historyPickerStyle = "";
      }
      this.sessionPickerOpen = true;
    },
    pickerOpenSession(sid) {
      this.sessionPickerOpen = false;
      this.openTab(sid);
    },
    pickerRowMenu(ev, sid) {
      if (ev && ev.stopPropagation) ev.stopPropagation();
      this.sessionPickerOpen = false;
      this.showTabMenu(ev, sid);
    },
    async pickerDeleteSession(sid, ev) {
      if (ev && ev.stopPropagation) ev.stopPropagation();
      const s = this.sessions.find(x => x.id === sid);
      const name = (s && s.name) || sid.slice(0, 8);
      const ok = await this.confirm({
        title: this.lang === "zh" ? "删除会话" : "Delete session",
        body: this.lang === "zh"
          ? `要删除「${name}」吗？此操作不可恢复（CLI JSONL 也会被删除）。`
          : `Delete "${name}"? Not undoable — the CLI JSONL will be removed too.`,
        okText: this.lang === "zh" ? "删除" : "Delete",
        danger: true,
      });
      if (!ok) return;
      await this.deleteSessionById(sid);
      this.openTabIds = this.openTabIds.filter(x => x !== sid);
      if (this.tabState[sid]) delete this.tabState[sid];
      this.savePrefs();
    },

    // Right-click context menu on a tab (or a row in the session picker).
    // Also called by the mobile ⋮ kebab button (which uses normal click).
    // We DEFER the actual menu open by one tick — otherwise the click that
    // triggered showTabMenu propagates to the document during the same
    // synchronous flow, and the newly-mounted menu's @click.outside listener
    // (or any other ancestor click handler) immediately closes / re-acts on
    // the same event. setTimeout(0) lets the trigger event finish first.
    showTabMenu(ev, id) {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (ev && ev.stopPropagation) ev.stopPropagation();
      const cx = (ev && (ev.clientX || (ev.touches && ev.touches[0] && ev.touches[0].clientX))) || 100;
      const cy = (ev && (ev.clientY || (ev.touches && ev.touches[0] && ev.touches[0].clientY))) || 100;
      const x = Math.min(cx, window.innerWidth - 220);
      const y = Math.min(cy, window.innerHeight - 200);
      setTimeout(() => {
        this.sessionPickerOpen = false;
        this.tabCtxMenu = { id, x, y };
      }, 0);
    },
    closeTabMenu() { this.tabCtxMenu = null; },
    async menuRename(id) {
      this.closeTabMenu();
      // Inline rename input lives inside the tab DOM, so the tab must be open
      // for the input to appear. If the user right-clicked a session from the
      // history picker that isn't a tab yet, promote it first.
      if (!this.openTabIds.includes(id)) await this.openTab(id);
      this.startRenameTab(id);
    },
    async menuEditPrompt(id) {
      this.closeTabMenu();
      // editSessionPrompt() reads currentId. Borrow it briefly to target this
      // tab's session without forcing a full switch.
      const orig = this.currentId;
      this.currentId = id;
      try { await this.editSessionPrompt(); }
      finally { this.currentId = orig; }
    },
    async menuClose(id) { this.closeTabMenu(); await this.closeChatTab(id); },
    async menuDelete(id) {
      this.closeTabMenu();
      // Close side effects (ES / interval) on the dying tab BEFORE the
      // server delete, but defer tabState[id] cleanup until after we've
      // removed the tab from openTabIds (so x-for unmounts its DOM first).
      const st = this.tabState[id];
      if (st) {
        if (st.es) { try { st.es.close(); } catch {} }
        if (st._streamTimer) clearInterval(st._streamTimer);
      }
      // deleteSessionById handles server delete + sessions-list refresh AND
      // bumps the user to a remaining session if id was current.
      await this.deleteSessionById(id);
      this.openTabIds = this.openTabIds.filter(x => x !== id);
      if (!this.openTabIds.includes(this.currentId)) {
        this.openTabIds.push(this.currentId);
      }
      if (this.tabState[id]) delete this.tabState[id];
      this.savePrefs();
    },
    async switchSession() {
      // Switch the visible tab. We do NOT touch other tabs' streams — each
      // tab's ES is in its own tabState[id], and stream callbacks write
      // there directly. Switching is just "show that tab".
      this._activateTabState(this.currentId);
      this.savePrefs();
      const st = this._ensureTabState(this.currentId);
      if (!st._loaded) {
        await this.loadSession(this.currentId);
        st._loaded = true;
      } else {
        // Already loaded — just re-bind UI state. messages reference unchanged.
        if (this.model) {
          // Stay on the current root model; no auto-update needed.
        }
        this.atBottom = true;
        this.scrollToBottom(true);
        this.$nextTick(() => this.highlightCode(".chat-body"));
      }
    },
    async loadSession(sid) {
      if (!sid) return;
      const st = this._ensureTabState(sid);
      // Skeleton on the active tab during the fetch — markdown rendering of
      // a long history can also take a noticeable beat after the network
      // returns, so the flag must wrap both phases.
      const isCurrent = sid === this.currentId;
      if (isCurrent) this.messagesLoading = true;
      try {
        const r = await fetch("/api/chat/sessions/" + sid, { headers: this.hdr() });
        if (!r.ok) {
          st.messages.length = 0;
          if (isCurrent) this.messages = st.messages;
          return;
        }
        const s = await r.json();
        const next = (s.messages || []).map((m, i) => {
          const out = { ...m, _k: sid + "-" + i };
          if (m.role === "assistant" && m.text) out.html = this.mdRender(m.text);
          return out;
        });
        // Mutate in place — preserves the Array reference Alpine is watching.
        st.messages.length = 0;
        st.messages.push(...next);
        if (isCurrent) {
          this.messages = st.messages;
          if (s.model) this.model = s.model;
          this.atBottom = true;
          this.scrollToBottom(true);
          this.$nextTick(() => this.highlightCode(".chat-body"));
        }
        await this._fetchTabUsage(sid);
      } finally {
        if (isCurrent) this.messagesLoading = false;
      }
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

    // Delete any session from the picker dropdown's inline × button.
    async deleteSessionById(sid) {
      const s = this.sessions.find(x => x.id === sid);
      const ok = await this.confirm({
        title: this.lang === "zh" ? "删除会话" : "Delete session",
        body: this.lang === "zh"
          ? `确定删除「${s?.name || sid.slice(0, 8)}」？此操作不可恢复（含 CLI 历史）。`
          : `Delete '${s?.name || sid.slice(0, 8)}'? Irreversible (clears CLI history too).`,
        danger: true,
        okText: this.lang === "zh" ? "删除" : "Delete",
      });
      if (!ok) return;
      // Tear down per-tab cached state before we forget about the session.
      const dyingTab = this.tabState[sid];
      if (dyingTab) {
        if (dyingTab.es) { try { dyingTab.es.close(); } catch {} }
        if (dyingTab._streamTimer) clearInterval(dyingTab._streamTimer);
        delete this.tabState[sid];
      }
      await fetch(`/api/chat/sessions/${sid}`, { method: "DELETE", headers: this.hdr() });
      await this.refreshSessions();
      if (this.currentId === sid) {
        if (this.sessions.length === 0) {
          // newSession already pushes to openTabIds + activates tab state.
          await this.newSession();
        } else {
          this.currentId = this.sessions[0].id;
          this._activateTabState(this.currentId);
          await this.switchSession();
        }
        this.savePrefs();
      }
    },

    // ===== Versions + upgrade =====
    async loadVersions() {
      this.settings.versionsLoading = true;
      try {
        const r = await fetch("/api/settings/versions", { headers: this.hdr() });
        if (r.ok) this.settings.versions = await r.json();
        else this.toast(this.lang === "zh" ? "版本检查失败" : "Version check failed", "error", 3000);
      } catch (e) {
        this.toast((this.lang === "zh" ? "版本检查失败：" : "Check failed: ") + e.message, "error", 3000);
      } finally {
        this.settings.versionsLoading = false;
      }
    },
    async runUpgrade(only = null) {
      // only = "sdk" | "cli" | null. When null, upgrade everything available.
      if (!this.settings.versions) return;
      let targets;
      if (only) {
        targets = [only];
      } else {
        targets = [];
        if (this.settings.versions.sdk_upgrade_available) targets.push("sdk");
        if (this.settings.versions.system_cli_upgrade_available) targets.push("cli");
      }
      if (targets.length === 0) {
        this.toast(this.lang === "zh" ? "无需升级" : "Nothing to upgrade", "info", 2000);
        return;
      }
      this.settings.upgradeRunning = true;
      this.settings.upgradeResult = null;
      try {
        const r = await fetch("/api/settings/upgrade", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ targets }),
        });
        if (r.ok) {
          this.settings.upgradeResult = await r.json();
          if (this.settings.upgradeResult.ok) {
            this.toast(this.lang === "zh" ? "升级完成" : "Upgrade complete", "success", 3000);
            // Refresh the versions table so user sees the new numbers
            await this.loadVersions();
          } else {
            this.toast(this.lang === "zh" ? "升级失败 — 查看日志" : "Upgrade failed — see log", "error", 5000);
          }
        } else {
          this.toast((this.lang === "zh" ? "请求失败：" : "Request failed: ") + r.status, "error", 4000);
        }
      } catch (e) {
        this.toast((this.lang === "zh" ? "升级出错：" : "Upgrade error: ") + e.message, "error", 5000);
      } finally {
        this.settings.upgradeRunning = false;
      }
    },

    async saveSettings() {
      const body = {
        default_model: this.settings.draftDefaults.model,
        default_permission: this.settings.draftDefaults.permission,
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
        if (r2.ok) {
          this.availableModels = (await r2.json()).models || [];
          this._rebindModelSelect();
        }
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
      // Persist preview-pane state so a refresh restores tabs + selected.
      this.savePrefs();
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
      else if (["xlsx", "xlsm", "xltx", "xltm"].includes(ext)) {
        // xlsx preview: backend serializes the workbook into capped per-sheet
        // string matrices so the frontend just renders <table>s. No formula
        // evaluation; cells show the last-cached value.
        const r = await fetch("/api/files/xlsx?path=" + encodeURIComponent(n.path),
                              { headers: this.hdr() });
        if (r.ok) {
          const data = await r.json();
          this.previewMode = "xlsx";
          this.xlsxSheets = data.sheets || [];
          this.xlsxActive = (this.xlsxSheets[0] && this.xlsxSheets[0].name) || "";
          this.xlsxLimits = data.limits || null;
          this.xlsxSheetsTruncated = !!data.sheets_truncated;
        } else {
          this.previewMode = "unsupported";
        }
      }
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
      await this.revealInTree(path);
    },
    async revealInTree(path) {
      // Expand every ancestor directory so the file's row exists in `visible`,
      // then scroll its <li> into view. Skips no-op when the file is already
      // visible (`scrollIntoView({block:"nearest"})` won't scroll if visible).
      if (!path) return;
      const parts = path.split("/");
      parts.pop();   // drop the filename, keep only directory chain
      const dirPath = parts.join("/");
      if (dirPath) await this.expandPath(dirPath);
      this.$nextTick(() => {
        const el = document.querySelector(
          `.filelist li[data-path="${(window.CSS && CSS.escape) ? CSS.escape(path) : path}"]`);
        if (el) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
      });
    },
    closeTab(path) {
      const idx = this.tabs.findIndex(t => t.path === path);
      if (idx < 0) return;
      this.tabs.splice(idx, 1);
      if (this.selected === path) {
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
          return;   // openByPath → openFile → savePrefs runs there
        }
      }
      this.savePrefs();
    },

    rawUrl(p) {
      const v = this.previewVersion ? `&_v=${this.previewVersion}` : "";
      return "/api/files/raw?path=" + encodeURIComponent(p)
              + "&token=" + encodeURIComponent(this.token) + v;
    },
    async reloadPreview() {
      // Manual "🗘 reload" button in preview header. Bumps previewVersion
      // (cache-buster for iframe / image / pdf rawUrl) AND re-fetches the
      // read endpoint for md / text. Useful when the file changed outside
      // muselab's normal write paths (terminal git pull, external editor).
      if (!this.selected) return;
      this.previewVersion = Date.now();
      if (this.previewMode === "md" || this.previewMode === "text") {
        const url = "/api/files/read?path=" + encodeURIComponent(this.selected)
                     + "&_v=" + this.previewVersion;
        try {
          const r = await fetch(url, { headers: this.hdr() });
          if (r.ok) {
            this.rawText = await r.text();
            if (this.previewMode === "md") {
              this.renderedMd = this.mdRender(this.rawText);
              this.$nextTick(() => this.highlightCode(".markdown"));
            } else {
              this.$nextTick(() => {
                document.querySelectorAll(".text code")
                  .forEach(el => { delete el.dataset.hl; });
                this.highlightCode(".text");
              });
            }
          }
        } catch (_e) { /* network blip */ }
      }
      this.toast(this.lang === "zh" ? "已刷新预览" : "Preview reloaded",
                  "success", 1200);
    },

    async _maybeReloadPreview(toolFilePath) {
      // Called from the tool_use SSE handler when a write-style tool fires.
      // Refresh the preview pane if the tool's target path matches what's
      // currently being previewed. Path matching is by basename + suffix
      // match against this.selected (which may be absolute or ROOT-relative);
      // false-positives across same-named files in different dirs are
      // acceptable (we'd over-refresh, which is harmless) vs missing a real
      // hit by being too strict on path normalization.
      if (!this.selected || !toolFilePath) return;
      const selBase = this.selected.split("/").pop();
      const toolBase = toolFilePath.split("/").pop();
      if (selBase !== toolBase) return;
      // Same basename → cache-bust. For html/img/pdf/iframe the new
      // previewVersion flows through rawUrl on next render; for md/text we
      // also need to re-fetch since rawText is cached in the component.
      this.previewVersion = Date.now();
      if (this.previewMode === "md" || this.previewMode === "text") {
        const url = "/api/files/read?path=" + encodeURIComponent(this.selected)
                     + "&_v=" + this.previewVersion;
        try {
          const r = await fetch(url, { headers: this.hdr() });
          if (r.ok) {
            this.rawText = await r.text();
            if (this.previewMode === "md") {
              this.renderedMd = this.mdRender(this.rawText);
              this.$nextTick(() => this.highlightCode(".markdown"));
            } else {
              this.$nextTick(() => {
                document.querySelectorAll(".text code")
                  .forEach(el => { delete el.dataset.hl; });
                this.highlightCode(".text");
              });
            }
          }
        } catch (e) { /* network blip — manual refresh still possible */ }
      }
    },
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
    // Coarse category string used as a data-ext CSS hook for icon tinting.
    // Keeps the SVG sprite small (no new icons needed) — colors do the
    // disambiguation work.
    fileExtClass(n) {
      if (!n || n.is_dir) return "";
      const name = (n.name || n.path.split("/").pop() || "").toLowerCase();
      const ext = name.includes(".") ? name.split(".").pop() : "";
      if (["md", "markdown", "rst", "txt"].includes(ext)) return "doc";
      if (["py", "ipynb"].includes(ext))                  return "py";
      if (["js", "mjs", "cjs", "jsx"].includes(ext))      return "js";
      if (["ts", "tsx"].includes(ext))                    return "ts";
      if (["go", "rs", "java", "kt", "swift"].includes(ext)) return "compiled";
      if (["c", "cpp", "h", "hpp", "cc", "cxx"].includes(ext)) return "cstyle";
      if (["sh", "bash", "zsh", "fish"].includes(ext))    return "shell";
      if (["json", "yaml", "yml", "toml", "ini", "conf"].includes(ext)) return "config";
      if (["html", "htm", "css", "scss", "less", "vue", "svelte"].includes(ext)) return "web";
      if (["png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp"].includes(ext)) return "image";
      if (["pdf"].includes(ext))                          return "pdf";
      if (["zip", "tar", "gz", "tgz", "bz2", "7z", "rar"].includes(ext)) return "archive";
      if (["csv", "tsv", "xls", "xlsx"].includes(ext))    return "data";
      if (["mp3", "wav", "ogg", "flac", "m4a"].includes(ext)) return "audio";
      if (["mp4", "mkv", "mov", "webm", "avi"].includes(ext)) return "video";
      if (["sql"].includes(ext))                          return "data";
      if (["log"].includes(ext))                          return "log";
      return "";
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
        // Attach copy button to every <pre> wrapping a code block — only once.
        this._attachCopyBtn(el);
      });
    },

    // Wraps a fenced-code-block <code> with a hover-revealed copy button on
    // its enclosing <pre>. Idempotent (data-copybtn marker).
    _attachCopyBtn(codeEl) {
      const pre = codeEl.parentElement;
      if (!pre || pre.tagName !== "PRE") return;       // inline `code`, skip
      if (pre.dataset.copybtn === "1") return;          // already attached
      pre.dataset.copybtn = "1";
      pre.classList.add("has-copy-btn");
      const btn = document.createElement("button");
      btn.className = "code-copy-btn";
      btn.type = "button";
      const labelCopy = this.lang === "zh" ? "复制" : "Copy";
      const labelOk   = this.lang === "zh" ? "已复制" : "Copied";
      btn.textContent = labelCopy;
      btn.setAttribute("aria-label", labelCopy);
      btn.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        try {
          // textContent strips the hljs <span> tags, gives clean source
          const raw = codeEl.textContent || "";
          if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(raw);
          } else {
            // Fallback for http://localhost where clipboard API needs a permission
            const ta = document.createElement("textarea");
            ta.value = raw; ta.style.position = "fixed"; ta.style.left = "-9999px";
            document.body.appendChild(ta); ta.select();
            document.execCommand("copy"); document.body.removeChild(ta);
          }
          btn.textContent = labelOk;
          btn.classList.add("copied");
          setTimeout(() => { btn.textContent = labelCopy; btn.classList.remove("copied"); }, 1500);
        } catch (e) {
          btn.textContent = this.lang === "zh" ? "失败" : "Failed";
          setTimeout(() => { btn.textContent = labelCopy; }, 1500);
        }
      });
      pre.appendChild(btn);
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
    // computedOpenFilesHeight() removed — auto-fit now relies on CSS
    // (.open-files-list max-height + .open-files height: auto). Splitter
    // drag still sets a pixel value, which wins via inline style.
    startOpenFilesResize(ev) {
      // Drag the splitter at the bottom of .open-files to resize. Reuses the
      // same fullscreen overlay trick as the pane resizer so iframe / video
      // children don't eat the mousemove.
      ev.preventDefault();
      const startY = ev.clientY;
      // openFilesHeight now controls the LIST height (not the container).
      // Snapshot the currently-rendered list height so the drag picks up
      // smoothly from wherever CSS auto-fit put it.
      const listEl = ev.currentTarget.parentElement.querySelector(".open-files-list");
      const startH = this.openFilesHeight
                       || (listEl ? listEl.offsetHeight : 100);
      const splitter = ev.currentTarget;
      splitter.classList.add("active");
      document.body.style.cursor = "ns-resize";
      document.body.style.userSelect = "none";
      const overlay = document.createElement("div");
      overlay.style.cssText =
        "position:fixed;inset:0;z-index:99999;cursor:ns-resize;background:transparent;";
      document.body.appendChild(overlay);
      const onMove = (e) => {
        const delta = e.clientY - startY;
        // min ~1 row, max ~70% viewport.
        this.openFilesHeight = Math.max(28, Math.min(window.innerHeight * 0.7, startH + delta));
      };
      const onUp = () => {
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        splitter.classList.remove("active");
        overlay.remove();
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        this.savePrefs();
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },

    startResize(which, ev) {
      ev.preventDefault();
      const startX = ev.clientX;
      const startW = which === "left" ? this.leftWidth : this.rightWidth;
      const target = ev.currentTarget;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      target.classList.add("active");
      // 关键修复：拖动时鼠标经过 HTML 预览的 sandboxed iframe（或其他
      // 嵌入元素）时，mousemove 事件被 iframe 吞掉，分隔条跟不上鼠标，
      // 释放后还会"跳脱"到错位置。覆盖一个全屏透明 overlay 在 iframe
      // 上方接管事件命中区，但不 stopPropagation —— mousemove 仍冒泡
      // 到 document 让 onMove 接收。
      const overlay = document.createElement("div");
      overlay.style.cssText =
        "position:fixed;inset:0;z-index:99999;cursor:col-resize;background:transparent;";
      document.body.appendChild(overlay);
      const onMove = (e) => {
        const delta = which === "left" ? (e.clientX - startX) : (startX - e.clientX);
        const w = Math.max(180, Math.min(700, startW + delta));
        if (which === "left") this.leftWidth = w;
        else this.rightWidth = w;
      };
      const onUp = () => {
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        target.classList.remove("active");
        overlay.remove();
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
        // Bump previewVersion so HTML / PDF / image iframes pick up the new
        // file content. Without this, iframes keep showing the stale render
        // (browser disk cache + same URL) until the user hard-refreshes —
        // the issue was visible when editing a html report styled in dark
        // mode to light mode: editor saved, preview iframe still showed dark.
        this.previewVersion = Date.now();
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
      // 撑高到内容 + 上限（避免无限增长把 chat 区挤没）。max 与 .chat-input
      // textarea CSS 的 max-height 保持一致 (200px)。
      ta.style.height = "auto";
      const max = 200;
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
          const oldId = this.currentId;
          await fetch(`/api/chat/reset?token=${encodeURIComponent(this.token)}&session_id=${encodeURIComponent(oldId)}`,
                       { method: "POST" });
          await fetch(`/api/chat/sessions/${oldId}`, { method: "DELETE", headers: this.hdr() });
          await this.refreshSessions();
          // Drop the old session's tab + cached state, then open a fresh one
          // in its slot. newSession() handles tabState + openTabIds + switch.
          const oldStreamState = this.tabState[oldId];
          if (oldStreamState) {
            if (oldStreamState.es) { try { oldStreamState.es.close(); } catch {} }
            if (oldStreamState._streamTimer) clearInterval(oldStreamState._streamTimer);
            delete this.tabState[oldId];
          }
          this.openTabIds = this.openTabIds.filter(x => x !== oldId);
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
            `- ${this.t("cost.context")}: ${((this.sessionUsage.context_used || this.sessionUsage.input_tokens || 0)/1000).toFixed(1)}K / ${(this.sessionUsage.context_limit/1000).toFixed(0)}K (${this.sessionUsage.context_used_pct}%)`,
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
    async openMyArchiveFile() {
      // Quick shortcut from Settings to open ROOT/CLAUDE.md for editing.
      // If it doesn't exist, prompt the user (a click-through into the help
      // dialog is friendlier than silently creating an empty file).
      this.settings.show = false;
      const path = "CLAUDE.md";
      const { ok } = await this.api(
        "/api/files/read?path=" + encodeURIComponent(path));
      if (!ok) {
        this.openClaudeMdHelp();
        return;
      }
      await this.openByPath(path);
      this.$nextTick(() => { if (this.isEditable(path)) this.toggleEdit(); });
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
             `${(u.context_used || u.input_tokens || 0).toLocaleString()} / ${u.context_limit.toLocaleString()} tokens (${u.context_used_pct}%)\n\n` +
             `${this.t("ctx.tip_line2")}`;
    },
    ctxMeterLabel() {
      const limit = this.sessionUsage.context_limit || 0;
      // Pre-fetch state — backend hasn't told us the real limit yet.
      // Show a placeholder rather than rendering "0K / 0K · NaN%".
      if (!limit) return this.lang === "zh" ? "上下文 …" : "Context …";
      const pct = this.sessionUsage.context_used_pct || 0;
      const usedTokens = (this.sessionUsage.context_used != null)
        ? this.sessionUsage.context_used
        : (this.sessionUsage.input_tokens || 0)
          + (this.sessionUsage.cache_read_tokens || 0)
          + (this.sessionUsage.cache_creation_tokens || 0);
      const cachedTokens = (this.sessionUsage.cache_read_tokens || 0)
                         + (this.sessionUsage.cache_creation_tokens || 0);
      const usedK = (usedTokens / 1000).toFixed(1);
      const cachedK = (cachedTokens / 1000).toFixed(1);
      const limitK = (limit / 1000).toFixed(0);
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
      if (this.streaming) {
        this.toast(this.t("ctx.compact_wait_streaming"), "warn", 2500);
        return;
      }
      // Empty-session guard. Frontend `messages` may be transiently empty
      // right after a page refresh (loadSession is still running async),
      // so fall back to the session metadata's message_count from the
      // backend — that survives reloads.
      const hasFrontendContent = this.messages.some(
        m => m.role === "assistant" && m.text);
      const meta = this.sessions.find(s => s.id === this.currentId);
      const backendCount = (meta && meta.message_count) || 0;
      if (!hasFrontendContent && backendCount < 2) {
        this.toast(this.t("ctx.compact_empty"), "warn", 2500);
        return;
      }

      // Native compact: send "/compact" to CLI via SDK, which writes
      // compact_boundary + isCompactSummary to the session JSONL. Lossless,
      // preserves tool use history, same session ID. Old self-implemented
      // summarize-and-fork is gone — it was lossy and unnecessary once we
      // realized the SDK forwards slash commands to CLI natively.
      this._compacting = true;
      // ctx-meter shows a persistent pulsing banner while _compacting is true
      // (CSS-only, see .ctx-meter.compacting). A short toast confirms the kick
      // — the long banner is what the user actually watches, not a 2-min toast.
      this.toast(this.lang === "zh" ? "📦 开始压缩..." : "📦 Compacting…", "info", 2000);

      try {
        const r = await fetch(`/api/chat/sessions/${this.currentId}/native-compact`,
                                { method: "POST", headers: this.hdr() });
        if (!r.ok) {
          const txt = await r.text();
          this.toast((this.lang === "zh" ? "压缩失败：" : "Compact failed: ") + txt, "error", 5000);
          return;
        }
        // Reload current session so the new compact-marker shows up.
        await this.loadSession(this.currentId);
        await this.refreshSessions();
        // Refresh ctx-meter — sessionUsage is only auto-updated on stream
        // 'done' events, so without this the meter shows the pre-compact
        // (large) value until the user sends a new message.
        await this._refreshCtxMeter();
        this.toast(this.lang === "zh" ? "📦 压缩完成" : "📦 Compacted", "success", 2000);
      } catch (e) {
        this.toast((this.lang === "zh" ? "压缩失败：" : "Compact failed: ") + e.message, "error", 5000);
      } finally {
        this._compacting = false;
        // Queued messages: ask the user before flushing. Auto-sending all
        // queued messages was surprising — the user could end up with 3
        // unexpected user-bubbles when they thought compact would let them
        // re-evaluate context first.
        if (this._compactQueue.length > 0) {
          const n = this._compactQueue.length;
          const previewLines = this._compactQueue
            .map((q, i) => `${i + 1}. ${(q.text || "").slice(0, 50)}`)
            .join("\n");
          const ok = await this.confirm({
            title: this.lang === "zh"
              ? `压缩期间排队了 ${n} 条消息`
              : `${n} message(s) queued during compact`,
            body: this.lang === "zh"
              ? `要依次发送吗？取消会全部丢弃。\n\n${previewLines}`
              : `Send them sequentially? Cancel discards all.\n\n${previewLines}`,
            okText: this.lang === "zh" ? `发送 ${n} 条` : `Send ${n}`,
            cancelText: this.lang === "zh" ? "丢弃" : "Discard",
          });
          if (ok) {
            while (this._compactQueue.length > 0) {
              const q = this._compactQueue.shift();
              this.input = q.text;
              this.pendingImages = q.pendingImages || [];
              this.pendingDocs = q.pendingDocs || [];
              await this.send();
            }
          } else {
            this._compactQueue.length = 0;
          }
        }
      }
    },

    onChatArrowUp(ev) {
      // 1. If a mention/slash popup is open, ↑ navigates it (preserves
      //    the prior keymap).
      if (this.mentionShow || this.slashShow) {
        this._navPop(-1);
        ev.preventDefault();
        return;
      }
      // 2. Empty input → recall the most recent user message so the user
      //    can edit + re-send (Slack/Cursor/iTerm/zsh style).
      if (!this.input.trim()) {
        const msgs = this.messages || [];
        for (let i = msgs.length - 1; i >= 0; i--) {
          const m = msgs[i];
          if (m && m.role === "user" && m.text) {
            this.input = m.text;
            ev.preventDefault();
            this.$nextTick(() => {
              const ta = this.$refs.chatInput;
              if (ta) {
                ta.focus();
                const len = this.input.length;
                ta.setSelectionRange(len, len);
                this.autoGrow(ta);
              }
            });
            return;
          }
        }
      }
      // Otherwise let the browser handle ↑ (cursor up inside textarea).
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
      // Mobile / touch screens: Enter inserts a newline; send is via the
      // button only. Desktop users get the keyboard-first flow.
      const isTouch = (window.matchMedia
                        && window.matchMedia("(pointer: coarse)").matches)
                      || window.innerWidth < 768;
      if (isTouch) {
        const ta = this.$refs.chatInput;
        if (ta) {
          const s = ta.selectionStart, e = ta.selectionEnd;
          this.input = this.input.slice(0, s) + "\n" + this.input.slice(e);
          this.$nextTick(() => {
            ta.setSelectionRange(s + 1, s + 1);
            this.autoGrow(ta);
          });
        } else {
          this.input += "\n";
        }
        return;
      }
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
      // Compact in progress → queue the message, drained when compact finishes.
      // Users keep typing & sending without waiting; the message stays out of
      // the visible transcript until it's actually dispatched (avoids the
      // "sent but no response" UX where the bubble sits there for 90 s).
      if (this._compacting) {
        this._compactQueue.push({
          text,
          pendingImages: this.pendingImages.slice(),
          pendingDocs: this.pendingDocs.slice(),
        });
        this.pendingImages = [];
        this.pendingDocs = [];
        this.input = "";
        this.$nextTick(() => { if (this.$refs.chatInput) this.autoGrow(this.$refs.chatInput); });
        this.toast(this.lang === "zh"
                    ? `📦 压缩中，消息已排队（${this._compactQueue.length}）`
                    : `📦 Compact in progress, message queued (${this._compactQueue.length})`,
                    "info", 2200);
        return;
      }
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
      // 发送后 textarea 重置高度 + 重新 focus (支持连续发送，免得用户手动点回输入框)
      this.$nextTick(() => {
        const ta = this.$refs.chatInput;
        if (ta) { this.autoGrow(ta); ta.focus(); }
      });
      this.mentionShow = false;
      // Capture per-stream tab state once at send time. All event handlers
      // below write to streamState (the ORIGIN tab) instead of `this`, so the
      // stream keeps targeting the right tab even if the user switches to a
      // different tab mid-reply. Root state (`this.streaming` etc.) is only
      // mirrored when the origin tab is still the active one.
      const streamSid = this.currentId;
      const streamState = this._ensureTabState(streamSid);

      streamState.streaming = true; this.streaming = true;
      streamState.streamingModel = this.model;
      this.streamingModel = this.model;   // 锁定 — pending bubble 用它，不跟着 dropdown
      // Tick elapsed time so user sees "thinking · 3.2s" when first token is slow.
      streamState._streamStartedAt = Date.now();
      this._streamStartedAt = streamState._streamStartedAt;
      streamState.streamElapsed = 0; this.streamElapsed = 0;
      if (streamState._streamTimer) clearInterval(streamState._streamTimer);
      streamState._streamTimer = setInterval(() => {
        const elapsed = (Date.now() - streamState._streamStartedAt) / 1000;
        streamState.streamElapsed = elapsed;
        if (this.currentId === streamSid) this.streamElapsed = elapsed;
      }, 200);
      this._streamTimer = streamState._streamTimer;
      this.atBottom = true;
      this.scrollToBottom(true);

      const url = "/api/chat/stream"
        + "?prompt=" + encodeURIComponent(text)
        + "&session_id=" + encodeURIComponent(streamSid)
        + "&model=" + encodeURIComponent(this.model)
        + "&permission=" + encodeURIComponent(this.permission)
        + "&show_thinking=" + (this.showThinking ? "true" : "false")
        + (attachIds.length ? "&image_ids=" + encodeURIComponent(attachIds.join(",")) : "")
        + "&token=" + encodeURIComponent(this.token);
      const es = new EventSource(url);
      streamState.es = es; this.es = es;

      // Active assistant bubble pointer. Text events open / extend it; tool /
      // thinking events close it so subsequent text starts a fresh bubble.
      // curBubble is a direct OBJECT reference — survives tab switches because
      // it lives inside streamState.messages (same array regardless of which
      // tab is active).
      let curBubble = null;
      let acc = "";
      const modelForBubble = this.model;
      // Scroll only if the active tab is the one receiving the stream;
      // otherwise we'd yank the user away from whatever they're reading.
      const _scrollIfActive = () => {
        if (this.currentId === streamSid) this.scrollToBottom(false);
      };
      const openAsst = () => {
        if (curBubble) return;
        const bubble = { role: "assistant", text: "", html: "", cost: "", model: modelForBubble };
        streamState.messages.push(bubble);
        curBubble = bubble;
        acc = "";
      };
      // Throttle markdown rendering during fast token streams. mdRender is
      // O(content length) and re-runs on every chunk; on long replies that's
      // hundreds of full re-parses. 80ms cap keeps UI smooth while still
      // feeling realtime. flushRender() forces a final render on close/done.
      const RENDER_MIN_INTERVAL = 80;
      let lastRender = 0;
      let pendingTimer = null;
      const renderNow = () => {
        if (!curBubble) { pendingTimer = null; return; }
        curBubble.html = this.mdRender(acc);
        lastRender = Date.now();
        pendingTimer = null;
      };
      const scheduleRender = () => {
        const since = Date.now() - lastRender;
        if (since >= RENDER_MIN_INTERVAL) {
          renderNow();
        } else if (!pendingTimer) {
          pendingTimer = setTimeout(renderNow, RENDER_MIN_INTERVAL - since);
        }
      };
      const flushRender = () => {
        if (pendingTimer) { clearTimeout(pendingTimer); pendingTimer = null; }
        if (curBubble) renderNow();
      };
      const closeAsst = () => { flushRender(); curBubble = null; acc = ""; };

      es.addEventListener("text", ev => {
        const d = JSON.parse(ev.data);
        openAsst();
        acc += d.text;
        curBubble.text = acc;
        scheduleRender();
        _scrollIfActive();
      });
      es.addEventListener("thinking", ev => {
        closeAsst();
        const d = JSON.parse(ev.data);
        // Backend yields one SSE event per thinking_delta. Coalesce them
        // into the most recent thinking message so we see ONE block per
        // reasoning segment, not N tiny ones. If the tail isn't a thinking
        // message (e.g. previous was tool_use), start a new one.
        const msgs = streamState.messages;
        const last = msgs[msgs.length - 1];
        if (last && last.role === "thinking") {
          last.text = (last.text || "") + (d.text || "");
        } else {
          msgs.push({ role: "thinking", text: d.text || "" });
        }
        _scrollIfActive();
      });
      es.addEventListener("tool_use", ev => {
        closeAsst();
        const d = JSON.parse(ev.data);
        const msg = { role: "tool_use", name: d.name, summary: d.summary, input: d.input };
        if (d.todos != null) msg.todos = d.todos;
        if (d.task != null) msg.task = d.task;
        if (d.plan != null) msg.plan = d.plan;
        streamState.messages.push(msg);
        // File-mutating tools invalidate any open preview of the same file.
        // Bump previewVersion → rawUrl picks up a new ?_v= → iframe reloads;
        // _reloadPreviewIfDirty re-fetches md/text contents inline.
        if (["Edit", "Write", "MultiEdit", "NotebookEdit"].includes(d.name)) {
          const fp = (d.input && (d.input.file_path
                                    || d.input.notebook_path)) || "";
          this._maybeReloadPreview(fp);
        }
        _scrollIfActive();
      });
      es.addEventListener("tool_result", ev => {
        const d = JSON.parse(ev.data);
        streamState.messages.push({
          role: "tool_result", preview: d.preview, truncated: d.truncated, is_error: d.is_error,
        });
        _scrollIfActive();
      });
      es.addEventListener("ask_user_question", ev => {
        closeAsst();
        const d = JSON.parse(ev.data);
        streamState.messages.push({
          role: "ask_user_question",
          id: d.id,
          questions: d.questions,
          pendingAnswers: {},
          submitted: false,
        });
        _scrollIfActive();
      });
      es.addEventListener("permission_request", ev => {
        closeAsst();
        const d = JSON.parse(ev.data);
        streamState.messages.push({
          role: "permission_request",
          id: d.id,
          tool: d.tool,
          summary: d.summary,
          resolved: false,
          decision: null,
        });
        _scrollIfActive();
      });
      const _stopTimer = () => {
        if (streamState._streamTimer) {
          clearInterval(streamState._streamTimer);
          streamState._streamTimer = null;
        }
        streamState.streamElapsed = 0;
        if (this.currentId === streamSid) {
          this._streamTimer = null;
          this.streamElapsed = 0;
        }
      };
      // Mark the stream done for the ORIGIN tab. If the user is on a
      // different tab, we still update tabState[streamSid] silently — they'll
      // see the final state when they switch back.
      const _markDone = () => {
        streamState.streaming = false;
        streamState.es = null;
        if (this.currentId === streamSid) {
          this.streaming = false;
          this.es = null;
          // textarea was :disabled while streaming → focus during stream was
          // a no-op. Re-focus now so the user can immediately type the next
          // message (supports rapid-fire conversation).
          this.$nextTick(() => {
            const ta = this.$refs.chatInput;
            if (ta && !ta.disabled) ta.focus();
          });
        }
      };
      es.addEventListener("done", ev => {
        flushRender();
        const d = JSON.parse(ev.data);
        if (d.total_cost_usd != null && curBubble) {
          curBubble.cost = "$" + d.total_cost_usd.toFixed(4);
        }
        if (d.stats) this.stats = { ...this.stats, ...d.stats };
        if (d.session_usage) {
          Object.assign(streamState.sessionUsage, d.session_usage);
          if (this.currentId === streamSid) this.sessionUsage = streamState.sessionUsage;
        }
        if (d.budget_usd > 0 && d.budget_used_pct >= 90 && !this._budgetWarned) {
          this._budgetWarned = true;
          this.toast(this.t("cost.budget_warn", { pct: d.budget_used_pct, usd: d.budget_usd }),
                      "warn", 5000);
        }
        es.close(); _markDone(); _stopTimer();
        this.refreshSessions();
        if (this.currentId === streamSid) {
          this.$nextTick(() => this.highlightCode(".chat-body"));
        }
      });
      es.addEventListener("error", ev => {
        flushRender();
        try {
          const d = JSON.parse(ev.data);
          this.toast(this._humanizeStreamError(d.error), "error", 6000);
        } catch {
          this.toast(this.lang === "zh"
                      ? "和 Muse 的连接断开了，重试一下"
                      : "Lost connection to Muse — try again",
                      "error");
        }
        // Mark the latest user bubble in this stream's messages as failed
        // so the UI can render a ↻ retry button next to it.
        for (let i = streamState.messages.length - 1; i >= 0; i--) {
          const m = streamState.messages[i];
          if (m.role === "user") { m._failed = true; break; }
        }
        es.close(); _markDone(); _stopTimer();
      });
      es.addEventListener("cancelled", () => {
        flushRender();
        this.toast("已中断", "warn", 2000);
        es.close(); _markDone(); _stopTimer();
      });
      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) { _markDone(); _stopTimer(); }
      };
    },
    stop() {
      // Stop only the active tab's stream — other tabs keep running their
      // own. Uses tabState[currentId] as the authoritative source for what
      // ES to close. Backend uses SDK's client.interrupt() — keeps the
      // client / CLI subprocess alive so the next message continues the
      // same conversation without reloading CLAUDE.md / MCP / system prompt.
      const sid = this.currentId;
      const st = this._ensureTabState(sid);
      if (st.es) { try { st.es.close(); } catch {} st.es = null; }
      st.streaming = false;
      this.streaming = false; this.es = null;
      if (st._streamTimer) { clearInterval(st._streamTimer); st._streamTimer = null; }
      this._streamTimer = null; this.streamElapsed = 0;
      fetch("/api/chat/interrupt?token=" + encodeURIComponent(this.token)
            + "&session_id=" + encodeURIComponent(sid),
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

    rootDisplay() {
      const root = (this.contextInfo && this.contextInfo.archive_root) || "";
      if (!root) return "—";
      // Best-effort tilde-substitute the user's $HOME prefix (POSIX). The
      // backend doesn't ship HOME so this is heuristic — strip the prefix
      // before the user's name (assumes /home/<user> or /Users/<user>).
      const m = root.match(/^(\/home\/[^/]+|\/Users\/[^/]+)(.*)$/);
      return m ? "~" + m[2] : root;
    },
    copyRootPath() {
      const root = (this.contextInfo && this.contextInfo.archive_root) || "";
      if (!root) return;
      navigator.clipboard?.writeText(root).then(
        () => this.toast(this.t("toast.copied") + ": " + root, "success", 1500),
        () => {});
    },

    async togglePinSession(sid) {
      const s = this.sessions.find(x => x.id === sid);
      if (!s) return;
      const newPinned = !s.pinned;
      // Optimistic UI update so the row jumps to the top instantly.
      s.pinned = newPinned;
      this.sessions = [...this.sessions];   // trigger sort re-render
      const { ok } = await this.api(`/api/chat/sessions/${sid}`, {
        method: "PATCH", json: { pinned: newPinned },
      });
      if (!ok) {
        s.pinned = !newPinned;   // revert
        this.toast(this.lang === "zh" ? "操作失败" : "Failed", "error");
        return;
      }
      await this.refreshSessions();
    },

    openLightbox(src) {
      if (!src) return;
      this.lightbox = { show: true, src };
    },

    retryFailedMessage(m) {
      if (!m || m.role !== "user" || !m._failed) return;
      // Drop the failed bubble, put text back in input, and send.
      const idx = this.messages.indexOf(m);
      if (idx >= 0) this.messages.splice(idx, 1);
      this.input = m.text || "";
      // pendingImages/Docs we don't have here (preview state) — re-prompt
      // user to re-attach if they had files. Acceptable: error retry is rare.
      this.$nextTick(() => {
        const ta = this.$refs.chatInput;
        if (ta) { ta.focus(); this.autoGrow(ta); }
        this.send();
      });
    },

    _humanizeStreamError(raw) {
      // Translate the raw SDK / vendor error strings into something the user
      // can act on. Falls through to the raw text if no pattern matches —
      // better to show something technical than swallow useful info.
      const zh = this.lang === "zh";
      const s = String(raw || "");
      if (/401|unauthorized|invalid.api.key/i.test(s))
        return zh ? "API key 无效，去 Settings 检查" : "Invalid API key — check Settings";
      if (/429|rate.?limit|too many/i.test(s))
        return zh ? "请求频率超限，等几秒再试" : "Rate limit hit — wait a few seconds";
      if (/quota|credit|insufficient.*balance/i.test(s))
        return zh ? "账户额度不足，去 vendor 控制台充值" : "Out of credit — top up at vendor console";
      if (/timeout|timed out/i.test(s))
        return zh ? "请求超时，可能模型在长上下文上忙，重试一下" : "Timed out — retry";
      if (/network|connection|ECONNREFUSED|fetch/i.test(s))
        return zh ? "网络断开，检查连接后重试" : "Network down — check connection";
      if (/context.*length|maximum.*tokens|too long/i.test(s))
        return zh ? "对话太长了，先压缩历史再试" : "Conversation too long — compact then retry";
      if (/ProcessError/i.test(s))
        return zh ? "Claude Code CLI 报错，看后端日志细节" : "CLI subprocess error — check backend logs";
      // Default: prefix + raw, so technical detail isn't lost but framed.
      return (zh ? "Muse 出错：" : "Muse error: ") + s;
    },
    copyMsg(m) {
      const text = m.text || "";
      navigator.clipboard?.writeText(text).then(
        () => {
          this.toast(this.t("toast.copied"), "success", 1500);
          // Inline ✓ feedback on the message — sets a flag the template
          // reads to swap the copy icon to a check for 1.2s. Faster signal
          // than the toast, which appears at the screen edge.
          m._copied = true;
          setTimeout(() => { m._copied = false; }, 1200);
        },
        () => this.toast("复制失败（需要 HTTPS）", "error")
      );
    },

    escape(s) {
      return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    },
  };
}
