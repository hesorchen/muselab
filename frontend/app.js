// ==========================================================================
// Global error capture — runs before anything else so we catch errors that
// happen during boot too (Alpine x-init, vendor scripts, etc).
//
// Why this exists: errors thrown inside Alpine expressions are re-thrown
// through alpine.dev.js:443 (handleError → setTimeout(throw)) which masks
// the real call site. Native fetch failures in Safari surface as the
// opaque "TypeError: Load failed" with no stack pointing at OUR code.
// Catching at window-level gives us the original reason + stack + the
// request URL (when it's a TypeError from fetch we can grab the last
// in-flight URL via the patched fetch below).
//
// Errors are mirrored to:
//   1. console.error (for desktop devtools)
//   2. window.__museErrors__ ring buffer (for "paste this array" diagnosis)
//   3. POST /api/log/client-error (for iOS Safari / no-devtools cases —
//      lands in uvicorn stderr / systemd journal / docker logs)
//
// Dedup: same (message + filename + lineno) within 10s is dropped, so a
// loop that throws every frame doesn't DoS the log endpoint.
// ==========================================================================
(function installErrorCapture() {
  if (typeof window === "undefined") return;
  if (window.__museErrorCaptureInstalled) return;
  window.__museErrorCaptureInstalled = true;

  const RING_MAX = 50;
  const DEDUP_WINDOW_MS = 10_000;
  const ring = window.__museErrors__ = [];
  const seen = new Map(); // sig -> last-ts

  // Last fetch URL/method, captured by the wrapper below. Safari's
  // "TypeError: Load failed" has no info about which request died;
  // pairing the error with the most recent fetch is a strong hint.
  window.__museLastFetch__ = null;

  function _sig(rec) {
    return [rec.kind, rec.message || "", rec.filename || "", rec.lineno || ""].join("|");
  }

  function _report(rec) {
    const sig = _sig(rec);
    const now = Date.now();
    const last = seen.get(sig) || 0;
    if (now - last < DEDUP_WINDOW_MS) return;
    seen.set(sig, now);

    rec.ts = new Date(now).toISOString();
    rec.ua = navigator.userAgent;
    rec.url = location.href;
    rec.lastFetch = window.__museLastFetch__;

    ring.push(rec);
    if (ring.length > RING_MAX) ring.shift();

    try { console.error("[muse-capture]", rec); } catch (_) { /* noop */ }

    // sendBeacon is fire-and-forget, survives page-unload, and Safari
    // supports it. Falls back to fetch+keepalive if sendBeacon refuses
    // the blob (rare; some Safari versions reject non-form blobs).
    try {
      const body = JSON.stringify(rec);
      const ok = navigator.sendBeacon &&
                 navigator.sendBeacon("/api/log/client-error",
                                      new Blob([body], { type: "application/json" }));
      if (!ok) {
        fetch("/api/log/client-error", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body, keepalive: true,
        }).catch(() => { /* swallow — don't recurse into the handler */ });
      }
    } catch (_) { /* never let logging break the app */ }
  }

  window.addEventListener("unhandledrejection", (ev) => {
    const r = ev.reason;
    _report({
      kind: "unhandledrejection",
      message: (r && (r.message || String(r))) || "(no reason)",
      name: r && r.name,
      stack: r && r.stack,
      filename: "", lineno: 0, colno: 0,
    });
  });

  window.addEventListener("error", (ev) => {
    // ev.error is null for cross-origin script errors and for resource-
    // load failures (<img> / <script> 404). For resource failures we
    // still want a record — ev.target gives the element.
    if (!ev.error && ev.target && ev.target !== window) {
      const t = ev.target;
      _report({
        kind: "resource",
        message: "resource load failed",
        tagName: t.tagName,
        src: t.src || t.href || "",
        filename: "", lineno: 0, colno: 0,
      });
      return;
    }
    const err = ev.error;
    _report({
      kind: "error",
      message: ev.message || (err && err.message) || "(no message)",
      name: err && err.name,
      stack: err && err.stack,
      filename: ev.filename || "",
      lineno: ev.lineno || 0,
      colno: ev.colno || 0,
    });
  }, true /* capture phase — catches <script> load failures too */);

  // Wrap fetch to remember the last URL/method. When the unhandled
  // rejection later fires "Load failed" with no body, we can correlate.
  // Keep the wrapper minimal — no retries, no body capture, just a tag.
  const _origFetch = window.fetch;
  if (typeof _origFetch === "function") {
    window.fetch = function (input, init) {
      try {
        const url = (typeof input === "string") ? input
                  : (input && input.url) ? input.url : String(input);
        const method = (init && init.method) || (input && input.method) || "GET";
        window.__museLastFetch__ = { url, method, t: Date.now() };
      } catch (_) { /* noop */ }
      return _origFetch.apply(this, arguments);
    };
  }
})();


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

// Per-message memo caches for the expensive tool-result parsers the message
// x-for re-invokes on EVERY reactive re-render (tab switch, every stream
// chunk). Keyed by the RAW message object (via Alpine.raw) so writes don't
// touch reactive props — no re-render loop — and entries GC with the message.
// editDiffOps in particular builds an O(m·n) LCS table; editDiffStats calls it
// again — without memo that's 2+ full diffs per Edit bubble per render.
const _diffOpsCache = new WeakMap();   // raw msg -> ops[]
const _mcpFmtCache  = new WeakMap();   // raw msg -> { kind, value }
const _readLinesCache = new WeakMap(); // raw msg -> { src, lines }
const _searchHitsCache = new WeakMap();// raw msg -> { src, hits }
const _toolMdCache = new WeakMap();    // raw msg -> { src, html }
// Unwrap an Alpine reactive proxy to its stable underlying object so the
// WeakMap key is identity-stable across renders. Falls back to the proxy
// (Alpine v3 caches proxies per target, so even that key is stable).
function _rawMsg(m) {
  try {
    return (window.Alpine && typeof Alpine.raw === "function") ? Alpine.raw(m) : m;
  } catch (_) { return m; }
}

// Module-level constants reused by hot-path helpers below. Hoisted out of the
// methods so we don't reallocate them on every call:
//   _HTML_ESCAPE_MAP — escape() previously built this object literal once per
//   matched char (every user message, every render).
//   _FILE_TOOLS — toolFilePath() previously did `new Set([...])` per call
//   (every tool_use bubble, every render).
const _HTML_ESCAPE_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const _FILE_TOOLS = new Set(["Read", "Edit", "Write", "NotebookEdit", "MultiEdit"]);

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

    // True on mouse/trackpad devices, false on touch. Used to gate
    // HTML5 draggable=true on chat tabs: on iOS/Android, draggable
    // elements eat the first tap (treats it as a drag-prep gesture),
    // forcing the user to tap twice just to switch tabs.
    isPointerDevice: typeof window !== "undefined"
                       && window.matchMedia
                       && window.matchMedia("(hover: hover)").matches,

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
    // Document-level "OS file drag in progress" flag. Set true when the
    // user starts dragging files from Finder / Files / Explorer over
    // any part of the page; reset on drop or when the drag leaves the
    // window. Drives the preview-pane's drop overlay so it can intercept
    // the drop even when an HTML preview iframe or an image element is
    // covering the preview-body (those swallow dragover events from
    // their parent — without a global flag the overlay never appears
    // and the drop falls through to the browser, which opens the file
    // as a navigation away from muselab). See init() for the listeners.
    osFileDragging: false,
    _dragCounter: 0,
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
    // Find-in-preview (magnifier). Searches the rendered DOM text of the
    // md/text preview modes only (html/pdf live in a sandboxed iframe we
    // can't reach; img has no text; xlsx/csv paginate). Case-insensitive,
    // keyword match. `matches` holds serializable {snippet} for the results
    // list; the parallel live <mark> elements are kept off-reactive in
    // _pfEls so Vue/Alpine never proxies DOM nodes.
    previewFind: { open: false, query: "", matches: [], active: -1, count: 0, listOpen: false },
    _pfEls: [],
    // xlsx preview state. previewMode==='xlsx' uses xlsxSheets (array of
    // {name, rows, rows_truncated, cols_truncated}). xlsxActive picks the
    // sheet tab. xlsxLimits carries the server's row/col caps for the UI
    // hint when truncation happens.
    xlsxSheets: [], xlsxActive: "", xlsxLimits: null, xlsxSheetsTruncated: false,
    // CSV preview — paginated, one window at a time. csvData holds the
    // backend response for the current page; csvOffset advances by limit
    // when the user pages forward.
    csvPath: "", csvData: null, csvOffset: 0, csvLimit: 200, csvLoading: false,
    // Bumped whenever an assistant tool_use edits a file. Used as a cache
    // buster on iframe / read URLs so the preview reflects the new content
    // without the user needing to manually refresh the page.
    previewVersion: 0,
    // Compact orchestration: the per-tab `compacting` flag (see
    // _blankTabState) marks the window where the CLI is busy summarising
    // *that session's* history. User messages typed during compact go into
    // the same per-session pendingQueue as messages typed during a streaming
    // turn — both paths are drained by _drainPendingQueue(sid). Per-tab so
    // a compact on session A doesn't show "📦 压缩中…" on every other tab
    // (regression from when _compacting was global — fixed 2026-05-22).
    // SDK get_context_usage() breakdown popup. Shows per-category token
    // counts (system prompt / tools / memory files / messages / mcp / skills)
    // so the user can see which slice is using their context window.
    ctxBreakdown: { show: false, loading: false, data: null, error: "" },
    // Per-category expansion state inside the breakdown popup. Keyed by
    // category name from SDK; only categories that map to a sub-list
    // (memoryFiles / mcpTools / agents) actually expand.
    ctxExpanded: {},
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
    // Gates revealing a freshly-loaded session: stays false from load-start
    // until syntax-highlight + artifacts finish, so the whole conversation
    // appears at once instead of janking in code-block-by-code-block (the
    // skeleton covers the gap). Default true so empty/streaming views aren't
    // hidden. Only loadSession flips it false→true.
    messagesReady: true,

    // ===== scheduled tasks (bell drawer) =====
    // Daily-fire prompts that dispatch into a dedicated muselab session.
    // The bell icon in the top bar shows unread_count from the server;
    // opening the drawer ack-zeros it. Drafts and tasks both live here
    // so the modal stays self-contained.
    scheduler: {
      show: false,
      tasks: [],
      history: [],
      unreadCount: 0,
      loading: false,
      // Per-task expansion state for the "show runs" affordance.
      // Keyed by task id → { open: bool, runs: [...], loading: bool }.
      // Only populated for tasks the user clicks to expand; lazy fetch.
      taskRuns: {},
      // Inline-create form state. Polymorphic — only the fields
      // matching `kind` get sent to the backend. Reset by _resetSchedDraft.
      // editingId: when non-null, saveSchedTask() PATCHes that task
      // instead of POSTing a new one. The same form serves both flows.
      draft: {
        editingId: null,
        name: "", prompt: "", model: "",
        kind: "daily",         // daily / weekly / monthly / once
        // times: list of {hour, minute}. Always length >= 1. For daily, the
        // user can add multiple slots ("每天 08:00 / 14:00 / 22:00"); other
        // kinds use times[0] as the single fire time. Replaced the old
        // top-level hour/minute pair to keep a single source of truth.
        times: [{ hour: 9, minute: 0 }],
        weekdays: [1, 2, 3, 4, 5],  // weekly: Mon-Fri default
        day: 1,                // monthly day-of-month
        onceDate: "",          // once: "YYYY-MM-DD"
        // 2026-05-28: "fresh" each run gets a new session; "reuse" all
        // runs append to one session bound at task creation.
        session_mode: "fresh",
      },
    },
    // Per-task "run-now" inflight flag — disables retry / send buttons
    // until the LLM call returns and history is reloaded. Keyed by task id.
    schedRunning: {},
    // Single notification switch — collapsed from 4 toggles (vibrate /
    // push / notify_scheduled / notify_normal) on 2026-05-28 because the
    // user-facing intent was always just "tell me when something finishes".
    // Behavior when ON, best-effort across capabilities:
    //   - foreground: navigator.vibrate on unread tick-up (any device with
    //     a vibration motor — handheld; PC silently no-ops)
    //   - background: Web Push subscription via service worker, gated by
    //     server-side presence.recently_active() so we never double-notify
    //     a user who's actively at a screen
    // Storage: localStorage `muselab_notify_enabled`. Migration from the
    // old 2-key shape lives in loadNotifyPrefs().
    notifyEnabled: false,
    // Last-seen unread, used to detect a tick-up and trigger vibration.
    _lastSeenUnread: 0,

    // ===== command palette (Cmd/Ctrl+K) =====
    // Single fuzzy-search dropdown across: quick actions, open sessions,
    // and any file under the archive root (via /api/files/search). Action
    // items have a `run` closure; selecting an item fires it and closes
    // the palette.
    palette: {
      show: false,
      query: "",
      activeIndex: 0,
      fileResults: [],   // populated by _fetchPaletteFiles() (server-side)
      fileQuery: "",     // last query the server fetch ran for
      fileLoading: false,
      // Cross-session full-text message search — populated by
      // _fetchPaletteMessages() against /api/chat/search. Lets the
      // palette double as a "find anything I ever said" jump tool.
      messageResults: [],
      messageQuery: "",
      messageLoading: false,
    },
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
    // Reasoning effort override for the current session — "" means let the
    // SDK pick adaptively (the existing default). Persisted on the session
    // via PATCH so each tab keeps its own setting across reloads.
    effort: "",
    // Extended thinking on/off for the current session — default true.
    // NOTE this is NOT the old `showThinking` (which controlled DISPLAY of
    // thinking blocks and was removed 2026-05-20). This toggles the backend
    // thinking CONFIG: false → ThinkingConfigDisabled, so the model emits no
    // thinking blocks at all. It's the escape hatch for the CLI streaming-
    // interleaving 400 ("thinking blocks ... cannot be modified") — a
    // thinking-free session can't produce the interleaved shape that trips
    // the API. Persisted per-session via PATCH {thinking}; triggers a client
    // rebuild on next turn.
    thinkingEnabled: true,
    // Always render thinking blocks (when they exist). Display toggle removed
    // 2026-05-19/05-20 — adaptive thinking was causing invisible mid-reply
    // stalls when hidden; thinking display is now unconditional (see comment
    // in chat.py near ThinkingConfigEnabled).
    input: "", streaming: false, es: null,
    // 锁定当前在跑的那条请求用的模型——dropdown 切到别的，pending bubble 不能跟着变。
    streamingModel: "",
    // Elapsed seconds since send() — pending bubble shows it after 1s so the
    // user knows the system isn't stuck.
    streamElapsed: 0,
    _streamTimer: null,
    _streamStartedAt: 0,
    pendingImages: [],    // [{id, mime, preview (data URL), uploading, error, file}]
    pendingDocs: [],      // [{id, name, kind: 'pdf'|'text', uploading, error}]
    // Image annotation editor (L1: pen / rect / arrow / eraser + 5 colors + 3 sizes).
    // Opened via the ✎ button on an .img-chip. State is module-scoped on `this`
    // so the modal template can read it via x-show / :class. _baseBitmap is the
    // pristine ImageBitmap decoded once on open — kept around for the eraser
    // tool, which "erases" by re-drawing original pixels from this bitmap into
    // a circular clip region (so annotations vanish but image content beneath
    // is restored — better than the naive "paint with white" approach).
    // history[] caps at 15 dataURL JPEG-70% frames (~100 KB each, ~1.5 MB total).
    imageEditor: {
      show: false,
      entryIndex: -1,
      tool: "pen",           // pen / rect / arrow / eraser
      color: "#ef4444",      // red default — most common for "look at this"
      size: 6,
      history: [],           // dataURL strings, oldest first
      historyIdx: -1,        // pointer into history; -1 = empty
      _drawing: false,
      _startX: 0, _startY: 0,
      _snapshot: null,       // ImageData captured at pointerdown (for rect/arrow live preview)
      _baseBitmap: null,     // original ImageBitmap — used by eraser
    },
    // Flipped true inside sendMessage when the user clicks send while an
    // attachment upload is still in flight. Disables the send button so a
    // double-click can't enqueue two sends. Auto-resets when the wait
    // resolves (usually < 1 s; 30 s hard timeout).
    _sendWaitingForUpload: false,
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
    //
    // Each muse has TWO conversation-opener strings, deliberately split
    // by perspective (the previous single-string design conflated them
    // and produced grammatically wrong fills — "聊聊你的..." in the
    // user's input box reads as the user asking Muse to talk about
    // herself):
    //   - invite:  Muse → user (card preview). "讲讲你的..." / "聊聊你..."
    //   - prompt:  user → Muse (prefilled into chat input on click).
    //              "我想聊聊..." — first-person, statement (not question)
    //              so user can hit Enter immediately or tweak before sending.
    MASCOTS: [
      { id: "hex",      greek: "Calliope",    zhName: "卡利俄佩",       domain: { zh: "史诗", en: "Epic poetry" },
        invite: { zh: "讲讲你的大故事——这一年你最在意的三件事是什么?",
                  en: "Tell me the big story — what are the 3 things you care most about this year?" },
        prompt: { zh: "我想和你聊聊这一年我最在意的三件事",
                  en: "I want to talk through the 3 things I care most about this year" } },
      { id: "bars",     greek: "Clio",        zhName: "克利俄",         domain: { zh: "历史", en: "History" },
        invite: { zh: "整理一下你的时间线——过去半年最关键的变化是什么?",
                  en: "Walk me through your timeline — what changed most in the last six months?" },
        prompt: { zh: "帮我整理一下过去半年最关键的变化",
                  en: "Help me walk through what changed most in the last six months" } },
      { id: "lens",     greek: "Erato",       zhName: "厄拉托",         domain: { zh: "情诗", en: "Love poetry" },
        invite: { zh: "聊聊你在乎的人——最近谁需要你多一点注意?",
                  en: "Tell me about who matters to you — who needs your attention right now?" },
        prompt: { zh: "想聊聊我在乎的人——感觉最近有谁需要我多一点注意",
                  en: "I want to talk about people who matter to me — someone might need more attention from me right now" } },
      { id: "wave",     greek: "Euterpe",     zhName: "欧忒耳佩",       domain: { zh: "音乐", en: "Music" },
        invite: { zh: "讲讲你的节奏——最近哪件日常的小事做得最顺?",
                  en: "Talk about your rhythm — what daily thing has been clicking lately?" },
        prompt: { zh: "想聊聊我最近的节奏——哪件日常小事做得最顺",
                  en: "Let me talk about my rhythm lately — a daily thing that's been clicking" } },
      { id: "crescent", greek: "Melpomene",   zhName: "墨尔波墨涅",     domain: { zh: "悲剧", en: "Tragedy" },
        invite: { zh: "聊聊最近的烦恼——什么事让你睡不踏实?",
                  en: "Tell me what's weighing on you — what's been keeping you up?" },
        prompt: { zh: "想聊聊最近的烦恼——有件事让我睡不踏实",
                  en: "Want to talk about what's been weighing on me — something's keeping me up" } },
      { id: "halo",     greek: "Polyhymnia",  zhName: "波吕许谟尼亚",   domain: { zh: "圣诗", en: "Sacred hymns" },
        invite: { zh: "聊聊你的信念——什么事让你觉得「必须做」?",
                  en: "Talk about what you believe in — what feels non-negotiable to you?" },
        prompt: { zh: "想聊聊我的信念——有件事让我觉得「必须做」",
                  en: "Want to talk about what I believe in — something feels non-negotiable to me" } },
      { id: "trio",     greek: "Terpsichore", zhName: "忒耳普西科瑞",   domain: { zh: "舞蹈", en: "Dance" },
        invite: { zh: "讲讲你的身体——最近的状态怎么样?",
                  en: "Tell me about your body — how are you feeling lately?" },
        prompt: { zh: "想聊聊我最近身体的状态",
                  en: "Want to talk about how my body's been feeling lately" } },
      { id: "spark",    greek: "Thalia",      zhName: "塔利亚",         domain: { zh: "喜剧", en: "Comedy" },
        invite: { zh: "来点轻松的——最近有什么有意思的事?",
                  en: "Lighten things up — what's something fun that happened recently?" },
        prompt: { zh: "来点轻松的——最近有件有意思的事想跟你说",
                  en: "Let's lighten things up — something fun happened recently I want to share" } },
      { id: "orbit",    greek: "Urania",      zhName: "乌拉尼亚",       domain: { zh: "天文", en: "Astronomy" },
        invite: { zh: "聊聊你的好奇心——什么大问题最近一直在想?",
                  en: "Talk about what you're curious about — what big question is on your mind?" },
        prompt: { zh: "想聊聊我最近的好奇心——有个大问题一直在脑子里",
                  en: "Want to talk about something I'm curious about — a big question that's been on my mind" } },
    ],
    mascotIdx: 0,
    mascotGreet: false,

    leftOpen: true,
    rightOpen: true,
    leftWidth: 280,
    rightWidth: 440,
    showHidden: false,
    // ===== Trash =====
    // Files /delete moves into <ROOT>/.muselab-dustbin/ instead of unlink
    // (see backend/files.py). The trash UI lives as a docked icon at the
    // bottom of the files pane; click → modal with restore / purge actions.
    // `count` is mirrored from the trash list whenever the modal opens OR
    // an item is deleted (used to badge the trash icon).
    trash: {
      modalOpen: false,
      loading: false,
      items: [],
      count: 0,
    },
    // Toggled by onTrashDragOver/onTrashDragLeave to drive the red-highlight
    // animation on the trash button. Stays out of the trash{} sub-object
    // so it can be touched in dragover handlers (60+ events/sec while
    // dragging) without thrashing the nested reactive proxy.
    dragOverTrash: false,
    // ===== Code Artifacts =====
    // Both Mermaid and HTML artifacts are always on. The HTML path
    // mounts AI-supplied markup in a sandboxed iframe WITHOUT
    // allow-same-origin — the iframe has a null origin and cannot
    // touch muselab's DOM, cookies, token, or localStorage. See the
    // sandbox config in _toggleHtmlIframe for the exact attrs and
    // SECURITY.md (Threat model section) for the threat model.
    _mermaidLoadPromise: null,
    _artifactClickBound: false,
    // Desktop-only "fullscreen" for one pane — "preview" or "chat" (or ""
    // = normal 3-column). Triggered by the maximize button in each pane's
    // header; hides files / the other pane / both resizers via CSS so the
    // remaining pane takes the full viewport. Click the same button again
    // (icon flips to minimize) to exit. No ESC binding because it would
    // collide with every modal's @keydown.escape.window handler — a single
    // ESC press would close the modal AND exit fullscreen. Mobile ignores
    // this entirely — the @media single-pane layout already covers
    // "immersive on phone." Persisted to localStorage (FIX ⑥) so a refresh /
    // PWA reopen restores the exact focus mode the user was in.
    desktopFullPane: "",

    // Mobile: viewport < 900px collapses the 3 panes into a single visible
    // tab. Default "chat" since that's the primary action; auto-switches to
    // "preview" when user opens a file, and "chat" when they @-mention one.
    mobileTab: "chat",
    // Mobile-only: when user scrolls down in preview, hide top header + tab
    // bar + bottom-nav for immersive reading. onPreviewScroll updates this
    // based on direction + threshold. Reset on mobileTab change away from
    // preview (see init() $watch).
    previewImmersive: false,
    _lastPreviewScrollTop: 0,
    // Sidebar "message outline" — opens a collapsible list of user prompts
    // in the current session so you can jump back to a question in a long
    // conversation. Toggled by the outline button in the files pane header.
    msgOutlineOpen: false,

    // ===== @ mention =====
    mentionShow: false, mentionResults: [], mentionIdx: 0, mentionAnchor: -1,

    // ===== toast / modal / ctx menu =====
    toasts: [], _toastId: 0,
    modal: { show: false, title: "", body: "", input: null, confirm: null, cancel: null, okText: "", cancelText: "", danger: false },
    ctxMenu: { show: false, x: 0, y: 0, node: null },
    // File-tree clipboard. Ctrl+C on a selected file (focus outside any
    // input) sets this; Ctrl+V calls /api/files/copy-bak to materialise a
    // .bak in the currently-selected directory (or the source's parent if
    // nothing else is selected). Files only — directories are out of scope.
    // Cleared after a successful paste so the same source doesn't keep
    // duplicating on accidental repeated Ctrl+V.
    fileClipboard: { path: "", name: "" },

    // ===== settings =====
    // Keyboard cheat-sheet modal — toggled by `?` keypress outside any
    // input. Discoverability tool: muselab has 10+ shortcuts and no one
    // reads the README.
    cheatSheet: { show: false },

    settings: {
      show: false,
      providers: [],
      draftKeys: {},
      draftDefaults: { model: "", permission: "" },
      // (Removed 2026-05-28) draftParams — used to carry notify_scheduled /
      // notify_normal server-side toggles. Subscription state is now the
      // sole on/off; see `notifyEnabled` at the top of this object.
      // MCP server list (loaded from /api/settings/mcp)
      mcpServers: [],
      mcpExamples: [],
      // MCP add-form draft. `transport` picks the shape: "stdio" (local
      // subprocess → command/args) or "remote" (http/sse connector → url +
      // optional Authorization header), mirroring Claude app's local-vs-remote
      // connector split.
      mcpDraft: { show: false, transport: "stdio", name: "", command: "", argsStr: "", url: "", authHeader: "" },
      // Provider editor drafts — keyed by stable provider id. Each holds the
      // in-flight edit of a provider's endpoint / prefix / model-list / key,
      // plus an `open` flag toggling the inline editor. Separate from the
      // committed `providers` list so cancel just drops the draft.
      providerDrafts: {},
      // "Add a brand-new provider" form (id minted server-side on save).
      providerNew: { show: false, base_url: "", prefix: "", models: "", api_key: "" },
      skills: [],         // discovered skill list (read-only browse)
      skillFilter: "",    // free-text filter (name / description / source)
      probeResults: {},   // env_key -> {ok, text} from last "Test" click
      // Versions + upgrade — populated by loadVersions(), set by runUpgrade()
      versions: null,
      versionsLoading: false,
      upgradeRunning: false,
      upgradeResult: null,
      restarting: false,    // true while restart is in progress
      // Mobile-only: iOS-style 2-level menu state. null = top-level
      // menu list shown; "lang" / "provider" / ... = that section's
      // detail page shown. Desktop ignores this entirely (every
      // section is always rendered, the menu list is CSS-hidden).
      activePage: null,
    },
    // Cost dashboard state — lazily loaded when the user opens the
    // Settings → Cost section. Lives outside `settings` so it survives
    // settings modal close/open without refetching unless user clicks
    // refresh.
    cost: {
      loading: false,
      data: null,        // null = never loaded; object = last response
      // Per-category visibility filters. User can click the chips at the
      // top of the dashboard to toggle off input / output / cache tokens
      // from every aggregate (KPI cards, day bars, vendor + model rows).
      // Default: everything visible. Not persisted across sessions —
      // resetting to all-visible on reload matches expectation that the
      // dashboard "shows everything by default" each time the user opens
      // Settings. Per user feedback 2026-05-22: 用量统计只要 token 数 +
      // 三类可点击隐藏。
      filters: { input: true, output: true, cache: true },
    },

    // Reactive viewport flag — used by Settings mobile menu to decide
    // whether to honor activePage (mobile) or render everything
    // (desktop). matchMedia listener in init() flips this on rotate /
    // resize so the same DOM works for both modes without reload.
    isWideScreen: typeof window !== "undefined"
                    && window.matchMedia
                    && window.matchMedia("(min-width: 721px)").matches,

    // Session picker dropdown open state (replaces native <select> so each
    // row can have an inline delete button).
    sessionPickerOpen: false,
    // Per-group expand state for the session picker. Keys are group keys
    // ("earlier", "month"); true = user clicked "show all". Reset when the
    // picker closes or when the search query changes (search already shows
    // everything so expansion is irrelevant).
    pickerGroupExpanded: {},
    // Inline rename inside a picker row. Keeps the keyboard popup tied to
    // the original user click (iOS Safari requires synchronous focus()).
    renamingPickerSid: "",
    pickerRenameDraft: "",

    // Per-provider help hints rendered under the API-key input. Anthropic
    // gets the most because it has two valid paths (Pro OAuth or API key);
    // others are just a link to where to get the key.
    // i18n for provider display labels — backend ships a single string
    // (e.g. "百度千帆", "MiniMax (国际)") that's fine for zh users but
    // shows Chinese text in the English UI. Map only the entries that
    // contain CJK; everything else passes through unchanged.
    PROVIDER_DISPLAY_I18N: {
      "百度千帆":         { en: "Baidu Qianfan" },
      "MiniMax (国际)":   { en: "MiniMax (International)" },
      "Qwen (国际)":      { en: "Qwen (International)" },
    },
    localizeProviderDisplay(d) {
      const m = this.PROVIDER_DISPLAY_I18N[d];
      return (m && m[this.lang]) || d;
    },

    PROVIDER_HELP: {
      ANTHROPIC_API_KEY: {
        url: "https://console.anthropic.com/settings/keys",
        zh: "API 按量付费。去 console.anthropic.com 拿 key 填这里。Pro/Max 订阅请用上面的 Claude Auth 卡片。两个都配 → CLI 自动用 Pro,不会重复扣费。",
        en: "Pay-per-use API. Get a key at console.anthropic.com and paste it here. For Pro/Max subscription, use the Claude Auth card above. With both configured, CLI prefers Pro automatically — no double-billing.",
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
      MOONSHOT_API_KEY: {
        url: "https://platform.moonshot.cn/console/api-keys",
        zh: "去 platform.moonshot.cn 控制台创建 API key（Kimi K2 系列走此 key）。",
        en: "Create an API key at platform.moonshot.cn (for Kimi K2 models).",
      },
      DASHSCOPE_API_KEY: {
        url: "https://dashscope.console.aliyun.com/apiKey",
        zh: "去阿里云灵积 DashScope 控制台创建 API key（Qwen 系列走此 key）。注册后在「API-KEY管理」页面生成。",
        en: "Create an API key in the DashScope console at dashscope.console.aliyun.com (for Qwen models). Find it under 「API-KEY管理」after registering.",
      },
      XIAOMI_MIMO_API_KEY: {
        url: "https://platform.xiaomimimo.com",
        zh: "去 platform.xiaomimimo.com 申请 MiMo API 内测资格并创建 key。",
        en: "Apply for MiMo API beta access and create a key at platform.xiaomimimo.com.",
      },
      QIANFAN_API_KEY: {
        url: "https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application/v2",
        zh: "去百度智能云千帆控制台创建应用，获取 API key（ERNIE 系列走此 key）。注意需要 IAM 鉴权，非普通 sk-xxx 格式。",
        en: "Create an app in Baidu Qianfan console to get an API key (for ERNIE models). Note: IAM auth, not plain sk-xxx format.",
      },
    },

    _pendingExpanded: null,

    // ===== Claude Auth (Pro/Max OAuth) — standalone provider =====
    // Treated as its own card in Settings — separate from PROVIDER_HELP /
    // settings.providers because it has no API key field. Auth lives in
    // ~/.claude/.credentials.json (written by `claude login`), identity
    // comes from `claude auth status --json` — both exposed via
    // /api/settings/claude-auth/{status,disconnect}.
    claudeAuth: {
      loaded: false,           // first /status fetch completed
      cli_installed: false,
      cli_path: null,
      credentials_file_present: false,
      logged_in: false,
      email: null,
      org_name: null,
      subscription_type: null,  // "max" / "pro" / "free" / null
      expires_at: null,         // ms-since-epoch
      reason: null,
    },
    // Connect modal state — managed independently from generic confirm()
    // because it has its own polling lifecycle (every 3 sec until logged_in).
    claudeAuthModal: {
      open: false,
      polling: false,
      pollHandle: null,
      copyToast: null,        // which command got copied ("install" | "login")
    },

    // ===== init =====
    onGlobalKeyDown(ev) {
      // ---- Command palette ----
      // Cmd/Ctrl+K from anywhere opens it. While open, palette owns
      // ↑/↓/Enter; everything else falls through to the input.
      if ((ev.ctrlKey || ev.metaKey) && (ev.key === "k" || ev.key === "K")) {
        ev.preventDefault();
        this.openPalette();
        return;
      }
      if (this.palette.show) {
        if (ev.key === "ArrowDown") { ev.preventDefault(); this.paletteMove(1); return; }
        if (ev.key === "ArrowUp")   { ev.preventDefault(); this.paletteMove(-1); return; }
        if (ev.key === "Enter")     { ev.preventDefault(); this.onPaletteEnter(); return; }
        if (ev.key === "Escape")    { ev.preventDefault(); this.closePalette(); return; }
        // Don't return — let typed chars reach the palette input naturally.
      }
      // Ctrl/Cmd+S → 保存（编辑模式下）；Esc → 关 modal/menu/停止流式
      if ((ev.ctrlKey || ev.metaKey) && ev.key === "s") {
        if (this.editing && this.selected) {
          ev.preventDefault();
          this.saveEdit();
        }
        return;
      }
      // Ctrl/Cmd+C / Ctrl/Cmd+V — file-tree "copy as .bak" flow.
      // Strict gating so we don't hijack normal text copy/paste:
      //   1. Focus must NOT be in an input / textarea / contenteditable
      //      (editor, chat input, prompt modal etc.)
      //   2. A file (not a directory) must be selected in the tree.
      //   3. Modifier must be Ctrl/Cmd alone — no shift/alt combos.
      // Anything else falls through to native behaviour.
      if ((ev.ctrlKey || ev.metaKey) && !ev.shiftKey && !ev.altKey
          && (ev.key === "c" || ev.key === "C" || ev.key === "v" || ev.key === "V")) {
        const ae = document.activeElement;
        const tag = ae && ae.tagName;
        const inField = tag === "INPUT" || tag === "TEXTAREA"
                        || (ae && ae.isContentEditable);
        // Don't fight native copy/paste of selected text in any input.
        if (inField) return;
        // Also bail if user has a non-empty text selection — they're
        // probably copying rendered text from the preview pane.
        const sel = window.getSelection && window.getSelection();
        if (sel && sel.toString && sel.toString().length > 0) return;
        const isCopy = ev.key === "c" || ev.key === "C";
        if (isCopy) {
          // Need a selected file (not directory). this.selected holds the
          // path string of the currently-highlighted tree node.
          const node = this._findTreeNode(this.selected);
          if (!node || node.is_dir) return;
          ev.preventDefault();
          this.fileClipboard = { path: node.path, name: node.name };
          this.toast(
            this.t("toast.copy_marked").replace("{name}", node.name),
            "success",
            1800,
          );
        } else {
          // Paste
          if (!this.fileClipboard.path) return;
          ev.preventDefault();
          this.doPasteBak();
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
        if (this.cheatSheet.show) { this.cheatSheet.show = false; return; }
        if (this.mentionShow) { this.mentionShow = false; return; }
        if (this.ctxMenu.show) { this.ctxMenu.show = false; return; }
        if (this.tabCtxMenu) { this.closeTabMenu(); return; }
        if (this.settings.show) { this.settings.show = false; return; }
        if (this.modal.show && this.modal.cancel) { this.modal.cancel(); return; }
        // 退出编辑 — guard against silently discarding unsaved edits when ESC
        // is pressed out of habit (blur the focus). Only confirm when dirty.
        if (this.editing) { if (this._confirmLoseEdits()) this.editing = false; return; }
        if (this.streaming) { this.stop(); return; }          // 停止流式
      }
      // `?` shows keyboard cheat-sheet — only when nothing has focus
      // (we don't want to swallow it inside the chat textarea or any
      // settings input). Don't fire on modifiers; Shift+/ alone = `?`.
      if (ev.key === "?" && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
        const ae = document.activeElement;
        const tag = ae && ae.tagName;
        const inField = tag === "INPUT" || tag === "TEXTAREA"
                        || (ae && ae.isContentEditable);
        if (!inField) {
          ev.preventDefault();
          this.cheatSheet.show = true;
        }
      }
    },

    init() {
      // Idempotency guard. Alpine 3 already auto-invokes init() when x-data
      // resolves; if anything (a stray x-init="init()", a hot-reload tool,
      // a future Alpine bump that double-fires) triggers a second call,
      // every event listener / heartbeat / interrupted-turn toast would
      // double. Cheap to gate at the front; expensive to debug after.
      if (this._initialized) return;
      this._initialized = true;
      // 全局快捷键（绑在 document，避免每个 textarea 单独处理）
      document.addEventListener("keydown", e => this.onGlobalKeyDown(e));
      // (Cross-tab queue sync via localStorage `storage` events was removed
      // when the queue moved server-side: there's one authoritative copy now,
      // and each tab refreshes its mirror via _syncQueueFromServer on load /
      // tab-activate / after a turn — no per-tab localStorage to reconcile.)
      // 一次性迁移旧 localStorage key（portal_* → muselab_*），让现有用户无感升级
      for (const [oldK, newK] of [
        ["portal_token", "muselab_token"],
        ["portal_prefs", "muselab_prefs"],
        ["portal_theme", "muselab_theme"],
        ["portal_chat", "muselab_chat"],
      ]) {
        const v = localStorage.getItem(oldK);
        if (v != null && localStorage.getItem(newK) == null) {
          this._setLS(newK, v);
        }
        localStorage.removeItem(oldK);
      }
      this.initTheme();
      this.initLang();
      this.initMascot();
      this.configureMarked();
      this._initArtifacts();
      this._initStreamSelectionGuard();
      this._initAriaLabelMirror();
      // NOTE: loadTrash() does NOT run here — init() executes before the
      // user has supplied a token (token gating happens in _bootApp /
      // login). Calling it here produced a 401 spam in the network tab.
      // Trash is loaded in _bootApp (saved-token boot) + login() (first
      // login), and lazily on openTrashModal as a backstop.
      this._initMobileKeyboardWatch();
      // PTR (pull-to-refresh) helper exposed for x-init use on scroll
      // containers. Mobile only — no-op on devices without touch.
      // ============================================================
      // Auto-scroll the chat-tabs strip to the active tab whenever
      // currentId changes — covers newSession, openTab from history
      // picker, slash /resume, etc. without requiring each entry point
      // to remember to call _scrollTabIntoView.
      this.$watch("currentId", (tid) => {
        if (!tid) return;
        this.$nextTick(() => this._scrollTabIntoView(tid));
        // Persist the current tab to server so other devices follow.
        // Goes through the same debounced ui-state push that openTabIds /
        // previewTabs use (last_session_id carries currentId) — keeps
        // payload coherent and avoids two PUT races overwriting each other.
        this._pushUIStateSoon();
      });
      // Leaving preview tab on mobile cancels immersive mode so the next
      // tab is rendered with its bars visible. Also persists the choice so
      // closing + reopening the PWA lands the user back on the same tab —
      // not always "preview" (which is what the previewSelected restore
      // would otherwise force).
      this.$watch("mobileTab", (t) => {
        if (t !== "preview" && this.previewImmersive) {
          this.previewImmersive = false;
          document.body.classList.remove("preview-immersive");
        }
        this.savePrefs();
      });

      // ============================================================
      // Document-level OS-file-drag detection
      // ------------------------------------------------------------
      // Tracks whether the user is currently dragging a file from the
      // OS into the muselab window. Drives `osFileDragging` so the
      // preview pane's drop overlay can render with pointer-events:auto
      // and intercept the drop even when an HTML preview iframe is
      // covering the preview-body. (Iframes / cross-origin embeds
      // swallow drag events; without this overlay-on-top approach the
      // drop falls through to the browser default — "open this file"
      // — and navigates away from muselab.)
      //
      // dragenter/dragleave fire many times per drag (once per child
      // element entered) so we count depth instead of trusting a single
      // event. dragend on the source doesn't fire for OS drags (the
      // source is in a different process), so we reset on drop too.
      //
      // The `types.includes('Files')` check filters OUT internal drags
      // (tree reorder, tab reorder) which use 'text/plain' / custom
      // mime types — those drags don't carry real File objects and
      // shouldn't make the upload overlay flash.
      // ============================================================
      const _hasFileType = (dt) => {
        if (!dt || !dt.types) return false;
        // DataTransferItemList is iterable in modern browsers; older
        // Safari returned a plain Array-like with .length only.
        try {
          for (const t of dt.types) if (t === "Files") return true;
        } catch (_e) {
          for (let i = 0; i < dt.types.length; i++) {
            if (dt.types[i] === "Files") return true;
          }
        }
        return false;
      };
      document.addEventListener("dragenter", (e) => {
        if (!_hasFileType(e.dataTransfer)) return;
        this._dragCounter++;
        if (this._dragCounter === 1) this.osFileDragging = true;
      });
      document.addEventListener("dragleave", (e) => {
        // Some browsers (Firefox) don't expose types on dragleave; we
        // decrement unconditionally because every leave matches an
        // earlier enter and the counter floor at 0 prevents drift.
        if (this._dragCounter > 0) this._dragCounter--;
        if (this._dragCounter === 0) this.osFileDragging = false;
      });
      // Required for drop to fire: dragover MUST be preventDefault'd at
      // some level. The preview overlay does this when visible, but for
      // areas of the page that aren't drop targets we also need a
      // no-op handler — otherwise the browser's default "open file as
      // navigation" kicks in if the user releases over chrome (toolbar
      // / sidebar). Without this every aborted drag could navigate away.
      document.addEventListener("dragover", (e) => {
        if (!_hasFileType(e.dataTransfer)) return;
        e.preventDefault();
      });
      document.addEventListener("drop", (e) => {
        // If the drop wasn't handled by an explicit zone (preview /
        // chat input), suppress browser default and reset state.
        if (_hasFileType(e.dataTransfer)) e.preventDefault();
        this._dragCounter = 0;
        this.osFileDragging = false;
      });

      // Listen for SW → page messages. The service worker posts
      // `muselab/notification-clicked` when the user taps a push
      // banner; we ack the unread badge immediately so they don't
      // have to open the bell drawer to clear it.
      if ("serviceWorker" in navigator) {
        navigator.serviceWorker.addEventListener("message", (ev) => {
          const t = ev && ev.data && ev.data.type;
          if (t === "muselab/notification-clicked") {
            if (this.scheduler && this.scheduler.unreadCount > 0) {
              this.ackSchedulerUnread();
            }
          } else if (t === "muselab/open-session") {
            // Push deep-link landed on an already-open tab: focus()
            // can't navigate, so the SW hands us the session id here and
            // we open it ourselves (matches the ?session= URL path used
            // when a fresh window is spawned).
            const id = ev.data && ev.data.id;
            if (id) this._openSessionFromDeeplink(id);
          }
        });
      }
      // Keep isWideScreen reactive across rotate / window resize so
      // Settings's mobile 2-level menu logic works without a reload.
      if (window.matchMedia) {
        const mq = window.matchMedia("(min-width: 721px)");
        const handler = e => {
          this.isWideScreen = e.matches;
          // Crossing into desktop with the modal open but no tab selected
          // (e.g. resized while on the mobile menu) → land on a default tab
          // so the content pane isn't blank next to the sidebar.
          if (e.matches && this.settings.show && !this.settings.activePage) {
            this.settings.activePage = "provider";
          }
        };
        // addEventListener is the modern API; older Safari needs
        // addListener (deprecated but still supported). Try both.
        if (mq.addEventListener) mq.addEventListener("change", handler);
        else if (mq.addListener) mq.addListener(handler);
      }
      // Per-session-load seed so the inspire prompts feel fresh each
      // time the user lands on the empty chat screen, rather than
      // always showing the same first 5. shuffleInspirePrompts() bumps
      // this on demand for "give me another batch".
      this._inspireSeed = Math.floor(Math.random() * 1e9);
      // Welcome-card visibility — Alpine-reactive so dismissWelcome()
      // immediately re-renders the chat-body. localStorage flag persists
      // dismissal across reloads / PWA reopens.
      this._welcomeDismissed = localStorage.getItem("muselab_welcome_dismissed") === "1";
      // Vibration / push prefs come from localStorage (per-device) so a
      // shared muselab between a desktop + phone keeps independent
      // settings — your phone can vibrate; the desktop tab silently
      // updates the bell badge.
      this.loadNotifyPrefs();
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
      // Removed: rightOpen toast ("Muse 回来了") — the panel opening is self-evident.
      // 编辑模式下切换文件时，重新挂载 CM 加载新文件内容
      this.$watch("selected", () => { if (this.editing) { this.unmountCM(); this.mountCM(); } });
      // beforeunload guard for the editor: register a handler ONLY while there
      // are unsaved edits, and remove it the moment they're saved/discarded.
      // Attaching beforeunload unconditionally would defeat the browser's
      // back/forward cache (bfcache) and degrade the "refresh = SSE reconnect"
      // experience the chat side relies on — so we keep it scoped to dirty.
      // cmStatus mutates on every CM change (dirty flips there); editing
      // toggles entry/exit — watch both so the guard syncs on each.
      this.$watch("cmStatus", () => this._syncBeforeUnloadGuard());
      this.$watch("editing", () => this._syncBeforeUnloadGuard());
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
    // Tap-on-chat-input handler. Two things matter:
    //   1. mark body.kb-open so CSS hides the bottom tab bar (legacy behaviour)
    //   2. scroll the textarea into view AFTER the keyboard's resize animation
    //      finishes. iOS Safari fires the visualViewport `resize` event
    //      ~250-350 ms after focus on PWA standalone; calling scrollIntoView
    //      before that resize is harmless (target is already visible per the
    //      pre-keyboard layout) but the resize then re-hides it. The deferred
    //      call lands the textarea above the keyboard reliably.
    //
    // `scroll-margin-bottom: 16px` on the textarea (see styles.css) leaves a
    // breathing-room gap so the input isn't flush against the keyboard top.
    onChatInputFocus(ev) {
      document.body.classList.add("kb-open");
      const ta = ev && ev.target;
      if (!ta || typeof ta.scrollIntoView !== "function") return;
      // Two pings: one fast (covers fast keyboards / Android), one slow
      // (waits out iOS PWA's lazy resize). Idempotent — second call is a
      // no-op if the input is already in view.
      const lift = () => {
        try { ta.scrollIntoView({ block: "end", behavior: "smooth" }); }
        catch (_) { try { ta.scrollIntoView(false); } catch (__) {} }
      };
      setTimeout(lift, 50);
      setTimeout(lift, 400);
    },

    // Triple-click (or any 3+ rapid click) on the chat input selects all
    // text. Browsers natively give us:
    //   single-click → place cursor
    //   double-click → select word
    //   triple-click → select paragraph (for <textarea> this is one line)
    // None of those select the WHOLE composed message, which is what the
    // user usually wants when re-prompting ("oh let me just retype this"
    // or "wrong tab, retry on the right model"). Listening to event.detail
    // (the consecutive-click counter that resets after ~500ms idle) is
    // the cleanest cross-platform path — no manual debounce timer state
    // to maintain, no conflict with the OS double-click word selection.
    onChatInputClick(ev) {
      const ta = ev && ev.target;
      if (!ta) return;
      // detail counts consecutive clicks: 1 / 2 / 3 / ... Browser resets
      // after a short idle window. We trigger on >= 3 so double-click's
      // word selection still works normally.
      if (ev.detail >= 3 && ta.value && ta.value.length > 0) {
        // Default for triple-click on textarea is "select current line".
        // Override with full select — preventDefault stops the partial
        // selection from racing the explicit select() call.
        ev.preventDefault();
        ta.select();
      }
    },

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

    // Attach iOS-style pull-to-refresh to a scrollable element. Mobile
    // only — skips immediately on devices with no touch (matchMedia
    // `pointer: coarse` would also wrap iPad pencil; we gate on
    // `hover: hover` instead, which is true for mouse / trackpad).
    //
    // Usage: <ul x-init="_attachPTR($el, () => reloadX())">. The
    // helper inserts an indicator element above the scroller, listens
    // to touchstart/move/end, applies a damped translateY while the
    // user is pulling, and calls onRefresh() when released past 60px.
    // Indicator stays visible during refresh, snaps back when the
    // promise resolves (so the user sees progress).
    _attachPTR(el, onRefresh) {
      if (!el || typeof onRefresh !== "function") return;
      if (window.matchMedia && window.matchMedia("(hover: hover)").matches) return;
      // Insert indicator just above the scroller (inside the same flex
      // parent so layout doesn't shift). pointer-events:none — pulling
      // the indicator itself shouldn't intercept the user's gesture.
      const ind = document.createElement("div");
      ind.className = "ptr-indicator";
      ind.innerHTML = "<span class='ptr-icon'>↓</span><span class='ptr-text'></span>";
      const txt = ind.querySelector(".ptr-text");
      const icon = ind.querySelector(".ptr-icon");
      el.parentElement.insertBefore(ind, el);
      const THRESHOLD = 60;
      let startY = 0, currentY = 0, pulling = false, refreshing = false;
      const setLabel = (state) => {
        const zh = this.lang === "zh";
        if (state === "pull")    txt.textContent = zh ? "下拉刷新" : "Pull to refresh";
        else if (state === "release") txt.textContent = zh ? "释放刷新" : "Release to refresh";
        else if (state === "loading") txt.textContent = zh ? "刷新中…" : "Refreshing…";
      };
      el.addEventListener("touchstart", (e) => {
        if (refreshing) return;
        if (el.scrollTop > 0) return;
        startY = e.touches[0].clientY;
        currentY = startY;
        pulling = true;
      }, { passive: true });
      el.addEventListener("touchmove", (e) => {
        if (!pulling || refreshing) return;
        currentY = e.touches[0].clientY;
        const dy = currentY - startY;
        if (dy <= 0) {
          ind.style.transform = "";
          ind.style.opacity = "0";
          return;
        }
        // Prevent page-level overscroll while the user is actively
        // pulling — without this, iOS Safari bounces the whole page.
        // Only block when we're genuinely pulling (dy > a few px).
        if (dy > 4 && el.scrollTop === 0 && e.cancelable) e.preventDefault();
        const damped = Math.min(dy * 0.5, 90);
        ind.style.transform = `translateY(${damped}px)`;
        ind.style.opacity = String(Math.min(1, damped / 40));
        icon.style.transform = damped >= THRESHOLD ? "rotate(180deg)" : "";
        setLabel(damped >= THRESHOLD ? "release" : "pull");
      }, { passive: false });
      el.addEventListener("touchend", async () => {
        if (!pulling || refreshing) return;
        pulling = false;
        const dy = currentY - startY;
        if (dy * 0.5 >= THRESHOLD) {
          refreshing = true;
          ind.style.transform = `translateY(50px)`;
          ind.style.opacity = "1";
          icon.style.transform = "";
          ind.classList.add("ptr-spinning");
          setLabel("loading");
          try { await onRefresh(); }
          catch (e) { /* swallow — the refresh fn's own toast handles err */ }
          finally {
            ind.classList.remove("ptr-spinning");
            ind.style.transform = "";
            ind.style.opacity = "0";
            refreshing = false;
          }
        } else {
          ind.style.transform = "";
          ind.style.opacity = "0";
        }
      }, { passive: true });
      el.addEventListener("touchcancel", () => {
        pulling = false;
        if (!refreshing) {
          ind.style.transform = "";
          ind.style.opacity = "0";
        }
      });
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
      // Push-notification deep-link: a turn-done notification opens
      // `/?session=<id>` in a fresh tab. After sessions load, jump to that
      // session so the user lands in the conversation they were pinged about
      // (the already-open-tab case is handled via the SW postMessage above).
      this.initSessions().then(() => {
        try {
          const sid = new URLSearchParams(location.search).get("session");
          if (sid) this._openSessionFromDeeplink(sid);
        } catch (_) { /* noop */ }
      });
      this.fetchStats();
      // Trash badge state — light fetch (just count), gated by token
      // which is already verified at this point. Fire-and-forget;
      // failures degrade silently to "no badge styling".
      this.loadTrash();
      // Surface any in-flight turns that were cut short by a previous
      // process death (OOM kill / power loss / manual restart mid-stream).
      // Fire-and-forget — purely informational, doesn't block boot. Backend
      // returns [] when nothing was interrupted (the common case).
      this._checkInterruptedTurns();
      // First-run hint — surface key shortcuts so the user doesn't have to
      // hunt for them. Flagged in localStorage so it only fires once. Short
      // delay lets the splash clear first.
      if (!localStorage.getItem("muselab_seen_help")) {
        setTimeout(() => {
          this.toast(
            this.lang === "zh"
              ? "Tip：⌘K 命令面板 · / 斜杠命令 · @ 引用文件 · ↑ 回滚上一条"
              : "Tip: ⌘K command palette · / slash commands · @ to reference files · ↑ to recall last message",
            "info", 7000);
          this._setLS("muselab_seen_help", "1");
        }, 1500);
      }
      // Same preview-file restore that login() does — covers the
      // already-authed boot path (page refresh with saved token).
      if (this._pendingPreviewSelected) {
        const path = this._pendingPreviewSelected;
        this._pendingPreviewSelected = null;
        // Preserve the restored tab's preview/pinned state — restoring the
        // selection must not silently pin a tab that was left in preview.
        const _restored = this.tabs.find(t => t.path === path);
        this.openFile({ path, name: path.split("/").pop() },
                      { preview: !!(_restored && _restored.preview) })
            .catch(() => { /* file gone — silent */ });
      }
      // Restore mobile tab choice AFTER openFile (which would otherwise
      // force-switch us to "preview" on mobile every reopen). $nextTick
      // ensures the openFile-induced mobileTab="preview" assignment
      // settles before we override it back to the user's actual last tab.
      // Desktop ignores mobileTab entirely so this is a no-op there.
      if (this._pendingMobileTab) {
        const wantTab = this._pendingMobileTab;
        this._pendingMobileTab = null;
        this.$nextTick(() => {
          if (this._isMobileLayout()) this.mobileTab = wantTab;
        });
      }
      // Block readiness on context-info (the most important one for the
      // onboarding cards). Others come along in parallel.
      try {
        await this.fetchContextInfo();
        this._markReady();
      } catch (e) {
        // Will retry via heartbeat
      }
      this._startLiveConnections();
    },

    // Start the always-on background connections: conn heartbeat (drives
    // stale-JS auto-reload + connState/bell badge refresh) and presence
    // reporting (suppresses phone push while the user is at the screen).
    // Shared by _bootApp (saved-token boot) AND login (first sign-in) so a
    // freshly logged-in user gets heartbeat/presence/bell immediately instead
    // of only after a manual refresh. Both underlying starts are idempotent
    // (clearInterval before re-arming), but this is only ever called once per
    // boot path, so there's no double-start.
    _startLiveConnections() {
      this._startHeartbeat();
      this._startPresence();
      this._startSessionsSync();
    },

    // FIX ⑪: near-real-time multi-device sync for the session LIST while the
    // tab is FOREGROUND. Previously the only cross-device propagation points
    // were visibilitychange and the manual refresh button — so two devices
    // both sitting open & visible never saw each other's changes (a turn
    // started on the phone left the laptop's blue dot dark until the laptop
    // was hidden→shown). This lightweight poll closes that gap: every 10s
    // (visible only, for battery) it re-pulls the session list, which now
    // carries the server-authoritative `active` flag (FIX ⑩) and drives the
    // active→idle green-dot transition. refreshSessions is cheap (server
    // caches it on a short TTL), so this is far lighter than a full SSE
    // broadcast subsystem while delivering the live blue/green dot feel.
    //
    // Deliberately does NOT call _pullUIStateAndMerge here: that force-syncs
    // currentId via activateTab(), which on a 10s cadence would make two
    // open+visible devices fight over the active tab and yank the user
    // mid-read. Tab-strip / currentId reconciliation stays on visibilitychange
    // (and manual refresh), where the user expects a catch-up — not while
    // they're actively looking at the screen.
    _startSessionsSync() {
      if (this._sessionsSyncTimer) clearInterval(this._sessionsSyncTimer);
      this._sessionsSyncTimer = setInterval(async () => {
        if (typeof document !== "undefined"
            && document.visibilityState !== "visible") return;
        if (!this.token) return;
        try {
          await this._syncSessionListQuiet();
        } catch (_) { /* best-effort; next tick retries */ }
      }, 10_000);
    },

    _markReady() {
      if (this.appReady) return;
      this.appReady = true;
      clearTimeout(this._splashHintTimer);
      clearTimeout(this._splashHardTimeout);
      this.splashHint = "";
    },

    // Friendly "5 min ago" / "刚刚" / "3 h ago" formatter for the
    // interrupted-turn toast. Only used here; no need to factor out.
    _agoLabel(ts) {
      if (!ts) return this.lang === "zh" ? "未知时间" : "unknown";
      const diff = Date.now() / 1000 - ts;
      if (diff < 60) return this.lang === "zh" ? "刚刚" : "just now";
      if (diff < 3600) {
        const m = Math.round(diff / 60);
        return this.lang === "zh" ? `${m} 分钟前` : `${m}m ago`;
      }
      if (diff < 86400) {
        const h = Math.round(diff / 3600);
        return this.lang === "zh" ? `${h} 小时前` : `${h}h ago`;
      }
      const d = Math.round(diff / 86400);
      return this.lang === "zh" ? `${d} 天前` : `${d}d ago`;
    },

    // Toast any turns the previous muselab process left in-flight at the
    // moment it died. Backend persists `sessions/active_turns/<sid>.json`
    // on turn start and deletes it on clean completion — anything left
    // over after restart is an interrupted turn.
    //
    // We do NOT auto-resume. Auto-resume would burn tokens on conversations
    // the user has already moved past, and bypasses their own judgment of
    // whether the prompt is worth rephrasing. Frontend just surfaces the
    // sid + preview; user decides.
    //
    // We dismiss on the backend immediately after toasting (regardless of
    // whether the user clicks the action). The point is "tell the user
    // once" — if they let the toast fade, they've still been notified, and
    // re-nagging on every restart would be annoying.
    async _checkInterruptedTurns() {
      let resp;
      try {
        resp = await this.api("/api/chat/interrupted-turns");
      } catch (e) {
        return;   // network / auth issue — heartbeat will retry boot
      }
      if (!resp.ok || !resp.data || !Array.isArray(resp.data.turns)) return;
      const turns = resp.data.turns;
      if (!turns.length) return;
      for (const turn of turns) {
        const ago = this._agoLabel(turn.started_at);
        const preview = (turn.preview || "").trim();
        const truncated = preview.length > 60
          ? preview.slice(0, 59) + "…"
          : preview || (this.lang === "zh" ? "(空消息)" : "(empty prompt)");
        const msg = this.lang === "zh"
          ? `上次对话被中断（${ago}）：${truncated}`
          : `Last turn interrupted (${ago}): ${truncated}`;
        this.toast(msg, "warn", 0, {
          label: this.lang === "zh" ? "打开" : "Open",
          onClick: () => { this.openTab(turn.sid).catch(() => {}); },
        });
        // Mark dismissed on backend — see method docstring for rationale.
        fetch(`/api/chat/interrupted-turns/${turn.sid}/dismiss`, {
          method: "POST", headers: this.hdr(),
        }).catch(() => { /* best-effort */ });
      }
    },

    // 10s heartbeat — pings /api/meta. If 2 consecutive fails, flag reconnecting;
    // when one comes back, flash "reconnected" then auto-clear.
    _startHeartbeat() {
      if (this._connHeartbeat) clearInterval(this._connHeartbeat);
      this._connHeartbeat = setInterval(() => this._pingHealth(), 10_000);
    },

    // Presence heartbeat — tells the backend "this device is at the
    // screen right now" so the chat turn-done push gate (see
    // backend/presence.py + chat.py) doesn't fan a notification out to
    // the user's phone while they're using their laptop. Sent every
    // 15s WHILE the page is visible; stops as soon as the tab is
    // minimized / switched away. Also fires immediately on every
    // visibility-change to "visible" (so coming back into focus
    // re-arms the suppression before the next push could fire).
    _startPresence() {
      const ping = () => {
        if (typeof document === "undefined") return;
        if (document.visibilityState !== "visible") return;
        try {
          fetch("/api/presence", { method: "POST", headers: this.hdr() })
            .catch(() => {});   // silent — presence is best-effort
        } catch (_) { /* ignore */ }
      };
      // Fire once on init so we don't wait up to 15s for the first ping.
      ping();
      if (this._presenceTimer) clearInterval(this._presenceTimer);
      this._presenceTimer = setInterval(ping, 15_000);
      // Tab returning to foreground → ping immediately. Without this, a
      // user who just opened the laptop after a 5-minute lunch break
      // might still get a phone push for a turn that finishes in the
      // first 15s of being back.
      document.addEventListener("visibilitychange", async () => {
        if (document.visibilityState !== "visible") return;
        ping();                  // presence: re-arm push suppression
        this._pingHealth();      // health: refresh conn state immediately
        // Cross-device tab sync — refresh sessions first (in case another
        // device created/deleted sessions while this tab was hidden), then
        // flush any pending local push so the GET sees our latest writes
        // BEFORE pulling, then merge remote state into the tab strip.
        try {
          await this.refreshSessions();
          await this._flushUIStatePush();
          await this._pullUIStateAndMerge();
        } catch (_) {}
      });
    },
    async _pingHealth() {
      // Skip when tab is hidden — heartbeat purpose is "show user we're
      // connected", which is meaningless if the user isn't looking. Mobile
      // PWA in background used to fire 8.6k fetches/day (10s × 24h)
      // draining battery + radio for no UI benefit. Coming back to
      // foreground triggers an immediate ping below via the
      // visibilitychange handler, so there's no "unknown for up to 10s"
      // gap on return.
      if (typeof document !== "undefined"
          && document.visibilityState !== "visible") return;
      try {
        const r = await fetch("/api/meta", { headers: this.hdr() });
        if (!r.ok) throw new Error("status " + r.status);
        // Stale-JS detector. Mobile Safari frequently resumes a
        // backgrounded PWA tab without re-fetching HTML, so the page
        // keeps running last week's app.js against today's API. When
        // /api/meta.asset_version is newer than the version baked into
        // OUR HTML at boot (via <meta name="muselab-asset-version">),
        // hard-reload to pick up the new bundle. Guarded so we only
        // reload once per session — bailing out cleanly if the user
        // is mid-stream or the meta tag is missing (old HTML still
        // cached, no placeholder).
        try {
          const meta = await r.clone().json();
          const remoteVer = meta && meta.asset_version;
          if (remoteVer && !this._appVersionReloadFired) {
            const localVer = (document.querySelector(
              'meta[name="muselab-asset-version"]') || {}).content || "";
            // localVer === "" → fresh deploy whose HTML still uses the
            // un-substituted placeholder is impossible (backend always
            // replaces). Treat "" or the literal placeholder as "unknown,
            // skip" rather than infinite-reload.
            const knownLocal = localVer
              && localVer !== "__MUSELAB_ASSET_VERSION__";
            if (knownLocal && localVer !== String(remoteVer)
                && !this.streaming) {
              this._appVersionReloadFired = true;
              console.info("[muselab] asset version changed",
                           localVer, "→", remoteVer, "— reloading");
              location.reload();
              return;
            }
          }
        } catch (_) { /* meta parse failed — non-fatal, skip check */ }
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
        // Refresh scheduler unread count — cheap (single JSON, no auth
        // round-trip beyond what /api/meta already costs) and keeps the
        // bell badge live without forcing the user to open the drawer.
        this.fetchSchedulerUnread();
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
    async mountCM() {
      // CodeMirror is lazy-loaded — kick off the fetch (no-op if already
      // present) and only proceed once the global is exposed. Without this
      // wait, every first edit-mode entry per session would silently fall
      // through and the textarea fallback would render.
      if (!window.CodeMirror) {
        try { await this._loadCodemirror(); }
        catch (e) { console.warn("[muselab] CodeMirror lazy load failed:", e); return; }
      }
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
            // Ctrl/Cmd+S inside the editor → save. Without this, on some
            // browsers the browser's own "save page" dialog can fire even
            // when the document-level keydown handler exists, because
            // CodeMirror's contenteditable subtree captures the event
            // first. Hooking it here is the most defensive spot.
            extraKeys: {
              "Ctrl-S": () => { this.saveEdit(); },
              "Cmd-S":  () => { this.saveEdit(); },
            },
          });
          // Initial status
          this.cmStatus = {
            line: 1, col: 1, sel: 0,
            lines: cm.lineCount(),
            chars: cm.getValue().length,
            mode: this.shortMode(modeStr),
            dirty: false,
          };
          // Status is derived from ONE getValue() snapshot. The change
          // handler used to trigger three full-document reads per keystroke
          // (editText mirror + `chars` length + `dirty` compare), each O(n)
          // on a big file. We now read once and thread the value through.
          // NOTE: CM invokes cursorActivity handlers with the instance as the
          // first arg, so updateStatus must be CALLED (not passed directly) or
          // `val` would receive the CM object — hence the () => wrappers.
          // (perf: RED — app.js CM getValue 3×/keystroke)
          const updateStatus = (val) => {
            if (val === undefined) val = cm.getValue();
            const c = cm.getCursor();
            const sel = cm.getSelection().length;
            this.cmStatus = {
              line: c.line + 1, col: c.ch + 1, sel,
              lines: cm.lineCount(),
              chars: val.length,
              mode: this.shortMode(modeStr),
              dirty: val !== String(this.rawText || ""),
            };
          };
          cm.on("change", () => {
            const v = cm.getValue();
            this.editText = v;
            updateStatus(v);
          });
          cm.on("cursorActivity", () => updateStatus());
          window.__muselab_cm = cm;
          setTimeout(() => { cm.refresh(); updateStatus(); }, 50);
        } catch (e) {
          console.error("[muselab] CodeMirror init failed:", e);
          this.toast(
            (this.lang === "zh" ? "编辑器初始化失败：" : "Editor init failed: ")
              + e.message, "error", 6000);
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
      if (saved === "light" || saved === "dark" || saved === "eyecare") {
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
        // Eyecare reuses the dark hljs theme — its softer contrast fits
        // the warm paper background better than the high-contrast light theme.
        link.href = this.theme === "light"
          ? "/static/vendor/highlight-theme-light.css"
          : "/static/vendor/highlight-theme.css";
      }
      // CodeMirror: use default (light) theme for both light and eyecare
      // since material-darker is too harsh on warm backgrounds.
      if (window.__muselab_cm) {
        window.__muselab_cm.setOption("theme",
          this.theme === "dark" ? "material-darker" : "default");
      }
      // PWA status-bar color. The <meta name="theme-color"> tags in index.html
      // are media-scoped to the SYSTEM scheme, so a user on a light OS who
      // manually picks dark/eyecare would get a mismatched status bar. Force
      // every theme-color meta to the active in-app theme so whichever one the
      // browser picks agrees with what's actually rendered.
      const themeColor = ({ light: "#ffffff", dark: "#0f1115", eyecare: "#f5f0e0" })[this.theme] || "#0f1115";
      document.querySelectorAll('meta[name="theme-color"]').forEach(m => {
        m.setAttribute("content", themeColor);
      });
    },
    applyAccent() {
      // 主色 + 派生色（hover / soft 半透明 / 文字色用浅化 mix 实现）
      const r = document.documentElement.style;
      const isLight = this.theme === "light" || this.theme === "eyecare";
      r.setProperty("--c-accent", this.accent);
      r.setProperty("--c-accent-hover", this._shade(this.accent, isLight ? -15 : 12));
      r.setProperty("--c-accent-soft", this._withAlpha(this.accent, isLight ? 0.10 : 0.14));
      r.setProperty("--c-accent-fg", isLight
        ? this._shade(this.accent, -25)
        : this._shade(this.accent, 25));
    },
    setAccent(color) {
      this.accent = color;
      this._setLS("muselab_accent", color);
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
      this._setLS("muselab_lang", lang);
      document.documentElement.lang = lang;
      this.toast(this.t("toast.lang_switched"), "success", 1500);
    },
    // t("key.path", {var: "x"}) — 简单变量插值；缺 key 时回退到 key 本身（方便发现遗漏）
    t(key, vars) {
      const table = STRINGS[this.lang] || STRINGS.zh;
      let s = table[key];
      if (s == null) s = (STRINGS.zh[key] != null ? STRINGS.zh[key] : key);
      // Minimal plural support: a token of the form {n|singular|plural} picks
      // the singular form when vars.n === 1, else the plural. English-only in
      // practice (Chinese has no plural inflection so both forms are equal);
      // both dictionaries stay symmetric — zh just writes the same word twice
      // or omits the token entirely.
      const n = (vars && typeof vars.n !== "undefined") ? Number(vars.n) : null;
      // Guard the plural regex: the vast majority of the ~248 call sites have
      // no {n|…} token, so skip the global regex scan unless one is present.
      // The `n === 1 ? one : many` branch (incl. the n===null → many case) is
      // unchanged from the always-run version — only the no-token fast path
      // is added.
      if (s.indexOf("{n|") !== -1) {
        s = s.replace(/\{n\|([^|{}]*)\|([^|{}]*)\}/g,
                       (_, one, many) => (n === 1 ? one : many));
      }
      if (vars) {
        for (const k in vars) s = s.split("{" + k + "}").join(vars[k]);
      }
      return s;
    },

    // ===== Muse mascot =====
    initMascot() {
      // User-pinned mascot (set by cycleMascot) wins over the time-based
      // default. Without persistence, a user who clicked through to e.g.
      // Urania saw it reset on every reload — the daily rotation logic
      // ignored their explicit choice.
      const pinned = localStorage.getItem("muselab_mascot_idx");
      if (pinned !== null) {
        const i = parseInt(pinned, 10);
        if (Number.isInteger(i) && i >= 0 && i < this.MASCOTS.length) {
          this.mascotIdx = i;
          this.applyFavicon();
          setTimeout(() => this.greetMascot(this.mascotLabel()), 400);
          return;
        }
      }
      // First time ever (no pinned value): pick by today's hash, AND
      // immediately persist so it stays put. Otherwise every page load
      // would re-roll mascot every hour (date+hour seed) — which is
      // what "I want my pick to stay" complaints are really about.
      const seed = new Date().toISOString().slice(0, 13);
      let h = 5381;
      for (let i = 0; i < seed.length; i++) h = ((h << 5) + h + seed.charCodeAt(i)) | 0;
      this.mascotIdx = Math.abs(h) % this.MASCOTS.length;
      try { localStorage.setItem("muselab_mascot_idx", String(this.mascotIdx)); } catch {}
      this.applyFavicon();
      setTimeout(() => this.greetMascot(this.mascotLabel()), 400);
    },
    mascot() { return this.MASCOTS[this.mascotIdx]; },
    mascotHref() { return "#m-" + this.mascot().id; },
    // Short label shown inside the user-side message avatar. muselab has no
    // identity layer (single-user, token-auth), so we just stamp "我" / "U"
    // by language. Cheap, requires no extra SVG asset.
    userAvatarText() { return this.lang === "zh" ? "我" : "U"; },
    // 是否给第 i 条消息的头像加流式动效。
    // 之前的内联条件 `streaming && (i===0 || messages[i-1].role==='user')`
    // 命中的是「每一个 assistant 轮次的首条」——历史里每个轮次都满足，
    // 于是流式时所有轮次的头像一起动。正确语义是「仅当前（最新）轮」：
    // 既是轮首（前一条是 user 或开头），又是最后一轮（自此往后不再有 user）。
    isStreamingTurnAvatar(i) {
      if (!this.streaming) return false;
      const msgs = this.messages;
      if (!msgs || !msgs[i] || msgs[i].role === "user") return false; // 仅 assistant 侧头像
      const isTurnStart = i === 0 || (msgs[i - 1] && msgs[i - 1].role === "user");
      if (!isTurnStart) return false;
      for (let j = i + 1; j < msgs.length; j++) {
        if (msgs[j] && msgs[j].role === "user") return false; // 后面还有用户消息 → 不是最新轮
      }
      return true;
    },
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
      // Pin the choice — initMascot reads this on next load.
      try { localStorage.setItem("muselab_mascot_idx", String(this.mascotIdx)); } catch {}
      this.applyFavicon();
      this.greetMascot(this.mascotLabel());
    },
    // 把当前 mascot 渲染成 data:image/svg+xml favicon，跟着主题色走
    applyFavicon() {
      const id = this.mascot().id;
      // 重新声明每个 mascot 的 SVG body —— defs 在 document 里通过 <use> 引用，但 favicon
      // data URL 是独立文档，必须把图形内嵌。集中在这里维护成 lookup。
      //
      // FAVICON-SPECIFIC SIMPLIFICATIONS:
      //   - orbit (Urania): drop the satellite small-circle. At 16px in
      //     a browser tab, the filled dot at (18.5, 6) reads as an
      //     unread-notification badge, which is a false signal. The
      //     in-page mascot (rendered larger via the SVG <defs>) still
      //     uses the satellite — only the favicon variant is stripped.
      //   - trio (Thalia): keep all three dots; at 16px they still read
      //     as a triangle of dots, not a badge.
      //   - spark (Erato): keep — center dot is on a cross so reads as
      //     a hub, not a notification.
      const SHAPES = {
        hex:      '<path d="M12 3 L20 7.5 L20 16.5 L12 21 L4 16.5 L4 7.5 Z"/>',
        bars:     '<line x1="4" y1="7" x2="20" y2="7"/><line x1="7" y1="12" x2="17" y2="12"/><line x1="10" y1="17" x2="14" y2="17"/>',
        lens:     '<circle cx="9" cy="12" r="6"/><circle cx="15" cy="12" r="6"/>',
        wave:     '<circle cx="12" cy="12" r="9"/><path d="M5 12 Q 8.5 6 12 12 T 19 12"/>',
        crescent: '<path d="M16 3 A 9 9 0 1 0 16 21 A 7 7 0 1 1 16 3 Z"/>',
        halo:     '<circle cx="12" cy="14" r="5"/><path d="M5 8 A 7 4 0 0 1 19 8"/>',
        trio:     '<circle cx="12" cy="6" r="2" fill="currentColor"/><circle cx="6" cy="17" r="2" fill="currentColor"/><circle cx="18" cy="17" r="2" fill="currentColor"/>',
        spark:    '<line x1="12" y1="3" x2="12" y2="21"/><line x1="3" y1="12" x2="21" y2="12"/><circle cx="12" cy="12" r="2" fill="currentColor"/>',
        // orbit favicon: just the planet circle, centered. The satellite
        // dot is kept in the in-page mascot defs (see #m-orbit in
        // index.html) so the brand mark still has it where size allows.
        orbit:    '<circle cx="12" cy="12" r="7"/>',
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
      // Cycle: light → dark → eyecare → light
      const order = ["light", "dark", "eyecare"];
      const idx = order.indexOf(this.theme);
      this.setTheme(order[(idx + 1) % order.length]);
    },
    // Direct theme set — used by the Settings → Appearance picker. Mobile
    // hides the files-pane-head toggle (styles.css), so Settings is the
    // ONLY theme entry point on phones; this backs it.
    setTheme(t) {
      if (!["light", "dark", "eyecare"].includes(t)) return;
      this.theme = t;
      this.applyTheme();
      this.applyAccent();   // 派生色对深浅敏感，重算
      this._setLS("muselab_theme", this.theme);
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
                         ".conf", ".cfg", ".rs", ".go", ".java", ".c", ".h",
                         ".cpp", ".hpp", ".rb", ".php", ".swift", ".kt",
                         ".sql"];
      if (textExts.some(ext => name.endsWith(ext))) return "text";
      // Spreadsheets — backend converts them to CSV-style text via
      // openpyxl. Use "text" kind for the chip since that's what they
      // become downstream; the backend echoes the same.
      const xlsxExts = [".xlsx", ".xlsm", ".xltx", ".xltm"];
      if (xlsxExts.some(ext => name.endsWith(ext))) return "text";
      // Anything else: still try to upload — the backend has the final say
      // and will reject with a clear error if it can't be handled. Better
      // UX than silently filtering files out client-side and confusing the
      // user (the "I uploaded but nothing happened" bug).
      return "text";
    },
    // Client-side image compression. Re-encode large photos to JPEG
    // capped at 1600px on the long edge, quality 0.85. Cuts a typical
    // 4 MB iPhone photo down to ~300 KB and removes the upload-stall
    // pain on 4G / slow Wi-Fi. Skips:
    //   - non-images (PDFs etc.)
    //   - GIF (would lose animation)
    //   - SVG (vector — re-rasterising is destructive)
    //   - tiny files (< 256 KB — overhead > savings)
    // Falls back to the original file if any step fails (older Safari
    // rejecting createImageBitmap options, HEIC the browser can't
    // decode, OOM on huge canvases, etc.) — better a slow upload than
    // none. The returned File preserves the original base name with a
    // .jpg extension so the backend's name-based classification still
    // sees an image.
    // Generate a small thumbnail data URL (≤160 px wide/tall, JPEG 70%)
    // from a File or Blob. Always returns a data URI string — safe for
    // long-term storage in Alpine reactive data and across session reloads.
    _imageToThumbDataURL(file) {
      return new Promise(resolve => {
        const img = new Image();
        const objUrl = URL.createObjectURL(file);
        img.onload = () => {
          URL.revokeObjectURL(objUrl);
          const MAX = 160;
          const scale = Math.min(1, MAX / Math.max(img.width, img.height));
          const w = Math.round(img.width * scale);
          const h = Math.round(img.height * scale);
          const canvas = document.createElement("canvas");
          canvas.width = w; canvas.height = h;
          const ctx = canvas.getContext("2d");
          ctx.drawImage(img, 0, 0, w, h);
          resolve(canvas.toDataURL("image/jpeg", 0.70));
        };
        img.onerror = () => {
          URL.revokeObjectURL(objUrl);
          // Decode failed (corrupt file, unsupported format, ...).
          // The previous fallback created ANOTHER blob URL via
          // createObjectURL and returned it — but if the browser
          // couldn't decode the file the first time, the new blob URL
          // wouldn't render either, AND it was never revoked (the
          // closeChatTab sweep was removed when previews moved to data
          // URLs in 2026-04). Return empty string instead so callers
          // show a generic "image" placeholder.
          resolve("");
        };
        img.src = objUrl;
      });
    },

    async _maybeCompressImage(file) {
      if (!file || !file.type || !file.type.startsWith("image/")) return file;
      // GIF: animation would be lost re-encoding to a still frame.
      // SVG: vector — re-encoding to raster destroys the whole point.
      if (file.type === "image/gif" || file.type === "image/svg+xml") return file;
      // Compression tuned 2026-05-21 → re-tuned same day after user
      // reported "图片发送很慢 还发不出去". The WebP encode path was the
      // likely culprit:
      //   - iOS Safari's canvas.toBlob("image/webp") is supported in
      //     iOS 14+ but is significantly slower than JPEG on the same
      //     canvas (especially on older iPhone hardware) — by the time
      //     the encode finishes the user has already tapped send and is
      //     staring at a frozen UI.
      //   - When WebP encode falls back (PNG / null), we ran JPEG too,
      //     doubling total encode time.
      //   - Failure modes between WebP and JPEG diverge, making errors
      //     harder to debug.
      // Rolling back to JPEG-only — simpler, faster, universally
      // supported. We keep the smaller dimension / quality from the
      // previous attempt because those translate directly to upload
      // bytes (the main bottleneck on 4G).
      const COMPRESS_THRESHOLD = 256 * 1024;
      if (file.size < COMPRESS_THRESHOLD) return file;
      const MAX_DIM = 1280;     // pixel count down ~36% vs 1600
      const QUALITY = 0.72;     // file size down ~30-40% vs 0.85
      try {
        let bitmap;
        try {
          bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
        } catch (_) {
          // Older Safari rejects unknown options — retry without.
          bitmap = await createImageBitmap(file);
        }
        const w0 = bitmap.width, h0 = bitmap.height;
        const ratio = Math.min(1, MAX_DIM / Math.max(w0, h0));
        const w = Math.max(1, Math.round(w0 * ratio));
        const h = Math.max(1, Math.round(h0 * ratio));
        const canvas = document.createElement("canvas");
        canvas.width = w; canvas.height = h;
        const ctx = canvas.getContext("2d", { alpha: false });
        // JPEG has no alpha — flatten transparent originals onto white
        // so a PNG with cutout becomes a sensible JPEG instead of black.
        ctx.fillStyle = "#fff";
        ctx.fillRect(0, 0, w, h);
        ctx.drawImage(bitmap, 0, 0, w, h);
        if (bitmap.close) bitmap.close();
        const blob = await new Promise(res =>
          canvas.toBlob(res, "image/jpeg", QUALITY));
        if (!blob || blob.size >= file.size) return file;   // not worth it
        const base = (file.name || "image").replace(/\.[^.]+$/, "");
        return new File([blob], base + ".jpg",
                          { type: "image/jpeg", lastModified: Date.now() });
      } catch (e) {
        console.warn("[muselab] image compression failed, sending original:", e);
        return file;
      }
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

      // Diagnostic timing — when uploads feel slow the user has no way to
      // tell if it's compression CPU on the phone or network throughput.
      // We split the timeline into "compress" / "data-url" / "upload" and
      // attach the totals to a console.log + the chip's title attribute.
      // Cheap (just performance.now() calls) and only kept user-visible
      // via a debug toast when an upload exceeds 3 s (likely-feeble feedback
      // surface; cleaner mechanism than scattering prompts).
      const t0 = performance.now();
      const origSize = file.size;
      let tCompressEnd = t0;

      let entry;
      if (kind === "image") {
        // Compress BEFORE generating the preview + upload — both reuse
        // the smaller file.
        file = await this._maybeCompressImage(file);
        tCompressEnd = performance.now();
        // Generate a small base64 thumbnail (≤160 px, JPEG 70%) stored
        // directly in the image entry. This survives session reload,
        // tab switches, and iOS Safari's blob-URL lifecycle quirks —
        // the data URI is a self-contained string that never expires.
        // The chip and the sent-message bubble both use this thumbnail.
        const preview = await this._imageToThumbDataURL(file);
        // Stash the compressed File on the entry so the in-chip "✎ Annotate"
        // button can re-open the bitmap in the image editor without
        // round-tripping back to the server. Memory cost is small (~300 KB
        // per image after compression), cleared on send.
        const raw = { id: null, mime: file.type, preview,
                       uploading: true, error: false, file };
        this.pendingImages.push(raw);
        // Alpine v3 wraps each pushed item in a Proxy. The local `raw`
        // reference still points at the original (non-proxied) object;
        // mutating raw.uploading bypasses the Proxy's set trap and
        // doesn't fire reactivity → the chip's `:class="{uploading:...}"`
        // binding never re-evaluates and the progress bar slides forever
        // even after the upload completes. Bug observed 2026-05-21.
        // Pull the proxied version back out of the array so subsequent
        // mutations go through Alpine's reactive layer.
        entry = this.pendingImages[this.pendingImages.length - 1];
      } else {
        const raw = { id: null, name: file.name, kind,
                       uploading: true, error: false };
        this.pendingDocs.push(raw);
        // Same Alpine-proxy gotcha as above — must use the proxied
        // reference for entry.uploading = false to actually trigger UI.
        entry = this.pendingDocs[this.pendingDocs.length - 1];
      }

      const tUploadStart = performance.now();
      const fd = new FormData();
      fd.append("file", file);
      try {
        const r = await fetch("/api/chat/upload-image", {
          method: "POST", headers: this.hdr(), body: fd,
        });
        if (!r.ok) {
          entry.error = true; entry.uploading = false;
          // Include HTTP status in the toast so "image upload failed: 413
          // file too large" or ":400 unsupported file type" is visible to
          // the user instead of just "upload failed". Helps a lot when
          // diagnosing why a particular photo won't go through.
          let body = "";
          try { body = await r.text(); } catch (_) {}
          this.toast(`${this.t("img.upload_failed")} (HTTP ${r.status})${body ? ": " + body : ""}`,
                      "error", 5000);
          return;
        }
        const d = await r.json();
        entry.id = d.id; entry.uploading = false;
        // Stash the on-disk extension the server will use when persisting
        // this image at send-time. Used to construct the lightbox URL
        // upfront so the full-res original is accessible even if the
        // user reloads before the stream-completion annotation hook fires.
        if (d.attach_ext) entry.attach_ext = d.attach_ext;
        // Server's classification wins for kind label.
        if (d.kind && entry.kind) entry.kind = d.kind;
        // === Diagnostic timing report ===
        const tEnd = performance.now();
        const compressMs = Math.round(tCompressEnd - t0);
        const networkMs = Math.round(tEnd - tUploadStart);
        const totalMs = Math.round(tEnd - t0);
        const finalKB = Math.round(file.size / 1024);
        const origKB = Math.round(origSize / 1024);
        // Console line is always emitted (only visible if devtools open).
        console.log(
          `[muselab][upload] ${file.name || "(unnamed)"} ` +
          `orig=${origKB}KB → final=${finalKB}KB · ` +
          `compress=${compressMs}ms · network=${networkMs}ms · total=${totalMs}ms`
        );
        // Visible toast ONLY when upload felt slow (>3 s). On phone this
        // turns "huh, that was slow" into "ah, network was 4.2 s — my Wi-Fi
        // is bad", actionable instead of mysterious. Below threshold we
        // stay silent so normal fast uploads don't add chrome.
        if (totalMs > 3000) {
          this.toast(
            this.lang === "zh"
              ? `📤 上传 ${totalMs}ms (压缩 ${compressMs}ms · 网络 ${networkMs}ms · ${finalKB}KB)`
              : `📤 Upload ${totalMs}ms (compress ${compressMs}ms · network ${networkMs}ms · ${finalKB}KB)`,
            "info", 4000);
        }
      } catch (e) {
        entry.error = true; entry.uploading = false;
        // Network-level failure (TypeError: Failed to fetch / NetworkError /
        // AbortError). Surface the error name so user sees whether it's
        // "lost connection" vs "request aborted" vs "CORS rejected". Most
        // common on mobile is intermittent 4G drops.
        const reason = (e && (e.name + (e.message ? ": " + e.message : "")))
                         || "network error";
        this.toast(`${this.t("img.upload_failed")} — ${reason}`, "error", 5000);
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
    removePendingImage(i) {
      // preview is now a data URL (base64) — no revoke needed.
      this.pendingImages.splice(i, 1);
    },
    removePendingDoc(i) { this.pendingDocs.splice(i, 1); },

    // ===== image annotation editor (L1) =====
    // Open the editor over pendingImages[i]. The chip must have already
    // finished its initial upload (img.uploading false) AND retained its
    // File ref (entry.file, stashed at attach time). We decode the file
    // into an ImageBitmap, draw it onto the canvas at up to 1280px on the
    // long edge (matches _maybeCompressImage's cap), and start the history
    // stack with this clean frame.
    async openImageEditor(i) {
      const entry = this.pendingImages[i];
      if (!entry || !entry.file) {
        this.toast(this.lang === "zh"
          ? "无法编辑此图（原图引用丢失）"
          : "Can't edit — image source missing", "warn", 3000);
        return;
      }
      this.imageEditor.show = true;
      this.imageEditor.entryIndex = i;
      this.imageEditor.history = [];
      this.imageEditor.historyIdx = -1;
      this.imageEditor._drawing = false;
      this.imageEditor._baseBitmap = null;
      this.imageEditor._snapshot = null;
      // Defer canvas init until Alpine has rendered the modal — $refs is
      // only populated for visible elements.
      await this.$nextTick();
      try {
        await this._initImageEditorCanvas(entry.file);
      } catch (e) {
        console.error("[imgEditor] init failed:", e);
        this.toast(this.lang === "zh" ? "图片加载失败" : "Failed to load image",
                    "error", 3000);
        this.imageEditor.show = false;
      }
    },

    async _initImageEditorCanvas(file) {
      const canvas = this.$refs.imgEditorCanvas;
      if (!canvas) throw new Error("canvas ref missing");
      // Decode → ImageBitmap. Honors EXIF orientation when supported so
      // portrait photos taken on iPhone don't render sideways.
      let bitmap;
      try {
        bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
      } catch (_) {
        bitmap = await createImageBitmap(file);
      }
      // Cap canvas at 1280px on the long edge — matches our compress ceiling.
      // Going larger would burn memory on phones and add latency to every
      // history-snapshot toDataURL call.
      const MAX = 1280;
      const ratio = Math.min(1, MAX / Math.max(bitmap.width, bitmap.height));
      const w = Math.max(1, Math.round(bitmap.width * ratio));
      const h = Math.max(1, Math.round(bitmap.height * ratio));
      canvas.width = w; canvas.height = h;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, w, h);
      ctx.drawImage(bitmap, 0, 0, w, h);
      // Keep the bitmap around — eraser tool re-draws regions from it.
      this.imageEditor._baseBitmap = bitmap;
      // Seed history with the clean frame so the first undo returns here.
      this._pushImageEditHistory();
      // Wire pointer events. Stored on the canvas itself so they auto-clean
      // when the modal closes and Alpine x-show hides the parent.
      this._bindImageEditorPointer(canvas);
    },

    _bindImageEditorPointer(canvas) {
      const ed = this.imageEditor;
      // Translate a pointer's clientX/Y into canvas pixel coordinates.
      // CSS may scale the canvas down (e.g. on phones canvas pixels =
      // 1280×960 but rendered at 360×270) — without rescaling, strokes
      // would appear "drift" by the inverse of that ratio.
      const getPos = (ev) => {
        const rect = canvas.getBoundingClientRect();
        const sx = canvas.width / rect.width;
        const sy = canvas.height / rect.height;
        return { x: (ev.clientX - rect.left) * sx,
                  y: (ev.clientY - rect.top) * sy };
      };

      canvas.onpointerdown = (ev) => {
        ev.preventDefault();
        try { canvas.setPointerCapture(ev.pointerId); } catch (_) {}
        const { x, y } = getPos(ev);
        ed._drawing = true;
        ed._startX = x; ed._startY = y;
        const ctx = canvas.getContext("2d");
        // Snapshot before drawing — used by rect/arrow for live preview.
        try {
          ed._snapshot = ctx.getImageData(0, 0, canvas.width, canvas.height);
        } catch (_) { ed._snapshot = null; }
        if (ed.tool === "pen") {
          ctx.beginPath();
          ctx.moveTo(x, y);
        } else if (ed.tool === "eraser") {
          this._eraseAt(ctx, x, y);
        }
      };

      canvas.onpointermove = (ev) => {
        if (!ed._drawing) return;
        const { x, y } = getPos(ev);
        const ctx = canvas.getContext("2d");
        if (ed.tool === "pen") {
          ctx.strokeStyle = ed.color;
          ctx.lineWidth = ed.size;
          ctx.lineCap = "round";
          ctx.lineJoin = "round";
          ctx.lineTo(x, y);
          ctx.stroke();
          ctx.beginPath();
          ctx.moveTo(x, y);
        } else if (ed.tool === "eraser") {
          this._eraseAt(ctx, x, y);
        } else if (ed.tool === "rect" && ed._snapshot) {
          // Restore pre-stroke state + redraw rect from start to current.
          ctx.putImageData(ed._snapshot, 0, 0);
          ctx.strokeStyle = ed.color;
          ctx.lineWidth = ed.size;
          ctx.strokeRect(ed._startX, ed._startY,
                          x - ed._startX, y - ed._startY);
        } else if (ed.tool === "arrow" && ed._snapshot) {
          ctx.putImageData(ed._snapshot, 0, 0);
          this._drawArrow(ctx, ed._startX, ed._startY, x, y, ed.color, ed.size);
        }
      };

      const endStroke = (ev) => {
        if (!ed._drawing) return;
        ed._drawing = false;
        try { canvas.releasePointerCapture(ev.pointerId); } catch (_) {}
        ed._snapshot = null;
        // Snapshot the new state for undo.
        this._pushImageEditHistory();
      };
      canvas.onpointerup = endStroke;
      canvas.onpointercancel = endStroke;
      // Pointer-leave does NOT end the stroke — we want the user to drag
      // off the canvas and back (e.g. extending an arrow), which is the
      // standard freehand-tool behavior.
    },

    // Erase by re-drawing the original bitmap into a circular clip region.
    // This restores the actual image pixels at that spot (so an annotation
    // disappears AND the underlying photo content comes back through),
    // which is what users expect from "eraser" — the naive "paint white"
    // approach would leave a white blob over an otherwise correct photo.
    _eraseAt(ctx, x, y) {
      const ed = this.imageEditor;
      if (!ed._baseBitmap) return;
      const r = Math.max(8, ed.size * 4);
      ctx.save();
      ctx.beginPath();
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      ctx.clip();
      const canvas = ctx.canvas;
      ctx.drawImage(ed._baseBitmap, 0, 0, canvas.width, canvas.height);
      ctx.restore();
    },

    _drawArrow(ctx, x1, y1, x2, y2, color, size) {
      const headLen = Math.max(12, size * 3.5);
      const angle = Math.atan2(y2 - y1, x2 - x1);
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = size;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      // Shaft — stop slightly short of the tip so the line doesn't poke
      // out the front of the arrowhead triangle.
      const shaftEndX = x2 - Math.cos(angle) * headLen * 0.5;
      const shaftEndY = y2 - Math.sin(angle) * headLen * 0.5;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(shaftEndX, shaftEndY);
      ctx.stroke();
      // Filled arrowhead.
      ctx.beginPath();
      ctx.moveTo(x2, y2);
      ctx.lineTo(x2 - headLen * Math.cos(angle - Math.PI / 6),
                  y2 - headLen * Math.sin(angle - Math.PI / 6));
      ctx.lineTo(x2 - headLen * Math.cos(angle + Math.PI / 6),
                  y2 - headLen * Math.sin(angle + Math.PI / 6));
      ctx.closePath();
      ctx.fill();
    },

    _pushImageEditHistory() {
      const canvas = this.$refs.imgEditorCanvas;
      if (!canvas) return;
      const ed = this.imageEditor;
      // If user undid then drew new content, discard redo branch.
      if (ed.historyIdx < ed.history.length - 1) {
        ed.history = ed.history.slice(0, ed.historyIdx + 1);
      }
      // JPEG @ 70% is the sweet spot — ~100 KB per 1280×960 frame.
      // PNG would be 2-3× bigger and slower to encode.
      const url = canvas.toDataURL("image/jpeg", 0.7);
      ed.history.push(url);
      // Cap depth to bound memory.
      const HIST_CAP = 15;
      if (ed.history.length > HIST_CAP) {
        ed.history = ed.history.slice(ed.history.length - HIST_CAP);
      }
      ed.historyIdx = ed.history.length - 1;
    },

    canUndoImageEdit() { return this.imageEditor.historyIdx > 0; },

    undoImageEdit() {
      const ed = this.imageEditor;
      if (ed.historyIdx <= 0) return;
      ed.historyIdx--;
      this._restoreImageEditFrame(ed.history[ed.historyIdx]);
    },

    _restoreImageEditFrame(url) {
      const canvas = this.$refs.imgEditorCanvas;
      if (!canvas || !url) return;
      const img = new Image();
      img.onload = () => {
        const ctx = canvas.getContext("2d");
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      };
      img.src = url;
    },

    // Close the editor. commit=true → flatten canvas to JPEG, replace the
    // chip's File ref, re-thumbnail, re-upload. commit=false → just close.
    // Either way we drop the history stack + base bitmap so memory comes
    // back. The modal's x-show hides the DOM; pointer handlers will be
    // re-bound when the editor opens next time.
    async closeImageEditor(commit) {
      if (commit) {
        await this._saveImageEdit();
      }
      const ed = this.imageEditor;
      ed.show = false;
      ed.entryIndex = -1;
      ed.history = [];
      ed.historyIdx = -1;
      if (ed._baseBitmap && ed._baseBitmap.close) {
        try { ed._baseBitmap.close(); } catch (_) {}
      }
      ed._baseBitmap = null;
      ed._snapshot = null;
    },

    async _saveImageEdit() {
      const canvas = this.$refs.imgEditorCanvas;
      const i = this.imageEditor.entryIndex;
      const entry = this.pendingImages[i];
      if (!canvas || !entry) return;
      // Flatten the canvas to a JPEG blob @ 88% — high enough to look
      // crisp, low enough that re-upload payload doesn't balloon.
      const blob = await new Promise(res => canvas.toBlob(res, "image/jpeg", 0.88));
      if (!blob) {
        this.toast(this.lang === "zh" ? "保存失败：导出 blob 失败"
                                       : "Save failed: blob export error",
                    "error", 3000);
        return;
      }
      const baseName = ((entry.file && entry.file.name) || "image")
                         .replace(/\.[^.]+$/, "");
      const file = new File([blob], baseName + ".jpg",
                              { type: "image/jpeg", lastModified: Date.now() });
      // In-place swap — keep the chip at its current array index so the
      // user's mental model ("the 2nd image is the one I just edited")
      // holds. Mark uploading so the chip shows the upload progress hairline.
      entry.file = file;
      entry.uploading = true;
      entry.error = false;
      entry.preview = await this._imageToThumbDataURL(file);
      // Re-upload to the same endpoint. Backend returns a fresh `id` —
      // tool_use messages built at send-time use this latest id.
      const fd = new FormData();
      fd.append("file", file);
      try {
        const r = await fetch("/api/chat/upload-image",
                                { method: "POST", headers: this.hdr(), body: fd });
        if (!r.ok) {
          entry.error = true; entry.uploading = false;
          let body = "";
          try { body = await r.text(); } catch (_) {}
          this.toast(`${this.t("img.upload_failed")} (HTTP ${r.status})${body ? ": " + body : ""}`,
                      "error", 4000);
          return;
        }
        const d = await r.json();
        entry.id = d.id; entry.uploading = false;
        if (d.attach_ext) entry.attach_ext = d.attach_ext;
      } catch (e) {
        entry.error = true; entry.uploading = false;
        const reason = (e && (e.name + (e.message ? ": " + e.message : "")))
                          || "network error";
        this.toast(`${this.t("img.upload_failed")} — ${reason}`, "error", 4000);
      }
    },

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
    // Format a millis-since-epoch timestamp as "HH:MM" in the user's
    // local timezone. Used by the per-turn footer to show when a
    // muse reply finished. Returns "" for falsy input so x-show can
    // gate on m.ts directly. 24h clock by design.
    fmtHM(ts) {
      if (!ts) return "";
      const d = new Date(ts);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      return hh + ":" + mm;
    },
    // Filter messages for the sidebar outline. Returns only user prompts
    // (skipping the auto-injected compact summaries) — they're what the
    // user remembers asking, so they make the best jump targets.
    outlineMessages() {
      // Touch reactivity ping so the modal re-renders when backend fetch
      // completes (same mechanism conversationOutline uses).
      const _ = this.outlineVersion;
      // Fire off a background backend fetch so the list reflects the
      // FULL session, not just the lazy-loaded visible window. This was
      // the source of "outline shows only 2 user messages on a 45-user
      // session" — the original filter walked this.messages which only
      // contains the recent slice after the long-history performance
      // optimization (commit 664304a).
      const sid = this.currentId;
      if (sid) this.refreshOutlineFromBackend(sid);
      // Primary: backend-sourced list, shaped to look like message
      // objects so the modal template (which calls outlineText(m) and
      // _scrollToUserMsg(m.uuid)) keeps working unchanged.
      const st = sid && this.tabState && this.tabState[sid];
      const backendList = st && st._backendOutline;
      if (Array.isArray(backendList) && backendList.length > 0) {
        return backendList.map(c => ({
          uuid: c.uuid,
          text: c.preview,         // outlineText reads .text first
          role: "user",
          ts: c.ts || null,
          _fromBackend: true,
        }));
      }
      // Fallback: live filter on the visible window (original behavior).
      return (this.messages || []).filter(
        m => m && m.role === "user" && !m._is_compact_summary);
    },
    // Outline click → scroll the chat to that user msg + flash highlight.
    // .msg[data-uuid] is rendered for every message (see chat template);
    // on mobile we also switch to the chat tab so the jump is visible.
    _scrollToUserMsg(m) {
      const uuid = m && m.uuid;
      if (!uuid) return;
      if (this._isMobileLayout()) this.mobileTab = "chat";
      const tryScroll = () => {
        const el = document.querySelector(
          `.msg[data-uuid="${CSS.escape(uuid)}"]`);
        if (!el) return false;
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.classList.add("msg-highlight");
        setTimeout(() => el.classList.remove("msg-highlight"), 2400);
        return true;
      };
      this.$nextTick(() => {
        if (tryScroll()) return;
        // Target not in DOM — it lives in the lazy stash. Find it there
        // and pull everything from that index forward into visible
        // messages, then retry scroll. Mirrors jumpToOutlineItem's
        // backend branch but for the modal outline path.
        const sid = this.currentId;
        const st = sid && this.tabState && this.tabState[sid];
        const earlier = (st && st._earlierMessages) || [];
        const idx = earlier.findIndex(em => em && em.uuid === uuid);
        if (idx < 0) {
          // Not in DOM and not in the in-memory stash. Two possibilities:
          //  1) Older post-compact history still on the server — page a
          //     window back from the backend, then retry (cheap, repeats
          //     until the target lands in the stash or we exhaust offset).
          //  2) PRE-compaction (absent from the post-compact chain entirely)
          //     — reload in full raw-JSONL mode once. _fullLoaded guards
          //     against an infinite reload loop if the uuid isn't on disk.
          if (st && st._loadedOffset > 0 && !st._fetchingOlder) {
            (async () => {
              const pulled = await this._fetchOlderWindow(sid);
              if (pulled > 0) {
                this.$nextTick(() => this._scrollToUserMsg(m));
              } else if (!st._fullLoaded) {
                await this.loadSession(sid, { full: true });
                this.$nextTick(() => this._scrollToUserMsg(m));
              }
            })();
            return;
          }
          if (st && !st._fullLoaded) {
            (async () => {
              await this.loadSession(sid, { full: true });
              this.$nextTick(() => this._scrollToUserMsg(m));
            })();
          }
          return;
        }
        const batch = earlier.splice(idx);
        batch.forEach(em => {
          if (em.role === "assistant" && em.text && !em.html) {
            em.html = this.mdRender(em.text);
          }
        });
        const oldScrollEl = this.$refs.chatBody;
        const oldScrollHeight = oldScrollEl ? oldScrollEl.scrollHeight : 0;
        const oldScrollTop = oldScrollEl ? oldScrollEl.scrollTop : 0;
        st.messages.unshift(...batch);
        this.messages = st.messages;
        st._hasMoreHistory =
          (st._earlierMessages || []).length > 0 || st._loadedOffset > 0;
        this.$nextTick(() => {
          if (oldScrollEl) {
            const newScrollHeight = oldScrollEl.scrollHeight;
            oldScrollEl.scrollTop = oldScrollTop + (newScrollHeight - oldScrollHeight);
          }
          // Scope highlight to the bubbles we just unshifted (the first
          // batch.length `.msg` children) rather than rescanning the whole
          // chat body.
          const newEls = this._leadingMsgEls(batch.length);
          this.highlightCode(".chat-body", newEls.length ? newEls : null);
          setTimeout(tryScroll, 50);
        });
      });
    },
    // Short preview text for an outline row — first line, trimmed.
    outlineText(m) {
      const t = (m && (m.text || m.body || "")) || "";
      const oneLine = t.replace(/\s+/g, " ").trim();
      return oneLine.slice(0, 60) || (this.lang === "zh" ? "（无文本）" : "(no text)");
    },
    // Manual toggle — used by the floating button. Necessary fallback for
    // iframe / pdf / img preview where onPreviewScroll can't fire because
    // scroll events don't cross the sandbox boundary.
    toggleImmersive() {
      this.previewImmersive = !this.previewImmersive;
      document.body.classList.toggle("preview-immersive",
                                     this.previewImmersive);
    },
    // Mobile preview scroll — toggle immersive mode (hide top header + tab
    // bar + bottom-nav) based on scroll direction. Desktop: no-op.
    onPreviewScroll(el) {
      if (!el || window.innerWidth > 900) return;
      if (this.mobileTab !== "preview") return;
      const cur = el.scrollTop;
      const last = this._lastPreviewScrollTop || 0;
      const delta = cur - last;
      // Near the top → always show (so the user can reach the file name /
      // tab bar without scrolling back up first).
      if (cur < 50) {
        if (this.previewImmersive) {
          this.previewImmersive = false;
          document.body.classList.remove("preview-immersive");
        }
        this._lastPreviewScrollTop = cur;
        return;
      }
      // Scrolling down ≥30px → hide bars.
      if (delta > 30) {
        if (!this.previewImmersive) {
          this.previewImmersive = true;
          document.body.classList.add("preview-immersive");
        }
        this._lastPreviewScrollTop = cur;
      // Scrolling up ≥10px → show bars (lighter threshold so it feels
      // responsive when the user wants navigation back).
      } else if (delta < -10) {
        if (this.previewImmersive) {
          this.previewImmersive = false;
          document.body.classList.remove("preview-immersive");
        }
        this._lastPreviewScrollTop = cur;
      }
      // Sub-threshold deltas: don't update last — let movement accumulate.
    },
    // Elapsed-stream formatting used by the streaming dots in both the
    // turn-footer (bottom of every just-finished/in-progress assistant
    // turn) and the pending bubble (first-token wait state). Format:
    //   < 1s      → "" (suppressed — too noisy mid-fast-replies)
    //   1-59s     → "12s"
    //   1-59m     → "2m50s"
    //   ≥ 60m     → "1h05m" (extremely long agentic runs)
    // Compact, no decimals at minute granularity — second-precision past
    // ~30s adds visual noise without information value.
    fmtStreamElapsed(secs) {
      if (!secs || secs < 1) return "";
      const s = Math.floor(secs);
      if (s < 60) return `${s}s`;
      const m = Math.floor(s / 60);
      const rs = s % 60;
      if (m < 60) return `${m}m${String(rs).padStart(2, "0")}s`;
      const h = Math.floor(m / 60);
      const rm = m % 60;
      return `${h}h${String(rm).padStart(2, "0")}m`;
    },
    // Footer time: today → HH:MM, same year → MM-DD HH:MM, cross-year → YYYY-MM-DD HH:MM.
    fmtTurnTime(ts) {
      if (!ts) return "";
      const d = new Date(ts);
      const now = new Date();
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const sameDay = d.getFullYear() === now.getFullYear()
                       && d.getMonth() === now.getMonth()
                       && d.getDate() === now.getDate();
      if (sameDay) return `${hh}:${mm}`;
      const M = String(d.getMonth() + 1).padStart(2, "0");
      const D = String(d.getDate()).padStart(2, "0");
      if (d.getFullYear() === now.getFullYear()) return `${M}-${D} ${hh}:${mm}`;
      return `${d.getFullYear()}-${M}-${D} ${hh}:${mm}`;
    },
    // True when index i in messages[] is the tail of a turn — i.e. it
    // is muse-side AND the next message is either nonexistent or
    // user-role. Used by the per-turn footer (index.html) to decide
    // which message gets the footer rendered underneath it. Cheap O(1)
    // lookup; Alpine re-evaluates it per render which is fine since
    // it's a few comparisons.
    isTurnTail(i) {
      const arr = this.messages;
      if (!arr || i < 0 || i >= arr.length) return false;
      const m = arr[i];
      if (!m || m.role === "user") return false;
      const next = arr[i + 1];
      return !next || next.role === "user";
    },
    // Normalize a model-emitted path into something openByPathToasted can hand
    // to /api/files/list. Handles three things the model commonly does wrong:
    //   - absolute path under ROOT  →  strip ROOT prefix
    //   - "~/..." path              →  return "" (we don't know HOME-vs-ROOT)
    //   - path prefixed by ROOT's basename (e.g. "muselab-archive/health/x.md"
    //     when ROOT itself is /home/u/muselab-archive) → strip the duplicate
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
      // Model often writes "<root-basename>/foo/bar.md" thinking the archive
      // root is the parent of the archive directory. Strip the basename of
      // ROOT if it's the first segment and stripping leaves a non-empty
      // remainder.
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
      if (!_FILE_TOOLS.has(m.name)) return "";
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

    // ===== MCP tool enrichment =====
    // Known MCP servers get a recognizable emoji icon + short label so
    // the user can quickly scan "ah, that's a Gmail call" vs "that's a
    // memory write" vs "that's a custom muselab tool" — instead of every
    // mcp__* call looking the same. Unknown servers fall back to the
    // generic plug icon.
    MCP_SERVER_ICONS: {
      // Google ecosystem
      "Gmail":                "📧",  "gmail":                "📧",
      "Google_Calendar":      "📅",  "google_calendar":      "📅",
      "Google_Drive":         "💾",  "google_drive":         "💾",
      "Google_Docs":          "📄",
      // Dev / collaboration
      "github":               "🐙",  "GitHub":               "🐙",
      "git":                  "⎇",
      "linear":               "📋",
      "slack":                "💬",
      "notion":               "📝",
      // Cognitive / data
      "memory":               "🧠",
      "sequential-thinking":  "🤔",
      "filesystem":           "📁",
      "time":                 "⏰",
      "fetch":                "🌐",
      // muselab-internal
      "muselab":              "🎭",
    },
    // Parse mcp__<server>__<tool> into a UI-friendly descriptor.
    // Server names sometimes carry an OAuth-provider prefix like
    // "claude_ai_Gmail" — we strip the known prefix to find the icon.
    mcpServerInfo(toolName) {
      if (!toolName || !toolName.startsWith("mcp__")) return null;
      const rest = toolName.slice(5);
      const parts = rest.split("__");
      let rawServer = parts[0] || "";
      const tool = parts.slice(1).join(" · ");
      // Strip OAuth provider prefix if present ("claude_ai_Gmail" → "Gmail")
      let cleanServer = rawServer;
      const OAUTH_PREFIXES = ["claude_ai_", "claude_ai__"];
      for (const p of OAUTH_PREFIXES) {
        if (cleanServer.startsWith(p)) {
          cleanServer = cleanServer.slice(p.length);
          break;
        }
      }
      const icon = this.MCP_SERVER_ICONS[cleanServer] || "🔌";
      return {
        rawServer,
        serverLabel: cleanServer.replace(/_/g, " "),
        toolLabel: tool,
        icon,
      };
    },
    // Pretty-print MCP tool input for the bubble — compact, with smart
    // truncation. Long values (>60 chars) get an ellipsis.
    mcpInputPreview(m) {
      const inp = m && m.input;
      if (!inp || typeof inp !== "object") return "";
      const pairs = [];
      for (const [k, v] of Object.entries(inp)) {
        let val;
        if (v === null || v === undefined) val = "—";
        else if (typeof v === "string") {
          val = v.length > 60 ? v.slice(0, 57) + "…" : v;
        }
        else if (typeof v === "boolean" || typeof v === "number") val = String(v);
        else if (Array.isArray(v)) val = `[${v.length} items]`;
        else val = "{...}";
        pairs.push(`${k}: ${val}`);
      }
      return pairs.slice(0, 3).join(" · ")
              + (pairs.length > 3 ? ` · +${pairs.length - 3} more` : "");
    },
    // Try to render MCP tool result intelligently. JSON gets pretty-printed,
    // arrays become bullet lists, plain text passes through. Falls back to
    // raw text on parse failure (always safe — never throws).
    mcpResultFormatted(m) {
      const text = (m && (m.text || m.preview)) || "";
      // Memo: the template calls this TWICE per render (value slice + md
      // render) and the message x-for re-invokes on every render. JSON.parse
      // of a large MCP payload each time is wasteful. Cache keyed by raw msg,
      // re-derived only if the source text actually changed (streaming-safe).
      const _raw = _rawMsg(m);
      const _hit = _mcpFmtCache.get(_raw);
      if (_hit && _hit.src === text) return _hit.value;
      const value = this._computeMcpResultFormatted(text);
      _mcpFmtCache.set(_raw, { src: text, value });
      return value;
    },
    _computeMcpResultFormatted(text) {
      if (!text) return { kind: "empty", value: "" };
      const trimmed = text.trim();
      // Try JSON first
      if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
        try {
          const parsed = JSON.parse(trimmed);
          if (Array.isArray(parsed)) {
            return { kind: "array", value: parsed };
          }
          return { kind: "object", value: JSON.stringify(parsed, null, 2) };
        } catch (e) { /* fall through to text */ }
      }
      return { kind: "text", value: text };
    },

    // Memoized markdown render — the single most expensive call in the app
    // (marked + DOMPurify + a detached DOM build + KaTeX scan + linkify
    // querySelectorAll). The message list re-invokes it inline for every
    // tool-result / MCP / plan bubble on re-render, and the load-earlier path
    // runs it in batches; without a cache those are all full re-parses. Cache
    // is keyed by exact input text and LRU-evicted at a hard cap so memory
    // stays bounded. The live streaming bubble deliberately bypasses this
    // (calls _mdRenderUncached) so its ever-growing intermediate strings never
    // pollute the cache or evict useful static-message entries.
    mdRender(text) {
      if (!text) return "";
      const cache = this._mdCache || (this._mdCache = new Map());
      const hit = cache.get(text);
      if (hit !== undefined) {
        cache.delete(text); cache.set(text, hit);   // bump LRU recency
        return hit;
      }
      const out = this._mdRenderUncached(text);
      cache.set(text, out);
      if (cache.size > 400) cache.delete(cache.keys().next().value);
      return out;
    },

    // opts.streaming === true → cheap path used on every throttled re-render
    // of an in-flight bubble: parse + sanitize only. The expensive passes
    // (KaTeX math typesetting + the full-DOM _linkifyFilePaths walk) are
    // skipped mid-stream and run ONCE on the final render (flushRender →
    // renderNow(true) → opts.streaming falsy). Doing them on every chunk was
    // a big chunk of the "long reply pegs the phone CPU / freezes" cost:
    // each tick re-walked the entire rendered DOM for code/anchor nodes and
    // re-ran KaTeX over the whole bubble. File-links + math only need to be
    // live once the message is complete.
    _mdRenderUncached(text, opts = {}) {
      if (!text) return "";
      const streaming = !!opts.streaming;
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
      // KaTeX is lazy-loaded — kick off the load if not yet present so the
      // NEXT mdRender call has math (this render falls through with plain
      // delimiters). Cheap heuristic: only trigger if the input looks like
      // it contains math, avoiding the network cost for pure-prose messages.
      // Mid-stream cheap path: return the sanitized HTML as-is, skipping the
      // KaTeX typeset + file-path linkify DOM walk below. These run on the
      // final render only (see opts.streaming docstring above).
      if (streaming) return safe;
      const tmp = document.createElement("div");
      tmp.innerHTML = safe;
      const looksLikeMath = /\$\$|\\\(|\\\[|\$[^$\n]+\$/.test(text);
      if (!window.renderMathInElement && looksLikeMath) {
        this._loadKatex().then(() => {
          // Reactive re-render: bump a counter that downstream renderedMd
          // computations depend on so Alpine re-walks affected bindings.
          // Cheaper than re-running mdRender on every static message — only
          // future renders pick up KaTeX support; existing rendered HTML
          // stays unchanged (worst case: math shows as raw $...$ in older
          // bubbles until the user reloads, which is acceptable).
        }).catch((e) => console.warn("[muselab] katex lazy load failed:", e));
      }
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
        if (/^[a-z]+:/i.test(href)) {                  // http: / https: / mailto: / etc.
          // External web links open in a NEW tab so a click never unloads the
          // chat SPA (the default same-tab navigation threw the user out of
          // the conversation). rel guards against tab-nabbing + referrer leak.
          // Non-web schemes (mailto:/tel:/…) are left untouched.
          if (/^https?:/i.test(href)) {
            a.setAttribute("target", "_blank");
            a.setAttribute("rel", "noopener noreferrer");
          }
          continue;
        }
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
      // External web link: force a NEW browsing context explicitly. In a
      // standalone PWA a plain target=_blank anchor often navigates the app
      // window itself (no tab concept), throwing the user out of the chat.
      // window.open breaks out to the system browser reliably across PWA/web.
      if (/^https?:/i.test(href)) {
        ev.preventDefault();
        window.open(href, "_blank", "noopener,noreferrer");
        return;
      }
      if (/^[a-z]+:/i.test(href)) return;             // mailto:/tel:/… → let browser handle
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
        // Mirror the click into the file tree: expand parents + scroll the
        // row into view so the user sees where the file lives. Use background
        // mode — a chat-link click wants the file's CONTENT (the preview
        // pane), so we must NOT let interactive reveal hijack mobileTab to
        // "files". Best-effort: never let a tree-sync hiccup swallow the open.
        this.revealInTree(path, { mode: "background" }).catch(() => {});
      } catch (e) {
        this.toast(this.lang === "zh" ? `打开失败：${path}` : `Open failed: ${path}`, "warn");
      }
    },

    // Open a background-task result file. Its output_file lives under
    // /tmp/claude-<uid>/.../tasks/<id>.output — OUTSIDE the archive root, so
    // the archive-scoped /api/files/read (and openByPathToasted's parent-dir
    // list) can't reach it. Route through the dedicated /api/chat/task-output
    // endpoint via openFile's readUrl override. No tree reveal (not in tree).
    async openTaskOutput(ts) {
      if (!ts || !ts.output_file) {
        this.toast(this.lang === "zh" ? "没有结果文件" : "No result file", "warn");
        return;
      }
      const path = ts.output_file;
      const name = path.split("/").pop();
      const url = "/api/chat/task-output?session_id="
        + encodeURIComponent(this.currentId)
        + "&path=" + encodeURIComponent(path);
      try {
        await this.openFile({ path, name }, { readUrl: url });
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
        this._setLS("muselab_token", this.token);
        this.authed = true;
        this.loadPrefs();
        await this.loadRoot();
        await this.initSessions();
        this.fetchStats();
        this.loadTrash();
        // Restore the preview file the user was looking at before refresh.
        // openFile is idempotent on tabs[] (won't duplicate); if the file
        // no longer exists we silently no-op (no toast — refresh restoration).
        if (this._pendingPreviewSelected) {
          const path = this._pendingPreviewSelected;
          this._pendingPreviewSelected = null;
          // Preserve the restored tab's preview/pinned state (see _bootApp).
          const _restored = this.tabs.find(t => t.path === path);
          this.openFile({ path, name: path.split("/").pop() },
                        { preview: !!(_restored && _restored.preview) })
              .catch(() => { /* file went away — nothing to do */ });
        }
        // First sign-in never routes through _bootApp (it inlines a subset of
        // the boot work above), so without this the new user has no heartbeat
        // / stale-JS reload / presence reporting / bell badge refresh until a
        // manual refresh. Shared with _bootApp; safe to call once here.
        this._startLiveConnections();
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
    // ===== localStorage wrapper =====
    // Browsers cap localStorage at ~5–10 MB per origin and throw
    // QuotaExceededError on setItem. Hot writers (savePrefs runs on
    // every tab open/close/scroll/expand) used to call setItem directly,
    // so any user accumulating enough sessions+prefs+queue would hit the
    // cap and the next pref save would crash the call site mid-stack.
    // Centralize the writes here so:
    //   1. Quota / private-mode / disabled-storage throws never escape.
    //   2. Failure surfaces ONCE per session as a non-blocking toast,
    //      not every keystroke (Date.now-based rate limit).
    //   3. All call sites read the same JSON-stringify discipline (caller
    //      passes pre-stringified value — we do NOT auto-stringify so
    //      bare strings like the token don't get double-quoted).
    // Returns true on success, false on failure.
    _lsLastWarnAt: 0,
    _setLS(key, value) {
      try {
        localStorage.setItem(key, value);
        return true;
      } catch (e) {
        // Quota errors come back with different `name`s across browsers
        // ("QuotaExceededError" / "NS_ERROR_DOM_QUOTA_REACHED" / code 22
        // / 1014). Detection isn't critical — every failure is treated
        // the same way.
        if (Date.now() - this._lsLastWarnAt > 60_000) {
          this._lsLastWarnAt = Date.now();
          // Best-effort toast; if `toast` itself isn't ready (very early
          // boot), swallow silently — the next save attempt warns.
          try {
            const msg = this.lang === "zh"
              ? "本地存储已满，部分偏好可能不会保存。建议清理对话历史或导出/重置。"
              : "Local storage is full — some preferences may not persist. Consider clearing chat history or resetting.";
            this.toast?.(msg, "warn", 6000);
          } catch {}
        }
        return false;
      }
    },
    // Symmetrical getter — also tolerates JSON.parse failures so a
    // corrupt key (manual edit, partial write from a quota throw)
    // doesn't crash boot. Returns `fallback` on any error.
    _getLSJson(key, fallback = null) {
      try {
        const raw = localStorage.getItem(key);
        if (raw == null) return fallback;
        return JSON.parse(raw);
      } catch {
        return fallback;
      }
    },

    // Convenience: bilingual error toast for common "<verb> failed: <body>"
    // patterns. Call sites used to inline `this.toast("保存失败：" + …, "error")`
    // which gave English users a Chinese-only message. Pass the verb key
    // ("save" / "delete" / "rename" / "upload" / "create" / "load") and the
    // raw error body; we render the right prefix for the user's lang.
    errToast(verbKey, body) {
      const zhPrefix = ({
        save: "保存失败：", delete: "删除失败：", rename: "重命名失败：",
        upload: "上传失败：", create: "创建失败：", load: "加载失败：",
        copy: "复制失败：", read: "无法读取文件：", generic: "失败：",
      })[verbKey] || "失败：";
      const enPrefix = ({
        save: "Save failed: ", delete: "Delete failed: ",
        rename: "Rename failed: ", upload: "Upload failed: ",
        create: "Create failed: ", load: "Load failed: ",
        copy: "Copy failed: ", read: "Cannot read file: ",
        generic: "Failed: ",
      })[verbKey] || "Failed: ";
      const prefix = this.lang === "zh" ? zhPrefix : enPrefix;
      this.toast(prefix + (body || ""), "error");
    },
    toast(msg, type = "info", timeout = null, action = null) {
      // Default timeout depends on severity: errors need to stay long
      // enough to read (and copy if needed); info/success can fade fast.
      // Explicit timeout arg always wins (1500ms for "copied" toasts etc).
      if (timeout === null) {
        timeout = type === "error" ? 6000 : 3000;
      }
      const id = ++this._toastId;
      this.toasts.push({ id, msg, type, action });
      // Bound the stack. Persistent toasts (falsy timeout — e.g. the "open
      // finished background task" prompt at the only timeout=0 call site)
      // never auto-dismiss, so without a cap they accumulate in this.toasts
      // indefinitely. Keep the most recent few; anything older is stale by
      // the time a 7th appears. (perf: ORANGE — app.js toasts unbounded)
      const MAX_TOASTS = 6;
      if (this.toasts.length > MAX_TOASTS) {
        this.toasts.splice(0, this.toasts.length - MAX_TOASTS);
      }
      if (timeout) setTimeout(() => this.dismissToast(id), timeout);
    },
    dismissToast(id) { this.toasts = this.toasts.filter(t => t.id !== id); },
    runToastAction(t) {
      try { t.action && t.action.onClick && t.action.onClick(); }
      finally { this.dismissToast(t.id); }
    },

    // ===== modal =====
    confirm({ title, body = "", okText, cancelText, danger = false }) {
      // Don't depend on this.t() for default labels — in some call paths
      // (observed 2026-05-28 from deleteSchedTask) `this.t` evaluates to
      // undefined and the entire confirm flow throws "this.t is not a
      // function" before the modal even opens (so the user sees nothing
      // happen). Inline the zh/en strings directly off this.lang to
      // sidestep whatever's eating the i18n method in that context.
      const zh = this.lang === "zh";
      if (!title) title = zh ? "确认" : "Confirm";
      if (!okText) okText = zh ? "确认" : "Confirm";
      if (!cancelText) cancelText = zh ? "取消" : "Cancel";
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
      // Same defensive pattern as confirm() above — avoid this.t for defaults.
      const zh = this.lang === "zh";
      if (!title) title = zh ? "输入" : "Input";
      if (!okText) okText = zh ? "确认" : "Confirm";
      if (!cancelText) cancelText = zh ? "取消" : "Cancel";
      return new Promise((resolve) => {
        this.modal = {
          show: true, title, body, input: value, placeholder,
          okText, cancelText, danger: false,
          confirm: () => { const v = this.modal.input; this.modal.show = false; resolve(v); },
          cancel: () => { this.modal.show = false; resolve(null); },
        };
        this.$nextTick(() => { if (this.$refs.modalInput) this.$refs.modalInput.focus(); });
      });
    },

    // Hard reload escape hatch. The hairline progress / streaming flag /
    // EventSource state are all in-memory; if any of them get wedged (rare
    // but it happens), there's no graceful in-app reset. Reload nukes
    // everything, then loadPrefs restores currentId / openTabIds / preview
    // selection / mobileTab from localStorage, and the SSE auto-reconnect
    // path picks up any still-running backend turn from active sidecar.
    // The toast is more than UX polish: on slow networks the user might
    // tap before the unload finishes, so a visible "正在刷新…" confirms
    // the click registered.
    reloadApp() {
      // Best-effort: persist current state first in case savePrefs hasn't
      // run recently (e.g. user has been streaming for a while and prefs
      // didn't change). Cheap, idempotent.
      try { this.savePrefs(); } catch (_) {}
      this.toast(this.lang === "zh" ? "正在刷新…" : "Reloading…", "info", 1500);
      // Slight delay lets the toast render before the page tears down.
      setTimeout(() => { location.reload(); }, 150);
    },

    // Soft chat refresh — no full page reload. Re-fetches sessions, context
    // info, models, and the current session's messages. Covers the common
    // "chat feels stale / stuck" case without destroying the user's
    // browser state (scroll position, open file tabs, typed draft, etc.).
    async refreshChat() {
      this.toast(this.lang === "zh" ? "刷新中…" : "Refreshing…", "info", 1500);
      // Manual refresh is the user's "pull updates from other devices"
      // intent — same flush→pull dance as visibilitychange. Flush any
      // pending local ui-state PUT first so the subsequent GET doesn't
      // overwrite changes we haven't pushed yet; then refresh sessions
      // (pullUIStateAndMerge filters openTabIds by this.sessions, so
      // session list must land first); then merge remote ui-state.
      try { await this._flushUIStatePush(); } catch (_) {}
      await Promise.all([
        this.fetchContextInfo(),
        this.refreshSessions(),
        this.fetchStats(),
      ]);
      try { await this._pullUIStateAndMerge(); } catch (_) {}
      if (this.currentId) await this.loadSession(this.currentId);
    },

    // ===== prefs =====
    savePrefs() {
      // Preview-pane state (tabs, selected) persists too so a refresh restores
      // the exact files the user was looking at — matches the chat-tab strip's
      // behavior via openTabIds.
      this._setLS("muselab_prefs", JSON.stringify({
        schema: 2,          // bump when prefs format changes incompatibly
        model: this.model, permission: this.permission,
        currentId: this.currentId,
        openTabIds: this.openTabIds,
        previewTabs: this.tabs.map(t => ({ path: t.path, name: t.name, preview: !!t.preview })),
        previewSelected: this.selected,
        expanded: Array.from(this.expanded),
        leftOpen: this.leftOpen, rightOpen: this.rightOpen,
        leftWidth: this.leftWidth, rightWidth: this.rightWidth,
        showHidden: this.showHidden,
        openFilesCollapsed: this.openFilesCollapsed,
        openFilesHeight: this.openFilesHeight,
        // Mobile-only: remember which of the 3 tabs (files / preview / chat)
        // the user was last on so a PWA close+reopen lands them in the right
        // place. Without this, restoring `previewSelected` triggers openFile
        // which auto-switches to "preview" — meaning every reopen dumps the
        // user on the preview tab even if they were chatting before.
        mobileTab: this.mobileTab,
        // FIX ⑥: desktop fullscreen focus mode ("preview" / "chat" / "")
        // survives a refresh.
        desktopFullPane: this.desktopFullPane,
      }));
      // Fire-and-forget cross-device sync. Debounced inside.
      this._pushUIStateSoon();
    },
    // Cross-device sync of the tab strip, preview tab strip, AND currentId
    // via /api/settings/ui-state. Debounced because savePrefs() fires on
    // every tab open/close/reorder/preview-select — un-debounced we'd PUT
    // 5+ times for a single drag-reorder gesture. 300ms is short enough
    // that a tab change on device A shows up on device B as soon as B's
    // visibilitychange GET runs, but long enough to collapse bursts.
    //
    // currentId rides as last_session_id (the existing field — it always
    // meant "most recently active session ID"; we now treat it as the
    // cross-device authoritative current tab, not just a new-device boot
    // fallback). $watch("currentId") routes through this same path so
    // tab switches sync without a separate PUT race.
    _uiStatePayload() {
      return {
        last_session_id: this.currentId || null,
        open_tab_ids: Array.isArray(this.openTabIds) ? this.openTabIds : [],
        preview_tabs: (this.tabs || []).map(t => ({
          path: t.path, name: t.name || "", preview: !!t.preview,
        })),
      };
    },
    _pushUIStateSoon() {
      if (!this.token) return;                          // pre-login: no-op
      if (this._uiStatePushTimer) clearTimeout(this._uiStatePushTimer);
      this._uiStatePushTimer = setTimeout(() => {
        this._uiStatePushTimer = null;
        this._uiStatePushInflight = this.api("/api/settings/ui-state", {
          method: "PUT", json: this._uiStatePayload(),
        }).catch(() => {}).finally(() => { this._uiStatePushInflight = null; });
      }, 300);
    },
    // Force any pending PUT to flush + complete BEFORE the next GET so
    // visibilitychange doesn't pull stale remote state that overwrites
    // local changes we haven't pushed yet.
    async _flushUIStatePush() {
      if (this._uiStatePushTimer) {
        clearTimeout(this._uiStatePushTimer);
        this._uiStatePushTimer = null;
        try {
          await this.api("/api/settings/ui-state", {
            method: "PUT", json: this._uiStatePayload(),
          });
        } catch (_) {}
      }
      if (this._uiStatePushInflight) {
        try { await this._uiStatePushInflight; } catch (_) {}
      }
    },
    // Pull remote ui-state and merge into local openTabIds, preview tabs,
    // and currentId. Merge rules:
    //   - remote field undefined / not present → leave local alone (avoids
    //     wiping local state on a fresh install where remote has no record
    //     of these fields yet).
    //   - remote field is an array (incl. empty) → REPLACE local. Empty is
    //     a meaningful "user closed everything elsewhere" state.
    //   - openTabIds: filter to sessions that still exist server-side.
    //   - last_session_id: if remote says a different (valid + open) tab
    //     is current, ACTIVATE it via activateTab(). The other device is
    //     authoritative because it pushed more recently. No healing —
    //     currentId being cross-synced means the local view follows the
    //     last device that switched tabs.
    //   - previewTabs: replace wholesale. Content is lazy-fetched on click.
    //
    // To avoid yanking the user mid-conversation, we suppress the currentId
    // sync when the local tab is actively streaming (a turn would visually
    // jump away from the user). Local will catch up next visibilitychange.
    async _pullUIStateAndMerge() {
      if (!this.token) return false;
      let data;
      try {
        const r = await this.api("/api/settings/ui-state");
        if (!r.ok) return false;
        data = r.data || {};
      } catch (_) { return false; }
      let changed = false;
      const validIds = new Set((this.sessions || []).map(s => s.id));
      if (Array.isArray(data.open_tab_ids)) {
        const remote = data.open_tab_ids.filter(id => validIds.has(id));
        const before = JSON.stringify(this.openTabIds || []);
        if (JSON.stringify(remote) !== before) {
          this.openTabIds = remote;
          changed = true;
        }
      }
      // currentId sync via last_session_id. Only switch if (a) remote is
      // different from local, (b) the target session still exists, (c) it's
      // in the open tab strip, and (d) the local tab isn't streaming (don't
      // interrupt an in-flight turn the user is reading).
      const remoteCur = typeof data.last_session_id === "string"
        ? data.last_session_id : null;
      const localStreaming = !!(this.tabState && this.tabState[this.currentId]
                                 && this.tabState[this.currentId].streaming);
      if (remoteCur && remoteCur !== this.currentId
          && validIds.has(remoteCur)
          && (this.openTabIds || []).includes(remoteCur)
          && !localStreaming) {
        this.activateTab(remoteCur);
        changed = true;
      } else if (this.currentId && !(this.openTabIds || []).includes(this.currentId)
                 && (this.openTabIds || []).length) {
        // currentId got dropped by the openTabIds replacement above and
        // we couldn't (or didn't) jump to the remote current → fall back
        // to the first surviving tab so we don't end up on a closed one.
        this.activateTab(this.openTabIds[0]);
        changed = true;
      }
      if (Array.isArray(data.preview_tabs)) {
        const remote = data.preview_tabs
          .filter(t => t && typeof t.path === "string")
          .map(t => ({ path: t.path, name: t.name || t.path.split("/").pop(), preview: !!t.preview }));
        const before = JSON.stringify((this.tabs || []).map(t => ({ path: t.path, name: t.name, preview: !!t.preview })));
        const after = JSON.stringify(remote);
        if (before !== after) {
          this.tabs = remote;
          changed = true;
        }
      }
      // Persist the merged view to localStorage so a reload reflects what
      // we just synced. savePrefs() also re-pushes to server, but the
      // payload will match what's already there → server is a no-op.
      if (changed) {
        try { this.savePrefs(); } catch (_) {}
      }
      return changed;
    },

    loadPrefs() {
      try {
        const p = JSON.parse(localStorage.getItem("muselab_prefs") || "{}");
        if ((p.schema || 1) < 2) {
          // Prefs format changed — clear stale data to avoid partial restore
          try { localStorage.removeItem("muselab_prefs"); } catch (_) {}
          return;
        }
        if (p.model) this.model = p.model;
        if (p.permission) this.permission = p.permission;
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
        // Stash the mobile tab choice in a "pending" slot — actually applying
        // it has to wait until after _bootApp's openFile(previewSelected)
        // restoration runs, because openFile force-switches to "preview" on
        // mobile. _bootApp's tail re-applies _pendingMobileTab over that.
        if (typeof p.mobileTab === "string"
            && ["files", "preview", "chat"].includes(p.mobileTab)) {
          this._pendingMobileTab = p.mobileTab;
        }
        // FIX ⑥: restore desktop fullscreen focus mode. Only honored on a
        // desktop layout — mobile uses the single-pane @media layout, where a
        // lingering desktopFullPane would do nothing but is harmless.
        if (typeof p.desktopFullPane === "string"
            && ["", "preview", "chat"].includes(p.desktopFullPane)) {
          this.desktopFullPane = p.desktopFullPane;
          // Mirror toggleDesktopFull's invariant: fullscreen chat needs the
          // right pane open or it restores to a blank screen (chat is hidden
          // by .pane-hidden when rightOpen is false). rightOpen is restored
          // just above, but guard against a persisted false slipping through.
          if (p.desktopFullPane === "chat") this.rightOpen = true;
        }
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
      await this.fetchMcp();
      try {
        const r = await fetch("/api/chat/providers", { headers: this.hdr() });
        if (r.ok) {
          this.availableModels = (await r.json()).models || [];
          this._ensureValidModel();
          this._rebindModelSelect();
        }
      } catch {}
    },

    // Standalone MCP fetch — called both from fetchStats (initial / periodic)
    // and from toggleMcpDrawer (refresh on drawer open). Extracted so the
    // drawer open path doesn't trigger usage / providers fetches it doesn't
    // need.
    async fetchMcp() {
      try {
        const r = await fetch("/api/chat/mcp", { headers: this.hdr() });
        if (r.ok) this.mcp = await r.json();
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
      // If the new model doesn't honor the current effort (e.g. switched
      // from Opus 4.7 → Sonnet with effort=xhigh, or to any non-Claude
      // vendor), reset to "" (auto). Without this the option becomes
      // hidden by _effortAllowed but the select still reports the stale
      // value, and the backend would forward a no-op effort param on
      // every turn. Fire-and-forget the PATCH — the local reset already
      // makes the UI consistent.
      if (!this._effortAllowed(this.effort, newM)) {
        this.effort = "";
        this.onEffortChange();
      }

      // Decide empty vs has-messages from BOTH the persisted message_count
      // AND the in-memory messages array — take the max. Two failure modes
      // we need to cover simultaneously:
      //   (a) sessions list metadata loaded before messages stream in →
      //       this.messages temporarily empty but persisted count > 0 →
      //       prefer persisted count.
      //   (b) user switches model mid-first-turn (or before the FIRST turn's
      //       bump_session has fired) → persisted count still 0 but
      //       this.messages already has user + streaming-assistant bubbles →
      //       prefer in-memory length. The old single-source logic took
      //       persisted=0 here and silently switched without the "新建会话?"
      //       confirm (2026-05-23 user feedback).
      const persistedFromMeta = (cur && typeof cur.message_count === "number")
        ? cur.message_count : 0;
      const persistedCount = Math.max(persistedFromMeta, this.messages.length || 0);

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

    // ===== Effort knob =====
    // Effort changes don't fork or corrupt the transcript (they only affect
    // future-turn budget), so no confirm modal — PATCH in place, toast, done.
    // Backend disconnects the cached client so the next turn rebuilds with
    // the new value.
    //
    // Effort support varies by model. Per claude_agent_sdk types.py:
    //   - "low"/"medium"/"high"/"max"  → all Anthropic models honor
    //   - "xhigh"                       → Opus 4.7 / 4.8 only; SDK silently
    //                                     falls back to "high" on Sonnet / Haiku
    //   - non-Claude (DeepSeek / GLM / MiniMax) → param is forwarded to
    //                                     vendor's anthropic-compatible
    //                                     proxy but honoring is vendor-
    //                                     specific and undocumented;
    //                                     hide the entire dropdown rather
    //                                     than pretend it works
    // Per "无效的直接隐藏" feedback (2026-05-22) we don't grey-out — we
    // hide. User feedback: greyed options still look pick-able and waste
    // dropdown space.
    _isClaudeModel(model) {
      return (model || "").startsWith("claude-");
    },
    _isOpus47(model) {
      // Misnomer kept for blast-radius reasons: this gate fires for any
      // Opus model that supports the `xhigh` effort level. Per Anthropic's
      // effort-level docs, that is Opus 4.7 AND Opus 4.8 — both are listed
      // as "Available on Claude Opus 4.8 and Claude Opus 4.7". Future
      // Opus models will likely keep the privilege; extend this match
      // when they ship.
      const m = (model || "");
      return m.startsWith("claude-opus-4-7") || m.startsWith("claude-opus-4-8");
    },
    _supportsEffort(model) {
      // The whole dropdown shows only for Claude models.
      return this._isClaudeModel(model);
    },
    _supportsThinking(model) {
      // Thinking toggle shows for any provider whose endpoint honors the
      // standard Anthropic thinking config (provider-level supports_thinking,
      // surfaced per-model by /api/chat/providers). Vendors that reject it
      // (e.g. Qianfan) are hidden so the switch isn't a no-op. Default true
      // when the model isn't in the catalog yet (optimistic — matches the
      // backend, which enables thinking unless a provider opts out).
      const meta = (this.availableModels || []).find(x => x.model === model);
      if (!meta) return true;
      return meta.supports_thinking !== false;
    },
    _effortAllowed(level, model) {
      if (level === "") return true;            // "auto" always available
      if (!this._isClaudeModel(model)) return false;
      if (level === "xhigh") return this._isOpus47(model);
      return true;                              // low / medium / high / max
    },
    async onEffortChange() {
      if (!this.currentId) return;
      const e = this.effort || "";
      try {
        const r = await fetch("/api/chat/sessions/" + this.currentId, {
          method: "PATCH",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ effort: e }),
        });
        if (!r.ok) throw new Error(await r.text());
        const label = this.t("effort." + (e || "auto"));
        this.toast(this.t("effort.changed", { label }), "info", 1800);
        // Mirror into the session list cache so tab-switch sees the right value.
        const cur = this.sessions.find(s => s.id === this.currentId);
        if (cur) cur.effort = e;
      } catch (err) {
        this.toast(this.lang === "zh" ? "切换失败" : "Switch failed", "error");
      }
    },
    async onThinkingChange() {
      // Toggles the backend thinking CONFIG for this session (not display).
      // PATCH {thinking} → server rebuilds the client; next turn honors it.
      if (!this.currentId) return;
      const on = !!this.thinkingEnabled;
      try {
        const r = await fetch("/api/chat/sessions/" + this.currentId, {
          method: "PATCH",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ thinking: on }),
        });
        if (!r.ok) throw new Error(await r.text());
        this.toast(this.t(on ? "thinking.on" : "thinking.off"), "info", 1800);
        // Mirror into the session list cache so tab-switch sees the right value.
        const cur = this.sessions.find(s => s.id === this.currentId);
        if (cur) cur.thinking = on;
      } catch (err) {
        // Revert the checkbox so UI reflects the real (unchanged) state.
        this.thinkingEnabled = !on;
        this.toast(this.lang === "zh" ? "切换失败" : "Switch failed", "error");
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
        // True when this tab's turn finished while the user was looking at a
        // different tab — drives a green dot on the tab strip so the user
        // notices "this one's ready". Cleared when the user activates the tab.
        unread: false,
        // Per-session message queue. Populated when the user sends while
        // this tab's turn is still streaming OR while a compact is in
        // flight. Drained automatically on the next `done` event /
        // compact-finally / activateTab. Items are {id, text,
        // pendingImages, pendingDocs, enqueuedAt}.
        pendingQueue: [],
        // Set to true when a turn errors out while the queue is non-empty.
        // Stops auto-drain so the user explicitly chooses to resume vs
        // discard (auto-draining post-failure would burn tokens on a
        // quota/auth error and confuse the user). Cleared by explicit
        // resume-queue or discard-queue actions on the failed user bubble.
        _queuePaused: false,
        // True only during _attachToServerTurn's poll window (between a turn's
        // done and the server starting the next queued turn) — suppresses the
        // idle "Queue waiting" banner so it doesn't flash mid-drain.
        _draining: false,
        // True while a native /compact is in flight on THIS session — drives
        // the "📦 压缩对话中…" pending bubble at the bottom of the chat. Used
        // to be a single global flag (app._compacting), which made every tab
        // show the animation when only one session was actually compacting.
        // Per-tab now so the bubble follows the session being compacted.
        compacting: false,
        // True while an async _fetchTabUsage request is in flight for this
        // session — prevents concurrent duplicate fetches from piling up.
        _usageFetching: false,
        // Lazy-load stash: older messages from this session that haven't
        // been rendered yet. Populated by loadSession() when history
        // exceeds INITIAL_LOAD; drained in batches by loadEarlierMessages.
        // mdRender on these is deferred — they hold raw text only.
        _earlierMessages: [],
        // True iff _earlierMessages is non-empty — drives the "Load earlier"
        // button visibility.
        _hasMoreHistory: false,
        // True iff the absolute MAX_TOTAL cap kicked in during loadSession
        // (sessions with thousands of messages). Shows a hint that not
        // every message is reachable from the UI, full history is in JSONL.
        _truncatedFromTop: false,
        // Backend windowing cursor: index (in the full server-side bubble
        // chain) of the OLDEST bubble currently held in memory — i.e. the
        // first bubble of the in-memory contiguous block (messages[] is the
        // tail of that block; _earlierMessages is its head). After a
        // `?tail=N` load this is the server's reported `offset`. >0 means
        // older bubbles still live on the server and can be paged in via
        // _fetchOlderWindow. 0 means we hold history back to the start.
        _loadedOffset: 0,
        // Total bubble count in the full server-side chain (server `total`),
        // so earlierMessageCount() can show how many older messages exist
        // beyond what's in memory.
        _total: 0,
        // True while an async backend older-window fetch is in flight, so
        // rapid "Load earlier" clicks don't fire duplicate requests.
        _fetchingOlder: false,
      };
    },
    _ensureTabState(id) {
      if (!this.tabState[id]) {
        this.tabState[id] = this._blankTabState();
        // The queue now lives server-side (sessions/{sid}.queue.json). We do
        // NOT pull it here — _ensureTabState is called synchronously all over
        // the place and shouldn't fire a fetch each time. The queue mirror is
        // refreshed by _syncQueueFromServer, invoked on load (_checkActiveTurn),
        // tab activation, and after every turn/queue mutation.
      }
      return this.tabState[id];
    },

    // ===== Per-session message queue =====
    // The user can keep typing & sending while Muse is still answering (or
    // while a compact is running). Each follow-up gets parked on
    // tabState[sid].pendingQueue and auto-drained the moment the in-flight
    // turn finishes. Drain is gated on sid === currentId — we never send a
    // queued message while the user is looking at a different tab (would
    // cause writes into the wrong tabState and surprise on switch back).
    // When the user comes back, activateTab() retries the drain.
    _isBusy(sid) {
      if (!sid) return false;
      const st = this.tabState[sid];
      return !!(st && (st.streaming || st.compacting));
    },
    // True when the CSS @media single-pane mobile layout is active —
    // EITHER the viewport is narrow (≤900px) OR we're on a touch device
    // in landscape (≤500px tall). Mirror of the CSS condition; used at
    // every "if mobile, switch the visible pane" branch so large phones
    // in landscape (e.g. iPhone 15 Pro Max = 932×430, exceeds 900 wide
    // but still a phone) get the mobile tab-switch behaviour instead of
    // forcing all three panes onto a 430px-tall strip.
    _isMobileLayout() {
      if (window.innerWidth <= 900) return true;
      return !!(window.matchMedia
                 && window.matchMedia("(pointer: coarse) and (max-height: 500px)").matches);
    },
    // The queue is authoritative server-side now (sessions/{sid}.queue.json,
    // drained autonomously by the backend — Option B). The browser keeps a
    // read-only mirror in st.pendingQueue + st._queuePaused for rendering and
    // refreshes it via _syncQueueFromServer on load / tab-activate / after any
    // turn or mutation. Every mutation below hits an endpoint then re-syncs.
    async _syncQueueFromServer(sid) {
      if (!sid) return;
      const st = this._ensureTabState(sid);
      let data;
      try {
        const r = await fetch("/api/chat/sessions/" + sid + "/queue",
                               { headers: this.hdr() });
        if (!r.ok) return;   // graceful: leave the current mirror untouched
        data = await r.json();
      } catch (_e) { return; }
      st.pendingQueue = (data.items || []).map(it => {
        // FIX ③: the server now resolves each upload id against its in-memory
        // store and returns `attachments: [{id, kind, name, mime, available}]`.
        // Split them into renderable image thumbnails vs doc chips. `src`
        // points at the queued-image endpoint (in-memory, token in query so a
        // bare <img> can load it). Expired ids (available:false) are counted
        // so the bubble can show "附件已过期".
        const atts = it.attachments || [];
        const tok = encodeURIComponent(this.token || "");
        const images = atts
          .filter(a => a.available && a.kind === "image")
          .map(a => ({
            id: a.id, mime: a.mime || "",
            src: `/api/chat/queued-image/${a.id}?token=${tok}`,
          }));
        const docs = atts
          .filter(a => a.available && a.kind !== "image")
          .map(a => ({
            id: a.id, kind: a.kind || "text",
            name: a.name || (a.kind === "pdf" ? "document.pdf" : "file"),
          }));
        const expiredCount = atts.filter(a => !a.available).length;
        return {
          id: it.id,
          text: it.text || "",
          image_ids: it.image_ids || "",
          hasAttach: !!((it.image_ids || "").trim()),
          images,
          docs,
          expiredCount,
          pendingImages: [],
          pendingDocs: [],
          enqueuedAt: it.enqueued_at || Date.now(),
        };
      });
      st._queuePaused = !!data.paused;
    },
    _currentQueueLen() {
      const st = this.tabState[this.currentId];
      return (st && st.pendingQueue) ? st.pendingQueue.length : 0;
    },
    async _enqueueMessage(sid, item) {
      this._ensureTabState(sid);
      // Only ready (uploaded, error-free) attachment IDs go to the server.
      // Preview blobs aren't persisted; if these IDs expire (10 min) before
      // the server drains the item, _start_turn skips them and sends text.
      const ids = []
        .concat((item.pendingImages || []).filter(im => im.id && !im.error).map(im => im.id))
        .concat((item.pendingDocs || []).filter(d => d.id && !d.error).map(d => d.id));
      const image_ids = ids.join(",");
      try {
        const r = await fetch("/api/chat/sessions/" + sid + "/queue", {
          method: "POST",
          headers: Object.assign({ "Content-Type": "application/json" }, this.hdr()),
          body: JSON.stringify({ text: item.text || "", image_ids }),
        });
        if (r.status === 409) {
          this.toast(this.lang === "zh"
            ? "消息队列已满（最多 10 条），请等当前回复结束"
            : "Queue full (max 10) — wait for the current reply",
            "warn", 3000);
          return false;
        }
        if (!r.ok) {
          this.toast(this.lang === "zh" ? "加入队列失败" : "Failed to queue", "error", 3000);
          return false;
        }
      } catch (_e) {
        this.toast(this.lang === "zh" ? "网络错误，未能入队" : "Network error — not queued",
                   "error", 3000);
        return false;
      }
      await this._syncQueueFromServer(sid);
      return true;
    },
    // Post-turn / on-activate hook. The SERVER drains the queue (pops the next
    // item + starts the next turn) on its own — the browser no longer sends
    // queued messages itself. This just refreshes the mirror and re-subscribes
    // to whatever turn the server started, so the user sees it stream live.
    // Name kept so existing call sites (done / compact-finally / activateTab)
    // don't change.
    _drainPendingQueue(sid) {
      if (!sid) return;
      if (sid !== this.currentId) {
        // Not the visible tab — just refresh its mirror; activateTab will
        // re-attach when the user returns.
        this._syncQueueFromServer(sid);
        return;
      }
      this._attachToServerTurn(sid, 8);
    },
    // Poll /active + re-subscribe to a server-started turn. Retries a few
    // times because the server's drain runs a beat AFTER it publishes the
    // previous turn's `done` (in the background task's finally), so /active
    // can flip to true just after we land here. Stops once we attach, run
    // out of tries, or the queue turns out empty/paused.
    async _attachToServerTurn(sid, tries) {
      const st0 = this.tabState[sid];
      if (this.currentId !== sid || this.streaming) {
        if (st0) st0._draining = false;
        this._syncQueueFromServer(sid);
        return;
      }
      // _draining suppresses the idle "Queue waiting" banner during the brief
      // poll window between a turn's `done` and the server starting the next
      // queued turn — otherwise the banner flashes for ~350ms each drain step.
      if (st0) st0._draining = true;
      await this._syncQueueFromServer(sid);
      const st = this.tabState[sid];
      const expect = !!(st && st.pendingQueue && st.pendingQueue.length && !st._queuePaused);
      let active = false, startedAt = 0, uText = "", uImages = [], uDocs = [];
      let continuation = false;
      try {
        const r = await fetch("/api/chat/sessions/" + sid + "/active",
                               { headers: this.hdr() });
        if (r.ok) {
          const d = await r.json();
          active = !!d.active; startedAt = d.started_at;
          continuation = !!d.continuation;
          uText = d.user_text || "";
          uImages = d.user_images || [];
          uDocs = d.user_docs || [];
        }
      } catch (_e) {}
      if (active && continuation && !this.streaming && this.currentId === sid) {
        // The slot holds a bg-task continuation turn, not a queued item. Hand
        // it to the continuation poller (no user bubble, no truncation) instead
        // of the queue-drain reconnect below.
        if (st) st._draining = false;
        this.send({ reconnect: true, continuation: true, startedAt });
        return;
      }
      if (active && !this.streaming && this.currentId === sid) {
        if (st) st._draining = false;
        // FIX (queue live-render): when the SERVER drained a queued item and
        // started its turn headlessly, the browser never pushed a user bubble
        // for it — so a live reconnect would replay the assistant stream under
        // the PREVIOUS user msg, and the drained item's prompt would vanish
        // until a manual refresh (which rebuilds it from bc.user_text). User
        // symptom: "发了四条，只剩一条" — only the first send's bubble showed.
        // Push the bubble here, before send({reconnect}) truncates+replays, so
        // the freshly-drained prompt renders live. Guard against the reload
        // case (loadSession already rebuilt this bubble): skip if the last
        // user message already matches this turn's prompt.
        const msgs = (st && st.messages) || [];
        let lastUserText = null;
        for (let i = msgs.length - 1; i >= 0; i--) {
          if (msgs[i].role === "user") { lastUserText = msgs[i].text || ""; break; }
        }
        if ((uText || uImages.length || uDocs.length) && lastUserText !== uText) {
          this._capLiveMessages(st);
          msgs.push({
            role: "user",
            text: uText,
            images: uImages,
            docs: uDocs,
          });
          this.atBottom = true;
          this.scrollToBottom(true);
        }
        this.send({ reconnect: true, startedAt });
        this.$nextTick(() => this._syncQueueFromServer(sid));
        return;
      }
      if (expect && tries > 1 && !this.streaming && this.currentId === sid) {
        setTimeout(() => this._attachToServerTurn(sid, tries - 1), 350);
      } else if (st) {
        st._draining = false;
      }
    },
    // ===== background-task continuation poller =====
    // When an SDK background task (Agent / Bash run_in_background) is still
    // running after its turn ended, the server keeps a watcher alive that — on
    // completion — opens a HEADLESS CONTINUATION turn (broadcast in
    // _active_turns, is_continuation=true) carrying the card flip + the model's
    // auto-continue reaction. The browser only opens an SSE on send/reconnect,
    // so without this poller the completion (30-60s later) would never surface
    // live; the user would have to reload. While the open session shows a
    // 'running' bg-task card, poll /active and attach in continuation mode the
    // moment the watcher's broadcast appears. Self-stops once no running card
    // remains (the task settled → card flipped) or after a hard cap.
    _bgHasRunningCard(sid) {
      const st = this.tabState[sid];
      if (!st || !st.messages) return false;
      return st.messages.some(m =>
        m && m.role === "tool_use" && m.task_status
        && m.task_status.state === "running");
    },
    _ensureBgContPoller(sid) {
      this._bgContPollers = this._bgContPollers || {};
      if (this._bgContPollers[sid]) return;          // already polling
      if (!this._bgHasRunningCard(sid)) return;       // nothing to wait for
      // Hard cap mirrors the server's MUSELAB_TASK_WATCH_TIMEOUT (1800s): at an
      // 8s cadence that's ~225 ticks. Prevents an interval lingering forever if
      // the user navigates away and the running card never resolves on the FE.
      let ticksLeft = 230;
      const tick = async () => {
        if (ticksLeft-- <= 0) { this._stopBgContPoller(sid); return; }
        // Card settled (flipped to done/failed) → done waiting.
        if (!this._bgHasRunningCard(sid)) { this._stopBgContPoller(sid); return; }
        // Not viewing this session, or already streaming → retry next tick.
        if (this.currentId !== sid || this.streaming) return;
        // PRIMARY completion path: a run_in_background task's terminal status
        // round-trips through the session JSONL as a <task-notification> record
        // (NOT a typed TaskNotificationMessage, so the cross-turn watcher's
        // continuation never opens in practice). The backend history rebuild
        // stamps the launching card's terminal task_status from that record;
        // here we poll the history tail and reconcile it onto the live running
        // cards so the badge flips ✅ in-place without a manual reload — then
        // the next tick's _bgHasRunningCard check self-stops the poller.
        try {
          const hr = await fetch("/api/chat/sessions/" + sid + "?tail=80",
                                  { headers: this.hdr() });
          if (hr.ok) {
            const hs = await hr.json();
            const settled = {};
            (hs.messages || []).forEach(m => {
              if (m && m.role === "tool_use" && m.id && m.task_status
                  && m.task_status.state
                  && m.task_status.state !== "running") {
                settled[m.id] = m.task_status;
              }
            });
            const st = this.tabState[sid];
            if (st && st.messages) {
              st.messages.forEach(m => {
                if (m && m.role === "tool_use" && m.task_status
                    && m.task_status.state === "running" && settled[m.id]) {
                  // Reassign the whole object so Alpine's :class re-evaluates.
                  m.task_status = Object.assign({}, m.task_status, settled[m.id]);
                }
              });
            }
          }
        } catch (_e) {}
        // BELT: the cross-turn continuation broadcast (works if a future SDK
        // delivers a typed TaskNotificationMessage). Harmless no-op otherwise.
        try {
          const r = await fetch("/api/chat/sessions/" + sid + "/active",
                                 { headers: this.hdr() });
          if (!r.ok) return;
          const d = await r.json();
          if (d.active && d.continuation && !this.streaming
              && this.currentId === sid) {
            // Dedup: /active surfaces a finished continuation from the
            // server's _recent_turns for the full 60s TTL, so if ANOTHER
            // bg task keeps this poller alive the same continuation would be
            // re-reconnected every 8s → duplicate reaction bubbles. Key on
            // the continuation's started_at (unique epoch per broadcast) and
            // replay each one at most once. The normal single-task case
            // self-stops on the card flip and never reaches a second tick,
            // but this makes the multi-task case safe too.
            this._consumedConts = this._consumedConts || {};
            const ckey = sid + ":" + d.started_at;
            if (!this._consumedConts[ckey]) {
              this._consumedConts[ckey] = true;
              this.send({ reconnect: true, continuation: true,
                           startedAt: d.started_at });
            }
          }
        } catch (_e) {}
      };
      this._bgContPollers[sid] = setInterval(tick, 8000);
      tick();   // kick once immediately
    },
    _stopBgContPoller(sid) {
      if (this._bgContPollers && this._bgContPollers[sid]) {
        clearInterval(this._bgContPollers[sid]);
        delete this._bgContPollers[sid];
      }
    },
    async removePendingQueueItem(sid, idx) {
      const st = this.tabState[sid];
      if (!st || !st.pendingQueue) return;
      const item = st.pendingQueue[idx];
      if (!item) return;
      try {
        await fetch("/api/chat/sessions/" + sid + "/queue/" + encodeURIComponent(item.id),
                    { method: "DELETE", headers: this.hdr() });
      } catch (_e) {}
      await this._syncQueueFromServer(sid);
    },
    async editPendingQueueItem(sid, idx) {
      // Lift the queued text — AND its attachments (FIX ③) — back into the
      // input box for editing, removing the server copy. The upload IDs still
      // live in _image_store (the DELETE below only drops the queue item, not
      // the uploads), so re-sending reuses the very same images/docs. Any IDs
      // that already expired (available:false) simply aren't restored.
      const st = this.tabState[sid];
      if (!st || !st.pendingQueue) return;
      const item = st.pendingQueue[idx];
      if (!item) return;
      // Snapshot before _syncQueueFromServer wipes the mirror.
      const imgs = (item.images || []).slice();
      const docs = (item.docs || []).slice();
      try {
        await fetch("/api/chat/sessions/" + sid + "/queue/" + encodeURIComponent(item.id),
                    { method: "DELETE", headers: this.hdr() });
      } catch (_e) {}
      await this._syncQueueFromServer(sid);
      if (sid !== this.currentId) return;
      this.input = item.text || "";
      // Rebuild the input-tray chips. No `file` on restored images, so the
      // in-chip "Annotate" button stays disabled (it guards on `!img.file`),
      // but the thumbnail + re-send path work fully.
      this.pendingImages = imgs.map(im => ({
        id: im.id, mime: im.mime || "", preview: im.src,
        uploading: false, error: false,
      }));
      this.pendingDocs = docs.map(d => ({
        id: d.id, name: d.name, kind: d.kind,
        uploading: false, error: false,
      }));
      this.$nextTick(() => {
        const ta = this.$refs.chatInput;
        if (ta) { this.autoGrow(ta); ta.focus(); }
      });
    },
    async resumeQueueDrain(sid) {
      // Un-pause server-side (which kicks its own drain), then attach to the
      // turn it starts. Also the manual "kick" for the post-restart case —
      // the server intentionally does NOT auto-resume draining on boot, so
      // dormant items wait here until the user hits Resume.
      try {
        await fetch("/api/chat/sessions/" + sid + "/queue/pause", {
          method: "POST",
          headers: Object.assign({ "Content-Type": "application/json" }, this.hdr()),
          body: JSON.stringify({ paused: false }),
        });
      } catch (_e) {}
      await this._syncQueueFromServer(sid);
      this._drainPendingQueue(sid);
    },
    async discardQueue(sid) {
      try {
        await fetch("/api/chat/sessions/" + sid + "/queue",
                    { method: "DELETE", headers: this.hdr() });
      } catch (_e) {}
      await this._syncQueueFromServer(sid);
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
      if (st._usageFetching) return;   // 已在飞则跳过，避免并发重复请求
      st._usageFetching = true;
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
      } catch (e) { /* non-fatal */ } finally {
        st._usageFetching = false;
      }
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
        // localStorage had no saved session (new device / cleared storage).
        // Try to restore the last active session from the server so the user
        // continues where they left off on their other device.
        let restored = false;
        try {
          const { ok, data } = await this.api("/api/settings/ui-state");
          if (ok && data && data.last_session_id) {
            const sid = data.last_session_id;
            if (this.sessions.find(s => s.id === sid)) {
              this.currentId = sid;
              restored = true;
            }
          }
        } catch (_) {}
        if (!restored) this.currentId = this.sessions[0].id;
      }
      // Reconcile openTabIds (restored from prefs) with what still exists on
      // the server: drop tabs whose session was deleted, then ensure currentId
      // is in the list. Other tabs are lazy-loaded on first switch.
      const validIds = new Set(this.sessions.map(s => s.id));
      this.openTabIds = (this.openTabIds || []).filter(id => validIds.has(id));
      if (!this.openTabIds.includes(this.currentId)) {
        this.openTabIds.push(this.currentId);
      }
      // Cross-device merge: pull the tab strip + preview tabs that other
      // devices last pushed. Happens AFTER local reconciliation so the
      // merge sees a clean local baseline. currentId is preserved by the
      // healing logic inside _pullUIStateAndMerge.
      try { await this._pullUIStateAndMerge(); } catch (_) {}
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
      this._applySessionList((data && data.sessions) || []);
      // <select x-model="currentId"> needs a tickle to sync display when
      // sessions populate (same Alpine-x-model-on-dynamic-options race).
      await this._rebindSelect("currentId");
    },
    // FIX ⑪: quiet variant for the 10s foreground poll. Identical list-merge
    // (incl. the FIX ⑩ active-flag dots + green-dot transition) but WITHOUT
    // _rebindSelect — that toggles currentId to "" and back, which re-fires
    // the currentId watcher → a ui-state PUT every 10s on every device. The
    // tickle is only needed when currentId itself must be re-displayed (login /
    // visibilitychange / explicit refresh), not on a background list poll.
    async _syncSessionListQuiet() {
      const { ok, data } = await this.api("/api/chat/sessions");
      if (!ok) return;
      this._applySessionList((data && data.sessions) || []);
    },
    // Shared session-list applier. Snapshots the prior server-side `active`
    // flags BEFORE swapping in the new list (FIX ⑩) so we can detect a
    // streaming→idle transition that happened on another device: when a turn
    // that was running elsewhere finishes, its reply is now "ready" — surface
    // the green unread dot here too (unless it's the tab the user is viewing).
    _applySessionList(raw) {
      const prevActive = {};
      for (const s of this.sessions) prevActive[s.id] = !!s.active;
      // Defensive: drop any entry without a usable id. Alpine x-for :key
      // bindings (session-picker, history popup) use `s.id`; an undefined
      // key crashes alpine morph with "Cannot read properties of undefined
      // (reading 'after')".
      this.sessions = (raw || []).filter(s => s && typeof s.id === "string" && s.id);
      for (const s of this.sessions) {
        if (prevActive[s.id] && !s.active && s.id !== this.currentId) {
          const st = this._ensureTabState(s.id);
          if (st && !st.streaming) st.unread = true;
        }
      }
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
      // Default name uses the user's BROWSER-LOCAL clock — the backend
      // generated it from datetime.now() which is the VPS's UTC, so users
      // in non-UTC timezones saw "新会话 05-19 08:26" when their wall
      // clock said 16:26. Generating the timestamp client-side fixes that
      // for every user without a server-side timezone config.
      const now = new Date();
      const pad = n => String(n).padStart(2, "0");
      const stamp = `${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
                    `${pad(now.getHours())}:${pad(now.getMinutes())}`;
      const prefix = this.lang === "zh" ? "新会话 " : "New chat ";
      const r = await fetch("/api/chat/sessions", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ name: prefix + stamp, model: this.model }),
      });
      const meta = await r.json();
      // Optimistic local update — UI switches the moment the backend
      // confirms the session ID, instead of waiting for two extra HTTP
      // roundtrips. refreshSessions/_fetchTabUsage run in the background
      // and will reconcile when they return (~100-200ms later); their
      // result is consistent with what we just inserted, so the user
      // sees zero flicker. Previously this was three sequential awaits
      // (~300-500ms perceived lag for "new chat" click).
      const existsLocally = this.sessions.some(s => s.id === meta.id);
      if (!existsLocally) {
        this.sessions = [meta, ...this.sessions];
      }
      this.currentId = meta.id;
      const st = this._ensureTabState(meta.id);
      st.messages.length = 0;
      st._loaded = true;
      this._activateTabState(meta.id);
      if (!this.openTabIds.includes(meta.id)) this.openTabIds.push(meta.id);
      this.savePrefs();
      // Background reconciliation (fire-and-forget).
      this.refreshSessions();
      this._fetchTabUsage(meta.id);
      return meta;
    },

    // Create a curator-mode session and kick off the workflow. As of
    // 2026-05-23 this covers BOTH archive tidying AND CLAUDE.md profile
    // gap completion (the old startProfileIntake was merged in — two
    // near-identical entry points were confusing, the curator prompt
    // step 3b now walks the user through any blank profile sections).
    // Confirms first (this creates a NEW session), POSTs to
    // /api/sessions/organize, switches to it, auto-sends the bilingual
    // initial message.
    async startOrganize() {
      const zh = this.lang === "zh";
      const ok = await this.confirm({
        title: zh ? "整理档案" : "Organize archive",
        body: zh
          ? "将新建一个 [整理档案] 会话：Muse 会扫描 archive、提出整理建议，并对 CLAUDE.md 里还没填的章节逐项问你。每一步动文件前都会等你确认。"
          : "Will create a new [Organize] session: Muse scans the archive, proposes tidy-up changes, and walks through any blank CLAUDE.md profile sections. Every file-modifying step waits for your confirmation.",
        okText: zh ? "开始" : "Start",
      });
      if (!ok) return;
      const r = await fetch("/api/chat/sessions/organize", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ model: this.model }),
      });
      if (!r.ok) {
        this.toast(this.lang === "zh"
          ? "创建失败：" + (await r.text())
          : "Create failed: " + (await r.text()), "error", 4000);
        return;
      }
      const meta = await r.json();
      await this.refreshSessions();
      this.currentId = meta.id;
      const st = this._ensureTabState(meta.id);
      st.messages.length = 0;
      st._loaded = true;
      this._activateTabState(meta.id);
      if (!this.openTabIds.includes(meta.id)) this.openTabIds.push(meta.id);
      this._fetchTabUsage(meta.id);
      this.savePrefs();
      // Auto-send the curator's initial prompt — the system prompt tells
      // Muse to begin the 5-step workflow on first message.
      const lang = this.lang === "en" ? "en" : "zh";
      const initialMsg = (meta.initial_message && meta.initial_message[lang])
        || meta.initial_message?.zh || "开始";
      this.input = initialMsg;
      // Defer a tick so the new session's tabState is fully wired into
      // Alpine reactivity before send() reads `this.currentId`.
      this.$nextTick(() => { this.send(); });
    },

    // ===== tabs =====
    // Switch to (and if needed open) a tab. Used by the picker dropdown to
    // promote a history session into a tab.
    async openTab(id, makeCurrent = true) {
      if (!this.openTabIds.includes(id)) {
        const MAX_TABS = 20;
        while (this.openTabIds.length >= MAX_TABS) {
          const oldest = this.openTabIds.find(tid => tid !== this.currentId);
          if (!oldest) break;
          await this.closeChatTab(oldest);
        }
        this.openTabIds.push(id);
      }
      if (makeCurrent && id !== this.currentId) {
        this.currentId = id;
        await this.switchSession();
      }
      this.savePrefs();
    },

    // Open a session id arriving from a push-notification deep-link
    // (?session=… on cold start, or a SW postMessage on an already-open
    // tab). Guard against stale/deleted ids so a tapped-but-since-deleted
    // notification doesn't push a phantom tab.
    async _openSessionFromDeeplink(id) {
      if (!id) return;
      try {
        if (!this.sessions.find(s => s.id === id)) {
          await this.refreshSessions();
          if (!this.sessions.find(s => s.id === id)) return;
        }
        await this.openTab(id);
      } catch (_) { /* best-effort; ignore */ }
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
        // preview is now a data URL (base64 thumbnail) — no blob revoke needed.
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
      this._clearSessionWarnFlags(id);
      this.savePrefs();
      // Closing a tab is a cheap, reversible action (session is still in
      // history picker / sidebar). The previous toast-with-undo was noise
      // for every close click; killed by user request.
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
      if (st && st.streaming) return true;
      // FIX ⑩: cross-device sync. tabState only knows about turns THIS browser
      // started; a turn kicked off on another device shows up via the
      // server-authoritative `active` flag on the session record (set from
      // chat.py's _active_turns). So the blue "streaming" dot lights up on
      // every device, not just the one that sent the message.
      const s = this.sessions.find(x => x.id === tid);
      return !!(s && s.active);
    },
    isTabUnread(tid) {
      // True when this tab's most recent turn finished while the user was
      // on a different tab AND they haven't activated this tab since.
      // The active tab can never be unread by construction (activateTab
      // clears the flag), but we double-check here to keep the template
      // logic-light.
      if (tid === this.currentId) return false;
      // Streaming has priority over unread — they must be mutually exclusive.
      // Guard against the FULL streaming check (isTabStreaming), not just the
      // local `st.streaming` flag: a turn can be live via the server-side
      // `s.active` flag (cross-device sync, or the next queued turn draining
      // the instant turn N finishes) while THIS browser's st.streaming has
      // already flipped false. The old `!st.streaming`-only guard let the
      // green "done" dot light up alongside the accent "in-progress" dot in
      // exactly those windows (2026-05-30 both-dots bug report).
      if (this.isTabStreaming(tid)) return false;
      const st = this.tabState[tid];
      return !!(st && st.unread);
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
    // Smart-collapse memory: once the user manually expands or collapses a
    // tool of kind X in this session, subsequent same-kind tools default
    // to that state. Reduces "expand 5 Read results in a row" friction
    // for users who actually want to see content; preserves the
    // default-collapsed behavior for users who don't touch anything.
    // Reset on each new session (loadSession clears via _ensureTabState).
    // 2026-05-24: Smart-collapse memory (per-kind expansion preference)
    // removed. Made every same-kind tool-result toggle in lockstep when
    // the user just wanted to peek at ONE specific Bash output — wildly
    // unintuitive. Each tool_result now toggles independently. Kept the
    // property name for back-compat with any localStorage that referenced
    // it, but it's no longer read or written.
    _kindExpansionPrefs: {},
    _msgKey(i, m) {
      if (!m) return "";
      return m.uuid || m._k || ("m-" + i);
    },
    isMsgExpanded(i, m, defaultOpen) {
      if (!m) return true;
      const k = this._msgKey(i, m);
      if (k in this._expandedMsgs) return this._expandedMsgs[k];
      // Explicit caller hint (e.g. diff strip wants to be open by default)
      // overrides the default-collapsed behavior. Caller still respects
      // user's explicit toggle (the _expandedMsgs check above).
      if (defaultOpen) return true;
      // Default: only the actively-streaming last block is expanded.
      const msgs = this.messages || [];
      return !!this.streaming && i === msgs.length - 1;
    },
    toggleMsgExpanded(m, i) {
      if (!m) return;
      const idx = (i ?? (this.messages || []).indexOf(m));
      const k = this._msgKey(idx, m);
      const cur = this.isMsgExpanded(idx, m);
      const newState = !cur;
      // Spread-assign so Alpine sees the replacement and re-evaluates.
      this._expandedMsgs = { ...this._expandedMsgs, [k]: newState };
    },
    toolResultClass(i, m) {
      let cls = "tool-result";
      if (m && m.is_error) cls += " err";
      if (!this.isMsgExpanded(i, m)) cls += " collapsed";
      // Per-tool class hooks let CSS show terminal / read-gutter / web-card
      // styling only for the relevant result. Falls back to plain text.
      const kind = this.toolResultKind(m);
      if (kind) cls += " kind-" + kind;
      return cls;
    },
    toolResultSummary(m, i) {
      const text = (m && (m.text || m.preview)) || "";
      const lines = text.split("\n").length;
      const kind = this.toolResultKind(m);
      const suffix = this.lang === "zh" ? " 行输出" : " lines";
      // Bash gets a more useful summary (exit code surfaced) so the user
      // doesn't have to expand to see if the command succeeded.
      if (kind === "bash" && m && m.bash && typeof m.bash.exit_code === "number") {
        const ec = m.bash.exit_code;
        const tag = ec === 0
          ? (this.lang === "zh" ? "✓ 成功" : "✓ ok")
          : (this.lang === "zh" ? `✗ 退出码 ${ec}` : `✗ exit ${ec}`);
        return `${tag} · ${lines}${suffix}`;
      }
      // Read / Edit / Write: bring the filename forward into the summary
      // by peeking at the immediately-preceding tool_use. With this the
      // user can see what was read even when the result is collapsed.
      if ((kind === "read" || kind === "search") && i !== undefined && i > 0) {
        const prev = this.messages[i - 1];
        if (prev && prev.role === "tool_use") {
          const path = this.toolFilePath(prev);
          if (path) {
            const fname = path.split("/").pop();
            return `${fname} · ${lines}${suffix}`;
          }
          // Grep / Glob: show the pattern
          if (prev.name === "Grep" || prev.name === "Glob") {
            const pat = (prev.input && (prev.input.pattern || prev.input.path)) || "";
            const matchSuffix = this.lang === "zh" ? " 项匹配" : " matches";
            if (pat) return `"${pat}" · ${lines}${matchSuffix}`;
          }
        }
      }
      return lines + suffix;
    },

    // Parse ripgrep / grep output into clickable rows.
    // ripgrep formats hits as `path:lineno:content` or `path:content` (without
    // -n). We split conservatively — anything we can't parse falls back to
    // the original text line so users still see the unstructured result.
    parseSearchHits(text) {
      if (!text) return [];
      const lines = text.split("\n");
      const out = [];
      for (const ln of lines) {
        if (!ln.trim()) continue;
        // Try path:lineno:content
        const m = ln.match(/^([^:]+):(\d+):(.*)$/);
        if (m) {
          out.push({ path: m[1], lineno: parseInt(m[2], 10), content: m[3], raw: ln });
          continue;
        }
        // Try path (Glob output is just paths)
        if (!ln.includes(":") || ln.match(/^[^\s:]+$/)) {
          out.push({ path: ln.trim(), lineno: null, content: "", raw: ln });
          continue;
        }
        out.push({ path: "", lineno: null, content: ln, raw: ln });
      }
      return out;
    },
    // WebFetch / WebSearch source url (from the input). Falls back to "" so
    // the template can hide the badge cleanly when not available.
    webSourceUrl(toolUseMessage) {
      const inp = toolUseMessage && toolUseMessage.input;
      if (!inp) return "";
      return inp.url || inp.query || "";
    },
    webSourceDomain(url) {
      if (!url) return "";
      // For URLs: extract hostname. For queries: just return as-is (with
      // 🔍 prefix in the template).
      if (/^https?:\/\//.test(url)) {
        try { return new URL(url).hostname; }
        catch (e) { return url; }
      }
      return url;
    },
    webIsUrl(url) {
      return /^https?:\/\//.test(url || "");
    },
    // Background-task status badge for the subagent card. `state` comes from
    // the SDK Task* lifecycle (running / completed / failed / stopped; "done"
    // is the forward-compat fallback for an unknown terminal status).
    taskStatusLabel(state) {
      const map = {
        running:   "⏳ " + this.t("subagent.task_running"),
        completed: "✅ " + this.t("subagent.task_completed"),
        failed:    "❌ " + this.t("subagent.task_failed"),
        stopped:   "⏹ " + this.t("subagent.task_stopped"),
        done:      "✅ " + this.t("subagent.task_completed"),
      };
      return map[state] || map.running;
    },
    // Skill card data — name + description + trigger summary.
    skillCardInfo(m) {
      if (!m || m.name !== "Skill") return null;
      const inp = m.input || {};
      return {
        skill: inp.skill || "",
        args: inp.args || "",
      };
    },

    // Hint generator for failed tool calls. Pattern-matches the error
    // text against common failure modes and returns an actionable fix.
    // Returns null for unrecognized errors — the renderer falls back to
    // the raw error body. Localized to current lang.
    errorFixHint(m) {
      if (!m || !m.is_error) return null;
      const txt = ((m.text || m.preview || "") + "").toLowerCase();
      const zh = this.lang === "zh";
      // Edit failure: old_string not unique / not found
      if (txt.includes("old_string") &&
          (txt.includes("not found") || txt.includes("could not find"))) {
        return zh
          ? "提示：旧字符串没匹配上。文件可能被改过——试试先 Read 一次再 Edit，或给 old_string 加更多上下文让它唯一。"
          : "Hint: old_string didn't match. The file may have changed between Read and Edit — try Read again, or extend old_string with more context to make it unique.";
      }
      if (txt.includes("old_string") && txt.includes("not unique")) {
        return zh
          ? "提示：旧字符串在文件里出现多次。给 old_string 加更多前后行让它唯一，或者用 replace_all=true。"
          : "Hint: old_string is not unique in the file. Add surrounding lines to disambiguate, or set replace_all=true.";
      }
      // File system failures
      if (txt.includes("no such file") || txt.includes("does not exist")) {
        return zh
          ? "提示：路径不存在。检查拼写，或者确认你跑在正确的工作目录。"
          : "Hint: path doesn't exist. Check the spelling, or confirm the current working directory.";
      }
      if (txt.includes("permission denied") || txt.includes("eacces")) {
        return zh
          ? "提示：权限不足。可能需要 chmod / sudo，或者文件被另一个进程占用。"
          : "Hint: permission denied. Try chmod, or check if another process has the file locked.";
      }
      // Timeout / hung
      if (txt.includes("timed out") || txt.includes("timeout")) {
        return zh
          ? "提示：超时。缩小命令范围（更窄的 grep / 更小的 head_limit），或显式传 timeout 参数。"
          : "Hint: timed out. Narrow the scope (tighter grep / smaller head_limit) or pass an explicit timeout.";
      }
      // JSON / parse failures
      if (txt.includes("json") && (txt.includes("decode") || txt.includes("parse"))) {
        return zh
          ? "提示：JSON 解析失败。检查工具返回是否为有效 JSON；也可能 server 报错时把 stderr 混进了 stdout。"
          : "Hint: JSON parse error. The tool may have returned non-JSON, or mixed stderr into stdout.";
      }
      // Network
      if (txt.includes("connection refused") || txt.includes("network") ||
          txt.includes("dns") || txt.includes("getaddrinfo")) {
        return zh
          ? "提示：网络问题。检查代理 / VPN / 目标服务是否在跑。"
          : "Hint: network problem. Check your proxy / VPN / whether the target service is up.";
      }
      // Auth
      if (txt.includes("401") || txt.includes("unauthorized") ||
          txt.includes("invalid api key") || txt.includes("authentication")) {
        return zh
          ? "提示：认证失败。检查 Settings 里对应 provider 的 API key，或 Claude Auth 是否仍然有效。"
          : "Hint: auth failed. Check the provider's API key in Settings, or whether Claude Auth is still valid.";
      }
      // Rate limit
      if (txt.includes("rate limit") || txt.includes("429") || txt.includes("too many requests")) {
        return zh
          ? "提示：触发限流。等几分钟再试，或换 provider。"
          : "Hint: rate limited. Wait a few minutes, or switch to another provider.";
      }
      return null;
    },

    // Find the matching tool_use for a given tool_result by walking
    // backwards through messages and matching tool_use_id. Used by the
    // diff-badge renderer to count +/- on Edit/Write/MultiEdit.
    findToolUseFor(toolResult, fromIdx) {
      if (!toolResult || fromIdx === undefined || fromIdx === null) return null;
      const id = toolResult.tool_use_id || toolResult.tool_id;
      if (!id) {
        // Fallback: walk backwards looking for the nearest tool_use
        for (let j = fromIdx - 1; j >= Math.max(0, fromIdx - 3); j--) {
          const c = this.messages[j];
          if (c && c.role === "tool_use") return c;
        }
        return null;
      }
      for (let j = fromIdx - 1; j >= 0; j--) {
        const c = this.messages[j];
        if (c && c.role === "tool_use" &&
            (c.id === id || c.tool_use_id === id)) return c;
      }
      return null;
    },
    // Declarative tool → renderer-kind registry. Replaces the original
    // switch so third-party plugins / MCP servers can register a renderer
    // kind without modifying core code:
    //
    //   window.muselabApp.registerToolRenderer('mcp__github__pr', 'web');
    //
    // The set of supported kinds is bounded by the templates baked into
    // index.html (bash / read / web / search / mcp / task). Picking an
    // unknown kind just falls back to the plain-text renderer — degrades
    // gracefully.
    TOOL_RENDERERS: {
      "Bash":     "bash",
      "Read":     "read",
      "WebFetch": "web",
      "WebSearch": "web",
      "Glob":     "search",
      "Grep":     "search",
    },
    // Tool-name pattern → kind. Order matters; first match wins. Used
    // for prefix-based matches (Task*, mcp__*) so the table stays compact.
    TOOL_RENDERER_PATTERNS: [
      { test: (n) => n.startsWith("Task"), kind: "task" },
      { test: (n) => n.startsWith("mcp__"), kind: "mcp" },
    ],
    toolResultKind(m) {
      if (!m) return "";
      const name = m.tool_name || "";
      if (this.TOOL_RENDERERS[name]) return this.TOOL_RENDERERS[name];
      for (const p of this.TOOL_RENDERER_PATTERNS) {
        if (p.test(name)) return p.kind;
      }
      return "";
    },
    // Whether this tool_result should be hidden entirely. Used to suppress
    // noise: Edit/Write/MultiEdit's "File has been updated successfully"
    // adds zero info beyond what the diff strip already shows; Task*'s
    // "Task #N created successfully" similarly. Failed cases (is_error
    // true) are NEVER hidden — the user needs to see what broke + the
    // errorFixHint banner attached to the same result.
    shouldHideToolResult(m) {
      if (!m) return false;
      if (m.is_error) return false;  // never hide failures
      const kind = this.toolResultKind(m);
      if (kind === "task") return true;
      const name = m.tool_name || "";
      if (["Edit", "Write", "MultiEdit"].includes(name)) return true;
      return false;
    },
    // Will this message render any visible content? Mirrors the x-if
    // conditions in index.html's message loop. Used by `:class` on the
    // .msg wrapper to add an `is-hidden` class on no-render messages,
    // which CSS then `display: none`s — collapsing the wrapper out of
    // flex layout so chat-body's `gap` doesn't reserve space for it.
    //
    // We had a CSS-only version via `.msg:not(:has(> :not(template)))
    // { display: none }`, but iOS Safari 15.x has known bugs where
    // :has() doesn't re-evaluate when Alpine adds/removes the rendered
    // sibling next to a <template x-if>. Symptom: persistent ~30-40px
    // blank space between consecutive Edits in chat (2026-05-28 user
    // report). Explicit class-based gate is browser-portable.
    //
    // Turn-tail exception (2026-05-28 follow-up): a tail muse-side msg
    // always renders even with no body content, because the .turn-footer
    // (HH:MM stamp + streaming dots) lives INSIDE the msg wrapper. If we
    // hid the wrapper because the body was empty (e.g. successful Edit
    // tool_result is suppressed by shouldHideToolResult), the footer
    // disappeared from the end of the turn. Symptom: "改动预览下方好像
    // 不会出现 footer 了". Cost: a 28px timestamp-only row at the end of
    // turns whose last msg has no body content — acceptable visual artifact
    // since it preserves the "turn ended at HH:MM" signal.
    isMsgRenderable(m, i) {
      if (!m) return false;
      // Tail check first: dominates any "body is empty" judgment below.
      const msgs = this.messages || [];
      const isTurnTail = m.role !== "user"
        && (i === msgs.length - 1
            || (msgs[i + 1] && msgs[i + 1].role === "user"));
      if (isTurnTail) return true;

      if (m._is_compact_summary) return true;
      switch (m.role) {
        case "user":
        case "assistant":
        case "assistant-turn":
        case "thinking":
        case "permission_request":
        case "ask_user_question":
          return true;
        case "tool_use": {
          // TodoWrite / Task|Agent / ExitPlanMode always render their own card.
          if (m.name === "TodoWrite" || m.name === "Task" || m.name === "Agent"
              || m.name === "ExitPlanMode") return true;
          // Task* (TaskList / TaskGet / TaskOutput) family uses one-line
          // log; shouldRenderTaskLine decides if even THAT shows.
          if (this.isTaskTool(m)) return this.shouldRenderTaskLine(m);
          // Default tool bubble.
          return true;
        }
        case "tool_result":
          return !this.shouldHideToolResult(m);
        default:
          return true;
      }
    },

    // True iff this Edit/Write/MultiEdit tool_use is Muse's CURRENT
    // action — i.e., there's no later tool call of ANY kind after it.
    // (Previous logic only checked for later Edit/Write, but a Bash or
    // Read after the Edit still means the Edit is "done and moved past"
    // — its diff should fold to keep the scroll history clean. Only the
    // truly latest action gets the auto-expanded diff.)
    // User-explicit toggles (via toggleMsgExpanded) still override.
    // There is AT MOST one "latest edit tool" in the whole conversation, so
    // compute its index once per render and cache it — the template calls
    // isLatestEditTool() once per rendered message, and the old per-call
    // forward scan made that O(n²), a real freeze contributor on long
    // sessions. Cache key is currentId:length (same scheme as
    // _taskSubjectMapForMessages): the only events that move the latest-edit
    // index are appends / evictions (length changes) and tab switches
    // (currentId changes); in-place streaming text mutations don't. Writing
    // the cache only on key change keeps it loop-safe under Alpine.
    _latestEditToolIdx() {
      const msgs = this.messages || [];
      const key = (this.currentId || "_") + ":" + msgs.length;
      const cached = this._cachedLatestEditIdx;
      if (cached && cached.key === key) return cached.idx;
      // Last Edit/Write/MultiEdit tool_use in the list…
      let e = -1;
      for (let k = msgs.length - 1; k >= 0; k--) {
        const mm = msgs[k];
        if (mm && (mm.name === "Edit" || mm.name === "Write" || mm.name === "MultiEdit")) {
          e = k; break;
        }
      }
      // …is "latest" only if nothing tool-ish follows it (a later Bash/Read
      // tool_use or any tool_result means the Edit is done and moved past →
      // its diff folds). At most one index satisfies this.
      let idx = -1;
      if (e >= 0) {
        let laterTool = false;
        for (let j = e + 1; j < msgs.length; j++) {
          const c = msgs[j];
          if (c && (c.role === "tool_use" || c.role === "tool_result")) { laterTool = true; break; }
        }
        idx = laterTool ? -1 : e;
      }
      this._cachedLatestEditIdx = { key, idx };
      return idx;
    },
    isLatestEditTool(i, m) {
      if (!m || !(m.name === "Edit" || m.name === "Write" || m.name === "MultiEdit")) return false;
      return i === this._latestEditToolIdx();
    },

    // Public hook for plugins / extensions. Adds an entry to the registry
    // at runtime — subsequent toolResultKind() calls see it. Returns true
    // if registration succeeded (the kind is one of the known templates),
    // false otherwise so the caller knows their kind won't render.
    registerToolRenderer(name, kind) {
      const KNOWN_KINDS = new Set(["bash", "read", "web", "search", "mcp", "task"]);
      if (!name || !kind) return false;
      this.TOOL_RENDERERS[name] = kind;
      return KNOWN_KINDS.has(kind);
    },

    // ===== Task* tool family — compact log-line rendering =====
    // The TaskCreate / TaskUpdate / TaskList / TaskGet / TaskOutput /
    // TaskStop tools are Muse's internal planning scratchpad. Dumping
    // the raw JSON of each call buries the actual conversation.
    // Instead we render each *meaningful* call as a single-line log
    // entry (icon + verb + #id + subject + state) and hide pure-read
    // calls (TaskList / TaskGet / TaskOutput) that don't change anything.
    // Adjacent task lines get visually fused into a "plan panel" via CSS.
    TASK_TOOL_NAMES: ["TaskCreate", "TaskUpdate", "TaskList", "TaskGet",
                       "TaskOutput", "TaskStop"],
    isTaskTool(m) {
      return !!(m && m.name && this.TASK_TOOL_NAMES.includes(m.name));
    },
    // Render-time data for a Task* tool_use bubble. Returns null when the
    // call should be hidden entirely (pure queries like TaskList).
    // Build a {taskId: subject} lookup so TaskUpdate (which only carries
    // taskId + status) can show the same subject the original TaskCreate
    // declared. TaskCreate doesn't see its own taskId — that's assigned
    // by the runtime and returned in the tool_result text like
    // "Task #15 created successfully: <subject>". We scan messages in
    // order, matching each TaskCreate tool_use to its tool_result by
    // tool_use_id, then parse the "#N" out of the result text.
    //
    // We also persist observed subjects to localStorage keyed per chat
    // session — so when the conversation context gets compacted (Claude
    // drops old messages from its rolling window to save tokens), later
    // TaskUpdate(delete #2) renderings can still resolve "#2 → Fix #2:
    // Windows .env 去 BOM" from the persistent map. Without this, long
    // chats render naked "✗ 删除 #2" with no subject — confusing.
    _taskSubjStorageKey() {
      return "muselab.taskSubjects." + (this.currentId || "_default");
    },
    _loadStoredTaskSubjects() {
      try {
        const raw = localStorage.getItem(this._taskSubjStorageKey());
        return raw ? JSON.parse(raw) : {};
      } catch (_) { return {}; }
    },
    _storeTaskSubjects(map) {
      try {
        localStorage.setItem(this._taskSubjStorageKey(), JSON.stringify(map));
      } catch (_) { /* quota / private mode — best-effort, ignore */ }
      // Per-session keys (muselab.taskSubjects.<sid>) were never evicted, so a
      // long-lived browser accumulated one entry per chat session forever,
      // creeping toward the ~5MB localStorage quota until setItem silently
      // started failing. Track touched sessions in an LRU and drop the oldest
      // beyond the cap. (perf: ORANGE — app.js taskSubjects unbounded growth)
      this._pruneTaskSubjectKeys(this.currentId || "_default");
    },
    _pruneTaskSubjectKeys(currentSid) {
      const PREFIX = "muselab.taskSubjects.";
      const LRU_KEY = PREFIX + "_lru";
      const MAX = 100;
      try {
        let order;
        try { order = JSON.parse(localStorage.getItem(LRU_KEY) || "[]"); }
        catch (_) { order = []; }
        if (!Array.isArray(order)) order = [];
        // Reconcile any per-session keys not yet tracked (e.g. created before
        // this LRU existed, or in another tab) — treat them as oldest so the
        // pre-existing backlog ages out first.
        const known = new Set(order);
        const orphans = [];
        for (let i = 0; i < localStorage.length; i++) {
          const k = localStorage.key(i);
          if (!k || !k.startsWith(PREFIX) || k === LRU_KEY) continue;
          const sid = k.slice(PREFIX.length);
          if (sid !== currentSid && !known.has(sid)) orphans.push(sid);
        }
        order = orphans.concat(order.filter((s) => s !== currentSid));
        order.push(currentSid); // most-recently-touched at the end
        while (order.length > MAX) {
          const victim = order.shift();
          if (victim && victim !== currentSid) {
            try { localStorage.removeItem(PREFIX + victim); } catch (_) { /* ignore */ }
          }
        }
        localStorage.setItem(LRU_KEY, JSON.stringify(order));
      } catch (_) { /* best-effort housekeeping — never block the UI */ }
    },
    _taskSubjectMapForMessages() {
      const msgs = this.messages || [];
      // Cache key includes session id — switching tabs/sessions must
      // invalidate even when message count happens to match.
      const cacheKey = (this.currentId || "_") + ":" + msgs.length;
      const cached = this._cachedTaskSubjectMap;
      if (cached && cached.key === cacheKey) return cached.map;

      // Start from the persistent per-session map — entries observed in
      // previous turns survive context compaction this way.
      const map = Object.assign({}, this._loadStoredTaskSubjects());
      const pendingCreate = {};  // tool_use_id → subject
      let dirty = false;
      for (const m of msgs) {
        if (!m) continue;
        if (m.role === "tool_use" && m.name === "TaskCreate") {
          const subj = (m.input && m.input.subject) || "";
          if (m.id) pendingCreate[m.id] = subj;
        } else if (m.role === "tool_use" && m.name === "TaskUpdate") {
          // Subsequent TaskUpdate may carry an updated subject — refresh
          const inp = m.input || {};
          const tid = inp.taskId || inp.task_id;
          if (tid && inp.subject && map[String(tid)] !== inp.subject) {
            map[String(tid)] = inp.subject;
            dirty = true;
          }
        } else if (m.role === "tool_result") {
          // Backend serializes tool_result's tool_use_id into `m.id`
          // (the same `id` field that tool_use uses, intentionally
          // matched as a pair). Older code expected m.tool_use_id /
          // m.tool_id, which never matched — every TaskCreate's
          // subject was silently dropped from the map.
          const tuId = m.tool_use_id || m.tool_id || m.id;
          if (tuId && pendingCreate[tuId] !== undefined) {
            // Parse "Task #N created successfully" out of result text
            const txt = m.text || m.preview || "";
            const match = txt.match(/Task\s+#(\d+)/i);
            if (match) {
              const tid = match[1];
              if (map[tid] !== pendingCreate[tuId]) {
                map[tid] = pendingCreate[tuId];
                dirty = true;
              }
            }
            delete pendingCreate[tuId];
          }
        }
      }
      if (dirty) this._storeTaskSubjects(map);
      this._cachedTaskSubjectMap = { key: cacheKey, map };
      return map;
    },
    taskLogLine(m) {
      if (!m || !m.name) return null;
      const inp = m.input || {};
      const status = inp.status || "";
      const taskId = String(inp.taskId || inp.task_id || "");
      const subject = inp.subject || "";
      const desc = inp.description || "";
      // For TaskUpdate / TaskStop, look up the original subject so the
      // log line reads "✓ 完成 #2 Fix #2: Windows .env 去 BOM" instead
      // of the bare "✓ 完成 #2".
      const subjectFromMap = taskId
        ? (this._taskSubjectMapForMessages()[taskId] || "")
        : "";

      // Verbs include "任务" / "task" so the line is self-explanatory
      // even when the subject lookup fails (compacted history etc).
      // "✗ 删除 #2" alone reads cryptic; "✗ 删除任务 #2" is obvious.
      //
      // `#N` is the SDK-assigned task ID — useful as a fallback handle
      // when the subject is missing, but noise when the subject is
      // already shown. Show #N only when we have no subject to display.
      const refFallback = (subj) => (subj ? "" : (taskId ? "#" + taskId : ""));
      switch (m.name) {
        case "TaskCreate":
          // TaskCreate has no taskId yet (assigned by runtime, returned
          // in the tool_result text). ref always empty here.
          return {
            verb: this.lang === "zh" ? "新建任务" : "Created task",
            icon: "+", colorClass: "task-created",
            ref: "", subject, detail: desc,
          };
        case "TaskUpdate":
          if (status === "completed") {
            return { verb: this.lang === "zh" ? "完成任务" : "Completed task",
                     icon: "✓", colorClass: "task-done",
                     ref: refFallback(subjectFromMap), subject: subjectFromMap, detail: "" };
          }
          if (status === "in_progress") {
            return { verb: this.lang === "zh" ? "开始任务" : "Started task",
                     icon: "→", colorClass: "task-started",
                     ref: refFallback(subjectFromMap), subject: subjectFromMap, detail: "" };
          }
          if (status === "deleted") {
            return { verb: this.lang === "zh" ? "删除任务" : "Deleted task",
                     icon: "✗", colorClass: "task-deleted",
                     ref: refFallback(subjectFromMap), subject: subjectFromMap, detail: "" };
          }
          if (status === "pending") {
            return { verb: this.lang === "zh" ? "重置任务" : "Reset task",
                     icon: "○", colorClass: "task-pending",
                     ref: refFallback(subjectFromMap), subject: subjectFromMap, detail: "" };
          }
          if (!taskId && !inp.subject && !inp.activeForm) return null;
          if (!taskId) return null;
          if (!inp.subject && !inp.activeForm && !inp.description) return null;
          {
            const subj = inp.subject || inp.activeForm || subjectFromMap;
            return { verb: this.lang === "zh" ? "更新任务" : "Updated task",
                     icon: "·", colorClass: "task-updated",
                     ref: refFallback(subj), subject: subj, detail: "" };
          }
        case "TaskStop":
          return { verb: this.lang === "zh" ? "停止任务" : "Stopped task",
                   icon: "✗", colorClass: "task-deleted",
                   ref: refFallback(subjectFromMap), subject: subjectFromMap, detail: "" };
        case "TaskList":
        case "TaskGet":
        case "TaskOutput":
          // Pure queries — Muse asking itself about state. Hidden.
          return null;
        default:
          return null;
      }
    },
    // Whether this Task tool_use should be visible at all. Used by Alpine
    // x-if to skip the whole bubble for pure queries.
    shouldRenderTaskLine(m) {
      return this.taskLogLine(m) !== null;
    },
    toolResultBodyText(m) {
      // Full body for the expanded view (or the truncation-marker tail).
      // Prefer `text` (50KB cap) over the legacy `preview` (500-char).
      if (!m) return "";
      const body = m.text || m.preview || "";
      if (m.text_truncated) {
        const suffix = this.lang === "zh"
          ? "\n\n…（输出已截断，剩余内容未传到前端）"
          : "\n\n…(output truncated — server did not forward the rest)";
        return body + suffix;
      }
      return body;
    },
    readResultLines(m) {
      // Read tool emits `   1→line one\n   2→line two\n...`. We rebuild a
      // [{n, content}] list so the template can render a line-number gutter
      // without re-splitting every render frame. Falls back to a single
      // synthetic entry when the format doesn't match (vendor wrapper,
      // mocked test, etc.).
      const body = this.toolResultBodyText(m);
      // Memo: re-split only when the body changes. The message x-for nests an
      // x-for over these lines, so without a cache every parent re-render
      // re-splits a potentially large Read result line-by-line.
      const _raw = _rawMsg(m);
      const _hit = _readLinesCache.get(_raw);
      if (_hit && _hit.src === body) return _hit.lines;
      const lines = this._computeReadResultLines(body);
      _readLinesCache.set(_raw, { src: body, lines });
      return lines;
    },
    _computeReadResultLines(body) {
      const out = [];
      const re = /^\s*(\d+)→(.*)$/;
      for (const ln of body.split("\n")) {
        const mm = ln.match(re);
        if (mm) {
          out.push({ n: Number(mm[1]), content: mm[2] });
        } else if (ln === "" && out.length) {
          // Trailing blank — Read often ends with an empty marker. Keep
          // it so spacing is faithful to the source file.
          out.push({ n: 0, content: "" });
        } else if (out.length === 0) {
          // Pre-data noise (e.g. "(Reading X lines from file Y)") — render
          // as a header line without a gutter number.
          out.push({ n: 0, content: ln });
        } else {
          // Line that doesn't match the n→ format mid-stream — append to
          // the previous content so wrapped output stays readable.
          out[out.length - 1].content += "\n" + ln;
        }
      }
      return out;
    },
    // Memoized markdown render of a tool-result body, keyed by raw msg. The
    // template binds x-html to this for web/search-card bodies; mdRender()
    // itself is already text-LRU-cached, but this avoids re-running the body
    // extraction + the full-string Map hash on every re-render of a long list.
    toolResultMd(m) {
      const body = this.toolResultBodyText(m);
      const _raw = _rawMsg(m);
      const _hit = _toolMdCache.get(_raw);
      if (_hit && _hit.src === body) return _hit.html;
      const html = this.mdRender(body);
      _toolMdCache.set(_raw, { src: body, html });
      return html;
    },
    // Memoized search-hit parse for a tool-result body, keyed by raw msg.
    // parseSearchHits scans the whole body with a regex; the template slices
    // the first 100 but the parse runs over everything each render without this.
    searchHitsFor(m) {
      const body = this.toolResultBodyText(m);
      const _raw = _rawMsg(m);
      const _hit = _searchHitsCache.get(_raw);
      if (_hit && _hit.src === body) return _hit.hits;
      const hits = this.parseSearchHits(body);
      _searchHitsCache.set(_raw, { src: body, hits });
      return hits;
    },
    bashResultText(m) {
      // Prefer the structured parse (stdout/stderr/exit_code separated)
      // when the backend provided it; otherwise fall back to the raw body.
      if (m && m.bash) {
        const stdout = (m.bash.stdout || "").replace(/\n+$/, "");
        const stderr = (m.bash.stderr || "").replace(/\n+$/, "");
        return { stdout, stderr,
                 exit_code: m.bash.exit_code,
                 interrupted: !!m.bash.interrupted };
      }
      return { stdout: this.toolResultBodyText(m), stderr: "",
               exit_code: undefined, interrupted: false };
    },
    // ---- LCS-based line diff for Edit/Write/MultiEdit ----
    //
    // Why we ship our own and not jsdiff: muselab has a no-build-step rule
    // (vendor/ is pre-built minified blobs only). A 40-line LCS is enough
    // for the Edit-tool case (typically <100-line snippets) and avoids the
    // 20KB jsdiff dependency. We cap input length so a pathological
    // 5000-line both-sides snippet doesn't pin the main thread.
    _lineDiff(oldText, newText, capLines = 800) {
      const a = (oldText || "").split("\n");
      const b = (newText || "").split("\n");
      // Trim to cap on each side and prepend a synthetic ellipsis line so
      // the user knows we capped — better than silently dropping context.
      if (a.length > capLines) a.length = capLines;
      if (b.length > capLines) b.length = capLines;
      const m = a.length, n = b.length;
      // Build LCS table. O(m·n) space — fine for capLines² = 640k cells.
      const dp = Array.from({ length: m + 1 }, () => new Uint32Array(n + 1));
      for (let i = m - 1; i >= 0; i--) {
        for (let j = n - 1; j >= 0; j--) {
          dp[i][j] = a[i] === b[j]
            ? dp[i + 1][j + 1] + 1
            : Math.max(dp[i + 1][j], dp[i][j + 1]);
        }
      }
      // Walk it to produce a unified-style op list.
      const ops = [];
      let i = 0, j = 0;
      while (i < m && j < n) {
        if (a[i] === b[j]) {
          ops.push({ op: "ctx", text: a[i] }); i++; j++;
        } else if (dp[i + 1][j] >= dp[i][j + 1]) {
          ops.push({ op: "del", text: a[i] }); i++;
        } else {
          ops.push({ op: "ins", text: b[j] }); j++;
        }
      }
      while (i < m) { ops.push({ op: "del", text: a[i++] }); }
      while (j < n) { ops.push({ op: "ins", text: b[j++] }); }
      return ops;
    },
    // +X / -Y badge data for an Edit / Write / MultiEdit tool_use bubble.
    // Counts the LCS ops we already compute for the diff strip — same
    // truth as the visible diff, no parallel logic to drift.
    editDiffStats(m) {
      if (!m) return null;
      const ops = this.editDiffOps(m);
      if (!ops || !ops.length) return null;
      let plus = 0, minus = 0;
      for (const op of ops) {
        if (op.op === "ins") plus++;
        else if (op.op === "del") minus++;
      }
      if (plus === 0 && minus === 0) return null;
      return { plus, minus };
    },
    editDiffOps(m) {
      // Returns ops for an Edit / Write / MultiEdit tool_use. MultiEdit's
      // `edits` array is flattened into a single op list with a separator
      // op between sub-edits so the template can render section labels.
      if (!m || !m.input) return [];
      // Memo: a tool_use's input/name is immutable after creation, so the
      // diff never changes. Cache keyed by the raw msg — editDiffOps and
      // editDiffStats both hit this, and the message x-for re-invokes it on
      // every render; the underlying _lineDiff is an O(m·n) LCS build.
      const _raw = _rawMsg(m);
      const _hit = _diffOpsCache.get(_raw);
      if (_hit !== undefined) return _hit;
      const _ops = this._computeEditDiffOps(m);
      _diffOpsCache.set(_raw, _ops);
      return _ops;
    },
    _computeEditDiffOps(m) {
      const inp = m.input;
      if (m.name === "MultiEdit" && Array.isArray(inp.edits)) {
        const out = [];
        inp.edits.forEach((e, idx) => {
          if (idx > 0) out.push({ op: "sep", text: `--- edit ${idx + 1} ---` });
          const sub = this._lineDiff(e.old_string || "", e.new_string || "");
          out.push(...sub);
        });
        return out;
      }
      if (m.name === "Write") {
        // Write creates / overwrites — show `content` as all-insertions so
        // the user sees what's about to land in the file.
        const body = inp.content || "";
        return body.split("\n").map(t => ({ op: "ins", text: t }));
      }
      // Edit (or fallback)
      return this._lineDiff(inp.old_string || "", inp.new_string || "");
    },
    // ---- error CTA dispatch ----
    errorCtaLabel(kind, cta) {
      if (cta === "open_settings") {
        return this.lang === "zh" ? "打开设置" : "Open Settings";
      }
      if (cta === "switch_model") {
        return this.lang === "zh" ? "换个模型" : "Switch model";
      }
      if (cta === "compact_or_fork") {
        return this.lang === "zh" ? "压缩对话" : "Compact session";
      }
      return this.lang === "zh" ? "重试" : "Retry";
    },
    errorCtaInvoke(m) {
      // m carries _error_kind / _error_cta. Map to the matching action —
      // we deliberately reuse existing methods to avoid duplicate codepaths.
      const cta = m && m._error_cta;
      if (cta === "open_settings") { this.openSettings(); return; }
      if (cta === "switch_model") {
        // Open the model picker if it exists; otherwise just toast hint.
        const pick = document.querySelector(".model-picker, #model-select");
        if (pick) pick.focus();
        this.toast(this.lang === "zh"
          ? "在右上模型下拉里选别的" : "Pick another model from the top dropdown",
          "info", 3500);
        return;
      }
      if (cta === "compact_or_fork") {
        this.runCompact && this.runCompact();
        return;
      }
      // Default "Retry" — reuse the existing failed-message retry path.
      if (m && m.role === "user" && m._failed) {
        this.retryFailedMessage(m);
      }
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
      // Write IN-PLACE into the tab's sessionUsage object, then re-point the
      // root `this.sessionUsage` at that same object. Replacing this.sessionUsage
      // with a fresh `{...}` literal (as before) detached it from
      // tabState[sid].sessionUsage — the shared reference _activateTabState /
      // the stream's done-handler rely on. After that split, later done-event
      // updates (Object.assign onto the tab object) no longer reached the
      // displayed ring, and a tab switch restored the stale tab object —
      // i.e. the meter "wasn't accurate". Mutating the tab object keeps every
      // reference in sync.
      const st = this._ensureTabState(this.currentId);
      Object.assign(st.sessionUsage, {
        context_used: used,
        context_limit: maxT,
        context_used_pct: maxT
          ? Math.round(used / maxT * 1000) / 10
          : 0,
      });
      this.sessionUsage = st.sessionUsage;
    },

    async showCtxBreakdown() {
      if (!this.currentId) return;
      this.ctxBreakdown = { show: true, loading: true, data: null, error: "" };
      this.ctxExpanded = {};
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
    // % of maxTokens used by this category — drives both the stacked bar
    // at the top of the popup and the per-row inline bar.
    ctxCategoryPct(cat) {
      const max = (this.ctxBreakdown.data && this.ctxBreakdown.data.maxTokens) || 0;
      if (!max || !cat || !cat.tokens) return 0;
      return Math.min(100, (cat.tokens / max) * 100);
    },
    // Pick a category color. SDK populates cat.color for known categories;
    // fall back to a stable hash-based hue for everything else so the bar
    // segments stay distinct.
    ctxCategoryColor(cat) {
      if (cat && cat.color) return cat.color;
      const n = (cat && cat.name) || "?";
      let h = 0;
      for (let i = 0; i < n.length; i++) h = (h * 31 + n.charCodeAt(i)) >>> 0;
      return `hsl(${h % 360}, 55%, 55%)`;
    },
    ctxFormatTokens(n) {
      if (!n) return "0";
      if (n >= 1000) return (n / 1000).toFixed(1) + "K";
      return String(n);
    },
    // Map a category name to its detailed sub-list. SDK returns
    // memoryFiles / mcpTools / agents as separate top-level arrays; we
    // surface them under whichever category row carries the same totals.
    // Match is fuzzy (lowercased + stripped of separators) since the SDK
    // labels may localize the category name.
    ctxRowChildren(name) {
      const data = this.ctxBreakdown.data || {};
      const key = String(name || "").toLowerCase().replace(/[\s_-]/g, "");
      if (key.includes("memory")) return data.memoryFiles || [];
      if (key.includes("mcp")) return data.mcpTools || [];
      if (key.includes("agent")) return data.agents || [];
      return [];
    },
    ctxToggleRow(name) {
      if (!this.ctxRowChildren(name).length) return;
      this.ctxExpanded[name] = !this.ctxExpanded[name];
    },

    ctxRingTitle() {
      const u = this.sessionUsage || {};
      const used = u.context_used || 0;
      const limit = u.context_limit || 0;
      const pct = u.context_used_pct || 0;
      const curSt = this.tabState[this.currentId];
      if (curSt && curSt.compacting) {
        const qn = this._currentQueueLen();
        return this.lang === "zh"
          ? `📦 压缩进行中，已排队 ${qn} 条`
          : `📦 Compact in progress (${qn} queued)`;
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
      const curSt = this.tabState[this.currentId];
      if (!curSt || !curSt.compacting) {
        try { return this.ctxMeterLabel(); }
        catch { return ""; }
      }
      const q = this._currentQueueLen();
      if (this.lang === "zh") {
        return q ? `📦 压缩中… 消息队列 ${q}` : "📦 压缩中…";
      }
      return q ? `📦 Compacting… queued ${q}` : "📦 Compacting…";
    },

    activateTab(tid) {
      if (tid === this.currentId) return;
      this.currentId = tid;
      // Clear the green "task done while you were elsewhere" dot now that
      // the user is actually looking at this tab.
      const st = this.tabState && this.tabState[tid];
      if (st && st.unread) st.unread = false;
      this.switchSession();
      // Scroll the newly-active tab into view — when the strip overflows
      // horizontally (many sessions open), keyboard shortcuts / programmatic
      // activation would otherwise leave the active tab hidden off-screen.
      this.$nextTick(() => this._scrollTabIntoView(tid));
      // Drain any queue that was waiting for this tab to become active.
      // _drainPendingQueue checks busy + paused + sid===currentId, so this
      // is safe to call unconditionally.
      this.$nextTick(() => this._drainPendingQueue(tid));
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
    _scrollPreviewSelectedIntoView() {
      // Mirrors _scrollTabIntoView for the preview pane: scrolls the active
      // file's row into view in both the Open files list (vertical) and the
      // preview tab bar (horizontal). Called from openFile after `selected`
      // updates. No-op when nothing is selected, or when the items happen
      // to already be visible — `block/inline: nearest` won't scroll then.
      const path = this.selected;
      if (!path) return;
      const sel = (window.CSS && CSS.escape) ? CSS.escape(path) : path;
      // Preview tab bar — horizontal scroll only.
      const tab = document.querySelector(`.tab-bar .tab[data-path="${sel}"]`);
      if (tab) tab.scrollIntoView({ inline: "nearest", block: "nearest" });
      // Open files list — vertical scroll. Skip when the list is collapsed
      // (the <ul> isn't rendered, so the lookup would be a no-op anyway,
      // but the check avoids a needless DOM hit on every file switch).
      if (!this.openFilesCollapsed) {
        const row = document.querySelector(`.open-files-list li[data-path="${sel}"]`);
        if (row) row.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
    },
    onTabAuxClick(ev, tid) {
      // 1 = middle-click — close the tab.
      if (ev.button === 1) this.closeChatTab(tid);
    },
    onPreviewTabAuxClick(ev, path) {
      // Same as onTabAuxClick but for the preview tab bar. The naive
      // @auxclick="closeTab" without a button check fires on right-click
      // too — auxclick is "any non-primary button" per spec — so right-
      // clicking a preview tab would both pop the context menu (via
      // @contextmenu) AND close the tab. Gate on button === 1 (middle).
      if (ev.button === 1) this.closeTab(path);
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
      // Unsaved-edits guard for bulk-close actions that may evict the tab
      // currently being edited. closeAll always does; closeOthers evicts the
      // active edit unless it's the kept tab; closeRight evicts it only when
      // the active edit sits to the right of `path`.
      if (this.editing) {
        const ti = this.tabs.findIndex(t => t.path === path);
        const si = this.tabs.findIndex(t => t.path === this.selected);
        const evictsEdit =
          action === "closeAll" ||
          (action === "closeOthers" && this.selected !== path) ||
          (action === "closeRight" && ti >= 0 && si > ti);
        if (evictsEdit && !this._confirmLoseEdits()) return;
      }
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
          this.tabs = []; this.selected = "";
          this.previewMode = "";
          this.rawText = "";
          this.renderedMd = "";
          this.editing = false;
          this.savePrefs();
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
            () => this.errToast("copy", this.lang === "zh"
                                            ? "需要 HTTPS"
                                            : "HTTPS required"));
          break;
      }
    },

    async onPreviewDrop(ev) {
      this.previewDragHover = false;
      this.osFileDragging = false;
      this._dragCounter = 0;
      const files = Array.from((ev.dataTransfer && ev.dataTransfer.files) || []);
      if (!files.length) return;
      // Always upload to the archive root (MUSELAB_ROOT), regardless of
      // which file is currently open in the preview pane. Earlier this
      // dropped into the previewed file's parent directory, but that
      // made it easy to accidentally pollute deep sub-folders (`health/
      // 2026-04/scans/random_screenshot.png`) when the user actually
      // wanted to triage drops from the top level first. Root is the
      // predictable target — the user can always move files later from
      // the file tree.
      //
      // Multi-file drop: upload in parallel and refresh the tree ONCE
      // at the end. The previous `for ... await uploadFileTo` was
      // sequential AND ran reloadTree() per file, so a 5-file drop
      // serialized 5 uploads + 5 tree refetches — slow enough that
      // users reported "looks like only one got uploaded" because
      // they hadn't waited for the others to finish.
      const results = await Promise.allSettled(
        files.map(f => this._uploadFileQuiet("", f))
      );
      const ok = results.filter(r => r.status === "fulfilled" && r.value).length;
      const failed = results.length - ok;
      this.reloadTree();
      if (failed && !ok) {
        this.toast(this.lang === "zh"
          ? `${failed} 个文件上传失败`
          : `${failed} file(s) failed to upload`, "error", 3500);
      } else if (failed) {
        this.toast(this.lang === "zh"
          ? `已上传 ${ok} 个，${failed} 个失败`
          : `Uploaded ${ok}, ${failed} failed`, "warn", 3500);
      } else if (ok === 1) {
        this.toast(this.lang === "zh"
          ? `已上传 ${files[0].name}`
          : `Uploaded ${files[0].name}`, "success", 2200);
      } else {
        this.toast(this.lang === "zh"
          ? `已上传 ${ok} 个文件`
          : `Uploaded ${ok} files`, "success", 2200);
      }
    },
    // Single-file upload without tree reload / toast — used by parallel
    // drop handlers (onPreviewDrop, tree-onDrop multi-file) so the caller
    // can batch the side effects. Returns true on success, false on error.
    async _uploadFileQuiet(dirPath, file) {
      const fd = new FormData();
      fd.append("path", dirPath);
      fd.append("file", file);
      try {
        const r = await fetch("/api/files/upload", {
          method: "POST", headers: this.hdr(), body: fd,
        });
        if (!r.ok) {
          console.warn("[upload]", file.name, "failed:", r.status);
          return false;
        }
        delete this.childCache[dirPath];
        return true;
      } catch (e) {
        console.warn("[upload]", file.name, "error:", e);
        return false;
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
      this._stopDragAutoScroll();
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
      this._stopDragAutoScroll();
    },
    // Edge auto-scroll for the "Open files" list while reordering. Native
    // HTML5 drag suppresses `wheel` events, so a list taller than its
    // viewport can't be scrolled by the mouse wheel mid-drag — items below
    // the fold become unreachable. `dragover` keeps firing though, so we use
    // it: when the cursor is within EDGE px of the top/bottom, run an rAF
    // loop that nudges scrollTop until the cursor leaves the zone or the
    // drag ends. Guarded to our tab-reorder drag so dragging a tree file /
    // an OS file over the list doesn't hijack scrolling.
    onListDragAutoScroll(ev, el) {
      if (!this._draggingPreviewTabPath || !el) return;
      const rect = el.getBoundingClientRect();
      const EDGE = 32, SPEED = 10;
      const y = ev.clientY;
      let dir = 0;
      if (y < rect.top + EDGE) dir = -1;
      else if (y > rect.bottom - EDGE) dir = 1;
      this._dragScroll = this._dragScroll || { raf: 0, dir: 0, el: null };
      this._dragScroll.dir = dir;
      this._dragScroll.el = el;
      if (dir && !this._dragScroll.raf) {
        const step = () => {
          const ds = this._dragScroll;
          if (!ds || !ds.dir || !ds.el) { if (ds) ds.raf = 0; return; }
          ds.el.scrollTop += ds.dir * SPEED;
          ds.raf = requestAnimationFrame(step);
        };
        this._dragScroll.raf = requestAnimationFrame(step);
      }
    },
    _stopDragAutoScroll() {
      const ds = this._dragScroll;
      if (!ds) return;
      ds.dir = 0; ds.el = null;
      if (ds.raf) { cancelAnimationFrame(ds.raf); ds.raf = 0; }
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
    // Bucket the filtered list into Pinned / Today / Yesterday / Last 7d /
    // Last 30d / Earlier so a few hundred sessions stay scannable. Pinned
    // always floats to the top; the rest are based on updated_at
    // (epoch seconds — same source as the existing sort).
    groupedFilteredSessions() {
      // The popup is x-show (not x-if), so this binding stays live even while
      // hidden — without this guard the full filter+bucket reran on every
      // unrelated re-render (each keystroke, each stream tick). When closed
      // nothing here is visible: return the last computed groups and skip the
      // work. Reading sessionPickerOpen registers it as a reactive dep so
      // reopening recomputes. When open we always recompute fresh, so renames
      // / reorders are never served stale.
      if (!this.sessionPickerOpen) return this._groupedSessionsCache || [];
      this._groupedSessionsCache = this._computeGroupedFilteredSessions();
      return this._groupedSessionsCache;
    },
    _computeGroupedFilteredSessions() {
      const items = this.filteredSessions();
      if (!items.length) return [];
      const now = new Date();
      const startOfToday = new Date(now.getFullYear(), now.getMonth(),
                                     now.getDate()).getTime() / 1000;
      const startOfYesterday = startOfToday - 86400;
      const startOf7d = startOfToday - 7 * 86400;
      const startOf30d = startOfToday - 30 * 86400;
      const pinned = [], today = [], yest = [], week = [], month = [], earlier = [];
      for (const s of items) {
        if (s.pinned) { pinned.push(s); continue; }
        const t = s.updated_at || s.created_at || 0;
        if (t >= startOfToday) today.push(s);
        else if (t >= startOfYesterday) yest.push(s);
        else if (t >= startOf7d) week.push(s);
        else if (t >= startOf30d) month.push(s);
        else earlier.push(s);
      }
      const zh = this.lang === "zh";
      const searching = !!(this.sessionPickerSearch || "").trim();
      // Groups with a limit collapse to PICKER_GROUP_LIMIT items until the
      // user expands them. Search bypasses limits — when the user is looking
      // for something specific they want to see everything.
      const LIMIT = 20;
      const _group = (key, label, arr, limited = false) => {
        if (!arr.length) return null;
        const expanded = searching || !limited || !!this.pickerGroupExpanded[key];
        const visibleItems = expanded ? arr : arr.slice(0, LIMIT);
        return { key, label, items: arr, visibleItems,
                 limited, hiddenCount: arr.length - visibleItems.length };
      };
      return [
        _group("pinned",    zh ? "置顶"       : "Pinned",       pinned),
        _group("today",     zh ? "今天"        : "Today",        today),
        _group("yesterday", zh ? "昨天"        : "Yesterday",    yest),
        _group("week",      zh ? "最近 7 天"   : "Last 7 days",  week),
        _group("month",     zh ? "最近 30 天"  : "Last 30 days", month,  true),
        _group("earlier",   zh ? "更早"        : "Earlier",      earlier, true),
      ].filter(Boolean);
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
      this.pickerGroupExpanded = {};  // reset collapse state on each open
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
    // Inline rename inside the picker row (✎ icon). MUST be fully
    // synchronous through to el.focus() — iOS Safari only opens the
    // on-screen keyboard when focus() is called within the same JS
    // tick as the click handler that received the user gesture. Any
    // await / $nextTick / setTimeout severs the chain and the user
    // sees the input appear but no keyboard. The input is already
    // mounted (x-show, not x-if) so we just need to flip the state
    // flag and call focus() in the same tick.
    pickerStartInlineRename(ev, sid) {
      if (ev && ev.stopPropagation) ev.stopPropagation();
      const s = this.sessions.find(x => x.id === sid);
      if (!s) return;
      this.renamingPickerSid = sid;
      this.pickerRenameDraft = s.name || "";
      // Synchronous focus — same tick as the click. No await / nextTick.
      const el = document.querySelector(
        `.session-picker-rename-input[data-sid="${CSS.escape(sid)}"]`);
      if (el) { el.focus(); el.select(); }
    },
    async pickerCommitInlineRename() {
      const sid = this.renamingPickerSid;
      const name = (this.pickerRenameDraft || "").trim();
      this.renamingPickerSid = "";
      this.pickerRenameDraft = "";
      if (!sid || !name) return;
      const cur = this.sessions.find(x => x.id === sid);
      if (!cur || cur.name === name) return;
      const r = await fetch("/api/chat/sessions/" + sid, {
        method: "PATCH",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (r.ok) {
        cur.name = name;
        cur.auto_named = false;
      } else {
        this.toast(this.lang === "zh" ? "重命名失败" : "Rename failed", "error", 3000);
      }
    },
    pickerCancelInlineRename() {
      this.renamingPickerSid = "";
      this.pickerRenameDraft = "";
    },
    async pickerDeleteSession(sid, ev) {
      if (ev && ev.stopPropagation) ev.stopPropagation();
      const s = this.sessions.find(x => x.id === sid);
      const name = (s && s.name) || sid.slice(0, 8);
      const ok = await this.confirm({
        title: this.t("modal.delete_session_title"),
        body: this.t("modal.delete_session_body", { name }),
        okText: this.t("modal.delete_session_ok"),
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
    menuExportMarkdown(id) {
      this.closeTabMenu();
      if (!id) return;
      // Use a transient anchor so the browser opens the streaming Response
      // as a file download. Token goes in the query string because anchor
      // requests can't carry custom headers.
      const url = `/api/chat/sessions/${id}/export?token=`
                  + encodeURIComponent(this.token);
      const a = document.createElement("a");
      a.href = url; a.style.display = "none";
      // download attribute lets the server's Content-Disposition take
      // precedence but still hints to the browser this isn't navigation.
      a.setAttribute("download", "");
      document.body.appendChild(a);
      a.click();
      setTimeout(() => a.remove(), 200);
    },
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
      // Mobile: any session switch implies "I want to see chat" — covers
      // every entry point at once (openTab from picker, activateTab from
      // chat-tabs strip, slash /resume, ctx-menu, programmatic newSession).
      // Earlier we only did this in openTab, which left chat-tabs taps and
      // a few other paths needing a second tap on the bottom Muse icon.
      if (this._isMobileLayout()) this.mobileTab = "chat";
      // Switch the visible tab. We do NOT touch other tabs' streams — each
      // tab's ES is in its own tabState[id], and stream callbacks write
      // there directly. Switching is just "show that tab".
      this._activateTabState(this.currentId);
      this.savePrefs();
      // Sync the model + effort dropdowns to THIS session's persisted
      // values on every tab switch. Without this, the dropdowns are
      // tied to root state (this.model / this.effort) which carries
      // over from whatever the user last picked on the previous tab.
      // Symptom (2026-05-23 user report): on session A (opus), open
      // new session B + pick haiku → switch back to A → dropdown
      // wrongly shows "haiku" even though A.model is still opus. The
      // backend was fine; only the UI label drifted. Same fix applied
      // to effort which has the same shape (per-session metadata).
      const cur = this.sessions.find(s => s.id === this.currentId);
      if (cur) {
        if (cur.model) this.model = cur.model;
        // effort: explicit assignment even when empty — switching from
        // a high-effort tab to one with no override should clear the
        // dropdown, not inherit the old value.
        this.effort = cur.effort || "";
        // thinking: default true when the field is absent (legacy sessions).
        this.thinkingEnabled = cur.thinking !== false;
      }
      const st = this._ensureTabState(this.currentId);
      if (!st._loaded) {
        await this.loadSession(this.currentId);
        st._loaded = true;
      } else {
        // Already loaded — just re-bind UI state. messages reference unchanged.
        this.atBottom = true;
        this.scrollToBottom(true);
        this.$nextTick(() => this.highlightCode(".chat-body"));
      }
    },
    // Background-completion hook: after loadSession populates the
    // JSONL-derived history, ask the backend whether this session has
    // an in-flight turn still running. If yes, transparently
    // reconnect to the broadcast — `send({reconnect: true})` opens
    // an empty-prompt SSE to the same endpoint, and the backend's
    // reconnect mode replays the existing event buffer then streams
    // live. User sees the reply continue right where it left off.
    async _checkActiveTurn(sid) {
      // Refresh the queue mirror on every load/reconnect probe so a session
      // with server-side queued items shows them immediately (e.g. items that
      // were waiting behind an active turn, or left dormant after a restart).
      this._syncQueueFromServer(sid);
      try {
        const r = await fetch("/api/chat/sessions/" + sid + "/active",
                               { headers: this.hdr() });
        if (!r.ok) return;
        const d = await r.json();
        if (this.currentId !== sid) return;
        if (d.active && !this.streaming) {
          // Reconnect any time the backend says there's an active turn.
          // get_session_api returns SDK-only messages (no broadcast
          // overlay), so loadSession's view is just the user msg — the
          // SSE replay we kick off here will refill thinking / text /
          // tool blocks live. Fire-and-forget; the SSE handlers populate
          // messages as events arrive.
          // Pass the turn's real server-side start so the elapsed counter
          // resumes from the true start, not from the reconnect moment
          // (otherwise the footer "running time" resets to 0 on every
          // reload / tab-switch / SSE re-subscribe to an in-flight turn).
          // continuation: a bg-task watcher's headless turn — attach in
          // continuation mode so we DON'T truncate the launching card.
          this.send({ reconnect: true, startedAt: d.started_at,
                       continuation: !!d.continuation });
          return;
        }
        // No active turn. The server drains the queue on its own when a turn
        // finishes; on a fresh load with no turn running, dormant items (e.g.
        // left after a process restart, which intentionally does NOT auto-
        // resume) just show in the UI with a Resume button — we don't auto-
        // kick here, matching the interrupted-turns "wait for the user" policy.
        // But if this loaded session has a still-⏳ bg-task card (the JSONL
        // rebuild rendered a running task), arm the continuation poller so its
        // eventual completion surfaces live without a manual reload.
        this._ensureBgContPoller(sid);
      } catch (e) { /* silent */ }
    },

    // Hover-prefetch: kick off loadSession when the user's mouse rests
    // on a session-picker row or chat tab. By the time they actually
    // click (typical hover→click gap 100-300 ms on desktop), the
    // background fetch has usually returned and switchSession finds
    // `st._loaded === true`, taking the instant code path. Net effect:
    // first-time switches feel ~150 ms snappier on desktop. Mobile (no
    // hover) is unaffected — falls back to on-click loadSession exactly
    // as before.
    //
    // Why 300 ms debounce: scanning a long session list with the mouse
    // would otherwise fire a full /api/chat/sessions/{id} request for
    // every row the cursor brushes over. The timer resets per hover so
    // only the row the user actually pauses on triggers a fetch.
    //
    // Safety: loadSession is per-session safe (writes only into
    // tabState[sid].messages, never touches this.messages or
    // messagesLoading unless sid === currentId), so prefetching an
    // off-screen session can't disturb the active view.
    prefetchSession(sid) {
      if (!sid) return;
      const st = this.tabState && this.tabState[sid];
      if (st && st._loaded) return;
      if (!this._prefetching) this._prefetching = {};
      if (this._prefetching[sid]) return;
      clearTimeout(this._prefetchTimer);
      this._prefetchTimer = setTimeout(async () => {
        if (this._prefetching[sid]) return;
        // Re-check loaded state: it may have flipped while we waited
        // (user clicked the row mid-debounce → switchSession ran).
        const st2 = this.tabState && this.tabState[sid];
        if (st2 && st2._loaded) return;
        this._prefetching[sid] = true;
        try {
          await this.loadSession(sid);
          const st3 = this._ensureTabState(sid);
          st3._loaded = true;
        } catch (_) { /* silent — actual click will retry */ }
        delete this._prefetching[sid];
      }, 300);
    },

    async loadSession(sid, opts = {}) {
      if (!sid) return;
      // full:true → fetch the raw-JSONL view (?full=1) so PRE-compaction
      // messages are included. Used by jump-to-outline when the target
      // prompt lives before a compact boundary (the default SDK view starts
      // at the compact summary and can't reach it). Records st._fullLoaded
      // so the jump retry doesn't loop re-requesting full mode.
      const full = !!opts.full;
      const st = this._ensureTabState(sid);
      // Skeleton on the active tab during the fetch — markdown rendering of
      // a long history can also take a noticeable beat after the network
      // returns, so the flag must wrap both phases.
      // Snapshot only drives the INITIAL skeleton flag (pre-await). After any
      // await the active tab may have changed, so every post-await decision
      // must re-check `sid === this.currentId` live — otherwise switching tabs
      // mid-load corrupts the now-active tab (messages not assigned / skeleton
      // stuck / model/effort overwritten by the old session). See loadSession race.
      const isCurrent = sid === this.currentId;
      if (isCurrent) { this.messagesLoading = true; this.messagesReady = false; }
      // Set true once we've scheduled the reveal (highlight→show). Guards the
      // finally so error / empty-result paths don't leave the skeleton stuck.
      let scheduledReveal = false;
      try {
        // Backend windowing (perf): a long / un-compacted session can shape
        // into thousands of bubbles and many MB of JSON. Shipping + parsing
        // the whole thing on every entry was the dominant freeze ("卡死").
        // So unless full mode is requested, ask the server for only the TAIL
        // we'll actually paint up front. The tail must be wide enough that
        // pickVisibleStart's "at least 2 user turns" guarantee still holds
        // (it can rewind up to INITIAL_LOAD*5), so we request that much; we
        // still render only ~INITIAL_LOAD and stash the rest. Older history
        // pages in from the server via _fetchOlderWindow on "Load earlier".
        const _coldEarly = !this.appReady;
        const _mobileEarly = this._isMobileLayout();
        const _initialLoadEarly = _mobileEarly
          ? (_coldEarly ? 8 : 15)
          : (_coldEarly ? 12 : 30);
        const FETCH_TAIL = _initialLoadEarly * 5;
        const qs = full ? "?full=1" : ("?tail=" + FETCH_TAIL);
        const r = await fetch("/api/chat/sessions/" + sid + qs, { headers: this.hdr() });
        if (!r.ok) {
          st.messages.length = 0;
          if (sid === this.currentId) this.messages = st.messages;
          return;
        }
        const s = await r.json();
        // Build a lookup of blob preview URLs from the current in-memory
        // messages so we can carry them over after the server rebuild.
        // Server messages only store {mime} for images — no preview URL —
        // so without this, any image sent in the current session loses its
        // thumbnail the moment loadSession is called (e.g. after refreshChat
        // or tab switch). We match by role + text + image count.
        const _blobPreviews = new Map();
        (st.messages || []).forEach(em => {
          if (em.role === "user" && em.images && em.images.length) {
            const key = (em.text || "") + ":" + em.images.length;
            if (!_blobPreviews.has(key)) {
              _blobPreviews.set(key, em.images.map(im => im.preview || null));
            }
          }
        });
        // Build message envelopes WITHOUT running mdRender — the heavy
        // markdown→HTML pass is the dominant cost for long sessions, so we
        // defer it until the message is actually about to be shown.
        const buildEnvelope = (m, idx) => {
          const out = { ...m, _k: sid + "-" + idx };
          // Restore blob preview URLs on user messages with images
          if (m.role === "user" && m.images && m.images.length) {
            const key = (m.text || "") + ":" + m.images.length;
            const saved = _blobPreviews.get(key);
            if (saved) {
              out.images = m.images.map((im, i) => ({
                ...im,
                preview: (saved[i] && saved[i].startsWith("blob:"))
                           ? saved[i] : (im.preview || null),
              }));
            }
          }
          return out;
        };
        const all = (s.messages || []).map(buildEnvelope);
        // Lazy-load thresholds — only render the tail of the conversation on
        // first paint; older messages stay in a "to-render" stash and get
        // mdRender'd on demand when the user clicks "Load earlier".
        // Rationale: a long indie-coding session can rack up hundreds of
        // assistant messages, each potentially with a 200-line code block.
        // mdRender + Alpine x-for over all of them locks up the main thread
        // for several seconds on initial load. Rendering only the recent
        // 30 keeps switch-to-session snappy; "Load earlier" lets the user
        // page back in batches of 50 as needed.
        // Narrower first paint on mobile — WebViews have far less headroom
        // than desktop, so render fewer bubbles up front and let "Load
        // earlier" page the rest in on demand.
        // Cold-boot guard: when this is the FIRST session loaded on app start
        // (appReady still false), the heavy markdown+highlight render competes
        // with Alpine's full-tree mount, first-time hljs/mermaid download+parse
        // and the boot fetches — landing directly in a long session froze the
        // page ("卡死"). Render far fewer bubbles up front in that window; the
        // rest page in via "Load earlier" once the app is warm. After boot the
        // normal thresholds apply, so warm tab-switches are unchanged.
        const INITIAL_LOAD = _initialLoadEarly;
        const renderMarkdown = (m) => {
          if (m.role === "assistant" && m.text && !m.html) {
            m.html = this.mdRender(m.text);
          }
        };
        // Split into earlier (deferred) vs visible (rendered now).
        //
        // Naive `slice(-INITIAL_LOAD)` breaks badly when the tail of the
        // conversation is tool-call heavy: one turn can easily have 20+
        // tool_use/tool_result/task-update messages, so the last 30 may
        // contain zero user/assistant TEXT — the user opens the session
        // and sees only Task-update bubbles with no actual conversation.
        //
        // Smarter strategy: rewind from the end until we've included AT
        // LEAST the last two user messages (so there's at least one full
        // back-and-forth visible), capped at INITIAL_LOAD * 5 so a
        // pathological 500-tool-call turn doesn't render everything.
        const pickVisibleStart = (msgs) => {
          if (msgs.length <= INITIAL_LOAD) return 0;
          // Default tail position
          let start = msgs.length - INITIAL_LOAD;
          // Walk backwards collecting user-message indices
          const userIdx = [];
          for (let j = msgs.length - 1; j >= 0; j--) {
            if (msgs[j] && msgs[j].role === "user") {
              userIdx.push(j);
              if (userIdx.length >= 2) break;
            }
          }
          // Anchor on the 2nd-most-recent user msg if we found one
          if (userIdx.length >= 2) start = Math.min(start, userIdx[1]);
          else if (userIdx.length === 1) start = Math.min(start, userIdx[0]);
          // Safety cap so a single huge turn doesn't render hundreds
          const HARD_CAP = INITIAL_LOAD * 5;
          if (msgs.length - start > HARD_CAP) start = msgs.length - HARD_CAP;
          return Math.max(0, start);
        };
        const startIdx = pickVisibleStart(all);
        const visible = all.slice(startIdx);
        const earlier = all.slice(0, startIdx);
        visible.forEach(renderMarkdown);
        // Mutate in place — preserves the Array reference Alpine is watching.
        st.messages.length = 0;
        st.messages.push(...visible);
        // Stash older messages on the per-tab state; the "Load earlier"
        // button reads from here.
        st._earlierMessages = earlier;
        // Backend windowing cursor. The server told us the index of the
        // first bubble it returned (`s.offset`); everything before that
        // still lives on disk and pages in via _fetchOlderWindow. full /
        // no-window responses report offset 0 (whole chain in hand).
        st._loadedOffset = Number.isInteger(s.offset) ? s.offset : 0;
        st._total = Number.isInteger(s.total) ? s.total : all.length;
        // More history exists if either the in-memory stash has older
        // bubbles OR the server holds bubbles before our window.
        st._hasMoreHistory = earlier.length > 0 || st._loadedOffset > 0;
        // Remember whether this load pulled the full raw-JSONL history, so
        // _scrollToUserMsg knows it can stop retrying after one full reload.
        st._fullLoaded = full;
        st._truncatedFromTop = false;
        // (The session outline is sourced from the backend via
        // refreshOutlineFromBackend (GET …/outline), not built here.)
        if (sid === this.currentId) {
          this.messages = st.messages;
          // Background-completion: if there's an in-flight turn on this
          // session that finished while we were elsewhere, the JSONL we
          // just loaded already has its complete output — nothing to do.
          // But if the turn is STILL in progress, tell the user so they
          // know the reply isn't done yet. A proper "reconnect SSE for
          // live streaming" UI is a larger refactor; for now we just
          // surface the state. The user can wait + reload to see more.
          this._checkActiveTurn(sid);
          if (s.model) this.model = s.model;
          // effort defaults to "" (adaptive); always assign so switching from
          // a high-effort tab to a fresh one doesn't leave the old value visible.
          this.effort = s.effort || "";
          // thinking: default true when absent (legacy sessions had no field).
          this.thinkingEnabled = s.thinking !== false;
          this.atBottom = true;
          // Hold the skeleton until highlight + artifacts finish, then reveal
          // the whole conversation at once. Without this, a big session paints
          // un-highlighted text first, then janks code-block-by-code-block as
          // the chunked highlighter catches up — very visible on mobile.
          // scrollToBottom runs AFTER reveal so it measures the final layout.
          scheduledReveal = true;
          this.$nextTick(async () => {
            try { await this.highlightCode(".chat-body"); }
            catch (_e) { /* highlight best-effort — reveal regardless */ }
            if (sid === this.currentId) {
              this.messagesReady = true;
              this.$nextTick(() => { this.atBottom = true; this.scrollToBottom(true); });
            }
          });
        }
        await this._fetchTabUsage(sid);
      } finally {
        // Re-check live: only clear the skeleton if this sid is STILL the
        // active tab. If the user switched away mid-load, the now-active tab
        // owns messagesLoading and must not be cleared by our stale completion.
        if (sid === this.currentId) {
          this.messagesLoading = false;
          // No reveal scheduled (error / early return) → don't trap the
          // skeleton: show whatever we have.
          if (!scheduledReveal) this.messagesReady = true;
        }
      }
    },

    // ===== Lazy-loaded history controls =====
    // Pop the next batch of older messages off the per-tab stash, mdRender
    // them on demand, prepend to messages[]. Critical: preserve scroll
    // position so the user's current viewport doesn't jump when older
    // content unfolds above.
    LOAD_MORE_BATCH: 50,
    // How many older bubbles to pull from the server in one backend page
    // when the in-memory stash runs dry (see _fetchOlderWindow). Larger
    // than LOAD_MORE_BATCH so several "Load earlier" clicks are served from
    // memory between network round-trips.
    HISTORY_PAGE: 200,
    // Absolute in-memory ceiling across messages[] + _earlierMessages. Once
    // hit we stop paging older windows from the server (full history stays
    // in the JSONL); historyTruncated() then surfaces the "not everything is
    // reachable" hint. Bounds memory on pathological thousands-of-bubbles
    // sessions even if the user keeps clicking "Load earlier".
    MAX_IN_MEMORY: 2000,
    // Live-session DOM ceiling. loadSession's INITIAL_LOAD / MAX caps
    // only apply when (re)entering a session — NOTHING trims messages[] while
    // a session is actively chatted in, so a long coding session grows DOM
    // nodes without bound and OOM-crashes mobile WebViews (desktop has the
    // headroom to mask it). Before each new user turn _capLiveMessages evicts
    // the oldest rendered bubbles back into the _earlierMessages stash so
    // "Load earlier" can page them back in. Mobile uses a tighter ceiling.
    LIVE_MESSAGE_CAP: 200,
    // Reactivity ping: bumped whenever refreshOutlineFromBackend writes
    // new data. outlineMessages() reads it so Alpine knows to re-render
    // the msg-outline-modal when async fetch completes. Without this,
    // the first paint sees an empty backend cache and Alpine never
    // re-checks it after fetch returns.
    outlineVersion: 0,

    // Build a navigable outline of the CURRENT session: every user
    // message becomes a clickable jump target. Spans both the visible
    // messages (already rendered) AND the deferred _earlierMessages
    // stash, so the user can scan the entire conversation arc and
    // jump to any point — even into history we haven't rendered yet.
    // Trigger a background fetch of session-level outline if we don't
    // have one yet or it's stale (>30s). Stores result on tabState so
    // outlineMessages() reads it synchronously. Idempotent.
    async refreshOutlineFromBackend(sid) {
      sid = sid || this.currentId;
      if (!sid) return;
      const st = this._ensureTabState(sid);
      const now = Date.now();
      if (st._outlineFetchedAt && (now - st._outlineFetchedAt) < 30000) return;
      if (st._outlineFetching) return;
      st._outlineFetching = true;
      try {
        // Dedicated outline endpoint: the server reads the raw JSONL (so the
        // outline includes PRE-compaction prompts) and returns ONLY the
        // user-prompt previews + uuids — a tiny payload. Previously this
        // fetched ?full=1 (the ENTIRE transcript, several MB) and filtered
        // client-side, which froze the page when opening the outline on a
        // big session. Now the heavy extraction happens server-side once.
        const r = await fetch("/api/chat/sessions/" + sid + "/outline", { headers: this.hdr() });
        if (!r.ok) return;
        const data = await r.json();
        const fresh = (data.outline || []).map(it => ({
          preview: it.preview || "(empty)",
          uuid: it.uuid || null,
        }));
        st._backendOutline = fresh;
        st._outlineFetchedAt = now;
        // Bump the reactivity ping so outlineMessages() re-runs and
        // the Alpine template re-renders with the freshly-fetched list.
        // Nested mutations on tabState[sid]._backendOutline alone do NOT
        // trigger Alpine's dependency graph (Proxy doesn't see deep
        // writes through a getter chain).
        this.outlineVersion++;
      } catch (_) {
        // swallow — fallback path keeps outline working
      } finally {
        st._outlineFetching = false;
      }
    },

    // Pull the next older window of bubbles from the server into the FRONT
    // of the in-memory stash and rewind _loadedOffset. Used when the stash
    // is exhausted but the server still holds older history (the backend-
    // paging counterpart to the in-memory stash drain). Idempotent under
    // concurrent calls via the _fetchingOlder guard. Returns the number of
    // bubbles actually pulled in (0 if nothing more / capped / error).
    async _fetchOlderWindow(sid) {
      sid = sid || this.currentId;
      if (!sid) return 0;
      const st = this._ensureTabState(sid);
      if (st._fetchingOlder) return 0;
      if (!(st._loadedOffset > 0)) return 0;
      // Respect the in-memory ceiling — stop paging once we hold too much.
      const held = (st.messages ? st.messages.length : 0)
                 + (st._earlierMessages ? st._earlierMessages.length : 0);
      if (held >= this.MAX_IN_MEMORY) return 0;
      const newOffset = Math.max(0, st._loadedOffset - this.HISTORY_PAGE);
      const limit = st._loadedOffset - newOffset;
      st._fetchingOlder = true;
      try {
        const r = await fetch(
          "/api/chat/sessions/" + sid + "?offset=" + newOffset + "&limit=" + limit,
          { headers: this.hdr() });
        if (!r.ok) return 0;
        const data = await r.json();
        const win = (data.messages || []).map((m, idx) => ({
          ...m, _k: sid + "-o" + newOffset + "-" + idx,
        }));
        // Prepend (older bubbles go to the front of the stash). mdRender is
        // still deferred until a bubble is paged into messages[].
        st._earlierMessages = win.concat(st._earlierMessages || []);
        st._loadedOffset = Number.isInteger(data.offset) ? data.offset : newOffset;
        if (Number.isInteger(data.total)) st._total = data.total;
        st._hasMoreHistory =
          (st._earlierMessages.length > 0) || st._loadedOffset > 0;
        return win.length;
      } catch (_) {
        return 0;
      } finally {
        st._fetchingOlder = false;
      }
    },
    async loadEarlierMessages(sid) {
      sid = sid || this.currentId;
      if (!sid) return;
      const st = this._ensureTabState(sid);
      // Stash empty but server holds older history → page a window in first.
      if ((!st._earlierMessages || !st._earlierMessages.length)
          && st._loadedOffset > 0) {
        await this._fetchOlderWindow(sid);
      }
      if (!st._earlierMessages || !st._earlierMessages.length) {
        // Nothing local and nothing (more) on the server: recompute flags
        // so the button hides itself.
        st._hasMoreHistory = st._loadedOffset > 0;
        return;
      }
      // Take from the END of the earlier stash (those are the messages
      // immediately preceding what's currently shown — "closest in time").
      const batchSize = this._isMobileLayout() ? 20 : this.LOAD_MORE_BATCH;
      const batch = st._earlierMessages.splice(-batchSize);
      // Now do the deferred mdRender pass on this batch only.
      batch.forEach(m => {
        if (m.role === "assistant" && m.text && !m.html) {
          m.html = this.mdRender(m.text);
        }
      });
      const isCurrent = sid === this.currentId;
      // Capture scroll geometry BEFORE the DOM grows so we can restore the
      // user's visible-content offset after Alpine re-renders.
      const scrollEl = isCurrent ? this.$refs.chatBody : null;
      const oldScrollHeight = scrollEl ? scrollEl.scrollHeight : 0;
      const oldScrollTop = scrollEl ? scrollEl.scrollTop : 0;
      st.messages.unshift(...batch);
      if (isCurrent) this.messages = st.messages;
      st._hasMoreHistory = st._earlierMessages.length > 0 || st._loadedOffset > 0;
      // Restore scroll position so the message the user was looking at
      // stays in place. Without this the viewport snaps to the new top.
      if (scrollEl) {
        this.$nextTick(() => {
          const newScrollHeight = scrollEl.scrollHeight;
          scrollEl.scrollTop = oldScrollTop + (newScrollHeight - oldScrollHeight);
          // Re-run code highlighting ONLY on the newly prepended bubbles
          // (the first batch.length `.msg` children). Rescanning the whole
          // chat body each click is O(total) — wasteful once a long history
          // has been paged in. Falls back to a full scan if the elements
          // can't be resolved.
          const newEls = this._leadingMsgEls(batch.length);
          this.highlightCode(".chat-body", newEls.length ? newEls : null);
        });
      }
    },
    // Evict the oldest rendered messages back to the lazy stash so a long
    // LIVE session doesn't accumulate unbounded DOM (the mobile OOM root
    // cause). Called right before a new user turn is appended — the mirror
    // image of loadEarlierMessages: front-of-messages[] → end-of-stash.
    // The evicted bubbles already carry .html, so paging them back via
    // "Load earlier" re-renders nothing. Eviction happens at the TOP while
    // the user sits at the bottom sending, so there's no scroll jump.
    _capLiveMessages(st) {
      if (!st || !st.messages) return;
      const cap = this._isMobileLayout()
        ? Math.min(50, this.LIVE_MESSAGE_CAP)
        : this.LIVE_MESSAGE_CAP;
      const overflow = st.messages.length - cap;
      if (overflow <= 0) return;
      const evicted = st.messages.splice(0, overflow);
      // Append to the END of the stash: these bubbles are newer than
      // everything already stashed but older than what stays visible, so
      // they're the "closest in time" batch loadEarlierMessages pops first.
      st._earlierMessages = (st._earlierMessages || []).concat(evicted);
      st._hasMoreHistory = st._earlierMessages.length > 0 || st._loadedOffset > 0;
    },
    hasMoreHistory(sid) {
      sid = sid || this.currentId;
      if (!sid) return false;
      const st = this.tabState[sid];
      if (!st) return false;
      // More to show if the in-memory stash has older bubbles OR the server
      // still holds bubbles before our window — but not once we've hit the
      // in-memory ceiling (then historyTruncated() takes over).
      const held = (st.messages ? st.messages.length : 0)
                 + (st._earlierMessages ? st._earlierMessages.length : 0);
      if (st._earlierMessages && st._earlierMessages.length) return true;
      return st._loadedOffset > 0 && held < this.MAX_IN_MEMORY;
    },
    earlierMessageCount(sid) {
      sid = sid || this.currentId;
      if (!sid) return 0;
      const st = this.tabState[sid];
      if (!st) return 0;
      // Older messages = those still on the server (before our window) plus
      // those held in the in-memory stash.
      const stash = st._earlierMessages ? st._earlierMessages.length : 0;
      return (st._loadedOffset || 0) + stash;
    },
    historyTruncated(sid) {
      sid = sid || this.currentId;
      if (!sid) return false;
      const st = this.tabState[sid];
      if (!st) return false;
      // Hit the in-memory ceiling while older history still exists on the
      // server: "Load earlier" stops here, the rest stays in the JSONL.
      const held = (st.messages ? st.messages.length : 0)
                 + (st._earlierMessages ? st._earlierMessages.length : 0);
      return st._loadedOffset > 0 && held >= this.MAX_IN_MEMORY
             && !(st._earlierMessages && st._earlierMessages.length);
    },

    async renameSession() {
      const cur = this.sessions.find(x => x.id === this.currentId);
      if (!cur) return;
      const name = await this.prompt({
        title: this.lang === "zh" ? "重命名会话" : "Rename session",
        value: cur.name,
      });
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
        title: this.lang === "zh"
          ? "本会话 system prompt（留空 = 用默认）"
          : "Per-session system prompt (empty = use default)",
        body: this.lang === "zh"
          ? "会拼在 muselab 默认 system prompt 前。改后下一条消息生效。"
          : "Prepended to muselab's default system prompt. Takes effect on the next message.",
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
      if (!r.ok) {
        this.toast(this.lang === "zh" ? "无法加载设置" : "Failed to load settings", "error");
        return;
      }
      const d = await r.json();
      this.settings.providers = d.providers;
      this.settings.draftKeys = Object.fromEntries(d.providers.map(p => [p.env_key, ""]));
      // Reset provider-editor drafts each open so a stale half-edit from a
      // previous session doesn't reappear. Seed one (closed) draft per
      // provider up front: the editor's x-model bindings reference
      // providerDrafts[p.id].base_url / .models / etc. without optional
      // chaining, and Alpine evaluates x-model even inside an x-show-hidden
      // block — so a missing entry throws "Cannot read properties of
      // undefined". Pre-seeding keeps every binding resolvable.
      this.settings.providerDrafts = Object.fromEntries(
        d.providers.map(p => [p.id, this._draftFromProvider(p)])
      );
      this.settings.providerNew = { show: false, base_url: "", prefix: "", models: "", api_key: "" };
      this.settings.draftDefaults = { ...d.defaults };
      // `d.params` is empty since 2026-05-28 (kept as {} for FE back-compat).
      // Desktop: sidebar is always visible, so land on a default tab
      // (provider — the most-used section) and render only that pane.
      // Mobile: stay at the top-level menu (activePage=null) and let the
      // user drill in; selecting a row shows that section + a Back button.
      this.settings.activePage = this.isWideScreen ? "provider" : null;
      this.settings.show = true;
      // Load MCP + Skill + Cost dashboard in parallel — non-fatal if any fails.
      // Cost is loaded eagerly so desktop users see data without having to
      // click "refresh" (mobile users hit it via the menu item's @click anyway).
      this.refreshMcpList();
      this.refreshSkillList();
      this.loadCostDashboard();
      this.loadClaudeAuthStatus();
    },

    // ===== Claude Auth methods =====
    async loadClaudeAuthStatus() {
      try {
        const r = await fetch("/api/settings/claude-auth/status", {
          headers: this.hdr(),
          // Bypass HTTP cache so a stale "未连接" verdict cached after an
          // earlier failed probe doesn't outlive a `claude login` the user
          // ran later in the terminal. Forces the backend to re-invoke the
          // CLI every call.
          cache: "no-store",
        });
        if (!r.ok) {
          // Surface the failure instead of silently keeping the old state.
          // Without this, a transient 500 / 401 made the UI stick on a
          // wrong "未连接" verdict forever (until next openSettings).
          this.claudeAuth = {
            ...this.claudeAuth,
            loaded: true,
            logged_in: false,
            reason: `http-${r.status}`,
          };
          return;
        }
        const d = await r.json();
        this.claudeAuth = { ...this.claudeAuth, ...d, loaded: true };
      } catch (e) {
        this.claudeAuth = {
          ...this.claudeAuth,
          loaded: true,
          logged_in: false,
          reason: `network: ${e && e.message ? e.message : 'unknown'}`,
        };
      }
    },
    openClaudeAuthModal() {
      this.claudeAuthModal.open = true;
      this.claudeAuthModal.copyToast = null;
      // Refresh status once before polling kicks in.
      this.loadClaudeAuthStatus();
      this.startClaudeAuthPoll();
    },
    closeClaudeAuthModal() {
      this.claudeAuthModal.open = false;
      this.stopClaudeAuthPoll();
    },
    startClaudeAuthPoll() {
      if (this.claudeAuthModal.pollHandle) return;
      this.claudeAuthModal.polling = true;
      this.claudeAuthModal.pollHandle = setInterval(async () => {
        await this.loadClaudeAuthStatus();
        if (this.claudeAuth.logged_in) {
          this.stopClaudeAuthPoll();
          this.closeClaudeAuthModal();
          this.toast(this.t("claude_auth.connect_success"), "success");
        }
      }, 3000);
    },
    stopClaudeAuthPoll() {
      if (this.claudeAuthModal.pollHandle) {
        clearInterval(this.claudeAuthModal.pollHandle);
        this.claudeAuthModal.pollHandle = null;
      }
      this.claudeAuthModal.polling = false;
    },
    async copyClaudeAuthCmd(which) {
      // which = "install" | "login"
      const cmd = which === "install"
        ? this.t("claude_auth.cli_install_cmd")
        : "claude login";
      try {
        await navigator.clipboard.writeText(cmd);
        this.claudeAuthModal.copyToast = which;
        setTimeout(() => {
          if (this.claudeAuthModal.copyToast === which) this.claudeAuthModal.copyToast = null;
        }, 1500);
      } catch (e) {
        this.toast("clipboard write failed", "error");
      }
    },
    async disconnectClaudeAuth() {
      const ok = await this.confirm({
        title: this.t("claude_auth.disconnect_confirm_title"),
        body:  this.t("claude_auth.disconnect_confirm_body"),
        confirmText: this.t("claude_auth.disconnect_btn"),
        kind: "warning",
      });
      if (!ok) return;
      try {
        const r = await fetch("/api/settings/claude-auth/disconnect",
                              { method: "POST", headers: this.hdr() });
        if (!r.ok) {
          this.toast(this.lang === "zh" ? "断开失败" : "Disconnect failed", "error");
          return;
        }
        const d = await r.json();
        this.toast(this.t("claude_auth.disconnect_done") + " " + (d.backup_path || ""), "success", 4000);
        await this.loadClaudeAuthStatus();
      } catch (e) {
        this.toast(e.message || "error", "error");
      }
    },
    async reauthClaude() {
      // Reuse Connect modal — `claude login` overwrites existing creds.
      this.openClaudeAuthModal();
    },
    claudeAuthExpiresHuman() {
      if (!this.claudeAuth.expires_at) return "—";
      const d = new Date(this.claudeAuth.expires_at);
      return d.toLocaleDateString(this.lang === "zh" ? "zh-CN" : "en-US",
              { year: "numeric", month: "short", day: "numeric" });
    },
    // ===== Muse main-chat empty-state opener + muse grid =====
    // museOpener() picks a state-aware first line for Muse to render as
    // a UI-only "Muse said" bubble at the top of a fresh chat. It's NOT a
    // real LLM call — it's a fixed template per archive state, chosen from
    // the new contextInfo fields (claude_md_meaningfully_filled + subdir
    // counts). Hidden the moment the user starts typing so it doesn't
    // distract from their own first message.
    museOpener() {
      const ci = this.contextInfo;
      if (!ci || !ci._fetched) return "";
      if (!ci.has_any_provider) return "";  // provider-warn card handles this state
      // Count filled subdirs (excludes "archives" which is purely cold storage)
      const subs = ci.subdir_present || {};
      const subdir_count = Object.entries(subs).filter(
        ([k, v]) => v && k !== "archives"
      ).length;
      // Files at archive root counted by archive_empty toggling false even
      // when no subdir has content (root-level docs)
      const has_root_files = !ci.archive_empty;
      const profile_filled = !!ci.claude_md_meaningfully_filled;

      // State 4: archive rich — ≥4 subdirs with content
      if (subdir_count >= 4) return this.t("muse_opener.rich");
      // State 3: some files — at least 1 subdir or root-level docs
      if (subdir_count >= 1 || (has_root_files && profile_filled)) {
        // Count total non-readme files across all subdirs (rough est.)
        // archive_empty was the only sub-count we get; use subdir count as proxy
        const n = subdir_count > 0 ? subdir_count : 1;
        return this.t("muse_opener.some_files", { n });
      }
      // State 2: only profile filled, no archive files
      if (profile_filled) return this.t("muse_opener.profile_only");
      // State 1: nothing filled — even if CLAUDE.md *file* exists (template)
      return this.t("muse_opener.empty");
    },
    museOpenerAction() {
      // State 1 + 2 get an action button to open / fill CLAUDE.md inline.
      const ci = this.contextInfo;
      if (!ci || !ci._fetched || !ci.has_any_provider) return null;
      const subs = ci.subdir_present || {};
      const subdir_count = Object.values(subs).filter(Boolean).length;
      if (subdir_count >= 1 || !ci.archive_empty) return null;  // states 3/4
      return {
        label: this.t("muse_opener.action_open_profile"),
        // Reuse the existing /organize workflow — it walks CLAUDE.md gaps too
        handler: () => this.startOrganize(),
      };
    },
    // Click any muse in the grid → switch mascot + prefill the chat input.
    // CRITICAL: prefill uses `m.prompt` (user-voice, "我想聊聊...") NOT
    // `m.invite` (Muse-voice, "聊聊你..."). The card preview displays
    // invite so it reads naturally as Muse asking; the input box gets
    // the first-person rewrite so the grammar / perspective is correct
    // when the user hits Enter. Falls back to invite if a muse hasn't
    // been given a prompt yet (forward-compat safety, all 9 currently
    // have one).
    pickMascotAndAsk(idx) {
      const m = this.MASCOTS[idx];
      if (!m) return;
      this.mascotIdx = idx;
      try { localStorage.setItem("muselab_mascot_idx", String(idx)); } catch {}
      this.mascotGreet = true;
      setTimeout(() => { this.mascotGreet = false; }, 900);
      const src = m.prompt || m.invite || null;
      const text = src ? (src[this.lang] || src.zh) : "";
      this.useSuggestedPrompt(text);
    },

    claudeAuthPlanLabel() {
      const s = this.claudeAuth.subscription_type;
      if (!s) return "—";
      if (s === "max") return "Max";
      if (s === "pro") return "Pro";
      if (s === "free") return "Free";
      return s;
    },
    async loadCostDashboard(force = false) {
      if (this.cost.loading) return;
      if (this.cost.data && !force) return;
      this.cost.loading = true;
      try {
        // Browser timezone offset is -getTimezoneOffset (JS reports east as
        // negative, server expects east-positive minutes).
        const tz = -new Date().getTimezoneOffset();
        // Window is fixed at 30 days (the adjustable dropdown was removed
        // 2026-05-22). Backend keeps the `days` param so older clients
        // still work; we just always pass 30 now.
        const r = await fetch(
          `/api/chat/cost-dashboard?days=30&tz_offset_minutes=${tz}`,
          { headers: this.hdr() });
        if (!r.ok) {
          this.cost.data = null;
          this.toast(this.lang === "zh" ? "用量看板加载失败" : "Usage dashboard failed", "error");
          return;
        }
        this.cost.data = await r.json();
      } catch (e) {
        this.cost.data = null;
      } finally {
        this.cost.loading = false;
      }
    },
    // Sum of all token classes for a usage bucket. Used everywhere a
    // single comparable number is needed (KPI cards, bar chart, per-model
    // ranking) so different vendors aggregate consistently — cost can't
    // be that number because third-party vendors report $0.
    totalTokens(bucket) {
      if (!bucket) return 0;
      // Respect the dashboard's per-category filter chips. If the user
      // clicks "cache" off, all sums (KPI cards / day bars / vendor + model
      // rows) recompute without cache tokens. cache_read + cache_creation
      // share the "cache" toggle — they're both Anthropic prompt-caching
      // accounting and the user thinks of them as one bucket.
      const f = (this.cost && this.cost.filters)
        || { input: true, output: true, cache: true };
      let total = 0;
      if (f.input)  total += bucket.input_tokens || 0;
      if (f.output) total += bucket.output_tokens || 0;
      if (f.cache) {
        total += bucket.cache_read_tokens || 0;
        total += bucket.cache_creation_tokens || 0;
      }
      return total;
    },
    // (fmtTokens defined below — reused for both the header badge and
    // the cost dashboard.)
    // Bar-chart helpers — return percentages capped at 100 so a single
    // outlier day doesn't push every other bar to invisible. Max baseline
    // is the busiest day in the window (or 1 to avoid divide-by-zero
    // when everything is empty). Argument is the total-token count for
    // that bucket (cost would diverge across vendors).
    costBarHeight(tokens) {
      const max = Math.max(1, ...(this.cost.data?.by_day || []).map(d => this.totalTokens(d)));
      const pct = Math.min(100, (tokens / max) * 100);
      return Math.max(pct, tokens > 0 ? 3 : 0);   // tiny non-zero bar = 3% so user sees it
    },
    costModelPct(tokens) {
      const max = Math.max(1, ...(this.cost.data?.by_model || []).map(m => this.totalTokens(m)));
      return Math.min(100, (tokens / max) * 100);
    },
    // costVendorPct removed 2026-05-22 — by_vendor rollup section was
    // deleted from the dashboard. Backend still emits by_vendor in the
    // response (other consumers / future re-use), we just don't render it.
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
      const remote = d.transport === "remote";
      // Build the request body per transport. Remote → {type:http, url,
      // headers}; local → {command, args, env}. Validation differs too.
      let body;
      if (remote) {
        const url = (d.url || "").trim();
        if (!name || !url) {
          this.toast(this.t("set.mcp.name_url_required"), "warn", 2500);
          return;
        }
        const headers = {};
        const auth = (d.authHeader || "").trim();
        if (auth) headers["Authorization"] = auth;
        body = { name, type: "http", url, headers, disabled: false };
      } else {
        const command = (d.command || "").trim();
        if (!name || !command) {
          this.toast(this.t("set.mcp.name_command_required"), "warn", 2500);
          return;
        }
        const args = (d.argsStr || "").trim().split(/\s+/).filter(Boolean);
        body = { name, command, args, env: {}, disabled: false };
      }
      const r = await fetch(`/api/settings/mcp/${encodeURIComponent(name)}`, {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (r.ok) {
        this.toast(this.t("set.mcp.added"), "success", 1500);
        this.settings.mcpDraft = { show: false, transport: "stdio", name: "", command: "", argsStr: "", url: "", authHeader: "" };
        this.refreshMcpList();
      } else {
        this.toast(this.t("set.mcp.save_failed"), "error", 3000);
      }
    },
    // Provider key self-test — hits the vendor's anthropic-compatible endpoint
    // with the configured key and reports back. Useful when user gets 401 and
    // doesn't want to paste keys to debug.
    async probeProvider(envKey, probeModel) {
      // probeModel is the first model in this provider's catalog (e.g. "qwen3-max"
      // for Qwen domestic, "qwen-intl:qwen3-max" for Qwen international). Using it
      // as the result key ensures two providers that share one env key (DASHSCOPE_API_KEY)
      // get independent probe results.
      if (!probeModel) return;
      this.settings.probeResults[probeModel] = { ok: null, text: this.t("set.probe_running") };
      try {
        const r = await fetch(`/api/chat/probe/${encodeURIComponent(probeModel)}`,
                                 { headers: this.hdr() });
        const d = await r.json();
        if (d.ok) {
          this.settings.probeResults[probeModel] = {
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
          this.settings.probeResults[probeModel] = {
            ok: false,
            text: `${status}: ${detail || "—"}${hint}`,
          };
        }
      } catch (e) {
        this.settings.probeResults[probeModel] = {
          ok: false, text: this.t("set.probe_failed") + ": " + e.message,
        };
      }
    },

    // Toggle a provider's visibility in the model picker. probeModel uniquely
    // identifies each provider (e.g. "qwen3.6-plus" vs "qwen-intl:qwen3.6-plus").
    // Save a single provider's key without going through the bottom-bar
    // Save (which writes every draft at once). Lets the Settings UI offer
    // an inline "保存" button next to the input — same one-key edit
    // gesture the user expects from any modern settings page. The bottom
    // bar still works for batch saves.
    async saveProviderKey(envKey) {
      const v = (this.settings.draftKeys[envKey] || "").trim();
      if (!v) {
        this.toast(this.lang === "zh" ? "请先输入 key" : "Enter a key first", "warn", 2000);
        return false;
      }
      try {
        const r = await fetch("/api/settings", {
          method: "PUT",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ provider_keys: { [envKey]: v } }),
        });
        if (!r.ok) throw new Error("status " + r.status);
        // Optimistic local refresh: mark this provider configured + clear
        // the draft so the row collapses back to "已配置" view next render.
        const p = this.settings.providers.find(x => x.env_key === envKey);
        if (p) p.configured = true;
        this.settings.draftKeys[envKey] = "";
        this.toast(this.lang === "zh" ? "✓ 已保存" : "✓ Saved", "success", 1800);
        // Refresh providers + model list so any newly-enabled model
        // appears in the chat dropdown immediately.
        await this._fetchModels();
        return true;
      } catch (e) {
        this.toast(this.lang === "zh" ? "保存失败：" + e.message : "Save failed: " + e.message, "error", 4000);
        return false;
      }
    },

    async toggleProvider(providerId, disabled) {
      // Visibility toggle now keys off the provider's STABLE id (not its
      // first model id, which changes when the user edits the model list).
      // Backend honours both forms so older toggles survive the migration.
      const r = await fetch("/api/settings", {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ provider_disabled: { [providerId]: disabled } }),
      });
      const d = await r.json();
      if (d.ok) {
        // Update local state so the toggle reflects immediately.
        const p = this.settings.providers.find(x => x.id === providerId);
        if (p) p.disabled = disabled;
        // Refresh the model list so the picker drops the hidden provider.
        await this._fetchModels();
      }
    },

    // ===== Provider editor (full endpoint / prefix / models / key) =====
    // Build a closed editor draft seeded from a provider's committed values.
    _draftFromProvider(p) {
      return {
        open: false,
        base_url: p.base_url || "",
        prefix: p.prefix || "",
        models: (p.models || []).join("\n"),
        api_key: "",
      };
    },

    // Open or close the inline editor for one provider. Opening seeds the
    // draft from the committed values; closing just drops the open flag so
    // the next open re-seeds (cancel = discard).
    toggleProviderEditor(p) {
      const drafts = this.settings.providerDrafts;
      if (drafts[p.id] && drafts[p.id].open) {
        drafts[p.id].open = false;
        return;
      }
      drafts[p.id] = { ...this._draftFromProvider(p), open: true };
    },

    _parseModels(text) {
      // Models entered one-per-line (or comma-separated); trim + drop blanks.
      return (text || "").split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
    },

    // ===== Anthropic / Claude model list (the one editable knob on the
    // special-auth Claude row; auth + endpoint stay fixed) =====
    toggleAnthropicModels(p) {
      const drafts = this.settings.providerDrafts;
      if (drafts[p.id] && drafts[p.id].open) { drafts[p.id].open = false; return; }
      drafts[p.id] = { open: true, models: (p.models || []).join("\n") };
    },

    async saveAnthropicModels(p) {
      const dr = this.settings.providerDrafts[p.id];
      if (!dr) return;
      const models = this._parseModels(dr.models);
      try {
        const r = await fetch("/api/settings/providers/anthropic-models", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ models }),
        });
        if (!r.ok) {
          let msg = "status " + r.status;
          try { const e = await r.json(); if (e.detail) msg = e.detail; } catch (_) {}
          throw new Error(msg);
        }
        this.toast(this.lang === "zh" ? "✓ 已保存" : "✓ Saved", "success", 1800);
        if (this.settings.providerDrafts[p.id]) this.settings.providerDrafts[p.id].open = false;
        await this._reloadProviders();
        await this._fetchModels();
      } catch (e) {
        this.toast((this.lang === "zh" ? "保存失败：" : "Save failed: ") + e.message, "error", 4000);
      }
    },

    async _submitProvider(body, pid) {
      try {
        const r = await fetch("/api/settings/providers", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!r.ok) {
          let msg = "status " + r.status;
          try { const e = await r.json(); if (e.detail) msg = e.detail; } catch (_) {}
          throw new Error(msg);
        }
        this.toast(this.lang === "zh" ? "✓ 已保存" : "✓ Saved", "success", 1800);
        if (pid && this.settings.providerDrafts[pid]) {
          this.settings.providerDrafts[pid].open = false;
        }
        await this._reloadProviders();
        await this._fetchModels();
        return true;
      } catch (e) {
        this.toast((this.lang === "zh" ? "保存失败：" : "Save failed: ") + e.message, "error", 4000);
        return false;
      }
    },

    async saveProvider(p) {
      const dr = this.settings.providerDrafts[p.id];
      if (!dr) return;
      const body = {
        id: p.id,
        base_url: (dr.base_url || "").trim(),
        prefix: (dr.prefix || "").trim(),
        display: p.display,
        env_key: p.env_key,
        models: this._parseModels(dr.models),
      };
      if ((dr.api_key || "").trim()) body.api_key = dr.api_key.trim();
      await this._submitProvider(body, p.id);
    },

    async addProviderFromDraft() {
      const n = this.settings.providerNew;
      const body = {
        id: null,
        base_url: (n.base_url || "").trim(),
        prefix: (n.prefix || "").trim(),
        models: this._parseModels(n.models),
      };
      if ((n.api_key || "").trim()) body.api_key = n.api_key.trim();
      const ok = await this._submitProvider(body, null);
      if (ok) {
        this.settings.providerNew = { show: false, base_url: "", prefix: "", models: "", api_key: "" };
      }
    },

    async restoreProvider(p) {
      try {
        const r = await fetch("/api/settings/providers/restore", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ id: p.id }),
        });
        if (!r.ok) throw new Error("status " + r.status);
        this.toast(this.lang === "zh" ? "✓ 已恢复默认" : "✓ Restored to default", "success", 1800);
        if (this.settings.providerDrafts[p.id]) this.settings.providerDrafts[p.id].open = false;
        await this._reloadProviders();
        await this._fetchModels();
      } catch (e) {
        this.toast((this.lang === "zh" ? "恢复失败：" : "Restore failed: ") + e.message, "error", 4000);
      }
    },

    async deleteProvider(p) {
      const zh = this.lang === "zh";
      const msg = zh
        ? (p.is_builtin
            ? `隐藏内置 provider「${p.display}」？随时可恢复默认。`
            : `删除 provider「${p.display}」？`)
        : (p.is_builtin
            ? `Hide built-in provider "${p.display}"? You can restore it anytime.`
            : `Delete provider "${p.display}"?`);
      // Use the custom danger modal so this matches every other destructive
      // confirm in the app (native confirm() here was the lone inconsistency).
      const ok = await this.confirm({
        title: zh ? (p.is_builtin ? "隐藏 provider" : "删除 provider")
                  : (p.is_builtin ? "Hide provider" : "Delete provider"),
        body: msg,
        danger: true,
        okText: zh ? (p.is_builtin ? "隐藏" : "删除") : (p.is_builtin ? "Hide" : "Delete"),
      });
      if (!ok) return;
      try {
        const r = await fetch("/api/settings/providers/delete", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ id: p.id }),
        });
        if (!r.ok) throw new Error("status " + r.status);
        this.toast(this.lang === "zh" ? "✓ 已删除" : "✓ Deleted", "success", 1800);
        await this._reloadProviders();
        await this._fetchModels();
      } catch (e) {
        this.toast((this.lang === "zh" ? "删除失败：" : "Delete failed: ") + e.message, "error", 4000);
      }
    },

    // Re-fetch the provider list (after a CRUD op) without wiping the rest of
    // the settings modal state. Adds draftKeys slots for any new env_keys.
    async _reloadProviders() {
      const r = await fetch("/api/settings", { headers: this.hdr() });
      if (!r.ok) return;
      const d = await r.json();
      this.settings.providers = d.providers;
      for (const p of d.providers) {
        if (!(p.env_key in this.settings.draftKeys)) this.settings.draftKeys[p.env_key] = "";
        // Seed a draft for any provider that doesn't have one yet (e.g. a
        // newly added custom provider) so its editor x-model bindings stay
        // resolvable. Don't clobber a draft that's mid-edit.
        if (!this.settings.providerDrafts[p.id]) {
          this.settings.providerDrafts[p.id] = this._draftFromProvider(p);
        }
      }
    },

    // Refresh the available model list from backend — called when provider
    // visibility changes so the model picker dropdown stays in sync.
    async _fetchModels() {
      try {
        const r = await fetch("/api/chat/providers", { headers: this.hdr() });
        if (r.ok) {
          this.availableModels = (await r.json()).models || [];
          this._ensureValidModel();
        }
      } catch (e) {
        // Silently skip — the dropdown can be refreshed next time it opens.
      }
    },
    // Ensure `this.model` references a model whose provider is currently
    // configured. Without this, a fresh install (.env's MUSELAB_MODEL
    // default = claude-sonnet-4-6) plus a user who only configured e.g.
    // DEEPSEEK_API_KEY would send chats as Claude and hit 401 on every
    // turn — the dropdown shows the wrong model on first load.
    _ensureValidModel() {
      // No providers configured at all → leave model empty so the dropdown
      // shows the placeholder and the no-provider onboarding card surfaces.
      if (!this.availableModels || this.availableModels.length === 0) {
        if (this.model) {
          this.model = "";
          this.savePrefs();
        }
        return;
      }
      // Current model is in the available list → nothing to do.
      if (this.availableModels.find(m => m.model === this.model)) return;
      // Current model isn't available → fall back to the first one. Toast
      // only when the user previously had a real selection (avoids a
      // confusing first-load toast on an empty-default install).
      const oldModel = this.model;
      this.model = this.availableModels[0].model;
      this.savePrefs();
      if (oldModel) {
        const newLabel = this.availableModels[0].label || this.model;
        this.toast(
          this.lang === "zh"
            ? `已切到 ${newLabel}（${oldModel} 对应的 provider 未配置）`
            : `Switched to ${newLabel} (${oldModel}'s provider not configured)`,
          "info", 3500);
      }
    },

    async installMcpPreset(ex) {
      // Presets may be either stdio (command/args) or remote (url/headers).
      const body = (ex.type === "http" || ex.type === "sse" || ex.url)
        ? { name: ex.name, type: ex.type || "http", url: ex.url,
            headers: ex.headers || {}, disabled: false }
        : { name: ex.name, command: ex.command, args: ex.args || [],
            env: ex.env || {}, disabled: false };
      const r = await fetch(`/api/settings/mcp/${encodeURIComponent(ex.name)}`, {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (r.ok) {
        this.toast(this.t("set.mcp.installed"), "success", 1500);
        this.refreshMcpList();
      } else {
        this.toast(this.t("set.mcp.save_failed"), "error", 3000);
      }
    },

    // Delete any session from the picker dropdown's inline × button.
    // Drop per-sid bookkeeping maps so they don't grow unbounded as sessions
    // come and go. These keep a flag per session id (one-shot warn dedupe,
    // in-flight prefetch guard); without this, deleting/closing sessions over
    // a long-lived tab slowly leaks entries. Call wherever a session is
    // permanently removed (delete) or its tab state is torn down.
    _clearSessionWarnFlags(sid) {
      if (this._budgetWarned) delete this._budgetWarned[sid];
      if (this._ctxWarned) delete this._ctxWarned[sid];
      if (this._autoCompacted) delete this._autoCompacted[sid];
      if (this._prefetching) delete this._prefetching[sid];
    },
    async deleteSessionById(sid) {
      const s = this.sessions.find(x => x.id === sid);
      const _dName = s?.name || sid.slice(0, 8);
      const ok = await this.confirm({
        title: this.t("modal.delete_session_title"),
        body: this.t("modal.delete_session_body", { name: _dName }),
        danger: true,
        okText: this.t("modal.delete_session_ok"),
      });
      if (!ok) return;
      // Tear down per-tab cached state before we forget about the session.
      const dyingTab = this.tabState[sid];
      if (dyingTab) {
        if (dyingTab.es) { try { dyingTab.es.close(); } catch {} }
        if (dyingTab._streamTimer) clearInterval(dyingTab._streamTimer);
        delete this.tabState[sid];
      }
      this._clearSessionWarnFlags(sid);
      try {
        await fetch(`/api/chat/sessions/${sid}`, { method: "DELETE", headers: this.hdr() });
      } catch (e) {
        this.errToast("delete", String((e && e.message) || e));
      }
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

    async restartService() {
      if (this.settings.restarting) return;
      // Confirm before restarting — a stray tap on a phone would otherwise
      // drop every active chat session for ~10s with no recourse. Native
      // confirm() is intentional: this is a destructive action and the
      // OS-level dialog forces deliberate user attention better than an
      // in-app modal that may blend into the settings drawer chrome.
      const msg = this.lang === "zh"
        ? "重启 muselab 服务？所有正在跑的对话会中断约 10 秒。"
        : "Restart muselab? All running chats will pause for ~10 seconds.";
      if (!window.confirm(msg)) return;
      this.settings.restarting = true;
      try {
        // Fire the restart request — the server responds before it restarts
        await fetch("/api/settings/restart", {
          method: "POST", headers: this.hdr(),
        });
      } catch (_) {
        // Expected: connection may drop immediately if the process exits fast
      }
      // Poll /api/health every 1.5 s until the server is back up, then
      // do a soft chat refresh (no full page reload — preserves open tabs).
      const pollStart = Date.now();
      const MAX_WAIT = 30_000;
      const poll = async () => {
        if (!this.settings.restarting) return;
        if (Date.now() - pollStart > MAX_WAIT) {
          this.settings.restarting = false;
          this.toast(this.lang === "zh" ? "服务重启超时，请手动刷新" : "Restart timed out — reload manually", "error", 5000);
          return;
        }
        try {
          const r = await fetch("/api/health", { cache: "no-store" });
          if (r.ok) {
            this.settings.restarting = false;
            this.toast(this.lang === "zh" ? "✓ 服务已重启" : "✓ Service restarted", "success", 3000);
            await this.refreshChat();
            await this.loadVersions();
            return;
          }
        } catch (_) { /* still restarting */ }
        setTimeout(poll, 1500);
      };
      // Give the process a moment to die before we start polling
      setTimeout(poll, 2000);
    },

    async saveSettings() {
      const body = {
        default_model: this.settings.draftDefaults.model,
        default_permission: this.settings.draftDefaults.permission,
      };
      // Send every typed provider key through the generic provider_keys
      // map. Backend whitelists against PROVIDER_KEYS (derived from
      // endpoints.CATALOG), so adding a new vendor only needs the FE to
      // render an input row — no field-name plumbing change here.
      // Old k2f mapping (anthropic_api_key / deepseek_api_key / ...) is
      // still accepted by the backend for backwards compat, but we don't
      // emit it anymore — drift between FE k2f and backend Pydantic was
      // the exact bug that hid Kimi / Qwen / MiMo from Settings UI.
      const providerKeys = {};
      for (const [envK, v] of Object.entries(this.settings.draftKeys || {})) {
        if (v && v.trim()) providerKeys[envK] = v.trim();
      }
      if (Object.keys(providerKeys).length > 0) body.provider_keys = providerKeys;
      const r = await fetch("/api/settings", {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (r.ok) {
        const d = await r.json();
        this.settings.show = false;
        // Prefer `updated_count` (user-facing tally) over `updated.length`
        // (raw env-key count). Backend dedupes the MUSELAB_MODEL +
        // MUSELAB_DEFAULT_MODEL pair so changing the model dropdown reads
        // as "1 setting" not "2". Fallback to .length keeps it working
        // against an older backend that doesn't return the new field.
        // n=0 means the user hit Save without changing anything — show a
        // different toast so they don't think a change was lost.
        const n = (typeof d.updated_count === "number")
          ? d.updated_count
          : (d.updated || []).length;
        let msg;
        if (n === 0) {
          msg = this.lang === "zh" ? "无改动" : "No changes";
        } else {
          msg = this.lang === "zh"
            ? `已保存 ${n} 项设置`
            : `Saved ${n} setting${n === 1 ? "" : "s"}`;
        }
        this.toast(msg, n === 0 ? "info" : "success");
        // Settings "default model" 改了之后，下一个新建会话应该用新值。
        // 之前只写了服务端 env，但前端的 this.model 还是 localStorage 里的
        // 老值 → 用户看不到任何变化。同步前端 + localStorage 让"我改了它生效"
        // 的预期成立。已建会话有自己 locked model，不受影响。
        const newDefaultModel = this.settings.draftDefaults.model;
        if (newDefaultModel && newDefaultModel !== this.model) {
          this.model = newDefaultModel;
          this.savePrefs();
        }
        const newDefaultPerm = this.settings.draftDefaults.permission;
        if (newDefaultPerm && newDefaultPerm !== this.permission) {
          this.permission = newDefaultPerm;
          this.savePrefs();
        }
        // 刷新可用 provider 列表
        const r2 = await fetch("/api/chat/providers", { headers: this.hdr() });
        if (r2.ok) {
          this.availableModels = (await r2.json()).models || [];
          // 关键：先校正 this.model 再 rebind。否则 default_model 仍是
          // 出厂值 claude-sonnet-4-6（GET /api/settings 在 MUSELAB_MODEL 未设
          // 时回退到它），而用户只配了 DeepSeek、没有 Anthropic 鉴权 → claude
          // 根本不在 availableModels 里 → 下一条消息按 claude 发送、命中
          // chat.py 的 auth 守卫报错。saveProviderKey（逐行保存）走 _fetchModels
          // 已含此校正，批量保存这条路径之前漏了。
          this._ensureValidModel();
          this._rebindModelSelect();
        }
        // 也刷新 contextInfo — has_any_provider 变了，否则 "no provider" 卡片不消失
        this.fetchContextInfo();
      } else {
        const prefix = this.lang === "zh" ? "保存失败：" : "Save failed: ";
        this.toast(prefix + (await r.text()), "error");
      }
    },
    async deleteSession() {
      const cur = this.sessions.find(x => x.id === this.currentId);
      if (!cur) return;
      const ok = await this.confirm({
        title: this.t("modal.delete_session_title"),
        body: this.t("modal.delete_session_body", { name: cur.name }),
        danger: true,
        okText: this.t("modal.delete_session_ok"),
      });
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
    // In-place removal of a node (and its descendants, if a dir) from the
    // visible flat-list. Avoids the full reloadTree() that delete used to
    // trigger — which clears the entire childCache and re-fetches every
    // expanded directory from disk, flickering the whole tree on every
    // single-file delete. Now: one splice, one cache key drop, done.
    //
    // Used by doDelete + drag-to-trash + (future) any other "this exact
    // path went away" event. Restore still uses _refreshParentInTree
    // because we don't have the new node's metadata client-side.
    _removeNodeFromTree(path) {
      if (!path) return;
      const idx = this.visible.findIndex(n => n.path === path);
      if (idx < 0) return;
      const node = this.visible[idx];
      // If it's a directory, swallow all rendered descendants too
      // (depth > node.depth runs contiguously below the parent in the
      // flat list, same shape collapse() relies on).
      let end = idx + 1;
      if (node.is_dir) {
        while (end < this.visible.length
                && this.visible[end].depth > node.depth) end++;
      }
      this.visible.splice(idx, end - idx);
      // Clean up expanded set: the node itself + any of its descendants
      // that were expanded. New Set() reassignment forces Alpine to
      // notice (Set mutation in place doesn't trigger reactivity).
      if (this.expanded.has(path)) this.expanded.delete(path);
      for (const p of Array.from(this.expanded)) {
        if (p.startsWith(path + "/")) this.expanded.delete(p);
      }
      this.expanded = new Set(this.expanded);
      // Invalidate the parent dir's cache so a manual collapse+expand
      // (or a future restore-into-this-parent) refetches truth from
      // disk. Both showHidden=true|false variants since we don't know
      // which one the user is currently viewing.
      const parent = path.split("/").slice(0, -1).join("/");
      delete this.childCache[`${parent}:true`];
      delete this.childCache[`${parent}:false`];
    },
    // After restore, the parent dir's contents include a new node we
    // don't have client-side. Cheapest correct behavior: invalidate
    // parent cache + refetch + splice in place. If the parent isn't
    // currently expanded (or restored to root and root isn't loaded),
    // we fall back to reloadTree — rare in practice but covers the
    // edge case without bespoke logic.
    async _refreshParentInTree(restoredPath) {
      if (!restoredPath) { this.reloadTree(); return; }
      const parent = restoredPath.split("/").slice(0, -1).join("/");
      delete this.childCache[`${parent}:true`];
      delete this.childCache[`${parent}:false`];
      if (!parent) {
        // Root-level restore: re-merge root children, preserving expanded subtrees.
        this.reloadTree();
        return;
      }
      if (!this.expanded.has(parent)) {
        // Parent is collapsed → nothing visible changes; expanding later
        // will refetch fresh.
        return;
      }
      const parentIdx = this.visible.findIndex(n => n.path === parent);
      if (parentIdx < 0) { this.reloadTree(); return; }
      const parentNode = this.visible[parentIdx];
      // Snapshot inner-expanded subtrees so we can restore them after
      // re-rendering the parent's children.
      const innerExpanded = Array.from(this.expanded)
        .filter(p => p !== parent && p.startsWith(parent + "/"));
      // Splice out parent's current rendered subtree.
      let end = parentIdx + 1;
      while (end < this.visible.length
              && this.visible[end].depth > parentNode.depth) end++;
      this.visible.splice(parentIdx + 1, end - parentIdx - 1);
      for (const p of innerExpanded) this.expanded.delete(p);
      const children = await this.fetchChildren(parent);
      const items = children.map(c => ({ ...c, depth: parentNode.depth + 1 }));
      this.visible.splice(parentIdx + 1, 0, ...items);
      // Re-expand previously-open inner subtrees in shortest-path-first
      // order so each expand() can find its parent already rendered.
      for (const p of innerExpanded.sort((a, b) => a.length - b.length)) {
        const node = this.visible.find(n => n.path === p);
        if (node && node.is_dir) await this.expand(node);
      }
      this.expanded = new Set(this.expanded);
    },
    async fetchChildren(path) {
      const cacheKey = `${path}:${this.showHidden}`;
      if (this.childCache[cacheKey]) return this.childCache[cacheKey];
      const url = "/api/files/list?path=" + encodeURIComponent(path)
        + (this.showHidden ? "&show_hidden=true" : "");
      const r = await fetch(url, { headers: this.hdr() });
      if (!r.ok) return [];
      const d = await r.json();
      this.childCache[cacheKey] = d.entries;
      // LRU: keep at most 100 directory entries to prevent unbounded growth
      const keys = Object.keys(this.childCache);
      if (keys.length > 100) {
        // Delete oldest 20 entries (insertion order preserved in modern JS)
        keys.slice(0, 20).forEach(k => delete this.childCache[k]);
      }
      if (d.truncated) {
        this.toast(this.t("toast.dir_truncated", { path: path || "", n: d.entries.length }), "warn", 3500);
      }
      return d.entries;
    },
    toggleHidden() {
      this.showHidden = !this.showHidden;
      this.savePrefs();
      this.reloadTree();
      this.toast(this.t(this.showHidden ? "toast.hidden_shown" : "toast.hidden_hidden"), "info", 1500);
    },
    async onNodeClick(n) {
      if (n.is_dir) {
        if (this.expanded.has(n.path)) this.collapse(n);
        else await this.expand(n);
        this.savePrefs();
      } else {
        // Single-click ⇒ ephemeral preview tab (VSCode behavior).
        await this.openFile(n, { preview: true });
      }
    },
    // Double-click a tree file ⇒ pin it as a permanent tab. The two preceding
    // single-click events already opened it in the preview slot; this just
    // promotes that slot to permanent (openFile's `existing && !asPreview`
    // branch clears the preview flag).
    async onNodeDblClick(n) {
      if (n && !n.is_dir) await this.openFile(n, { preview: false });
    },
    // Pin a preview (italic) tab so it stays open: double-clicking the tab
    // itself, or starting to edit its file, promotes it to permanent.
    pinTab(path) {
      const t = this.tabs.find(x => x.path === path);
      if (t && t.preview) {
        t.preview = false;
        this.savePrefs();
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
        case "copyAsBak":
          // Right-click "Copy as .bak" — paste-target defaults to the
          // source's own parent dir, matching user expectation of an
          // in-place duplicate. Cross-dir duplication is the Ctrl+C / V
          // path which lets the user pick the target via tree selection.
          if (!n.is_dir) await this.doCopyAsBak(n);
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
      const zh = this.lang === "zh";
      const name = await this.prompt({
        title: zh ? "新建文件" : "New file",
        body: (zh ? "在 " : "Inside ") + `/${dirNode.path}`,
        value: "new.md",
      });
      if (!name) return;
      const path = dirNode.path ? `${dirNode.path}/${name}` : name;
      let r;
      try {
        r = await fetch("/api/files/write", {
          method: "PUT",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ path, content: "" }),
        });
      } catch (e) { this.errToast("create", String((e && e.message) || e)); return; }
      if (r.ok) {
        delete this.childCache[dirNode.path];
        this.reloadTree();
        this.toast(this.t("toast.created_name", { name }), "success");
        // 自动打开编辑
        await this.openFile({ path, name });
        this.editing = true;
      } else this.errToast("create", await r.text());
    },
    async doNewDir(dirNode) {
      const zh = this.lang === "zh";
      const name = await this.prompt({
        title: zh ? "新建子目录" : "New subdirectory",
        body: (zh ? "在 " : "Inside ") + `/${dirNode.path}`,
        value: "",
      });
      if (!name) return;
      const path = dirNode.path ? `${dirNode.path}/${name}` : name;
      let r;
      try {
        r = await fetch("/api/files/mkdir", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ path }),
        });
      } catch (e) { this.errToast("create", String((e && e.message) || e)); return; }
      if (r.ok) {
        delete this.childCache[dirNode.path];
        this.reloadTree();
        this.toast(this.t("toast.created_dir", { name }), "success");
      } else this.errToast("generic", await r.text());
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
      const zh = this.lang === "zh";
      const newName = await this.prompt({
        title: zh ? "重命名" : "Rename",
        body: (zh ? "当前路径:" : "Current path: ") + n.path,
        value: n.name,
      });
      if (!newName || newName === n.name) return;
      const parent = n.path.split("/").slice(0, -1).join("/");
      const newPath = parent ? `${parent}/${newName}` : newName;
      let r;
      try {
        r = await fetch("/api/files/rename", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ src: n.path, dst: newPath }),
        });
      } catch (e) { this.errToast("rename", String((e && e.message) || e)); return; }
      if (r.ok) {
        if (this.selected === n.path) this.selected = newPath;
        // Old path no longer exists; drop its cached body so a future file
        // created at the same path can't serve this one's stale content.
        this._previewCacheDel(n.path);
        delete this.childCache[parent];
        this.reloadTree();
        this.toast(this.t("toast.renamed"), "success");
      } else this.errToast("rename", await r.text());
    },
    // Look up a tree node by path in the current flat-rendered tree.
    // Returns the node object or null. Used by the Ctrl+C keyboard
    // handler (which only has `this.selected` as a path string).
    _findTreeNode(path) {
      if (!path) return null;
      return this.visible.find(n => n.path === path) || null;
    },
    // Server-side derives the .bak[.N] name; we just refresh the parent
    // directory listing and toast the result. Shared by the right-click
    // "Copy as .bak" menu (dst_dir omitted → same-dir duplicate) and
    // the Ctrl+V paste path (dst_dir = selected directory).
    async _postCopyBak(srcPath, dstDir) {
      const body = dstDir ? { src: srcPath, dst_dir: dstDir }
                          : { src: srcPath };
      let r;
      try {
        r = await fetch("/api/files/copy-bak", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } catch (e) {
        this.toast(this.t("toast.copy_failed") + ": " + String((e && e.message) || e), "error", 4000);
        return null;
      }
      if (!r.ok) {
        const msg = await r.text();
        this.toast(this.t("toast.copy_failed") + ": " + msg, "error", 4000);
        return null;
      }
      const data = await r.json();
      // In-place tree update: only re-fetch the destination parent dir
      // (or fall back to full reload if parent isn't visible — root /
      // collapsed parent). Avoids the whole-tree flicker that the prior
      // reloadTree() caused, matching how delete + drag-to-trash
      // already optimise. The newly-created .bak appears in-place.
      await this._refreshParentInTree(data.path);
      this.toast(
        this.t("toast.copied_as_bak").replace("{name}", data.name),
        "success",
        1800,
      );
      return data;
    },
    async doCopyAsBak(n) {
      // In-place duplicate: backend defaults dst_dir to src.parent when
      // we omit it.
      await this._postCopyBak(n.path, "");
    },
    async doPasteBak() {
      const src = this.fileClipboard.path;
      if (!src) return;
      // Decide paste target directory:
      //   - currently-selected node is a dir → paste there
      //   - currently-selected node is a file → paste in its parent dir
      //   - nothing selected → fall through (backend defaults to src
      //     parent, i.e. same-directory duplicate)
      let dstDir = "";
      const selNode = this._findTreeNode(this.selected);
      if (selNode) {
        dstDir = selNode.is_dir
          ? selNode.path
          : selNode.path.split("/").slice(0, -1).join("/");
      }
      const ok = await this._postCopyBak(src, dstDir);
      if (ok) {
        // Clear clipboard so an accidental repeated Ctrl+V doesn't keep
        // generating .bak.2 / .bak.3 / … User can Ctrl+C again to
        // re-arm.
        this.fileClipboard = { path: "", name: "" };
      }
    },
    async doDelete(n) {
      const zh = this.lang === "zh";
      // Soft-delete now: backend moves the target into <ROOT>/.muselab-dustbin/
      // and returns a trash_id. Confirm copy says "move to trash" rather
      // than "permanently delete"; the prior "(only empty dirs)" caveat
      // is dropped because non-empty dir rename is just as cheap.
      // The trash footer is hidden on mobile (no click-to-restore there), so
      // the "restore from the trash icon" line would be a dead-end on touch.
      // On mobile we point at the Undo toast instead — the only recovery path.
      const mobile = this._isMobileLayout();
      const body = zh
        ? (mobile
            ? `把 ${n.name} 移到垃圾桶？删除后可在提示条点「撤销」恢复。`
            : `把 ${n.name} 移到垃圾桶？可以从左下角垃圾桶恢复。`)
        : (mobile
            ? `Move ${n.name} to trash? You can undo from the toast right after.`
            : `Move ${n.name} to trash? You can restore it from the trash icon at the bottom of the files pane.`);
      const ok = await this.confirm({
        title: zh ? "移到垃圾桶" : "Move to trash",
        body,
        danger: false,
        okText: zh ? "移到垃圾桶" : "Move to trash",
      });
      if (!ok) return;
      let r;
      try {
        r = await fetch("/api/files/delete", {
          method: "DELETE",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ path: n.path }),
        });
      } catch (e) { this.errToast("delete", String((e && e.message) || e)); return; }
      if (!r.ok) {
        this.errToast("delete", await r.text());
        return;
      }
      let data = {};
      try { data = await r.json(); } catch (_) {}
      // 同步 tabs：删了的文件如果在 tabs 也清掉
      this.tabs = this.tabs.filter(t => t.path !== n.path);
      if (this.selected === n.path) { this.selected = ""; this.previewMode = ""; }
      // In-place tree removal: no full reload, no flicker. Helper also
      // invalidates the parent's childCache so a future re-expand fetches
      // fresh truth from disk.
      this._removeNodeFromTree(n.path);
      // Bump trash count for the badge without a round-trip.
      this.trash.count += 1;
      // Toast with an Undo action — clicking calls /trash/restore with
      // the just-issued trash_id. 6s window matches the user expectation
      // that "I just hit Delete" stays undoable for a moment. The 4th
      // arg of toast() takes a `{label, onClick}` action — see
      // runToastAction + the toast template button.
      const tid = data.trash_id;
      const baseMsg = zh ? `已移到垃圾桶：${n.name}` : `Moved to trash: ${n.name}`;
      if (tid) {
        this.toast(baseMsg, "success", 6000, {
          label: zh ? "撤销" : "Undo",
          onClick: () => this.restoreTrash(tid),
        });
      } else {
        this.toast(baseMsg, "success", 3500);
      }
    },

    // ============================================================
    // Trash UI methods
    // ============================================================
    async loadTrash() {
      // Skip before user has authenticated — otherwise the request 401s
      // and pollutes the network panel on every fresh page load. Caller
      // paths that fire pre-auth (any future ones) just silently no-op
      // until login/_bootApp re-invokes us.
      if (!this.token) return;
      this.trash.loading = true;
      try {
        const r = await fetch("/api/files/trash/list", { headers: this.hdr() });
        if (!r.ok) {
          this.errToast("trash list", await r.text());
          return;
        }
        const d = await r.json();
        this.trash.items = d.items || [];
        this.trash.count = this.trash.items.length;
      } catch (e) {
        this.toast(String(e.message || e), "error");
      } finally {
        this.trash.loading = false;
      }
    },
    openTrashModal() {
      this.trash.modalOpen = true;
      this.loadTrash();
    },
    closeTrashModal() {
      this.trash.modalOpen = false;
    },
    async restoreTrash(tid) {
      let r;
      try {
        r = await fetch("/api/files/trash/restore", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ trash_id: tid }),
        });
      } catch (e) {
        const zh = this.lang === "zh";
        this.toast((zh ? "恢复失败：" : "Restore failed: ") + String((e && e.message) || e), "error", 4500);
        return;
      }
      if (!r.ok) {
        // 409 = original path occupied; surface backend detail verbatim
        let detail = "";
        try { detail = (await r.json()).detail || ""; } catch (_) {}
        const zh = this.lang === "zh";
        this.toast(detail || (zh ? "恢复失败" : "Restore failed"), "error", 4500);
        return;
      }
      const d = await r.json();
      this.trash.items = this.trash.items.filter(it => it.trash_id !== tid);
      this.trash.count = Math.max(0, this.trash.count - 1);
      // Targeted refresh: re-fetch ONLY the parent dir's children + splice
      // them back into the tree, preserving every other expanded subtree.
      // Falls back to reloadTree internally for the root / unloaded-parent
      // edge cases.
      const restored = d.restored_path || "";
      await this._refreshParentInTree(restored);
      const zh = this.lang === "zh";
      this.toast(zh ? `已恢复：${restored}` : `Restored: ${restored}`, "success", 2500);
    },
    async purgeTrash(tid) {
      const zh = this.lang === "zh";
      const ok = await this.confirm({
        title: zh ? "彻底删除" : "Permanently delete",
        body: zh ? "这一项将被永久删除，无法恢复。继续？"
                   : "This item will be permanently deleted. Continue?",
        danger: true,
        okText: zh ? "彻底删除" : "Delete forever",
      });
      if (!ok) return;
      let r;
      try {
        r = await fetch("/api/files/trash/purge", {
          method: "DELETE",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ trash_id: tid }),
        });
      } catch (e) { this.errToast("delete", String((e && e.message) || e)); return; }
      if (!r.ok) {
        this.errToast("trash purge", await r.text());
        return;
      }
      this.trash.items = this.trash.items.filter(it => it.trash_id !== tid);
      this.trash.count = Math.max(0, this.trash.count - 1);
      this.toast(zh ? "已彻底删除" : "Permanently deleted", "success", 2000);
    },
    async emptyTrash() {
      const zh = this.lang === "zh";
      if (!this.trash.items.length) return;
      const ok = await this.confirm({
        title: zh ? "清空垃圾桶" : "Empty trash",
        body: zh ? `${this.trash.items.length} 项将被永久删除，无法恢复。继续？`
                   : `${this.trash.items.length} item(s) will be permanently deleted. Continue?`,
        danger: true,
        okText: zh ? "清空" : "Empty",
      });
      if (!ok) return;
      let r;
      try {
        r = await fetch("/api/files/trash/empty", {
          method: "DELETE",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
        });
      } catch (e) { this.errToast("delete", String((e && e.message) || e)); return; }
      if (!r.ok) {
        this.errToast("trash empty", await r.text());
        return;
      }

      this.trash.items = [];
      this.trash.count = 0;
      this.toast(zh ? "垃圾桶已清空" : "Trash emptied", "success", 2000);
    },
    onTrashDragOver(ev) {
      // Only accept drops that came from inside the file tree (our custom
      // MIME). OS file drops onto the trash icon are meaningless — they'd
      // upload a file just to immediately trash it.
      const types = Array.from(ev.dataTransfer?.types || []);
      if (!types.includes(this._DRAG_MIME_INTERNAL)) {
        ev.dataTransfer.dropEffect = "none";
        return;
      }
      ev.dataTransfer.dropEffect = "move";
      this.dragOverTrash = true;
    },
    async onTrashDrop(ev) {
      this.dragOverTrash = false;
      const types = Array.from(ev.dataTransfer?.types || []);
      if (!types.includes(this._DRAG_MIME_INTERNAL)) return;
      const path = ev.dataTransfer.getData(this._DRAG_MIME_INTERNAL)
                    || this._dragSrcPath || "";
      this._dragSrcPath = null;
      if (!path) return;
      // Drag-to-trash skips the confirm modal — the drag itself is the
      // commitment, matching Finder / Files behavior. The Undo toast
      // covers accidental drops (6s window). We DO still want the same
      // tab-cleanup and tree-refresh side effects as doDelete(), so
      // hand off to a shared helper.
      await this._sendToTrash(path);
    },
    async _sendToTrash(path) {
      const name = path.split("/").pop() || path;
      let r;
      try {
        r = await fetch("/api/files/delete", {
          method: "DELETE",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ path }),
        });
      } catch (e) { this.errToast("delete", String((e && e.message) || e)); return; }
      if (!r.ok) {
        this.errToast("delete", await r.text());
        return;
      }
      let data = {};
      try { data = await r.json(); } catch (_) {}
      this.tabs = this.tabs.filter(t => t.path !== path);
      this._previewCacheDel(path);
      if (this.selected === path) { this.selected = ""; this.previewMode = ""; }
      // In-place removal — see _removeNodeFromTree for rationale.
      this._removeNodeFromTree(path);
      this.trash.count += 1;
      const tid = data.trash_id;
      const zh = this.lang === "zh";
      const baseMsg = zh ? `已移到垃圾桶：${name}` : `Moved to trash: ${name}`;
      if (tid) {
        this.toast(baseMsg, "success", 6000, {
          label: zh ? "撤销" : "Undo",
          onClick: () => this.restoreTrash(tid),
        });
      } else {
        this.toast(baseMsg, "success", 3500);
      }
    },
    trashItemTime(it) {
      // Format unix sec → local short time, e.g. "05-25 16:42" / "May 25 4:42 PM"
      const ts = it.deleted_at || 0;
      if (!ts) return "—";
      const d = new Date(ts * 1000);
      const zh = this.lang === "zh";
      return d.toLocaleString(zh ? "zh-CN" : "en-US",
        { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    },
    trashItemSize(it) {
      const n = it.size || 0;
      if (n < 1024) return n + " B";
      if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
      return (n / 1024 / 1024).toFixed(1) + " MB";
    },
    // ===== Preview-tab content cache =====
    // Chat-session tabs cache their loaded state (tabState[id]._loaded) so
    // switching back is instant; preview tabs had no equivalent — every
    // switchTab re-fetched the file body over HTTP and re-rendered, flashing
    // a "loading" state even for a file already shown moments ago. This LRU
    // caches the parsed/rendered preview for md / text / xlsx so a tab switch
    // back is synchronous (no fetch, no loading flash). Bounded by entry count
    // and per-entry body size so it can't hoard memory on huge files. csv is
    // paginated (stateful) and html/img/pdf are URL-based (already instant), so
    // neither is cached. Invalidated on edit-save / reload / external write.
    PREVIEW_CACHE_MAX_ENTRIES: 16,
    PREVIEW_CACHE_MAX_CHARS: 512 * 1024,   // skip caching bodies larger than this
    LARGE_MD_DEFER_CHARS: 200 * 1024,      // above this, render md off the click frame
    _previewCacheGet(path) {
      if (!this._previewCache || !path) return null;
      const e = this._previewCache.get(path);
      if (e === undefined) return null;
      // LRU bump
      this._previewCache.delete(path);
      this._previewCache.set(path, e);
      return e;
    },
    _previewCacheSet(path, entry) {
      if (!path || !entry) return;
      if (!this._previewCache) this._previewCache = new Map();
      this._previewCache.delete(path);
      this._previewCache.set(path, entry);
      while (this._previewCache.size > this.PREVIEW_CACHE_MAX_ENTRIES) {
        this._previewCache.delete(this._previewCache.keys().next().value);
      }
    },
    _previewCacheDel(path) {
      if (this._previewCache && path) this._previewCache.delete(path);
    },
    // Restore preview-pane reactive state from a cache entry; mirrors the
    // post-fetch assignments in openFile's per-mode branches.
    _applyPreviewCache(e) {
      this.rawText = e.rawText || "";
      this.renderedMd = e.renderedMd || "";
      this.xlsxSheets = e.xlsxSheets || [];
      this.csvData = null;
      this.previewMode = e.mode;
      if (e.mode === "md") {
        this.$nextTick(() => this.highlightCode(".markdown"));
      } else if (e.mode === "text") {
        this.previewLang = e.previewLang || "plaintext";
        this.$nextTick(() => {
          document.querySelectorAll(".text code").forEach(el => { delete el.dataset.hl; });
          this.highlightCode(".text");
        });
      } else if (e.mode === "xlsx") {
        this.xlsxActive = e.xlsxActive || "";
        this.xlsxLimits = e.xlsxLimits || null;
        this.xlsxSheetsTruncated = !!e.xlsxSheetsTruncated;
      }
    },
    // ----- Find in preview (magnifier) ----------------------------------
    // The live container whose DOM text we search/highlight. Only the two
    // text-rendering modes are addressable; everything else returns null and
    // the magnifier button is hidden for them.
    _previewFindContainer() {
      const body = document.querySelector(".pane.preview .preview-body");
      if (!body) return null;
      if (this.previewMode === "md") return body.querySelector(".markdown");
      if (this.previewMode === "text") return body.querySelector("pre.text code") || body.querySelector("pre.text");
      return null;
    },
    togglePreviewFind() {
      if (this.previewFind.open) { this.closePreviewFind(); return; }
      this.previewFind.open = true;
      // Focus the input on next paint, then re-run any leftover query.
      this.$nextTick(() => {
        const inp = document.querySelector(".preview-find-input");
        if (inp) inp.focus();
        if (this.previewFind.query) this.runPreviewFind();
      });
    },
    closePreviewFind() {
      this._clearPreviewFindMarks();
      this.previewFind.open = false;
      this.previewFind.matches = [];
      this.previewFind.active = -1;
      this.previewFind.count = 0;
      this.previewFind.listOpen = false;
    },
    // Unwrap every <mark class="find-hit"> we injected, restoring the original
    // text so a re-run (or close) leaves the DOM exactly as the renderer left
    // it. parent.normalize() re-merges the split text nodes.
    _clearPreviewFindMarks() {
      this._pfEls = [];
      const container = this._previewFindContainer();
      if (!container) return;
      const marks = container.querySelectorAll("mark.find-hit");
      marks.forEach((m) => {
        const parent = m.parentNode;
        if (!parent) return;
        parent.replaceChild(document.createTextNode(m.textContent), m);
        parent.normalize();
      });
    },
    _escHtml(s) {
      return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    },
    _previewFindSnippet(text, start, len) {
      const a = Math.max(0, start - 30);
      const b = Math.min(text.length, start + len + 30);
      const pre = (a > 0 ? "…" : "") + this._escHtml(text.slice(a, start));
      const hit = this._escHtml(text.slice(start, start + len));
      const post = this._escHtml(text.slice(start + len, b)) + (b < text.length ? "…" : "");
      return pre + "<mark>" + hit + "</mark>" + post;
    },
    runPreviewFind() {
      this._clearPreviewFindMarks();
      this.previewFind.matches = [];
      this.previewFind.active = -1;
      this.previewFind.count = 0;
      const q = this.previewFind.query || "";
      if (!q) return;
      const container = this._previewFindContainer();
      if (!container) return;
      const needle = q.toLowerCase();
      // Collect candidate text nodes first (mutating during a TreeWalker pass
      // would invalidate the walker).
      const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
        acceptNode: (n) => {
          if (!n.nodeValue) return NodeFilter.FILTER_REJECT;
          const p = n.parentNode;
          if (p && (p.nodeName === "SCRIPT" || p.nodeName === "STYLE")) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      const textNodes = [];
      let node;
      while ((node = walker.nextNode())) textNodes.push(node);
      const matches = [];
      const els = [];
      for (const tn of textNodes) {
        const text = tn.nodeValue;
        const lower = text.toLowerCase();
        const starts = [];
        let idx = 0, from = 0;
        while ((idx = lower.indexOf(needle, from)) !== -1) { starts.push(idx); from = idx + needle.length; }
        if (!starts.length) continue;
        const frag = document.createDocumentFragment();
        let cursor = 0;
        for (const s of starts) {
          if (s > cursor) frag.appendChild(document.createTextNode(text.slice(cursor, s)));
          const mark = document.createElement("mark");
          mark.className = "find-hit";
          mark.textContent = text.slice(s, s + needle.length);
          frag.appendChild(mark);
          els.push(mark);
          matches.push({ snippet: this._previewFindSnippet(text, s, needle.length) });
          cursor = s + needle.length;
        }
        if (cursor < text.length) frag.appendChild(document.createTextNode(text.slice(cursor)));
        tn.parentNode.replaceChild(frag, tn);
      }
      this._pfEls = els;
      this.previewFind.matches = matches;
      this.previewFind.count = matches.length;
      if (matches.length) this.previewFindGoto(0);
    },
    previewFindGoto(i) {
      const els = this._pfEls;
      if (!els.length) return;
      i = ((i % els.length) + els.length) % els.length;
      els.forEach((e) => e && e.classList.remove("find-hit-active"));
      const el = els[i];
      if (el) {
        el.classList.add("find-hit-active");
        el.scrollIntoView({ block: "center", behavior: "smooth" });
      }
      this.previewFind.active = i;
    },
    previewFindNext() { if (this._pfEls.length) this.previewFindGoto(this.previewFind.active + 1); },
    previewFindPrev() { if (this._pfEls.length) this.previewFindGoto(this.previewFind.active - 1); },
    async openFile(n, opts = {}) {
      // Unsaved-edits guard: switching to a DIFFERENT file while the editor
      // is dirty would silently drop the changes. Confirm first; abort the
      // switch if the user cancels. Re-opening the same path is a no-op for
      // dirtiness (we'd just re-enter the same buffer), so skip the prompt.
      if (this.editing && n && n.path !== this.selected && !this._confirmLoseEdits()) {
        return;
      }
      // Switching to a different file invalidates any open find session — the
      // old file's <mark> nodes get blown away by the new x-html/x-text render
      // anyway, so drop the find UI + state cleanly.
      if (this.previewFind.open && n && n.path !== this.selected) this.closePreviewFind();
      // VSCode-style preview tab: a plain single-click in the tree opens the
      // file in ONE reusable, ephemeral italic "preview" slot. The slot
      // always FOLLOWS the user's latest single-click — opening any other
      // file closes the lingering preview tab. A double-click, an edit, or a
      // deliberate open (chat link, context menu, search) pins the tab.
      // `opts.preview === true` ⇒ ephemeral slot.
      const asPreview = !!opts.preview;
      const name = n.name || n.path.split("/").pop();
      if (asPreview) {
        const existing = this.tabs.find(t => t.path === n.path);
        if (existing) {
          // Target already open (preview or pinned) — keep its pin state, but
          // the temp slot follows the user: drop any OTHER preview tab.
          this.tabs = this.tabs.filter(t => t.path === n.path || !t.preview);
        } else {
          // Recycle the single existing preview slot in place (keeps tab
          // order stable); otherwise create the preview tab.
          const pi = this.tabs.findIndex(t => t.preview);
          if (pi >= 0) this.tabs.splice(pi, 1, { path: n.path, name, preview: true });
          else this.tabs.push({ path: n.path, name, preview: true });
        }
      } else {
        // Deliberate open / pin. Promote an existing preview tab to permanent,
        // or create a permanent tab.
        const existing = this.tabs.find(t => t.path === n.path);
        if (existing) existing.preview = false;
        else this.tabs.push({ path: n.path, name, preview: false });
      }
      this.selected = n.path;
      this.editing = false;
      // Persist preview-pane state so a refresh restores tabs + selected.
      this.savePrefs();
      // Mobile: opening a file should jump to the preview pane (otherwise
      // the user is still on `files` tab and sees nothing change).
      if (this._isMobileLayout()) this.mobileTab = "preview";
      // When many files are open, the active row/tab can end up off-screen
      // in both the Open files list (vertical scroll) and the preview tab
      // bar (horizontal scroll). Scroll the active item into view so users
      // don't have to hunt for it. `block/inline: "nearest"` is a no-op if
      // the item is already visible, so this is cheap on the common path.
      this.$nextTick(() => this._scrollPreviewSelectedIntoView());
      // Cache hit: serve a previously-loaded md/text/xlsx body synchronously —
      // no fetch, no "loading" flash. Invalidated on edit/reload/external write.
      const cachedPrev = this._previewCacheGet(n.path);
      if (cachedPrev) {
        this._applyPreviewCache(cachedPrev);
        return;
      }
      // Clear the previous file's preview data and surface a loading
      // indicator. Without this, switching from a 10MB markdown file to
      // a small csv would briefly show the old markdown while the new
      // fetch is in flight — users read it as "click didn't register".
      // Each mode branch below overrides previewMode with the real type
      // once the content is ready.
      this.rawText = "";
      this.renderedMd = "";
      this.xlsxSheets = [];
      this.csvData = null;
      this.previewMode = "loading";
      const ext = name.split(".").pop().toLowerCase();
      if (["md", "markdown"].includes(ext)) {
        this.previewMode = "md";
        const r = await fetch("/api/files/read?path=" + encodeURIComponent(n.path), { headers: this.hdr() });
        if (r.ok) {
          this.rawText = await r.text();
          // For large markdown the synchronous markdown-it + sanitize pass can
          // block the main thread long enough to make the click feel frozen
          // ("点开即卡"). Paint the loading spinner first, then render on the
          // next animation frame. The `selected` guard aborts the deferred
          // render if the user switched tabs while it was pending.
          const targetPath = n.path;
          const renderMd = () => {
            if (this.selected !== targetPath) return;   // switched away mid-defer
            this.renderedMd = this.mdRender(this.rawText);
            this.previewMode = "md";
            if (this.rawText.length <= this.PREVIEW_CACHE_MAX_CHARS) {
              this._previewCacheSet(targetPath, { mode: "md", rawText: this.rawText, renderedMd: this.renderedMd });
            }
            this.$nextTick(() => this.highlightCode(".markdown"));
          };
          if (this.rawText.length > this.LARGE_MD_DEFER_CHARS) {
            this.previewMode = "loading";
            requestAnimationFrame(() => requestAnimationFrame(renderMd));
          } else {
            renderMd();
          }
        } else {
          // Without this branch a failed read left previewMode="md" with empty
          // rawText/renderedMd → blank white pane, no error. Mirror the text
          // branch: surface the error and fall back to unsupported.
          this.errToast("read", await r.text());
          this.previewMode = "unsupported";
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
          this._previewCacheSet(n.path, {
            mode: "xlsx", xlsxSheets: this.xlsxSheets, xlsxActive: this.xlsxActive,
            xlsxLimits: this.xlsxLimits, xlsxSheetsTruncated: this.xlsxSheetsTruncated,
          });
        } else {
          this.previewMode = "unsupported";
        }
      }
      else if (["csv", "tsv"].includes(ext)) {
        // CSV preview: paginated table render (xlsx-style) so a million-row
        // file doesn't blow the browser. First page only here; "next page"
        // button is wired through csvLoadPage().
        this.csvPath = n.path;
        this.csvOffset = 0;
        await this.csvLoadPage();
        if (this.csvData) {
          this.previewMode = "csv";
        } else {
          this.previewMode = "unsupported";
        }
      }
      else {
        // `opts.readUrl` lets callers point at a non-archive backend route
        // (e.g. bg-task .output files under /tmp via /api/chat/task-output)
        // that the archive-scoped /api/files/read can't reach.
        const readUrl = opts.readUrl || ("/api/files/read?path=" + encodeURIComponent(n.path));
        const r = await fetch(readUrl, { headers: this.hdr() });
        if (r.ok) {
          this.previewMode = "text";
          this.rawText = await r.text();
          this.previewLang = this.hljsLang(n.path);
          if (this.rawText.length <= this.PREVIEW_CACHE_MAX_CHARS) {
            this._previewCacheSet(n.path, { mode: "text", rawText: this.rawText, previewLang: this.previewLang });
          }
          // 强制重新高亮：删 dataset.hl 让 highlightCode 重新跑
          this.$nextTick(() => {
            document.querySelectorAll(".text code").forEach(el => { delete el.dataset.hl; });
            this.highlightCode(".text");
          });
        }
        else this.previewMode = "unsupported";
      }
    },
    async csvLoadPage() {
      if (!this.csvPath || this.csvLoading) return;
      this.csvLoading = true;
      try {
        const url = `/api/files/csv?path=${encodeURIComponent(this.csvPath)}`
                    + `&offset=${this.csvOffset}&limit=${this.csvLimit}`;
        const r = await fetch(url, { headers: this.hdr() });
        if (r.ok) {
          this.csvData = await r.json();
        } else {
          this.csvData = null;
          this.toast(this.lang === "zh" ? "CSV 解析失败" : "CSV parse failed",
                      "error", 3000);
        }
      } finally {
        this.csvLoading = false;
      }
    },
    csvNextPage() {
      if (!this.csvData) return;
      const next = this.csvOffset + this.csvLimit;
      if (next >= (this.csvData.total_rows || 0)) return;
      this.csvOffset = next;
      this.csvLoadPage();
    },
    csvPrevPage() {
      if (this.csvOffset <= 0) return;
      this.csvOffset = Math.max(0, this.csvOffset - this.csvLimit);
      this.csvLoadPage();
    },
    csvWindowEnd() {
      if (!this.csvData) return 0;
      return Math.min(this.csvOffset + (this.csvData.rows || []).length,
                       this.csvData.total_rows || 0);
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
      // 不再 push（已在 tabs 里），只是切换 selected 并重新加载内容。
      // Preserve the tab's preview/pinned state: a single-click to view a
      // preview tab must NOT pin it (only a double-click / edit does). Pass
      // the tab's current preview flag so openFile's "existing" branch leaves
      // it untouched.
      const cur = this.tabs.find(t => t.path === path);
      await this.openFile({ path, name: path.split("/").pop() },
                          { preview: !!(cur && cur.preview) });
      // Pass mode:"background" so we expand/scroll the tree quietly —
      // the user clicked a preview tab, they want to STAY in preview
      // (especially on mobile, where revealInTree's default mode would
      // bounce them to the files pane). The pulse animation also
      // doesn't fire here because there's no "I'm looking for this
      // file in the tree" user intent — they were already on it.
      await this.revealInTree(path, { mode: "background" });
    },
    async revealInTree(path, opts = {}) {
      // Make the file's row visible in the tree pane and flash it so the
      // user can see the locate operation actually happened.
      //
      // Two modes:
      //   - "interactive" (default): user explicitly asked to locate
      //     this file (context menu "在文件树定位"). Switch mobileTab to
      //     "files" so they see the result, clear searchMode (the tree
      //     is hidden under x-show otherwise), and pulse the row.
      //   - "background": caller wants the side-effect of expanding
      //     ancestors + scrolling, but the user is currently doing
      //     something else (e.g. switching preview tabs). Do NOT
      //     hijack mobileTab, do NOT clear search, do NOT pulse —
      //     just quietly position the row so it's already correct
      //     when the user later switches to the files pane.
      //
      // Failure modes the interactive mode guards against:
      //   1. Mobile — user is on mobileTab="preview", so even after the
      //      tree expands they see no change. Switch to "files" first.
      //   2. searchMode — filelist-wrap has x-show="!searchMode", so the
      //      <li> exists in DOM but is hidden inside a display:none parent.
      //      Clear searchMode so the tree is actually visible.
      //   3. Already in viewport — block:"nearest" + smooth scroll does
      //      nothing visibly. Use block:"center" + a temporary CSS pulse
      //      class so the user always sees feedback.
      //   4. Non-active tab — when the user right-clicks a non-current
      //      preview tab, `selected !== path`, so the row has no `sel`
      //      class. The pulse class handles that too.
      if (!path) return;
      const interactive = opts.mode !== "background";
      if (interactive) {
        if (this.searchMode) this.clearSearch();
        if (this._isMobileLayout()) this.mobileTab = "files";
      }
      const parts = path.split("/");
      parts.pop();   // drop the filename, keep only directory chain
      const dirPath = parts.join("/");
      if (dirPath) await this.expandPath(dirPath);
      // Two nextTicks: first to let the mobileTab/searchMode toggles flush
      // and the filelist-wrap become visible, second to let any final
      // splice from expandPath render before we query.
      this.$nextTick(() => this.$nextTick(() => {
        const sel = (window.CSS && CSS.escape) ? CSS.escape(path) : path;
        const el = document.querySelector(`.filelist li[data-path="${sel}"]`);
        if (!el) return;
        // Background mode: nearest (no animation if already on-screen)
        // keeps the operation invisible. Interactive: center it.
        el.scrollIntoView({
          block: interactive ? "center" : "nearest",
          behavior: "smooth",
        });
        if (!interactive) return;
        // Pulse highlight — independent of `sel` class so it fires even
        // when this isn't the active tab. Restart by removing+adding so
        // rapid re-reveals still trigger the animation.
        el.classList.remove("reveal-pulse");
        // Force reflow so the next add restarts the animation.
        void el.offsetWidth;
        el.classList.add("reveal-pulse");
        setTimeout(() => el.classList.remove("reveal-pulse"), 1600);
      }));
    },
    closeTab(path) {
      const idx = this.tabs.findIndex(t => t.path === path);
      if (idx < 0) return;
      // Closing the tab being edited would discard unsaved changes — confirm.
      if (this.editing && path === this.selected && !this._confirmLoseEdits()) return;
      this.tabs.splice(idx, 1);
      if (this.selected === path) {
        // 关掉的是当前 tab，切到旁边
        if (this.tabs.length === 0) {
          this._clearPreviewState();
        } else {
          const next = this.tabs[Math.min(idx, this.tabs.length - 1)];
          this.openByPath(next.path);
          return;   // openByPath → openFile → savePrefs runs there
        }
      }
      this.savePrefs();
    },
    // Reset every piece of UI state the preview pane reads from. Called
    // from closeTab (last-tab branch) AND closeAllTabs — both paths used
    // to inline the same 5 lines, with closeAllTabs's inline handler in
    // index.html missing previewMode / rawText / renderedMd / editing.
    // Symptom: user clicks "关闭全部" — open-files list empties, but the
    // preview pane keeps showing the last file's content because rawText/
    // renderedMd were never cleared (2026-05-23 user feedback).
    _clearPreviewState() {
      this.selected = "";
      this.previewMode = "";
      this.rawText = "";
      this.renderedMd = "";
      this.editing = false;
    },
    closeAllTabs() {
      if (!this.tabs.length) return;
      // Closing all tabs evicts the edited buffer — confirm if dirty.
      if (this.editing && !this._confirmLoseEdits()) return;
      this.tabs = [];
      this._clearPreviewState();
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
      // Re-render replaces the content nodes our find marks live in; close the
      // find session so it doesn't dangle on detached <mark> elements.
      if (this.previewFind.open) this.closePreviewFind();
      this.previewVersion = Date.now();
      // File content changed underneath us — drop any stale cached body so a
      // later tab switch back re-fetches instead of serving the old render.
      this._previewCacheDel(this.selected);
      if (this.previewMode === "md" || this.previewMode === "text") {
        const url = "/api/files/read?path=" + encodeURIComponent(this.selected)
                     + "&_v=" + this.previewVersion;
        try {
          const r = await fetch(url, { headers: this.hdr() });
          if (r.ok) {
            this.rawText = await r.text();
            if (this.previewMode === "md") {
              this.renderedMd = this.mdRender(this.rawText);
              if (this.rawText.length <= this.PREVIEW_CACHE_MAX_CHARS) {
                this._previewCacheSet(this.selected, {
                  mode: "md", rawText: this.rawText, renderedMd: this.renderedMd,
                });
              }
              this.$nextTick(() => this.highlightCode(".markdown"));
            } else {
              if (this.rawText.length <= this.PREVIEW_CACHE_MAX_CHARS) {
                this._previewCacheSet(this.selected, {
                  mode: "text", rawText: this.rawText, previewLang: this.previewLang,
                });
              }
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
      // An external/tool write invalidated the cached body — drop it.
      this._previewCacheDel(this.selected);
      if (this.previewMode === "md" || this.previewMode === "text") {
        const url = "/api/files/read?path=" + encodeURIComponent(this.selected)
                     + "&_v=" + this.previewVersion;
        try {
          const r = await fetch(url, { headers: this.hdr() });
          if (r.ok) {
            this.rawText = await r.text();
            if (this.previewMode === "md") {
              this.renderedMd = this.mdRender(this.rawText);
              if (this.rawText.length <= this.PREVIEW_CACHE_MAX_CHARS) {
                this._previewCacheSet(this.selected, {
                  mode: "md", rawText: this.rawText, renderedMd: this.renderedMd,
                });
              }
              this.$nextTick(() => this.highlightCode(".markdown"));
            } else {
              if (this.rawText.length <= this.PREVIEW_CACHE_MAX_CHARS) {
                this._previewCacheSet(this.selected, {
                  mode: "text", rawText: this.rawText, previewLang: this.previewLang,
                });
              }
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
    // Returns a Promise that resolves once every block in `root` is
    // highlighted AND artifacts (mermaid/HTML) are rendered. Most callers
    // fire-and-forget; loadSession awaits it to know when to reveal the view.
    highlightCode(root, scopeEls = null) {
      if (!window.hljs) {
        // Lazy-load hljs on first need, then re-call. Without this guard
        // a chat with no code blocks never pays the 124 KB download cost.
        // Re-call returns immediately if the root has no <code> children,
        // and idempotently highlights any blocks that appeared since the
        // last paint (data-hl="1" sentinel prevents double work).
        return this._loadHljs().then(() => this.highlightCode(root, scopeEls))
          .catch(e => console.warn("[muselab] hljs lazy-load failed:", e));
      }
      // Snapshot the not-yet-highlighted blocks. We DON'T highlight them all
      // in one synchronous forEach: on a long history (re)entered cold this
      // can be 150+ blocks, and hljs auto-detect on big ones takes many ms
      // each — the sum locked the main thread for seconds and froze the page
      // on boot. Instead process in small batches, yielding to the event loop
      // between them so the UI stays responsive (a brief flash of
      // unhighlighted code is acceptable; a frozen page is not).
      //
      // scopeEls (optional): when the caller knows the new content is confined
      // to a freshly-inserted subtree (e.g. a "Load earlier" batch prepended
      // to a long, paged-in history), it passes those element(s) so we query
      // only within them — O(new blocks) instead of rescanning the whole chat
      // body's O(total blocks) each click. The data-hl sentinel still makes a
      // too-broad scope merely redundant, never wrong.
      const nodes = scopeEls
        ? this._collectCodeNodes(scopeEls, "code").filter(el => el.dataset.hl !== "1")
        : Array.from(document.querySelectorAll(root + " code"))
            .filter(el => el.dataset.hl !== "1");
      const runArtifacts = () => {
        // After hljs finishes, scan for Artifact-eligible code blocks (mermaid
        // diagrams, HTML previews). Limited to chat messages and markdown
        // previews — file previews (.text) are raw read-only views where
        // auto-rendering embedded HTML / running Mermaid would be unexpected.
        if (root === ".chat-body" || root === ".markdown") {
          return this._renderArtifacts(root, scopeEls).catch(e =>
            console.warn("[muselab] artifacts render failed:", e));
        }
        return Promise.resolve();
      };
      if (!nodes.length) { return Promise.resolve(runArtifacts()); }
      const yieldToLoop = window.requestAnimationFrame
        ? (fn) => window.requestAnimationFrame(fn)
        : (fn) => setTimeout(fn, 16);
      const CHUNK = 8;
      let i = 0;
      return new Promise((resolve) => {
        const pump = () => {
          const end = Math.min(i + CHUNK, nodes.length);
          for (; i < end; i++) this._highlightOne(nodes[i]);
          if (i < nodes.length) yieldToLoop(pump);
          else resolve(runArtifacts());
        };
        pump();
      });
    },
    // Gather matching descendant (and self) elements within a scope that may
    // be a single Element, a NodeList, or an Array of Elements. Used to scope
    // highlightCode / _renderArtifacts to a freshly-inserted subtree instead
    // of rescanning the whole chat body.
    _collectCodeNodes(scopeEls, selector) {
      const els = (Array.isArray(scopeEls) || scopeEls instanceof NodeList)
        ? Array.from(scopeEls)
        : [scopeEls];
      const out = [];
      for (const el of els) {
        if (!el || !el.querySelectorAll) continue;
        if (el.matches && el.matches(selector)) out.push(el);
        el.querySelectorAll(selector).forEach(n => out.push(n));
      }
      return out;
    },
    // Walk the FIRST `n` `.msg` element children of the chat body — the
    // bubbles just prepended by a "Load earlier" / jump-into-history batch
    // (unshift puts them at the front). Stops as soon as it has n, so it's
    // O(n) regardless of how much history is already rendered. The
    // load-earlier-wrap is the chat body's first child but isn't a `.msg`,
    // so it's skipped naturally.
    _leadingMsgEls(n) {
      const body = this.$refs.chatBody;
      if (!body || n <= 0) return [];
      const out = [];
      let node = body.firstElementChild;
      while (node && out.length < n) {
        if (node.classList && node.classList.contains("msg")) out.push(node);
        node = node.nextElementSibling;
      }
      return out;
    },
    // Highlight a single <code> block (extracted from highlightCode so the
    // chunked pump can call it per element). Idempotent via the data-hl
    // sentinel — see highlightCode for why dedup matters during streaming.
    _highlightOne(el) {
      // Dedup: every stream chunk re-runs flushRender → highlightCode, and
      // without this guard we'd re-highlight every code block on every chunk.
      // The `data-hl="1"` sentinel is cleared by the preview-reload paths
      // (3 sites) when their underlying content changes, so cache
      // invalidation is still correct.
      if (!el || el.dataset.hl === "1") return;
      const text = el.textContent;
      // Skip expensive syntax highlighting for large files. hljs JavaScript /
      // TypeScript parsers can block the main thread for several seconds on
      // minified or very large files. 150 KB is a generous cap — most
      // human-authored source files are well under 50 KB.
      if (text.length > 150000) {
        el.classList.add("hljs");
        el.dataset.hl = "1";
        this._attachCopyBtn(el);
        return;
      }
      const m = el.className.match(/language-([\w+#-]+)/);
      const lang = m && m[1];
      try {
        const r = (lang && window.hljs.getLanguage(lang))
          ? window.hljs.highlight(text, { language: lang, ignoreIllegals: true })
          : window.hljs.highlightAuto(text);
        el.innerHTML = r.value;
        el.classList.add("hljs");
        el.dataset.hl = "1";
      } catch (e) { console.warn("[muselab] highlight failed:", e); }
      // Attach copy button to every <pre> wrapping a code block — only once.
      this._attachCopyBtn(el);
    },

    // ============================================================
    // Code Artifacts — inline render of mermaid diagrams + (opt-in)
    // sandboxed HTML preview. Designed to keep muselab's "no build,
    // no external CDN" constraint: mermaid is vendored locally
    // (frontend/vendor/mermaid.min.js, ~3.3 MB) and lazy-loaded only
    // when the first mermaid block appears. HTML artifacts use a
    // sandboxed iframe (no allow-same-origin; the AI-supplied markup
    // CANNOT access muselab's DOM, cookies, or token).
    //
    // Phase 1 scope:
    //   - Mermaid: always on; replaces the <pre><code> with SVG
    //   - HTML: opt-in via Settings → Appearance (default OFF). Wraps
    //     the <pre><code> with a toolbar; "Run" mounts a sandboxed iframe
    //   - React/JSX: NOT in scope; needs Sucrase/esbuild-wasm (~1-3 MB
    //     of transpiler) which violates the no-build constraint
    // ============================================================
    _initArtifacts() {
      // Single document-level click delegation for all artifact toolbar
      // buttons. Dynamic content inserted by _renderArtifacts doesn't get
      // Alpine-initialized, so plain DOM events are the path of least
      // resistance. Idempotent guard against init() double-fire (same
      // pattern as other listener attachments above).
      if (this._artifactClickBound) return;
      this._artifactClickBound = true;
      document.addEventListener("click", (ev) => {
        const btn = ev.target.closest && ev.target.closest(".artifact-btn");
        if (!btn) return;
        const wrap = btn.closest(".artifact-wrap");
        if (!wrap) return;
        this._onArtifactBtn(wrap, btn.dataset.action || "");
      });
    },

    // Pause streaming bubble re-renders while the user has an active text
    // selection in the chat area. The streaming text path assigns to m.html
    // on every chunk; Alpine's x-html replaces innerHTML, which forces the
    // browser to collapse any selection inside the bubble — making it
    // impossible to "select while Muse is typing, then Ctrl+C". The stream
    // closure stashes its deferred-render callback on streamState as
    // _pendingHtmlRender; when the selection clears we walk every tabState
    // and flush whichever streams accumulated text in the meantime.
    _initStreamSelectionGuard() {
      if (this._streamSelGuardBound) return;
      this._streamSelGuardBound = true;
      document.addEventListener("selectionchange", () => {
        if (this._selectionInChatBody()) return;
        const tabs = this.tabState || {};
        for (const sid in tabs) {
          const fn = tabs[sid] && tabs[sid]._pendingHtmlRender;
          if (typeof fn === "function") fn();
        }
      });
    },

    // A11y: mirror every button's `title` attribute into `aria-label` so
    // screen readers announce the localized tooltip text. Alpine renders
    // ~170 buttons across the app, the vast majority already have a
    // :title binding (lang === 'zh' ? '复制' : 'Copy'). Manually
    // duplicating each one as :aria-label would double the i18n surface
    // and is easy to forget when adding new buttons. Instead, watch the
    // DOM and copy title → aria-label whenever a button gets/changes its
    // title. Skips buttons that already have their own explicit
    // aria-label (the explicit one wins). Idempotent: re-running on a
    // button that's already mirrored is a no-op because aria-label gets
    // set with the same value.
    _initAriaLabelMirror() {
      if (this._ariaMirrorBound) return;
      this._ariaMirrorBound = true;
      const mirror = (root) => {
        const buttons = (root || document).querySelectorAll
          ? (root || document).querySelectorAll("button[title]")
          : [];
        buttons.forEach((b) => {
          const t = b.getAttribute("title");
          if (!t) return;
          const existing = b.getAttribute("aria-label");
          // Only mirror if there is no aria-label yet, OR the existing one
          // was previously set by us (matches our marker). This lets
          // explicit hand-written aria-label="..." on a button keep
          // priority.
          if (existing && b.dataset.ariaMirrored !== "1") return;
          if (existing === t) return;
          b.setAttribute("aria-label", t);
          b.dataset.ariaMirrored = "1";
        });
      };
      // Initial pass after Alpine has done its first render.
      this.$nextTick(() => mirror(document));
      // Long-running: watch for added buttons (new tabs, dynamic modals)
      // and for `title` attribute mutations (locale switch). The observer
      // is cheap because it only reacts to childList + attribute changes
      // and the mirror callback is microsecond-fast per node.
      const obs = new MutationObserver((mutations) => {
        for (const m of mutations) {
          // Skip mutations inside a rendered chat bubble. Streaming assigns
          // m.html via x-html on every throttled tick, which replaces the
          // bubble's entire innerHTML — generating a large childList mutation
          // each time. Rendered markdown never contains app buttons that need
          // title→aria mirroring (the only button we inject there, the code-
          // copy button, sets its own aria-label and carries no `title`), so
          // walking that subtree per chunk was pure waste — a real contributor
          // to long-reply CPU burn / freeze on phones.
          const tgt = m.target;
          if (tgt && tgt.nodeType === 1 && tgt.closest && tgt.closest(".bubble")) continue;
          if (m.type === "childList") {
            m.addedNodes.forEach((n) => {
              if (n.nodeType !== 1) return;
              if (n.tagName === "BUTTON") mirror(n.parentNode);
              else mirror(n);
            });
          } else if (m.type === "attributes" && m.target.tagName === "BUTTON") {
            mirror(m.target.parentNode);
          }
        }
      });
      obs.observe(document.body, {
        childList: true, subtree: true,
        attributes: true, attributeFilter: ["title"],
      });
    },

    // Returns true iff the current Selection is non-collapsed AND its
    // anchor sits inside a .chat-body element. Used by the streaming text
    // handler to decide whether to defer the next mdRender → x-html assign.
    _selectionInChatBody() {
      const sel = (typeof window !== "undefined") && window.getSelection && window.getSelection();
      if (!sel || sel.isCollapsed || sel.rangeCount === 0) return false;
      const range = sel.getRangeAt(0);
      const node = range.commonAncestorContainer;
      if (!node) return false;
      const el = node.nodeType === 1 ? node : node.parentElement;
      return !!(el && el.closest && el.closest(".chat-body"));
    },

    // Lazy-loads vendored mermaid.min.js (~3.3 MB) and initializes it
    // with strict securityLevel. Returns a Promise that resolves once
    // window.mermaid is ready. Subsequent calls reuse the cached promise.
    // Generic lazy-loader for vendor assets. Inserts <script>/<link> tags
    // on demand, caches the in-flight promise so concurrent callers fold
    // into one request, clears the cache on failure so a transient 4xx/5xx
    // doesn't permanently disable the feature.
    //
    // `srcs`: list of asset URLs (.js or .css — auto-detected by extension).
    // `key`: storage slot on `this` for the cached promise.
    // `ready`: predicate that returns true once the global is exposed
    //   (e.g. () => window.CodeMirror). Used both as a fast path on
    //   already-loaded scripts and to resolve the promise.
    _loadAssets(key, srcs, ready) {
      if (ready()) return Promise.resolve();
      if (this[key]) return this[key];
      const clearOnFail = (err) => { this[key] = null; return Promise.reject(err); };
      const loadOne = (src) => new Promise((resolve, reject) => {
        // Already in the DOM (e.g. from a previous lazy-load attempt)? Wait
        // for ready(); don't re-inject the tag.
        const sel = src.endsWith(".css")
          ? `link[href="${src}"]`
          : `script[src="${src}"]`;
        if (document.querySelector(sel)) {
          // Poll briefly for ready in case the in-flight load wasn't ours.
          const t0 = Date.now();
          (function wait() {
            if (ready()) return resolve();
            if (Date.now() - t0 > 5000) return reject(new Error("ready() timeout for " + src));
            setTimeout(wait, 50);
          })();
          return;
        }
        if (src.endsWith(".css")) {
          const l = document.createElement("link");
          l.rel = "stylesheet"; l.href = src;
          l.onload = resolve; l.onerror = () => reject(new Error("load failed: " + src));
          document.head.appendChild(l);
        } else {
          const s = document.createElement("script");
          s.src = src;
          s.onload = resolve; s.onerror = () => reject(new Error("load failed: " + src));
          document.head.appendChild(s);
        }
      });
      // Sequential load so dependency order is preserved (modes need
      // CodeMirror core loaded first, etc).
      this[key] = srcs.reduce(
        (p, src) => p.then(() => loadOne(src)),
        Promise.resolve(),
      ).then(() => {
        if (!ready()) throw new Error("loaded but ready() still false for " + key);
      }).catch(clearOnFail);
      return this[key];
    },

    // highlight.js is ~150 KB (124 KB core + 24 KB extra language files for
    // dockerfile / makefile / protobuf / accesslog / graphql) — only fires
    // when a message / file preview contains a ```fenced``` code block.
    // Plenty of "let me ask Muse a question" turns have zero code, so we
    // skip the download until the first highlightCode() call.
    // The hljs theme CSS (~4 KB) IS preloaded in index.html to prevent a
    // brief unstyled flash when the script lands and re-highlights blocks.
    // Language files load in parallel after the core (they depend on
    // window.hljs existing); _loadAssets's sequential model would block
    // them serially, so we hand-roll the parallel tail here.
    async _loadHljs() {
      if (window.hljs) return;
      if (this._hljsLoadPromise) return this._hljsLoadPromise;
      const inject = (src) => new Promise((resolve, reject) => {
        if (document.querySelector(`script[src="${src}"]`)) {
          // Already in DOM — just wait for window.hljs to materialize.
          const t0 = Date.now();
          (function wait() {
            if (window.hljs) return resolve();
            if (Date.now() - t0 > 5000) return reject(new Error("hljs ready timeout"));
            setTimeout(wait, 50);
          })();
          return;
        }
        const s = document.createElement("script");
        s.src = src;
        s.onload = resolve;
        s.onerror = () => reject(new Error("load failed: " + src));
        document.head.appendChild(s);
      });
      this._hljsLoadPromise = (async () => {
        await inject("/static/vendor/highlight.min.js");
        if (!window.hljs) throw new Error("hljs loaded but window.hljs missing");
        await Promise.all([
          inject("/static/vendor/hljs-langs/dockerfile.min.js"),
          inject("/static/vendor/hljs-langs/makefile.min.js"),
          inject("/static/vendor/hljs-langs/protobuf.min.js"),
          inject("/static/vendor/hljs-langs/accesslog.min.js"),
          inject("/static/vendor/hljs-langs/graphql.min.js"),
        ]);
      })().catch(e => {
        this._hljsLoadPromise = null;   // allow retry next call
        throw e;
      });
      return this._hljsLoadPromise;
    },

    // KaTeX is ~562 KB (with fonts) of math rendering — only fires when a
    // message body contains $...$, $$...$$, \(...\), or \[...\] delimiters.
    // Many users never see math, so loading at startup wastes their bandwidth.
    async _loadKatex() {
      return this._loadAssets("_katexLoadPromise", [
        "/static/vendor/katex/katex.min.css",
        "/static/vendor/katex/katex.min.js",
        "/static/vendor/katex/auto-render.min.js",
      ], () => window.katex && window.renderMathInElement);
    },

    // CodeMirror is ~308 KB (core + 11 modes + 3 addons) — only fires when
    // the user opens edit mode on a file preview. Most read-only browsing
    // never needs it.
    async _loadCodemirror() {
      return this._loadAssets("_cmLoadPromise", [
        "/static/vendor/cm/codemirror.min.css",
        "/static/vendor/cm/theme/material-darker.min.css",
        "/static/vendor/cm/codemirror.min.js",
        "/static/vendor/cm/addon/mode/simple.min.js",
        "/static/vendor/cm/addon/edit/closebrackets.min.js",
        "/static/vendor/cm/addon/edit/matchbrackets.min.js",
        "/static/vendor/cm/mode/meta.min.js",
        "/static/vendor/cm/mode/xml/xml.min.js",
        "/static/vendor/cm/mode/javascript/javascript.min.js",
        "/static/vendor/cm/mode/css/css.min.js",
        "/static/vendor/cm/mode/clike/clike.min.js",
        "/static/vendor/cm/mode/htmlmixed/htmlmixed.min.js",
        "/static/vendor/cm/mode/markdown/markdown.min.js",
        "/static/vendor/cm/mode/python/python.min.js",
        "/static/vendor/cm/mode/yaml/yaml.min.js",
        "/static/vendor/cm/mode/shell/shell.min.js",
        "/static/vendor/cm/mode/go/go.min.js",
        "/static/vendor/cm/mode/rust/rust.min.js",
      ], () => window.CodeMirror);
    },

    async _loadMermaid() {
      if (window.mermaid) return window.mermaid;
      if (this._mermaidLoadPromise) return this._mermaidLoadPromise;
      // Clear the cached promise on failure so a later block (or a retry
      // when the user scrolls back into view) can re-attempt. Otherwise
      // one transient 4xx/5xx on the static asset would permanently
      // disable mermaid for the session.
      const clearOnFail = (err) => {
        this._mermaidLoadPromise = null;
        return Promise.reject(err);
      };
      this._mermaidLoadPromise = new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = "/static/vendor/mermaid.min.js";
        s.onload = () => {
          try {
            // securityLevel: "strict" disables foreign HTML in labels,
            // click-event handlers, and arbitrary script injection inside
            // diagrams. theme=null lets us inherit CSS variables via our
            // own stylesheet rather than mermaid's baked-in themes.
            window.mermaid.initialize({
              startOnLoad: false,
              securityLevel: "strict",
              theme: this.theme === "dark" ? "dark" : "default",
            });
            resolve(window.mermaid);
          } catch (e) { reject(e); }
        };
        s.onerror = () => reject(new Error("mermaid load failed"));
        document.head.appendChild(s);
      }).catch(clearOnFail);
      return this._mermaidLoadPromise;
    },

    async _renderArtifacts(rootSelector, scopeEls = null) {
      // scopeEls: when given, only scan the freshly-inserted subtree for
      // artifact-eligible blocks instead of the whole chat body. Per-block
      // dataset guards already make a full rescan idempotent, but on a deeply
      // paged-in history the full `querySelectorAll` itself is the cost; the
      // scope keeps "Load earlier" O(new blocks).
      const pres = scopeEls
        ? this._collectCodeNodes(scopeEls, "pre > code")
        : document.querySelectorAll(rootSelector + " pre > code");
      for (const codeEl of pres) {
        const pre = codeEl.parentElement;
        // Skip <pre> nested INSIDE an existing artifact's source viewer
        // (we clone the original pre into .artifact-source for the
        // "view source" toggle — without this guard, re-runs would
        // double-wrap the clone). closest() returns null when no
        // ancestor matches, so this is safe even for top-level pres.
        if (pre.closest(".artifact-wrap")) continue;
        const m = (codeEl.className || "").match(/language-([\w+-]+)/);
        if (!m) continue;
        const lang = m[1].toLowerCase();
        if (lang === "mermaid") {
          // Streaming-safe: mid-stream the source is often incomplete
          // (closing ``` not yet arrived), so mermaid throws. We retry
          // on subsequent renders by hashing the current text — if it
          // matches the last failed attempt, skip; otherwise try again.
          // On success, _renderMermaidBlock replaces <pre> entirely.
          const src = codeEl.textContent || "";
          if (pre.dataset.artifactSrc === src) continue;  // unchanged failure
          await this._renderMermaidBlock(pre, codeEl, src);
        } else if (lang === "html" || lang === "htmlpreview") {
          if (pre.dataset.artifact === "1") continue;  // HTML is one-shot wrap
          pre.dataset.artifact = "1";
          this._renderHtmlBlock(pre, codeEl);
        }
      }
    },

    async _renderMermaidBlock(pre, codeEl, src) {
      // textContent rather than innerText so we don't get hljs's syntax
      // span structure — mermaid needs the raw source. Caller passes src
      // so we hash the same text we're rendering (the dataset.artifactSrc
      // check happens on the same value).
      try {
        const mm = await this._loadMermaid();
        const id = "mermaid-" + Math.random().toString(36).slice(2, 10);
        const { svg } = await mm.render(id, src);
        const wrap = document.createElement("div");
        wrap.className = "artifact-wrap artifact-mermaid";
        wrap.innerHTML = `
          <div class="artifact-toolbar">
            <span class="artifact-label">Mermaid</span>
            <button class="artifact-btn" data-action="toggle-source"
                    title="${this.lang === 'zh' ? '查看源码' : 'View source'}">
              &lt;/&gt;
            </button>
            <button class="artifact-btn" data-action="copy"
                    title="${this.lang === 'zh' ? '复制源码' : 'Copy source'}">
              ${this.lang === 'zh' ? '复制' : 'Copy'}
            </button>
          </div>
          <div class="artifact-render">${svg}</div>
          <div class="artifact-source" hidden></div>
        `;
        // Move the original highlighted <pre> into the source toggle so
        // "View source" still shows hljs colors.
        const srcSlot = wrap.querySelector(".artifact-source");
        const clonePre = pre.cloneNode(true);
        clonePre.removeAttribute("data-artifact");
        clonePre.removeAttribute("data-artifact-src");
        srcSlot.appendChild(clonePre);
        pre.replaceWith(wrap);
      } catch (e) {
        // Mid-stream partial blocks fail constantly (unclosed graph
        // syntax). Cache this source text so we don't re-render on every
        // chunk — _renderArtifacts checks dataset.artifactSrc and retries
        // only when the text changes. Keep the original code block
        // visible so the user can read the source while it grows.
        pre.dataset.artifactSrc = src;
        // Replace prior error chip (if any) instead of stacking.
        let note = pre.nextElementSibling;
        if (!note || !note.classList || !note.classList.contains("artifact-error")) {
          note = document.createElement("div");
          note.className = "artifact-error";
          pre.after(note);
        }
        note.textContent = (this.lang === "zh" ? "Mermaid 渲染失败：" : "Mermaid render failed: ")
                            + (e && e.message ? e.message : String(e)).slice(0, 200);
      }
    },

    _renderHtmlBlock(pre, codeEl) {
      const src = codeEl.textContent || "";
      const wrap = document.createElement("div");
      wrap.className = "artifact-wrap artifact-html";
      const runLabel  = this.lang === "zh" ? "运行" : "Run";
      const stopLabel = this.lang === "zh" ? "停止" : "Stop";
      const copyLabel = this.lang === "zh" ? "复制" : "Copy";
      wrap.innerHTML = `
        <div class="artifact-toolbar">
          <span class="artifact-label">HTML</span>
          <button class="artifact-btn primary" data-action="run-html" data-label-run="${runLabel}" data-label-stop="${stopLabel}">${runLabel}</button>
          <button class="artifact-btn" data-action="copy" title="${copyLabel}">${copyLabel}</button>
        </div>
        <div class="artifact-source"></div>
        <div class="artifact-render" hidden></div>
      `;
      // Keep the original (hljs-highlighted) <pre> visible by default so
      // the user can read what the AI proposed BEFORE running it.
      const srcSlot = wrap.querySelector(".artifact-source");
      const clonePre = pre.cloneNode(true);
      clonePre.removeAttribute("data-artifact");
      srcSlot.appendChild(clonePre);
      // Stash raw source on the wrapper so Run can read it without
      // re-walking the cloned hljs spans.
      wrap.dataset.htmlSource = src;
      pre.replaceWith(wrap);
    },

    _onArtifactBtn(wrap, action) {
      if (action === "toggle-source") {
        const ren = wrap.querySelector(".artifact-render");
        const src = wrap.querySelector(".artifact-source");
        const showingSource = !src.hasAttribute("hidden");
        if (showingSource) {
          src.setAttribute("hidden", "");
          if (ren) ren.removeAttribute("hidden");
        } else {
          src.removeAttribute("hidden");
          if (ren) ren.setAttribute("hidden", "");
        }
      } else if (action === "copy") {
        // Copy the original source. For HTML artifacts the raw source lives
        // on dataset; for Mermaid we re-read from the hidden source <pre>.
        const raw = wrap.dataset.htmlSource
                    || (wrap.querySelector(".artifact-source code")?.textContent ?? "");
        try {
          navigator.clipboard.writeText(raw);
          this.toast && this.toast(this.lang === "zh" ? "已复制" : "Copied",
                                    "success", 1200);
        } catch (e) {
          this.toast && this.toast("Clipboard error", "error");
        }
      } else if (action === "run-html") {
        this._toggleHtmlIframe(wrap);
      }
    },

    _toggleHtmlIframe(wrap) {
      const btn = wrap.querySelector('[data-action="run-html"]');
      const ren = wrap.querySelector(".artifact-render");
      const src = wrap.querySelector(".artifact-source");
      const running = !ren.hasAttribute("hidden");
      if (running) {
        // Tear down: blank the iframe content so any timers / fetches in
        // the AI-supplied HTML stop, then hide the render panel.
        ren.innerHTML = "";
        ren.setAttribute("hidden", "");
        src.removeAttribute("hidden");
        if (btn) btn.textContent = btn.dataset.labelRun || "Run";
        btn.classList.add("primary");
        return;
      }
      // Mount: build a fresh sandboxed iframe. CRITICAL security notes:
      //   - sandbox WITHOUT allow-same-origin → iframe gets a null origin,
      //     cannot read parent DOM, cookies, or localStorage. Any
      //     <script>parent.alert()</script> attack is blocked.
      //   - NO allow-top-navigation → AI-supplied HTML can't redirect
      //     the whole window.
      //   - srcdoc (not src) → no external network resource by URL; only
      //     what the AI literally wrote in the block.
      //   - allow-popups-to-escape-sandbox lets target=_blank links open
      //     in a real tab (which IS sandbox-free) — convenient for demos.
      const html = wrap.dataset.htmlSource || "";
      const iframe = document.createElement("iframe");
      iframe.className = "artifact-iframe";
      iframe.setAttribute("sandbox", "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms");
      iframe.srcdoc = html;
      ren.innerHTML = "";
      ren.appendChild(iframe);
      ren.removeAttribute("hidden");
      src.setAttribute("hidden", "");
      if (btn) {
        btn.textContent = btn.dataset.labelStop || "Stop";
        btn.classList.remove("primary");
      }
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
          if (navigator.clipboard && navigator.clipboard.writeText) {
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
      if (q.length < 2) {
        // Don't full clearSearch() here — that resets `searchQ = ""` and
        // breaks IME composition on mobile (observed 2026-05-28 on iOS
        // Chinese pinyin: every keystroke triggered 300ms-debounced
        // doSearch which saw length<2 and wiped the input mid-composition
        // → user thinks "can only type numbers"). Just exit search mode
        // and drop hits; leave the input value alone so the IME keeps
        // its session and the user can finish typing a Chinese word.
        this.searchMode = false;
        this.searchHits = [];
        this.grepHits = [];
        this.searchTruncated = false;
        this.grepTruncated = false;
        this.searching = false;
        return;
      }
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
      // Multi-file picker: upload all selected files in parallel to the
      // archive root and refresh the tree ONCE, mirroring onPreviewDrop.
      // Previously this read files[0] only, so picking several files via
      // the upload button silently uploaded just the first one.
      const files = Array.from(ev.target.files || []);
      if (!files.length) return;
      const results = await Promise.allSettled(
        files.map(f => this._uploadFileQuiet("", f))
      );
      const ok = results.filter(r => r.status === "fulfilled" && r.value).length;
      const failed = results.length - ok;
      this.reloadTree();
      if (failed && !ok) {
        this.toast(this.lang === "zh"
          ? `${failed} 个文件上传失败`
          : `${failed} file(s) failed to upload`, "error", 3500);
      } else if (failed) {
        this.toast(this.lang === "zh"
          ? `已上传 ${ok} 个，${failed} 个失败`
          : `Uploaded ${ok}, ${failed} failed`, "warn", 3500);
      } else if (ok === 1) {
        this.toast(this.lang === "zh"
          ? `已上传 ${files[0].name}`
          : `Uploaded ${files[0].name}`, "success", 2200);
      } else {
        this.toast(this.lang === "zh"
          ? `已上传 ${ok} 个文件`
          : `Uploaded ${ok} files`, "success", 2200);
      }
      ev.target.value = "";
    },
    async uploadFileTo(dirPath, file) {
      const fd = new FormData();
      fd.append("path", dirPath);
      fd.append("file", file);
      let r;
      try {
        r = await fetch("/api/files/upload", { method: "POST", headers: this.hdr(), body: fd });
      } catch (e) { this.errToast("upload", String((e && e.message) || e)); return; }
      if (r.ok) {
        delete this.childCache[dirPath];
        this.reloadTree();
        const data = await r.json().catch(() => ({}));
        // Backend trashes any same-name file it replaced; tell the user so a
        // silent overwrite isn't mistaken for a clean upload.
        if (data.replaced_trash_id) {
          this.toast(this.t("toast.uploaded_replaced", { name: file.name }), "success");
        } else {
          this.toast(this.t("toast.uploaded_to", { name: file.name, dir: dirPath || "" }), "success");
        }
      } else this.errToast("upload", await r.text());
    },
    // Custom MIME so tree-internal drags don't collide with OS file drops.
    // Reading getData with this type during dragover would force a stale
    // permissions roundtrip; we use ev.dataTransfer.types.includes() to
    // detect an internal drag without actually pulling the payload until
    // drop fires.
    _DRAG_MIME_INTERNAL: "application/x-muselab-path",

    onTreeNodeDragStart(ev, n) {
      // Stamp both our custom mime (used in onDrop to know "this is a
      // tree-internal move, not an OS upload") and text/plain (broad
      // compatibility — some browsers strip custom types in certain
      // scenarios). text/plain doubles as fallback.
      ev.dataTransfer.setData(this._DRAG_MIME_INTERNAL, n.path);
      ev.dataTransfer.setData("text/plain", n.path);
      ev.dataTransfer.effectAllowed = "move";
      this._dragSrcPath = n.path;
    },
    // ===== long-press → context menu (touch) =====
    // HTML5 drag-and-drop is dead on touch and iOS Safari hijacks long-press
    // for its own text callout, so touch users had no way to reach row actions
    // except the ⋯ kebab. We run our own ~500ms timer off touchstart and pop
    // the exact same ctx menu. Guard rails (learned from the chat-tab long-
    // press that was ripped out for eating taps):
    //   - touchmove past a small threshold = the user is scrolling → cancel.
    //   - when the press fires, touchend calls preventDefault() so the
    //     synthetic click is never generated. That keeps the row from also
    //     toggling AND stops @click.away from insta-closing the menu we just
    //     opened. We deliberately do NOT leave a sticky "suppress next click"
    //     flag around — that was the exact bug that ate later legit taps.
    _lpTimer: null,
    _lpFired: false,
    _lpStart: null,
    onTreeTouchStart(ev, n) {
      if (this.isPointerDevice) return;           // mouse devices use real DnD
      const t = ev.touches && ev.touches[0];
      if (!t) return;
      this._lpFired = false;
      this._lpStart = { x: t.clientX, y: t.clientY };
      clearTimeout(this._lpTimer);
      this._lpTimer = setTimeout(() => {
        this._lpFired = true;
        navigator.vibrate?.(15);                  // subtle haptic where supported
        this.openCtxMenu({ clientX: this._lpStart.x, clientY: this._lpStart.y }, n);
      }, 500);
    },
    onTreeTouchMove(ev) {
      if (!this._lpStart) return;
      const t = ev.touches && ev.touches[0];
      if (!t) return;
      if (Math.abs(t.clientX - this._lpStart.x) > 10
          || Math.abs(t.clientY - this._lpStart.y) > 10) {
        clearTimeout(this._lpTimer);              // it's a scroll, not a press
      }
    },
    onTreeTouchEnd(ev) {
      clearTimeout(this._lpTimer);
      if (this._lpFired) {
        // Swallow the synthetic click that would follow this touchend.
        ev.preventDefault();
        this._lpFired = false;
      }
      this._lpStart = null;
    },
    onTreeNodeDragOver(ev, n) {
      // Target dir = the node itself when it's a folder, or its parent
      // directory when it's a file. Dropping onto a file lands the
      // item next to that file (matches Finder / VSCode behavior).
      const targetDir = n.is_dir
        ? n.path
        : n.path.split("/").slice(0, -1).join("/");
      const src = this._dragSrcPath || "";
      // Block illegal targets: dropping onto itself, into its own
      // subtree, or onto something already in the same parent dir
      // (would be a no-op rename).
      if (src) {
        const srcParent = src.split("/").slice(0, -1).join("/");
        if (src === targetDir
            || (targetDir + "/").startsWith(src + "/")
            || srcParent === targetDir) {
          ev.dataTransfer.dropEffect = "none";
          return;
        }
      }
      // Highlight the target *directory* row so the user sees where
      // the drop will land — for file targets this means the parent
      // dir lights up, not the file itself.
      this.dragOver = targetDir;
      ev.dataTransfer.dropEffect = "move";
    },
    async onDrop(ev, n) {
      this.dragOver = "";
      const wasSrc = this._dragSrcPath;
      this._dragSrcPath = null;
      // Same dir-resolution rule as dragover: folder → self, file → parent.
      const targetDir = n.is_dir
        ? n.path
        : n.path.split("/").slice(0, -1).join("/");

      // Tree-internal drag → move via /api/files/rename. We check
      // dataTransfer.types first so we don't accidentally trip on plain
      // text from elsewhere on the page.
      const types = Array.from(ev.dataTransfer?.types || []);
      const isInternal = types.includes(this._DRAG_MIME_INTERNAL);
      if (isInternal) {
        const src = ev.dataTransfer.getData(this._DRAG_MIME_INTERNAL) || wasSrc;
        await this.moveTreeItem(src, targetDir);
        return;
      }

      // OS file upload — dropping onto a file uploads into that file's
      // parent dir (same dir-resolution as internal moves). Same
      // parallel-upload + batched-refresh pattern as onPreviewDrop so
      // a multi-file drop onto a tree node doesn't serialize.
      const files = Array.from(ev.dataTransfer?.files || []);
      if (!files.length) return;
      const results = await Promise.allSettled(
        files.map(f => this._uploadFileQuiet(targetDir, f))
      );
      const ok = results.filter(r => r.status === "fulfilled" && r.value).length;
      const failed = results.length - ok;
      this.reloadTree();
      const intoLabel = targetDir ? `/${targetDir}` : "/";
      if (failed && !ok) {
        this.toast(this.lang === "zh"
          ? `${failed} 个文件上传失败`
          : `${failed} file(s) failed`, "error", 3500);
      } else if (failed) {
        this.toast(this.lang === "zh"
          ? `已上传 ${ok} 个到 ${intoLabel}，${failed} 个失败`
          : `Uploaded ${ok} to ${intoLabel}, ${failed} failed`, "warn", 3500);
      } else if (ok === 1) {
        this.toast(this.lang === "zh"
          ? `已上传 ${files[0].name} 到 ${intoLabel}`
          : `Uploaded ${files[0].name} to ${intoLabel}`, "success", 2200);
      } else {
        this.toast(this.lang === "zh"
          ? `已上传 ${ok} 个文件到 ${intoLabel}`
          : `Uploaded ${ok} files to ${intoLabel}`, "success", 2200);
      }
    },
    async moveTreeItem(srcPath, targetDir) {
      if (!srcPath) return;
      const srcName = srcPath.split("/").pop();
      const srcParent = srcPath.split("/").slice(0, -1).join("/");
      // Same-parent drop = no-op (user dragged a file inside its own
      // directory without changing anything).
      if (srcParent === targetDir) return;
      // Dropping a directory onto itself or anywhere in its own subtree
      // would create a cycle. Backend would 422 on rename but we'd
      // rather not even attempt it — feedback is faster client-side.
      if (srcPath === targetDir
          || (targetDir + "/").startsWith(srcPath + "/")) {
        this.toast(this.lang === "zh"
          ? "不能把目录拖进自己的子目录"
          : "Can't move a folder into its own subtree", "warn", 2500);
        return;
      }
      const newPath = targetDir ? `${targetDir}/${srcName}` : srcName;
      const r = await fetch("/api/files/rename", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ src: srcPath, dst: newPath }),
      });
      if (!r.ok) {
        const err = await r.text();
        this.toast((this.lang === "zh" ? "移动失败：" : "Move failed: ") + err,
          "error", 4000);
        return;
      }
      this.toast(this.lang === "zh"
        ? `已移动到 /${targetDir || "(根)"}`
        : `Moved to /${targetDir || "(root)"}`, "success", 2000);
      // Refresh the tree and reroute selected/preview if we just moved
      // the currently-open file.
      if (this.selected === srcPath) this.selected = newPath;
      const openTab = this.tabs.find(t => t.path === srcPath);
      if (openTab) openTab.path = newPath;
      await this.reloadTree();
    },
    async mkdirPrompt() {
      const zh = this.lang === "zh";
      const name = await this.prompt({
        title: zh ? "新建目录" : "New directory",
        body: zh ? "输入相对根的路径，例如 archives/2026"
                 : "Path relative to root, e.g. archives/2026",
        placeholder: "archives/2026",
      });
      if (!name) return;
      const r = await fetch("/api/files/mkdir", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ path: name }),
      });
      if (r.ok) { this.reloadTree(); this.toast(this.t("toast.created"), "success"); }
      else this.errToast("generic", await r.text());
    },

    // ===== edit =====
    isEditable(path) {
      if (!path) return false;
      const name = path.split("/").pop().toLowerCase();
      const ext = name.includes(".") ? name.split(".").pop() : name;
      return EDITABLE_EXT.has(ext);
    },

    // Files-pane visibility toggle wired to the preview-pane header's chevron
    // button. Special-cased for fullscreen: when the preview pane is in
    // desktop-fullscreen mode (this.desktopFullPane === "preview"), the files
    // pane is already hidden by the fullscreen layout CSS — so tapping the
    // chevron in that state should EXIT fullscreen (not just flip a flag
    // that does nothing visible). We also force leftOpen=true on the exit
    // path so the user lands on a layout where the files pane IS visible,
    // matching the affordance the chevron just promised them.
    // 2026-05-28 user request: "全屏预览模式下，点击 隐藏文件区按钮，
    // 应该要自动退出全屏预览".
    toggleFilesPane() {
      if (this.desktopFullPane) {
        this.desktopFullPane = "";
        this.leftOpen = true;
      } else {
        this.leftOpen = !this.leftOpen;
      }
      this.savePrefs();
    },

    // Chat (Muse) pane visibility toggle, wired to the preview-pane header's
    // right-hand chevron. Mirrors toggleFilesPane's fullscreen special-case:
    // when the preview pane is fullscreen (desktopFullPane === "preview") the
    // Muse pane is already hidden by the fullscreen layout, so tapping
    // "hide Muse" would flip a flag that changes nothing visible. Instead we
    // EXIT fullscreen and force the Muse pane open, landing the user on a
    // layout where Muse IS visible — matching the affordance the button
    // promises. 2026-05-29 user request: "预览区全屏的情况下，点击隐藏 Muse
    // 按钮，应该自动退出全屏".
    toggleChatPane() {
      if (this.desktopFullPane) {
        this.desktopFullPane = "";
        this.rightOpen = true;
      } else {
        this.rightOpen = !this.rightOpen;
      }
      this.savePrefs();
    },

    layoutStyle() {
      // Desktop fullscreen on one pane — collapse to a single 1fr column
      // and let the CSS rule for [data-desktop-full="..."] handle hiding
      // the others. Skips the persisted leftWidth/rightWidth so the
      // chosen pane truly fills the viewport (no 280px ghost gutter).
      if (this.desktopFullPane) {
        return { gridTemplateColumns: "1fr" };
      }
      // 动态算 template，匹配实际渲染的元素数。否则 x-show 隐藏 resizer 时
      // 元素被移出 grid，剩余 children 错位填入空闲 column,导致右 resizer
      // 拿到 1fr 宽 → 鼠标 hover 它整片变成 accent 色。
      // Clamp persisted widths to 2/3 of viewport so window-shrink doesn't
      // leave one pane wider than the viewport (which would collapse the
      // center chat to 0 and lock the user out). Mirrors the drag clamp.
      const maxW = Math.max(220, Math.floor(window.innerWidth * 2 / 3));
      const cols = [];
      if (this.leftOpen) cols.push(Math.min(this.leftWidth, maxW) + "px", "4px");
      cols.push("1fr");
      if (this.rightOpen) cols.push("4px", Math.min(this.rightWidth, maxW) + "px");
      return { gridTemplateColumns: cols.join(" ") };
    },
    // Toggle desktop fullscreen for a pane. Click the same pane's
    // button again to exit, or click the other pane's button to swap
    // (e.g. fullscreen-preview → click chat-pane maximize → fullscreen-
    // chat). Mobile (single-pane @media) ignores desktopFullPane entirely.
    toggleDesktopFull(pane) {
      const next = (this.desktopFullPane === pane) ? "" : pane;
      this.desktopFullPane = next;
      // Force the target pane open — otherwise "fullscreen chat" with
      // rightOpen=false would land on a blank screen (chat is hidden by
      // `.pane-hidden` regardless of the data-desktop-full rules).
      // Preview shares the center column so always rendered; only the
      // chat side needs rightOpen forced. Skipped on exit (next === "")
      // to preserve the user's prior leftOpen/rightOpen layout.
      if (next === "chat") this.rightOpen = true;
      // FIX ⑥ (2026-05-30 follow-up): persist the fullscreen state so a
      // refresh restores it. There's no $watch("desktopFullPane"), and the
      // sibling exit paths (toggleFilesPane / toggleChatPane) already call
      // savePrefs — this entry/exit path was the one gap, so toggling
      // fullscreen via the maximize button never stuck across a reload.
      this.savePrefs();
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
      // Bounds.
      // Max: 2/3 of the viewport — leaves at least 1/3 for the center
      // chat. Big-monitor users get serious side-pane real estate.
      // Hide/show hysteresis: a single threshold would jitter when the
      // user wiggled around it. So two thresholds with a 20px gap —
      // dragging below HIDE_AT collapses the pane, dragging back above
      // SHOW_AT re-opens it AT THAT NEW WIDTH (not the pre-drag size).
      // Crucially: we don't end the drag on hide. The user can keep
      // dragging — pulling outward past SHOW_AT reopens the pane mid-
      // drag, so you can shrink-then-recover with one continuous gesture.
      const HIDE_AT = 200;
      const SHOW_AT = 220;
      const maxW = Math.floor(window.innerWidth * 2 / 3);
      const isLeft = which === "left";
      const onMove = (e) => {
        const delta = isLeft ? (e.clientX - startX) : (startX - e.clientX);
        const targetW = startW + delta;
        const isOpenNow = isLeft ? this.leftOpen : this.rightOpen;
        if (isOpenNow && targetW < HIDE_AT) {
          // Going-down threshold crossed. Hide and remember the pre-drag
          // width so chevron-reopen later restores that size (rather than
          // showing a sliver). Drag continues so you can pull back out.
          if (isLeft) { this.leftWidth = startW; this.leftOpen = false; }
          else        { this.rightWidth = startW; this.rightOpen = false; }
          return;
        }
        if (!isOpenNow && targetW >= SHOW_AT) {
          // Going-up threshold crossed during the same drag — re-open.
          if (isLeft) this.leftOpen = true;
          else        this.rightOpen = true;
        }
        // Only resize when actually open (post-show transition counts).
        const reopened = !isOpenNow && targetW >= SHOW_AT;
        if (isOpenNow || reopened) {
          const w = Math.max(SHOW_AT, Math.min(maxW, targetW));
          if (isLeft) this.leftWidth = w;
          else        this.rightWidth = w;
        }
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

    // ===== Editor unsaved-changes guard =====
    // Single source of truth for "the editor has edits that aren't saved".
    // CodeMirror keeps cmStatus.dirty in sync (mountCM); the textarea
    // fallback (CM init failure) has no cmStatus, so we also compare
    // editText against the last-saved rawText. Either signal means dirty.
    _editorDirty() {
      if (!this.editing) return false;
      if (this.cmStatus && this.cmStatus.dirty) return true;
      return String(this.editText || "") !== String(this.rawText || "");
    },
    // Returns true if it's safe to leave/replace the current editor buffer:
    // not dirty, or the user confirmed discarding. Native confirm() is used
    // intentionally — it's synchronous, so callers in non-async paths (ESC
    // handler, closeTab) can branch on the result without awaiting.
    _confirmLoseEdits() {
      if (!this._editorDirty()) return true;
      return window.confirm(this.lang === "zh"
        ? "当前文件有未保存的改动，确定要放弃吗？"
        : "You have unsaved changes. Discard them?");
    },
    // Attach/detach the beforeunload handler to match the current dirty state.
    // Idempotent: only touches the listener when the desired state changes,
    // so it never leaves a stale handler attached (which would break bfcache).
    _syncBeforeUnloadGuard() {
      const wantGuard = this._editorDirty();
      if (wantGuard && !this._beforeUnloadFn) {
        this._beforeUnloadFn = (e) => {
          // Re-check at fire time — state may have changed since attach.
          if (!this._editorDirty()) return;
          e.preventDefault();
          // Legacy browsers need returnValue set to trigger the native prompt;
          // the string itself is ignored by modern browsers.
          e.returnValue = "";
          return "";
        };
        window.addEventListener("beforeunload", this._beforeUnloadFn);
      } else if (!wantGuard && this._beforeUnloadFn) {
        window.removeEventListener("beforeunload", this._beforeUnloadFn);
        this._beforeUnloadFn = null;
      }
    },
    async toggleEdit() {
      if (this.editing) {
        if (!this._confirmLoseEdits()) return;
        this.editing = false;
        return;
      }
      // Entering edit mode hides the rendered .markdown/pre.text containers our
      // find marks live in — close find so it can't point at a hidden DOM.
      if (this.previewFind.open) this.closePreviewFind();
      // 进入编辑：确保 rawText 已加载（html/img/pdf 走 raw 模式时没 fetch 文本）
      if (!this.rawText || this.previewMode === "html" || this.previewMode === "pdf" || this.previewMode === "img") {
        const r = await fetch("/api/files/read?path=" + encodeURIComponent(this.selected), { headers: this.hdr() });
        if (!r.ok) {
          this.errToast("read", this.lang === "zh"
                                  ? "可能是二进制或太大 — " + (await r.text())
                                  : "binary or too large — " + (await r.text()));
          return;
        }
        this.rawText = await r.text();
      }
      this.editText = this.rawText;
      this.editing = true;
      // Editing is a deliberate commitment to the file — pin its tab so it
      // doesn't get recycled out from under the editor by the next preview.
      this.pinTab(this.selected);
    },
    async saveEdit() {
      let r;
      try {
        r = await fetch("/api/files/write", {
          method: "PUT",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ path: this.selected, content: this.editText }),
        });
      } catch (e) {
        // Keep editing=true so the unsaved buffer is preserved for retry.
        this.errToast("save", String((e && e.message) || e));
        return;
      }
      if (r.ok) {
        this.rawText = this.editText;
        // Keep the preview cache in step with the just-saved body. For md/text
        // we can refresh in place; other modes (xlsx/html/img/pdf) just drop
        // the stale entry so the next switch-back re-fetches.
        this._previewCacheDel(this.selected);
        if (this.previewMode === "md") {
          this.renderedMd = this.mdRender(this.rawText);
          if (this.rawText.length <= this.PREVIEW_CACHE_MAX_CHARS) {
            this._previewCacheSet(this.selected, {
              mode: "md", rawText: this.rawText, renderedMd: this.renderedMd,
            });
          }
          this.$nextTick(() => this.highlightCode(".markdown"));
        } else if (this.previewMode === "text"
                   && this.rawText.length <= this.PREVIEW_CACHE_MAX_CHARS) {
          this._previewCacheSet(this.selected, {
            mode: "text", rawText: this.rawText, previewLang: this.previewLang,
          });
        }
        // Bump previewVersion so HTML / PDF / image iframes pick up the new
        // file content. Without this, iframes keep showing the stale render
        // (browser disk cache + same URL) until the user hard-refreshes —
        // the issue was visible when editing a html report styled in dark
        // mode to light mode: editor saved, preview iframe still showed dark.
        this.previewVersion = Date.now();
        this.editing = false;
        this.toast(this.t("toast.saved"), "success");
      } else this.errToast("save", await r.text());
    },

    // ===== @ mention =====
    insertFileMention(path) {
      const mention = "@" + path + " ";
      this.input = (this.input || "") + (this.input && !this.input.endsWith(" ") ? " " : "") + mention;
      if (this.$refs.chatInput) this.$refs.chatInput.focus();
      this.toast(this.t("toast.mention_added", { path }), "success", 1500);
      // Mobile: @ mention is a chat-side action, jump to the chat pane
      if (this._isMobileLayout()) this.mobileTab = "chat";
    },
    autoGrow(ta) {
      // Grow to fit content up to max. The hard problem: iOS Safari
      // on touch forces font-size: 16px (anti-zoom) and reports a
      // scrollHeight a few px above the textarea's min-height for
      // single-line input — depending on subpixel rendering, line-
      // height rounding, etc, it can be min+1 through min+4. So the
      // naive "if scrollHeight > min-height, grow" fires on the very
      // first character typed and keeps the textarea inflated forever.
      //
      // Real fix: distinguish "single line" from "multi line" by
      // checking whether scrollHeight is closer to 1× or ≥ 2× the
      // min-height. Below 1.4×min — single line, clear inline height
      // and let CSS handle it. At or above 1.4×min — content has
      // genuinely wrapped to a second line, expand inline to fit.
      // 1.4 is comfortably between 1× (single line, ~min) and 2×
      // (two lines, ~2 × line-height + padding) on both PC and mobile.
      ta.style.height = "auto";
      const sh = ta.scrollHeight;
      const max = 200;
      const minH = parseFloat(getComputedStyle(ta).minHeight) || 34;
      if (sh < minH * 1.4) {
        ta.style.height = "";          // single line: hand control back to CSS
      } else {
        ta.style.height = Math.min(sh, max) + "px";
      }
    },

    // Ctrl/Cmd+X with NO active selection = cut the whole current line
    // (VSCode/Sublime behavior). Native textarea only cuts a selection, so
    // we synthesize it: select the line (incl. its newline) then execCommand
    // 'cut' — which both writes to the clipboard AND keeps the native undo
    // stack intact (Ctrl+Z still restores the line). If a selection already
    // exists we do nothing and let the browser's native cut run.
    onCutLine(ev) {
      const el = ev && ev.target;
      if (!el || el.tagName !== "TEXTAREA") return;
      if (el.selectionStart !== el.selectionEnd) return;   // has selection → native cut
      const val = el.value;
      const pos = el.selectionStart;
      let lineStart = val.lastIndexOf("\n", pos - 1) + 1;  // 0 when on first line
      let lineEnd = val.indexOf("\n", pos);
      let rangeEnd;
      if (lineEnd === -1) {
        // Last line (no trailing newline): also consume the preceding newline
        // so we don't leave a dangling blank line behind.
        rangeEnd = val.length;
        if (lineStart > 0) lineStart -= 1;
      } else {
        rangeEnd = lineEnd + 1;                            // include trailing newline
      }
      ev.preventDefault();
      el.setSelectionRange(lineStart, rangeEnd);
      let ok = false;
      try { ok = document.execCommand("cut"); } catch (_e) { ok = false; }
      if (!ok) {
        // Fallback for browsers that block execCommand('cut'): clipboard API
        // + manual splice. Loses the native undo entry but still functional.
        const cut = val.slice(lineStart, rangeEnd);
        try { navigator.clipboard && navigator.clipboard.writeText(cut); } catch (_e) {}
        el.value = val.slice(0, lineStart) + val.slice(rangeEnd);
        el.setSelectionRange(lineStart, lineStart);
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
      this.autoGrow(el);
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
          // /clear permanently DELETEs the session (CLI JSONL + sidecar +
          // uploaded attachments), no trash, no undo — despite the CLI-muscle-
          // memory expectation that /clear just resets context. Gate it behind
          // the same danger confirm the UI delete button uses.
          const zh = this.lang === "zh";
          const ok = await this.confirm({
            title: zh ? "删除当前会话" : "Delete current session",
            body: zh
              ? "这会永久删除当前会话（含上传的附件），不进垃圾桶、无法恢复，然后新建一个空会话。确定吗？"
              : "This permanently deletes the current session (including uploaded attachments) — no trash, no undo — then starts a fresh one. Continue?",
            danger: true,
            okText: zh ? "删除并新建" : "Delete & start fresh",
          });
          if (!ok) return;
          const oldId = this.currentId;
          // Token via header (not query) so it never lands in access / proxy
          // logs or browser history. /reset accepts header-or-query backend-side.
          try {
            await fetch(`/api/chat/reset?session_id=${encodeURIComponent(oldId)}`,
                         { method: "POST", headers: this.hdr() });
            await fetch(`/api/chat/sessions/${oldId}`, { method: "DELETE", headers: this.hdr() });
          } catch (e) {
            // Network failure mid-clear — surface it rather than throwing an
            // unhandledrejection and leaving the user staring at the old session.
            this.errToast("delete", String((e && e.message) || e));
            return;
          }
          await this.refreshSessions();
          // Drop the old session's tab + cached state, then open a fresh one
          // in its slot. newSession() handles tabState + openTabIds + switch.
          const oldStreamState = this.tabState[oldId];
          if (oldStreamState) {
            if (oldStreamState.es) { try { oldStreamState.es.close(); } catch {} }
            if (oldStreamState._streamTimer) clearInterval(oldStreamState._streamTimer);
            delete this.tabState[oldId];
          }
          this._clearSessionWarnFlags(oldId);
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
              .map(s => {
                const turns = s.turn_count ?? Math.floor((s.message_count || 0) / 2);
                return `- **${s.name}** (${turns}t, ${s.id.slice(0, 8)})`;
              }).join("\n");
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
    // The 7 preset skills shipped under skills/. Only labels are bespoke; the
    // inserted prompt comes from _skillSeed() so every skill reads identically.
    SKILL_TRIGGERS: [
      { name: "web-search",         label_zh: "查时效数据",   label_en: "live web fact" },
      { name: "markdown-formatter", label_zh: "整理 markdown", label_en: "clean markdown" },
      { name: "mermaid-helper",     label_zh: "画架构图",     label_en: "draw a diagram" },
      { name: "code-reviewer",      label_zh: "code review",  label_en: "code review" },
      { name: "citation-formatter", label_zh: "格式化引用",   label_en: "format a citation" },
      { name: "task-decomposer",    label_zh: "拆任务",       label_en: "decompose a goal" },
      { name: "summary-distiller",  label_zh: "长文摘要",     label_en: "summarize" },
    ],

    // Single source of truth for the prompt we seed when a user picks a skill
    // (onboarding chip OR a skill card's "Try this"). Naming the skill
    // explicitly ("用 X skill 帮我" / "Use the X skill to") makes the model read
    // it as an instruction to INVOKE that skill; the bare "用 X 帮我" form
    // triggered less reliably. Kept identical across every entry point so the
    // wording stays consistent everywhere.
    _skillSeed(name) {
      return this.lang === "zh" ? `用 ${name} skill 帮我：` : `Use the ${name} skill to: `;
    },

    skillSuggestions() {
      const loaded = new Set(this.settings.skills.map(s => s.name));
      const lang = this.lang;
      return this.SKILL_TRIGGERS
        .filter(t => loaded.has(t.name))
        .map(t => ({
          name: t.name,
          label: lang === "zh" ? t.label_zh : t.label_en,
          prompt: this._skillSeed(t.name),   // uniform seed across all skills
          description: lang === "zh" ? "触发 skill: " + t.name : "Triggers skill: " + t.name,
        }))
        .slice(0, 6);
    },

    // Filter the Settings → Skills grid by free-text search (name /
    // description / plugin source). Case-insensitive substring match.
    filteredSkills() {
      const q = (this.settings.skillFilter || "").trim().toLowerCase();
      if (!q) return this.settings.skills;
      return this.settings.skills.filter(s => {
        const hay = (s.name + " " + (s.description || "") + " " + (s.source || ""))
          .toLowerCase();
        return hay.includes(q);
      });
    },

    // "Try this" button on a skill card. Seeds the chat input with a uniform
    // skill-invocation prompt (see _skillSeed) and focuses it so the user can
    // fill in the rest. Closes the Settings modal first so the chat is visible.
    trySkill(sk) {
      const prompt = this._skillSeed(sk.name);
      // Close settings modal if open
      if (this.settings && this.settings.show) this.settings.show = false;
      // Close skills drawer if open
      if (this.skillsDrawerOpen) this.skillsDrawerOpen = false;
      this.input = prompt;
      this.$nextTick(() => {
        const ta = this.$refs.chatInput;
        if (ta) {
          ta.focus();
          // Put cursor at end so user can keep typing
          ta.selectionStart = ta.selectionEnd = ta.value.length;
          this.autoGrow(ta);
        }
      });
    },

    // Skills drawer (chat-input 🧩 entry). Reactive boolean so Alpine
    // re-renders on toggle.
    skillsDrawerOpen: false,
    toggleSkillsDrawer() {
      this.skillsDrawerOpen = !this.skillsDrawerOpen;
      // Refresh skills list each time the drawer opens — picks up newly
      // installed Claude Code skills without requiring a settings open.
      if (this.skillsDrawerOpen) this.loadSkills();
    },
    // MCP drawer (chat-input 🔌 entry). Same drawer chrome as skills
    // drawer; mirrors the read-only view that used to live as a tiny
    // top-bar badge — now expanded into a full card list showing each
    // MCP's source (muselab.json / ~/.claude.json / .mcp.json) and
    // enabled state. Editing still lives in Settings → MCP; this drawer
    // is a one-glance "what tools does Muse have right now?" surface.
    mcpDrawerOpen: false,
    toggleMcpDrawer() {
      this.mcpDrawerOpen = !this.mcpDrawerOpen;
      // Refresh on open so newly-added entries (from any source) show up
      // without requiring Settings → MCP visit + reload. fetchMcp is the
      // existing loader used by the (now-removed) top-bar badge.
      if (this.mcpDrawerOpen && typeof this.fetchMcp === "function") {
        this.fetchMcp();
      }
    },
    // Friendly label for an MCP's source — used as the card's `title`
    // attr (hover tooltip) so curious users can still trace where a
    // server came from, but the source isn't a chip cluttering every
    // card. Keys come from backend _load_mcp_merged's `_source` field.
    mcpSourceLabel(src) {
      const zh = this.lang === "zh";
      const m = {
        "muselab":              zh ? "muselab 自有 mcp.json" : "muselab mcp.json",
        "claude_user_global":   zh ? "Claude Code 用户全局（~/.claude.json）" : "Claude Code user-global (~/.claude.json)",
        "claude_user_settings": zh ? "Claude Code 用户设置（~/.claude/settings.json）" : "Claude Code user-settings (~/.claude/settings.json)",
        "claude_user_project":  zh ? "Claude Code 项目级（~/.claude.json 的 projects）" : "Claude Code per-project (~/.claude.json projects)",
        "archive_project":      zh ? "档案根 .mcp.json" : "archive root .mcp.json",
      };
      return m[src] || (src || "unknown");
    },
    // "Try" on an MCP card — same UX shape as trySkill: pre-fills the
    // chat input with a seed prompt mentioning this MCP server by name,
    // closes the drawer, focuses the textarea. Model then picks an
    // appropriate tool from that server (e.g. for "gmail" it could
    // call mcp__gmail__list_messages on the next turn).
    // Disabled MCPs can't be tried — the SDK won't mount them this
    // session, so the prompt would just confuse the model.
    tryMcp(s) {
      if (!s || s.disabled) return;
      const zh = this.lang === "zh";
      // Hand-crafted prompts for the well-known MCPs we ship; everything
      // else gets a generic seed naming the server.
      const handcrafted = {
        gmail: zh ? "用 gmail MCP 帮我看下最近 10 封未读邮件，简要列出标题和发件人。"
                  : "Use the gmail MCP to list my 10 most recent unread emails — just subject + sender.",
        fetch: zh ? "用 fetch MCP 抓一下 https://news.ycombinator.com 首页标题。"
                  : "Use the fetch MCP to grab the front-page titles from https://news.ycombinator.com.",
      };
      const prompt = handcrafted[s.name]
        || (zh ? `用 ${s.name} MCP 帮我：` : `Use the ${s.name} MCP to: `);
      this.mcpDrawerOpen = false;
      if (this.settings && this.settings.show) this.settings.show = false;
      this.input = prompt;
      this.$nextTick(() => {
        const ta = this.$refs.chatInput;
        if (ta) {
          ta.focus();
          ta.selectionStart = ta.selectionEnd = ta.value.length;
          this.autoGrow(ta);
        }
      });
    },
    async loadSkills() {
      try {
        const r = await fetch("/api/settings/skills", { headers: this.hdr() });
        if (r.ok) {
          const data = await r.json();
          this.settings.skills = data.skills || [];
        }
      } catch (e) { /* network / first-boot — silent fail */ }
    },

    onboardingPrompts() {
      // Inspire prompts come from window.MUSELAB_INSPIRE_PROMPTS (a 30+
      // tagged bilingual list). Filter to those whose tags either match
      // an existing archive subdir or are tagged "general" (always-on).
      // Shuffle and slice — gives a fresh-feeling sample each time the
      // chat-empty state renders. _inspireSeed is bumped by
      // shuffleInspirePrompts() so the user can ask for "another round"
      // without reloading.
      const list = window.MUSELAB_INSPIRE_PROMPTS || [];
      const sp = this.contextInfo.subdir_present || {};
      const lang = this.lang;
      const eligible = list.filter(p => {
        if (!p.tags || p.tags.length === 0) return true;
        return p.tags.some(t => t === "general" || sp[t]);
      });
      // Seeded shuffle (Fisher-Yates with a tiny linear-congruential PRNG
      // seeded by _inspireSeed) — keeps the chosen set stable as Alpine
      // re-renders during typing, but flips on shuffleInspirePrompts().
      const seed = this._inspireSeed || 1;
      const a = eligible.slice();
      let s = seed;
      for (let i = a.length - 1; i > 0; i--) {
        s = (s * 1664525 + 1013904223) & 0xffffffff;
        const j = Math.abs(s) % (i + 1);
        [a[i], a[j]] = [a[j], a[i]];
      }
      return a.slice(0, 5).map(p => p[lang] || p.zh);
    },
    shuffleInspirePrompts() {
      // Bump the seed so onboardingPrompts() picks a different sample.
      // +1 each time; the LCG inside onboardingPrompts spreads it.
      this._inspireSeed = (this._inspireSeed || 1) + 1;
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

    // 2026-05-23: startProfileIntake removed — 「设置档案」按钮已合并入
    // 「整理档案」(startOrganize). 整理档案 workflow 现在同时覆盖 archive
    // 整理 + CLAUDE.md profile 补全（见 backend/prompts.py CURATOR_SYSTEM_PROMPT
    // 第 3 步 3b 节）。后端 /sessions/profile-intake 端点保留向后兼容，
    // 现在 forward 到 /sessions/organize.

    // 2026-05-24: showWelcomeCard / dismissWelcome removed.
    // The pre-setup "what is muselab + 3 steps" card was replaced by the
    // Muse opener bubble + nine-muses grid (always-visible conversation
    // entry points). _welcomeDismissed key is still read at init for
    // back-compat but no longer drives any UI — safe to leave the
    // localStorage entry in place for existing installs.

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
    // 1.2K / 350 format — concise, intuitive (in / out). Use M for ≥1M, B for ≥1B.
    fmtTokens(n) {
      n = n || 0;
      if (n < 1000) return n.toString();
      if (n < 1_000_000) return (n / 1000).toFixed(n < 10_000 ? 1 : 0) + "K";
      if (n < 1_000_000_000) return (n / 1_000_000).toFixed(2) + "M";
      return (n / 1_000_000_000).toFixed(2) + "B";
    },
    // tokenBadgeText / costBadgeTitle removed 2026-05-22 — the chat-pane-
    // head token badge they fed was deleted at the user's request. Numbers
    // reset on every backend restart so they weren't a reliable usage
    // surface anyway; Settings → 用量看板 is the canonical view. `stats`
    // is still tracked server-side and exposed via /api/chat/usage for
    // anyone integrating muselab into a wider dashboard.
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
    async runCompact(targetSid, opts = {}) {
      // Default to the active session — the manual ctx-ring click + the
      // command palette both want "compact what I'm looking at". The
      // auto-compact path (done event when ctx >= 95%) passes streamSid
      // explicitly so a mid-stream tab switch doesn't end up compacting
      // a different session than the one whose context filled up.
      const sid = targetSid || this.currentId;
      if (!sid) return;
      const st = this.tabState[sid];
      // streaming check is per-target-session, not on `this.streaming`
      // (which mirrors the active tab — wrong source of truth when the
      // call comes from a background stream's done handler).
      if (st && st.streaming) {
        this.toast(this.t("ctx.compact_wait_streaming"), "warn", 2500);
        return;
      }
      // Empty-session guard. The target session's frontend message
      // mirror may be transiently empty (loadSession in flight on
      // background tabs), so fall back to backend's message_count.
      const targetMessages = (st && st.messages) || (sid === this.currentId ? this.messages : []);
      const hasFrontendContent = targetMessages.some(
        m => m.role === "assistant" && m.text);
      const meta = this.sessions.find(s => s.id === sid);
      const backendCount = (meta && meta.message_count) || 0;
      if (!hasFrontendContent && backendCount < 2) {
        this.toast(this.t("ctx.compact_empty"), "warn", 2500);
        return;
      }

      // Confirmation gate — compaction is a 20–60s, history-rewriting
      // action, so a misfired ctx-ring click shouldn't kick it off.
      // Only manual triggers (ctx ring / command palette / error CTA)
      // prompt; the automatic ≥95% safety compact and the 85–94% toast
      // button pass { skipConfirm:true } since they're already user-
      // intended (or deliberately silent to avoid the hard limit).
      if (!opts.skipConfirm) {
        const zh = this.lang === "zh";
        const ok = await this.confirm({
          title: zh ? "压缩对话？" : "Compact session?",
          body: zh
            ? "把当前对话历史归纳成摘要以释放上下文窗口，原始消息仍保留在会话记录里。耗时约 20–60 秒。"
            : "Summarize the conversation so far into a compact summary to free up the context window. Your original messages stay in the session file. Takes about 20–60s.",
          okText: zh ? "压缩" : "Compact",
          cancelText: zh ? "取消" : "Cancel",
        });
        if (!ok) return;
      }

      // Native compact: send "/compact" to CLI via SDK, which writes
      // compact_boundary + isCompactSummary to the session JSONL. Lossless,
      // preserves tool use history, same session ID. Old self-implemented
      // summarize-and-fork is gone — it was lossy and unnecessary once we
      // realized the SDK forwards slash commands to CLI natively.
      // Per-session compact flag. Setting only on `st` means the bottom
      // "📦 压缩对话中…" pending bubble (x-show binds to the current tab's
      // st.compacting) appears only on the session that's actually being
      // compacted — switching tabs mid-compact no longer drags the banner
      // along to unrelated tabs.
      const cst = this._ensureTabState(sid);
      cst.compacting = true;
      // A short toast confirms the kick — the bottom pending bubble is what
      // the user actually watches for the full 20–60s window, not the toast.
      this.toast(this.lang === "zh" ? "📦 开始压缩..." : "📦 Compacting…", "info", 2000);
      // Scroll to the bottom (active tab only — scrolling a background
      // tab the user isn't looking at is wasted work).
      if (sid === this.currentId) {
        this.$nextTick(() => this.scrollToBottom(true));
      }

      try {
        const r = await fetch(`/api/chat/sessions/${sid}/native-compact`,
                                { method: "POST", headers: this.hdr() });
        if (!r.ok) {
          const txt = await r.text();
          this.toast((this.lang === "zh" ? "压缩失败：" : "Compact failed: ") + txt, "error", 5000);
          return;
        }
        // Reload the compacted session if it's the active one; on a
        // background tab activateTab will reload it lazily later.
        if (sid === this.currentId) {
          await this.loadSession(sid);
        }
        await this.refreshSessions();
        // Refresh ctx-meter — sessionUsage is only auto-updated on stream
        // 'done' events, so without this the meter shows the pre-compact
        // (large) value until the user sends a new message.
        if (sid === this.currentId) {
          await this._refreshCtxMeter();
        }
        this.toast(this.lang === "zh" ? "📦 压缩完成" : "📦 Compacted", "success", 2000);
      } catch (e) {
        this.toast((this.lang === "zh" ? "压缩失败：" : "Compact failed: ") + e.message, "error", 5000);
      } finally {
        cst.compacting = false;
        // Auto-drain runs against the compacted session, not currentId —
        // a background auto-compact must drain its own queue, not the
        // user's currently-visible tab's queue.
        this.$nextTick(() => this._drainPendingQueue(sid));
        // Refresh ctx-meter for background compacts — when compact finishes
        // on a non-current tab the try-block's _refreshCtxMeter is skipped,
        // so we re-run it here unconditionally (it's cheap and idempotent).
        if (this.currentId === sid) {
          this.$nextTick(() => this._refreshCtxMeter && this._refreshCtxMeter());
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

      // Trigger the @ picker on the LAST @ in the prefix, regardless of what
      // sits before it. Previously we required @ to be at position 0 or
      // preceded by whitespace — that broke mid-sentence usage like
      // "看下 @foo.md 里写的什么" where @ follows a Chinese character.
      // Trade-off: typing an email address ("user@example.com") will also
      // pop the picker; that's accepted since chat input rarely contains
      // emails and the picker auto-hides as soon as a space is typed.
      // 2026-05-28 user request: "任何时候 @ 都弹出".
      const at = text.lastIndexOf("@");
      if (at < 0) { this.mentionShow = false; return; }
      const query = text.slice(at + 1);
      if (/\s/.test(query)) { this.mentionShow = false; return; }
      this.mentionAnchor = at;
      clearTimeout(this._mentionDebounce);
      this._mentionDebounce = setTimeout(() => this.fetchMention(query), 200);
    },
    async fetchMention(q) {
      // Currently open preview tabs — surface them at the top of the picker
      // so the user can quickly @-reference whatever they're already looking
      // at. `this.tabs` is the preview-pane tab strip; each entry has
      // { path, name }. We mark them with `_open: true` so the template can
      // badge them visually.
      const openTabs = (this.tabs || [])
        .filter(t => t && t.path)
        .map(t => ({ path: t.path, name: t.name || t.path.split("/").pop(), _open: true }));

      if (q.length === 0) {
        // Empty query: open files first (most likely what the user wants),
        // then a few root entries to keep the original "browse from root"
        // affordance. Dedupe so an open file at the root doesn't appear
        // twice.
        const root = (await this.fetchChildren("")).slice(0, 8);
        const openPaths = new Set(openTabs.map(t => t.path));
        const rootFresh = root.filter(e => !openPaths.has(e.path));
        this.mentionResults = [...openTabs, ...rootFresh].slice(0, 12);
      } else {
        // With query: filter open tabs first (substring match against name/
        // path), then run the server-side fuzzy search. Merge with open
        // matches up front so "open and matching" wins over "elsewhere in
        // archive and matching".
        const ql = q.toLowerCase();
        const openMatches = openTabs.filter(t =>
          (t.name || "").toLowerCase().includes(ql)
          || (t.path || "").toLowerCase().includes(ql)
        );
        const r = await fetch("/api/files/search?q=" + encodeURIComponent(q) + "&limit=15",
                                { headers: this.hdr() });
        const d = r.ok ? await r.json() : { entries: [] };
        const searchResults = (d.entries || []).filter(e => !e.is_dir);
        const openPaths = new Set(openMatches.map(t => t.path));
        const fresh = searchResults.filter(e => !openPaths.has(e.path));
        this.mentionResults = [...openMatches, ...fresh].slice(0, 15);
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
      // Newline-at-cursor for: Shift+Enter, Ctrl+Enter, Meta+Enter, and
      // touch devices (where bare Enter is a newline because send is via
      // the on-screen button). Inserting at selectionStart/End preserves
      // mid-paragraph editing — previously `input += "\n"` always tacked
      // the newline onto the end, which made it impossible to split a
      // paragraph at the caret.
      const isTouch = (window.matchMedia
                        && window.matchMedia("(pointer: coarse)").matches)
                      || window.innerWidth < 768;
      const wantNewline = ev.shiftKey || ev.ctrlKey || ev.metaKey || isTouch;
      if (wantNewline) {
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
      // Desktop: Enter always calls send(). While streaming, send() itself
      // enqueues the message and clears the input — the user sees the queue
      // badge and knows it will auto-send when the current turn finishes.
      this.send();
    },
    onChatScroll() {
      const el = this.$refs.chatBody;
      if (!el) return;
      // Strict "at bottom": 2px tolerance only — just enough to absorb
      // sub-pixel geometry (browser zoom / high-DPI displays can report
      // distance as 0.4–1.x even when visually at bottom; pure ==0 would
      // mis-classify and never re-engage auto-follow). Any user-driven
      // scroll-up of even a few pixels immediately disables mid-stream
      // auto-follow until they scroll all the way back down.
      // Layout jitter (pending bubble grows, queue badge inflates, etc.)
      // doesn't fire scroll events — only scrollTop changes do — so this
      // tight threshold is safe against false negatives from those.
      this.atBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 2;
    },
    scrollToBottom(force) {
      const el = this.$refs.chatBody;
      if (!el) return;
      // Strict semantics: when not forced, respect the user's atBottom
      // intent exclusively. Don't re-sample geometry — the prior
      // "sample-then-decide" approach used a 150px window that meant
      // every new chunk during streaming could re-engage auto-follow
      // even after the user had scrolled up to read history (each
      // chunk's small height kept distance briefly under 150). With
      // this guard, once the user is meaningfully scrolled up the
      // viewport stays put until they manually scroll back to the bottom.
      if (!force && !this.atBottom) return;
      if (force) {
        // Explicit jump (the ↓ FAB). `.msg` uses content-visibility:auto,
        // so off-screen bubbles report an ESTIMATED height (the 200px
        // contain-intrinsic-size placeholder). A one-shot scrollTop =
        // scrollHeight therefore lands short — as bottom content scrolls
        // into view and realizes its (taller) real height, scrollHeight
        // keeps growing. Re-slam to the bottom each frame until scrollHeight
        // stops growing. See _settleScrollToBottom.
        this._settleScrollToBottom();
        this.atBottom = true;
        return;
      }
      // Streaming auto-follow path: bottom region is already realized,
      // so the cheap single-shot is accurate and avoids per-chunk rAF.
      this.$nextTick(() => {
        el.scrollTop = el.scrollHeight;
        this.atBottom = true;
      });
    },

    // Re-slam the viewport to the very bottom each frame until the
    // document height stops growing. Needed because `.msg {
    // content-visibility: auto }` reports off-screen bubbles at their
    // estimated (200px placeholder) height; as the bottom content scrolls
    // into view its real, usually taller, height realizes and scrollHeight
    // keeps growing. A single `scrollTop = scrollHeight` therefore lands
    // short. Converge: keep pinning to the bottom until scrollHeight has
    // been stable for two consecutive frames (heights realized) or the
    // frame budget is spent.
    _settleScrollToBottom({ maxFrames = 40 } = {}) {
      const el = this.$refs.chatBody;
      if (!el) return;
      let frames = 0;
      let lastH = -1;
      let stable = 0;
      const step = () => {
        el.scrollTop = el.scrollHeight; // browser clamps to valid range
        frames++;
        const h = el.scrollHeight;
        if (Math.abs(h - lastH) < 1) {
          // Height held steady this frame; require two in a row so a
          // mid-realization plateau doesn't end the loop prematurely.
          if (++stable >= 2) return;
        } else {
          stable = 0;
        }
        lastH = h;
        if (frames >= maxFrames) return;
        requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    },

    // Scroll chatBody so the element returned by getEl() sits desiredRelTop
    // px below the viewport's top edge, converging across a handful of
    // frames. Needed because `.msg { content-visibility: auto }` makes
    // off-screen bubble heights mere estimates — an off-screen element's
    // getBoundingClientRect shifts as we scroll through siblings and their
    // real heights realize. Each frame we measure the element's CURRENT
    // distance from the top and nudge scrollTop by the residual, so the
    // landing stays accurate even as upstream heights change underfoot.
    _settleScrollToEl(getEl, desiredRelTop = 0, { maxFrames = 40 } = {}) {
      const el = this.$refs.chatBody;
      if (!el) return;
      let frames = 0;
      let onTarget = 0;
      // Mobile fallback guard: on some touch browsers the per-frame
      // `scrollTop += delta` nudge can be a no-op (momentum scrolling owns
      // the scroll position, or the effective scroller isn't chatBody). If
      // we keep asking for a move and scrollTop never budges, bail to the
      // native scrollIntoView, which the browser honors regardless. Desktop
      // converges in 1-2 frames so this branch never trips there.
      let stuckFrames = 0;
      const step = () => {
        const target = getEl();
        if (!target) return;
        const bodyTop = el.getBoundingClientRect().top;
        const cur = target.getBoundingClientRect().top - bodyTop;
        const delta = cur - desiredRelTop;
        frames++;
        if (Math.abs(delta) < 1) {
          // On target; require two consecutive on-target frames so a
          // transient match mid-realization doesn't stop us early.
          if (++onTarget >= 2) return;
        } else {
          onTarget = 0;
          const before = el.scrollTop;
          el.scrollTop += delta; // browser clamps to valid range
          // scrollTop refused to move despite a real residual → the nudge
          // path is dead on this device. Hand off to scrollIntoView once.
          if (Math.abs(el.scrollTop - before) < 1 && ++stuckFrames >= 3) {
            try { target.scrollIntoView({ block: "start" }); } catch (_) {
              target.scrollIntoView();
            }
            return;
          }
        }
        if (frames >= maxFrames) return;
        requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    },

    // Step the viewport up to the previous user message — the nearest
    // `.msg.user` whose top sits above the current viewport top. Repeated
    // clicks walk back through the conversation one user turn at a time.
    // If nothing is above (already at/above the first question) we snap to
    // the very first user message.
    jumpToPrevUser() {
      const el = this.$refs.chatBody;
      if (!el) return;
      const users = Array.from(el.querySelectorAll(".msg.user"));
      if (!users.length) return;
      const contTop = el.getBoundingClientRect().top;
      const threshold = 4; // px; element must be meaningfully above the top
      let target = null;
      for (const u of users) {
        // Position of this user msg's top relative to the scroll viewport.
        if (u.getBoundingClientRect().top - contTop < -threshold) {
          target = u; // keep the LAST one above → nearest above the fold
        } else {
          break; // DOM order is top→bottom; rest are below the fold
        }
      }
      if (!target) target = users[0];
      // Don't use scrollIntoView: with content-visibility:auto the target's
      // estimated position is wrong until its real height realizes, so a
      // single smooth scroll lands off. Converge on the measured position
      // each frame (12px breathing room above the top edge). See
      // _settleScrollToEl for the content-visibility rationale.
      this._settleScrollToEl(() => target, 12);
      target.classList.add("msg-highlight");
      setTimeout(() => target.classList.remove("msg-highlight"), 2400);
    },

    async send(opts = {}) {
      // ===== Pin target session at function entry =====
      // CRITICAL (fixes 2026-05-22 cross-tab leak): send() has multiple
      // await points downstream (stillUploading polling loop, queue drain
      // hand-off, etc). If `this.currentId` / `this.messages` are read
      // AFTER one of those awaits, a tab switch by the user during the
      // await silently retargets the entire send — the user msg bubble
      // ends up in the new tab, the stream URL still references the new
      // tab, but the cognitive model says "I sent in session A". User
      // reported the exact symptom: "I sent in session1, the message
      // appeared in session2, Muse's reply landed in session1" — the
      // mismatch is from race interactions between this and prior turns
      // queuing/draining on the old tab.
      //
      // Fix: snapshot the target session ID right here, before any await,
      // and route every downstream write through `_ensureTabState(sendSid)`
      // (NOT through `this.messages` which is just a proxy to
      // tabState[currentId].messages and may have been re-aliased by
      // activateTab during an await). The stream URL also uses sendSid.
      // If the user switches tabs while we're sending, the bubble + reply
      // both stay in the original tab — visible only when they switch
      // back — which is the contract `streamSid` was supposed to enforce
      // all along.
      const sendSid = this.currentId;
      // Reconnect mode: skip user-input validation + user-msg push.
      // Used by _reconnectActiveTurn() when loadSession discovers an
      // in-flight background turn on the current session — we just want
      // to subscribe to the existing TurnBroadcast (empty prompt =
      // attach), all the EventSource handlers below stay the same.
      const isReconnect = !!opts.reconnect;
      // Continuation mode: a reconnect that attaches to the bg-task watcher's
      // HEADLESS CONTINUATION turn (it published the finished task's card flip
      // + the model's auto-continue reaction). Unlike a queue-drain reconnect,
      // we must NOT truncate the in-flight portion — the launching tool_use
      // card lives there and the replayed task_notification needs to flip it
      // to ✅done. The continuation's events APPEND after the existing cards.
      const isContinuation = isReconnect && !!opts.continuation;
      // Resumed mode: _drainPendingQueue popped a previously-enqueued
      // message and asked us to send it. Pull text + attachments from
      // the item (NOT from this.input / this.pendingImages — those may
      // hold a different draft the user has typed since enqueue).
      const resumed = opts.resumedItem || null;
      // No-provider gate: with zero configured models every send would 401
      // the Anthropic auth pre-check AND lock the session to the unreachable
      // claude fallback on first send. Refuse the turn and route the user to
      // Settings instead. Reconnect (attach to an in-flight turn) is exempt —
      // it adds no new turn. This is the frontend half of the fix; the
      // backend leaves new sessions' model empty until a provider exists.
      if (!isReconnect && (!this.availableModels || !this.availableModels.length)) {
        this.toast(
          this.lang === "zh" ? "请先在设置里配置一个模型" : "Configure a model in Settings first",
          "warn", 3000);
        this.openSettings();
        return;
      }
      let text;
      if (isReconnect) text = "";
      else if (resumed) text = (resumed.text || "").trim();
      else text = this.input.trim();
      // Slash command: intercept BEFORE hitting the SDK. /word or /word arg.
      // Resumed items can't reach the slash branch — slash is processed
      // before enqueue, so a queued item is always plain text. While
      // busy (streaming/compacting), slash commands are intentionally NOT
      // enqueued — they're meta-actions (/clear, /compact, /export) that
      // depend on session state at execution time, not user intent at
      // type time. We toast "wait for this turn to finish" instead.
      if (!isReconnect && !resumed && text.startsWith("/")) {
        const m = text.match(/^\/(\w+)(?:\s+(.*))?$/);
        if (m) {
          if (this._isBusy(this.currentId)) {
            this.toast(this.t("queue.slash_blocked"), "warn", 3000);
            return;
          }
          this.input = "";
          this.$nextTick(() => { if (this.$refs.chatInput) this.autoGrow(this.$refs.chatInput); });
          await this._runSlash(m[1], m[2] || "");
          return;
        }
      }
      let readyImages, readyDocs;
      if (isReconnect) {
        readyImages = []; readyDocs = [];
      } else if (resumed) {
        readyImages = (resumed.pendingImages || []).filter(im => im.id && !im.error);
        readyDocs = (resumed.pendingDocs || []).filter(d => d.id && !d.error);
      } else {
        readyImages = this.pendingImages.filter(im => im.id && !im.error);
        readyDocs = this.pendingDocs.filter(d => d.id && !d.error);
      }
      if (isReconnect) {
        if (this.streaming || !this.currentId) return;
      } else {
        if (!text && !readyImages.length && !readyDocs.length) return;
        if (!this.currentId) return;
      }
      // Busy: streaming OR compacting → park on this session's
      // pendingQueue. Auto-drain happens on done / compact-finally /
      // activateTab. Reconnect path skips enqueue (it's already a
      // subscribe, no new message). Resumed path also skips — by
      // construction, drain only fires when not busy.
      if (!isReconnect && !resumed && this._isBusy(this.currentId)) {
        // Server-backed enqueue (POST → sessions/{sid}.queue.json). Only clear
        // the composer once the server accepted it; on failure (queue full /
        // network) _enqueueMessage toasts and we leave the draft intact so the
        // user can retry. The server drains the queue itself when the current
        // turn finishes — no client-side drain.
        const ok = await this._enqueueMessage(this.currentId, {
          text,
          pendingImages: this.pendingImages.slice(),
          pendingDocs: this.pendingDocs.slice(),
        });
        if (!ok) return;
        this.pendingImages = [];
        this.pendingDocs = [];
        this.input = "";
        this.$nextTick(() => { if (this.$refs.chatInput) this.autoGrow(this.$refs.chatInput); });
        // Scroll the chat-body to the bottom so the new queued bubble is
        // visible. Without this the user can be scrolled mid-history and
        // would never see their just-enqueued message land.
        this.atBottom = true;
        this.scrollToBottom(true);
        return;
      }
      // If any attachment is still mid-upload, silently wait for it to
      // finish before kicking off the turn. Per 2026-05-21 user feedback
      // the 30 s deadline was removed — on 4G / weak Wi-Fi an iPhone-
      // resolution photo can legitimately take longer than 30 s to upload,
      // and falling back to "wait_upload" toast + bail forced the user to
      // re-tap send for no good reason. We keep polling indefinitely; the
      // upload itself has its own per-request HTTP timeout (fetch network
      // stack default) which will mark `entry.error = true` if the request
      // fails permanently — that breaks `stillUploading()` so this loop
      // exits naturally with a red-border chip the user can remove + retry.
      // The send button stays disabled (_sendWaitingForUpload) so a double-
      // tap can't enqueue a second send while we wait.
      if (!isReconnect) {
        // For resumed (drained-from-queue) items, the relevant attachments
        // are the snapshot inside the queue item, not this.pendingImages
        // (which may belong to a different draft the user has typed since
        // enqueue). Snapshot stores object refs, so .uploading reflects
        // current state — we just have to look at the right collection.
        const stillUploading = () => {
          if (resumed) {
            return (resumed.pendingImages || []).some(im => im.uploading)
              || (resumed.pendingDocs || []).some(d => d.uploading);
          }
          return this.pendingImages.some(im => im.uploading)
            || this.pendingDocs.some(d => d.uploading);
        };
        if (stillUploading()) {
          this._sendWaitingForUpload = true;
          while (stillUploading()) {
            await new Promise(r => setTimeout(r, 80));
          }
          this._sendWaitingForUpload = false;
        }
        // The uploads may have just finished — re-resolve ready{Images,Docs}
        // now that .id is set (eager filter above could have returned empty).
        if (resumed) {
          readyImages = (resumed.pendingImages || []).filter(im => im.id && !im.error);
          readyDocs = (resumed.pendingDocs || []).filter(d => d.id && !d.error);
        }
      }
      // Push to the SENDING tab's messages array (looked up via sendSid),
      // not this.messages — `this.messages` may have been re-aliased to a
      // different tab if the user switched mid-await. See the "Pin target
      // session" block at function entry for the full story.
      const sendState = this._ensureTabState(sendSid);
      // Reconnect mode skips pushing a user msg — the backend already
      // has the user prompt from the original turn, and the
      // broadcast-rebuild on `/sessions/{sid}` GET produced it for us.
      if (!isReconnect) {
        // Trim the live backlog before growing it again — keeps long
        // sessions from ballooning the DOM past the mobile crash point.
        this._capLiveMessages(sendState);
        sendState.messages.push({
          role: "user", text,
          images: readyImages.map(im => ({
            preview: im.preview,
            // Pre-compute the URL the backend will serve once it
            // persists the full-res original (it does so the moment
            // the SSE stream consumes the upload's aid). This makes
            // the lightbox work even if the user reloads before the
            // stream-completion annotation hook fires.
            url: (im.id && im.attach_ext && sendSid)
              ? `/api/chat/attachments/${sendSid}/${im.id}.${im.attach_ext}`
              : undefined,
            mime: im.mime,
          })),
          docs: readyDocs.map(d => ({ name: d.name, kind: d.kind })),
        });
      } else if (!isContinuation) {
        // Truncate the in-flight portion: the backend broadcast will
        // replay every event from the start of the turn (thinking +
        // assistant + tool_use + ...), and our handlers below push
        // them as messages. The mid-turn rebuild that loadSession
        // already populated would otherwise be duplicated, so drop
        // anything after the most recent user msg before the replay
        // fills it back in.
        const roles = sendState.messages.map(m => m.role);
        const lastUserIdx = roles.lastIndexOf("user");
        if (lastUserIdx >= 0 && lastUserIdx < sendState.messages.length - 1) {
          sendState.messages.splice(lastUserIdx + 1);
        }
      }
      // (isContinuation: keep the existing messages intact — the watcher's
      // broadcast only replays the NEW completion events: task_notification
      // (flips the still-present running card) + the auto-continue reaction
      // (appends as a fresh assistant bubble). Nothing to truncate.)
      // Single id-list for both kinds — backend dispatches by stored kind.
      const attachIds = isReconnect ? [] : [
        ...readyImages.map(im => im.id),
        ...readyDocs.map(d => d.id),
      ];
      // Reconnect: nothing to clear. Resumed: input/pendingImages were
      // already cleared at enqueue time, and the user may have typed a
      // new draft since — don't touch their work-in-progress.
      if (!isReconnect && !resumed) {
        const erroredImages = this.pendingImages.filter(im => im.error);
        const erroredDocs = this.pendingDocs.filter(d => d.error);
        if (erroredImages.length || erroredDocs.length) {
          this.toast(this.lang === "zh"
            ? `${erroredImages.length + erroredDocs.length} 个附件上传失败，已跳过`
            : `${erroredImages.length + erroredDocs.length} attachment(s) failed and were skipped`,
            "warn", 4000);
        }
        // Note (2026-04): previews are now data URIs from canvas.toDataURL
        // (see _imageToThumbDataURL), not blob: URLs — they're safe to
        // hold across reloads with no revoke needed. The only path that
        // creates a blob URL is the img.onerror fallback, which now
        // returns "" rather than leaking — so no sweep here is needed.
        this.pendingImages = [];
        this.pendingDocs = [];
        this.input = "";
        this.$nextTick(() => {
          const ta = this.$refs.chatInput;
          if (!ta) return;
          this.autoGrow(ta);
          // Desktop: refocus so the user can fire off the next message
          // without re-clicking the input. Mobile: DON'T. A programmatic
          // focus here re-fires onChatInputFocus → adds body.kb-open, but
          // there's no paired blur (the textarea keeps focus through the
          // whole streaming reply) and iOS usually won't actually reopen the
          // soft keyboard for a non-gesture focus. Net result: the app is
          // stuck in keyboard-up layout (tab bar display:none + grid shrunk
          // by --kb-inset) with NO visible keyboard → a blank band appears
          // under the composer during the reply. Skipping focus on mobile
          // keeps kb-open's add/remove paired to real user focus/blur.
          if (!this._isMobileLayout()) ta.focus();
        });
      }
      this.mentionShow = false;
      // streamSid + streamState alias the sendSid snapshot taken at function
      // entry. We KEEP the local names `streamSid` / `streamState` because
      // every downstream event handler (text / thinking / tool_use / done /
      // error / cancelled) reads them — renaming would touch 40+ lines for
      // zero behavioural delta. The important thing is: NEITHER is recaptured
      // from `this.currentId` here. That was the bug.
      const streamSid = sendSid;
      const streamState = sendState;

      streamState.streaming = true; this.streaming = true;
      streamState.streamingModel = this.model;
      this.streamingModel = this.model;   // 锁定 — pending bubble 用它，不跟着 dropdown
      // Start the wall-clock NOW, at submit-time — not later in es.onopen.
      // The previous setup waited for the SSE handshake (which can take
      // 1-3s on slow networks / cold backends) before the counter began
      // ticking, so users saw "thinking · 0s" frozen for the first few
      // seconds and then jump. Setting it here AND not clobbering it on
      // reconnect (see es.onopen below) also fixes the "timer suddenly
      // resets to 0" bug — every SSE reconnect used to fire onopen which
      // overwrote _streamStartedAt with now.
      //
      // Reconnect anchor: when re-subscribing to an in-flight turn (page
      // reload mid-turn, tab switch back, SSE drop+re-open), DON'T reset the
      // elapsed counter to now — that's the "footer running time keeps
      // resetting" bug. Anchor it to the turn's true start instead:
      //   1. opts.startedAt — server-authoritative epoch SECONDS from the
      //      /active probe. Used by reload reconnect (tab state is fresh, so
      //      any local _streamStartedAt would be 0 or a STALE prior turn) and
      //      by the mid-stream auto-reconnect (passes d.started_at too).
      //      Self-host runs FE + BE on one clock, so server s → client ms
      //      maps cleanly.
      //   2. else the existing streamState._streamStartedAt — the
      //      mid-stream retry path (probe failed, no fresh started_at) keeps
      //      the current turn's real start that the original send() set.
      //   3. else now (shouldn't happen on a real reconnect; safe default).
      // Fresh (non-reconnect) sends always start at now.
      let _streamStartMs;
      if (isReconnect) {
        if (opts.startedAt) _streamStartMs = opts.startedAt * 1000;
        else if (streamState._streamStartedAt) _streamStartMs = streamState._streamStartedAt;
        else _streamStartMs = Date.now();
      } else {
        _streamStartMs = Date.now();
      }
      streamState._streamStartedAt = _streamStartMs;
      this._streamStartedAt = _streamStartMs;
      const _initElapsed = Math.max(0, (Date.now() - _streamStartMs) / 1000);
      streamState.streamElapsed = _initElapsed; this.streamElapsed = _initElapsed;
      // Tick immediately so the footer shows 0.0s right after submit
      // (without waiting for the first 200ms interval tick).
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
        + (attachIds.length ? "&image_ids=" + encodeURIComponent(attachIds.join(",")) : "")
        + "&token=" + encodeURIComponent(this.token);
      const es = new EventSource(url);
      streamState.es = es; this.es = es;
      // Reset auto-reconnect counter on each successful SSE open. NOTE
      // — we deliberately do NOT (re)start the elapsed-time counter
      // here. Timer + _streamStartedAt are set above at submit time so
      // (a) the footer shows "0.0s" immediately instead of waiting
      // through the SSE handshake, and (b) mid-stream reconnects don't
      // visibly reset the displayed elapsed back to zero.
      es.onopen = () => {
        streamState._reconnectAttempts = 0;
      };

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
        if (this.currentId !== streamSid) return;
        // Mid-stream DOM cap (root-cause fix for the mobile freeze on long
        // turns). _capLiveMessages otherwise only runs at user-send, so a
        // SINGLE long agentic turn — dozens of thinking / tool_use /
        // tool_result bubbles pushed before the user types again — grows
        // messages[] (and the rendered DOM) without bound. On phones the
        // ballooning DOM eventually freezes the WebView. Trim from the
        // front on every render/append tick: it's O(1) when under cap, and
        // splice(0, overflow) can never touch the streaming tail bubble.
        // Gate on atBottom — the same guard scrollToBottom uses — so we
        // never evict (and visually jump) while the user has scrolled up to
        // read history. Evicted bubbles land in the "Load earlier" stash.
        if (this.atBottom) this._capLiveMessages(streamState);
        this.scrollToBottom(false);
      };
      const openAsst = () => {
        if (curBubble) return;
        // Pre-declare every key the template might read so Alpine's
        // Proxy tracks them from t=0. Adding a key post-push (e.g.
        // m.elapsed in _markDone) doesn't reliably trigger x-show
        // re-evaluation — same root cause as the AskUserQuestion bug.
        // Keys touched by .turn-footer / .bubble: ts (completion stamp),
        // elapsed (total seconds), cost, model, text, html. All start
        // empty / null so x-show defaults match "not yet computed".
        const bubble = {
          role: "assistant",
          text: "", html: "", cost: "",
          model: modelForBubble,
          ts: null,
          elapsed: 0,
        };
        streamState.messages.push(bubble);
        // CRITICAL: pull the reactive-wrapped object back out of the
        // array, not the raw `bubble` reference. Alpine 3 (and Vue 3
        // reactivity under it) intercepts at the array level — accessing
        // `messages[i]` returns the proxy that has dependency tracking
        // wired up. Mutating the raw `bubble` directly bypasses the
        // proxy, so later changes to `curBubble.html` aren't seen by
        // the `x-html="m.html"` effect. Symptom that triggered this fix:
        // the first text_delta showed up (push triggered re-render with
        // initial state) but every subsequent chunk only became visible
        // after switching tabs (which forced Alpine to re-read the
        // array through the proxy).
        curBubble = streamState.messages[streamState.messages.length - 1];
        acc = "";
      };
      // Throttle markdown rendering during fast token streams. mdRender
      // re-parses the FULL accumulated text every tick, so the per-render cost
      // grows with reply length (O(n) per render → O(n²) over the whole
      // reply). On long replies — and especially on phones — re-parsing every
      // 80ms pegs the CPU and heats the device. Stretch the interval as `acc`
      // grows so total re-parse work stays bounded; short replies keep the
      // snappy 80ms feel. flushRender() always paints the complete final text
      // on done/close, so stretching never loses content.
      const _renderInterval = () => {
        const n = acc.length;
        if (n < 2000) return 80;
        if (n < 8000) return 160;
        if (n < 20000) return 320;
        // Very long replies: stretch the interval hard so the total re-parse
        // work stays bounded. marked.parse + DOMPurify is O(n) per render, so
        // a 100KB reply re-parsed every 600ms pegs a phone CPU at 100% for the
        // whole stream (heat + freeze). flushRender() always paints the
        // complete final text on done, so stretching never drops content.
        if (n < 50000) return 600;
        if (n < 120000) return 1000;
        return 1600;
      };
      let lastRender = 0;
      let pendingTimer = null;
      // final=true → the message is complete (done / tool boundary): run the
      // full render (KaTeX + file-link linkify). final=false → throttled
      // in-flight tick: cheap parse+sanitize only.
      const renderNow = (final = false) => {
        if (!curBubble) { pendingTimer = null; return; }
        // Uncached: each `acc` is a one-shot intermediate, never reused.
        curBubble.html = this._mdRenderUncached(acc, { streaming: !final });
        lastRender = Date.now();
        pendingTimer = null;
        // Coalesce auto-scroll onto the throttled render tick. Scrolling on
        // every token forced a full reflow (scrollHeight read) of the entire
        // message DOM per chunk — O(conversation length) per token, the main
        // reason long conversations froze mid-stream. Now it fires at most
        // once per render instead of once per token.
        _scrollIfActive();
      };
      const scheduleRender = () => {
        const interval = _renderInterval();
        const since = Date.now() - lastRender;
        if (since >= interval) {
          renderNow();
        } else if (!pendingTimer) {
          pendingTimer = setTimeout(renderNow, interval - since);
        }
      };
      const flushRender = () => {
        if (pendingTimer) { clearTimeout(pendingTimer); pendingTimer = null; }
        // final=true: this bubble is done (stream end / tool boundary), so do
        // the full render incl. KaTeX + file-path linkify that the throttled
        // ticks skipped.
        if (curBubble) renderNow(true);
      };
      const closeAsst = () => { flushRender(); curBubble = null; acc = ""; };

      // Re-render the in-flight assistant bubble from the accumulated text.
      // Exposed on streamState so the global selectionchange guard (see
      // _initStreamSelectionGuard) can flush a deferred render once the user
      // releases their text selection. Re-reads curBubble/acc from the closure
      // every call, so it stays correct even after openAsst/closeAsst swap
      // bubbles mid-stream.
      const renderStreamingHtml = () => {
        // Mid-stream deferred render (selection cleared): cheap path. The
        // done-handler's flushRender does the full final pass.
        if (curBubble) curBubble.html = this._mdRenderUncached(acc, { streaming: true });
        streamState._pendingHtmlRender = null;
      };
      streamState._renderStreamingHtml = renderStreamingHtml;

      // Attach SDK-native background-task lifecycle state to the launching
      // tool_use card. The Task* messages (TaskStarted/Progress/Notification)
      // carry tool_use_id = the SDK id of the Agent/Bash tool_use that started
      // the background task, so we locate that message in the live turn and
      // stamp `task_status` on it. The subagent card (index.html) renders
      // ⏳ running → ✅/❌ from this. merge=true so a later progress/terminal
      // event keeps fields an earlier event already set.
      const applyTaskStatus = (toolUseId, patch, merge = true) => {
        if (!toolUseId) return;
        const msgs = streamState.messages;
        for (let k = msgs.length - 1; k >= 0; k--) {
          if (msgs[k] && msgs[k].id === toolUseId) {
            const prev = (merge && msgs[k].task_status) ? msgs[k].task_status : {};
            msgs[k].task_status = Object.assign({}, prev, patch);
            return;
          }
        }
      };

      es.addEventListener("text", ev => {
        let d;
        try { d = JSON.parse(ev.data); } catch (_) { return; }
        openAsst();
        acc += d.text;
        curBubble.text = acc;
        // Skip the mdRender → x-html assignment while the user has an active
        // text selection in the chat body. x-html replaces innerHTML on every
        // chunk, which forces the browser to collapse any selection inside the
        // bubble — making "select while streaming, then Ctrl+C" impossible.
        // We still accumulate `acc`; the selectionchange listener flushes a
        // deferred render the moment the selection clears, and the `done`
        // handler's flushRender catches anything still pending at stream end.
        if (this._selectionInChatBody()) {
          streamState._pendingHtmlRender = renderStreamingHtml;
        } else {
          // Throttle instead of re-parsing the full accumulated text on every
          // token. scheduleRender() drives renderNow (which also coalesces the
          // auto-scroll); flushRender() on done/close catches any trailing
          // chunk still inside the throttle window. The scroll used to fire
          // here on every token (forcing a full-DOM reflow per chunk) — it's
          // now folded into renderNow so it runs at most once per render.
          scheduleRender();
        }
      });
      es.addEventListener("thinking", ev => {
        let d;
        try { d = JSON.parse(ev.data); } catch (_) { return; }
        closeAsst();
        // Backend yields one SSE event per thinking_delta. Coalesce them
        // into the most recent thinking message so we see ONE block per
        // reasoning segment, not N tiny ones. If the tail isn't a thinking
        // message (e.g. previous was tool_use), start a new one.
        const msgs = streamState.messages;
        const last = msgs[msgs.length - 1];
        let pushed = false;
        if (last && last.role === "thinking") {
          last.text = (last.text || "") + (d.text || "");
        } else {
          msgs.push({ role: "thinking", text: d.text || "" });
          pushed = true;
        }

        _scrollIfActive();
      });
      es.addEventListener("tool_use", ev => {
        let d;
        try { d = JSON.parse(ev.data); } catch (_) { return; }
        closeAsst();
        // `id` is the SDK's toolu_xxx tool_use_id. Critical for
        // _taskSubjectMapForMessages — it pairs each TaskCreate
        // tool_use with the tool_result that carries the assigned
        // task number. Dropping it here caused live TaskUpdate(#N)
        // lines to render with no subject in the same turn that
        // created the task (the historic-load path was fine because
        // backend/chat.py:1361 included id; the live stream path
        // just forgot to copy it across).
        const msg = { role: "tool_use", name: d.name, id: d.id,
                       summary: d.summary, input: d.input,
                       // Pre-declare the reactive key so a later
                       // task_started/notification event (applyTaskStatus)
                       // reliably triggers Alpine re-render. Adding a
                       // brand-new key after the fact is unreliable for
                       // :class re-eval (cf. ask_user_question pendingAnswers).
                       task_status: null };
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
        let d;
        try { d = JSON.parse(ev.data); } catch (_) { return; }
        // `text` (up to 50KB) drives the "expand" affordance and per-tool
        // rich renderers (Bash terminal / Read with gutter / WebFetch card).
        // `tool_name` lets the FE pick a renderer without scanning backwards
        // for the matching tool_use. `bash` is pre-parsed exit_code +
        // stdout/stderr when the result came from a Bash call.
        streamState.messages.push({
          role: "tool_result",
          id: d.id,
          tool_name: d.tool_name || "",
          preview: d.preview,
          text: d.text || "",
          truncated: d.truncated,
          text_truncated: d.text_truncated,
          is_error: d.is_error,
          bash: d.bash || null,
        });

        _scrollIfActive();
      });
      es.addEventListener("task_started", ev => {
        let d;
        try { d = JSON.parse(ev.data); } catch (_) { return; }
        applyTaskStatus(d.tool_use_id, {
          task_id: d.task_id,
          state: "running",
          description: d.description || "",
        });
        _scrollIfActive();
      });
      es.addEventListener("task_progress", ev => {
        let d;
        try { d = JSON.parse(ev.data); } catch (_) { return; }
        applyTaskStatus(d.tool_use_id, {
          task_id: d.task_id,
          state: "running",
          usage: d.usage || null,
          last_tool_name: d.last_tool_name || "",
        });
      });
      es.addEventListener("task_notification", ev => {
        let d;
        try { d = JSON.parse(ev.data); } catch (_) { return; }
        // SDK status ∈ {completed, failed, stopped}; map unknown → "done"
        // so a future status value still renders a terminal (not stuck)
        // state instead of silently staying on ⏳.
        const st = (d.status === "completed" || d.status === "failed"
                     || d.status === "stopped") ? d.status : "done";
        applyTaskStatus(d.tool_use_id, {
          task_id: d.task_id,
          state: st,
          summary: d.summary || "",
          output_file: d.output_file || "",
        });
        _scrollIfActive();
      });
      es.addEventListener("ask_user_question", ev => {
        let d;
        try { d = JSON.parse(ev.data); } catch (_) { return; }
        closeAsst();
        // Pre-populate pendingAnswers with one key per question (multiSelect
        // → []; single → null). Without this, Alpine's Proxy doesn't reliably
        // re-evaluate :class={picked: ...} when we add a brand-new key on
        // first click — the answer is set but the button doesn't visually
        // light up, so the user thinks the click was eaten and clicks again
        // (which auto-submits twice). Pre-declaring every key turns the
        // first click into a value MUTATION, which Alpine always tracks.
        // Also pre-initialise askOtherOpen / askOtherText for the same
        // reason — they're touched by openAskOther later.
        const pendingAnswers = {};
        for (const q of (d.questions || [])) {
          pendingAnswers[q.question] = q.multiSelect ? [] : null;
        }
        streamState.messages.push({
          role: "ask_user_question",
          id: d.id,
          questions: d.questions,
          pendingAnswers,
          submitted: false,
          askOtherOpen: false,
          askOtherText: "",
        });

        _scrollIfActive();
      });
      es.addEventListener("permission_request", ev => {
        let d;
        try { d = JSON.parse(ev.data); } catch (_) { return; }
        closeAsst();
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
      // `cancelled=true` is set when the done event carried a backend
      // cancellation flag (user clicked stop). For those, suppress the
      // green-dot unread indicator — the user knows they cancelled, an
      // "attention!" dot would imply something new arrived.
      const _markDone = (cancelled = false) => {
        streamState.streaming = false;
        streamState.es = null;
        // If the user is on a different tab when this turn lands, flag
        // unread so the tab strip can show a green dot. Doing it inside
        // _markDone covers every termination path (done / error /
        // cancelled / reconnect-give-up) — no scattered flagging logic.
        // EXCEPT user-cancelled — they don't need a "ding, ready!" cue.
        if (this.currentId !== streamSid && !cancelled) {
          streamState.unread = true;
        }
        // Stamp the tail of the just-finished turn with completion
        // timestamp + total elapsed seconds. A "turn" = contiguous run
        // of muse-side messages between two user messages; only the tail
        // assistant TEXT bubble carries .ts / .elapsed so .turn-footer
        // (HH:MM · 2m50s) renders under the actual reply, not a stray
        // tool_result row that happened to close the turn. Walk backwards
        // past tool_use / tool_result / thinking blocks until we hit an
        // assistant text or hit the user message that started the turn.
        //
        // elapsed: use the FE-tracked streamElapsed (matches the value
        // the user just watched tick up next to the dots). Backend's
        // d.duration_ms could differ slightly (covers SDK round-trip
        // only, not the local send→connect lag), and seeing the number
        // jump after "done" lands would feel like a bug.
        const _now = Date.now();
        const _elapsed = streamState.streamElapsed || 0;
        for (let k = streamState.messages.length - 1; k >= 0; k--) {
          const m = streamState.messages[k];
          if (m.role === "user") break;          // entered the previous turn
          // Skip tool blocks / standalone thinking; they're not the
          // "reply" the user reads time off.
          if (m.role !== "assistant") continue;
          if (!m.ts) m.ts = _now;                // found the tail text bubble
          if (!m.elapsed && _elapsed >= 1) m.elapsed = _elapsed;
          break;                                  // stop after the first one (most recent)
        }
        if (this.currentId === streamSid) {
          this.streaming = false;
          this.es = null;
          // textarea was :disabled while streaming → focus during stream was
          // a no-op. Re-focus now so the user can immediately type the next
          // message (supports rapid-fire conversation).
          this.$nextTick(() => {
            const ta = this.$refs.chatInput;
            // Desktop only — same reason as the post-send refocus above:
            // a mobile programmatic focus re-arms body.kb-open without a
            // paired blur and leaves a blank band under the composer.
            if (ta && !ta.disabled && !this._isMobileLayout()) ta.focus();
          });
        }
      };
      es.addEventListener("done", ev => {
        flushRender();
        // Guard JSON.parse: a malformed/empty `done` payload must NOT throw
        // before es.close()/_markDone()/_stopTimer() run below, else the
        // EventSource + timer interval leak and the UI stays streaming=true
        // (composer dead until reload). Fall back to {} and keep going; every
        // d.* read below is null-safe on a missing field.
        let d;
        try { d = JSON.parse(ev.data); } catch (_) { d = {}; }
        if (d.total_cost_usd != null && curBubble) {
          curBubble.cost = "$" + d.total_cost_usd.toFixed(4);
        }
        if (d.stats) this.stats = { ...this.stats, ...d.stats };
        if (d.session_usage) {
          Object.assign(streamState.sessionUsage, d.session_usage);
          if (this.currentId === streamSid) this.sessionUsage = streamState.sessionUsage;
        }
        this._budgetWarned = this._budgetWarned || {};
        if (d.budget_usd > 0 && d.budget_used_pct >= 90 && !this._budgetWarned[streamSid]) {
          this._budgetWarned[streamSid] = true;
          this.toast(this.t("cost.budget_warn", { pct: d.budget_used_pct, usd: d.budget_usd }),
                      "warn", 5000);
        }
        // Context window handling — two-tier:
        //   85-94%: one-shot toast "compact now?" (user decides)
        //   ≥95%:   silent auto-compact (don't let user hit hard limit)
        // _ctxWarned keys by streamSid so a new session starts fresh.
        this._ctxWarned = this._ctxWarned || {};
        this._autoCompacted = this._autoCompacted || {};
        const ctxPct = d.session_usage && d.session_usage.context_used_pct;
        const streamStCompacting = !!(this.tabState[streamSid] && this.tabState[streamSid].compacting);
        if (ctxPct >= 95 && !this._autoCompacted[streamSid] && !streamStCompacting) {
          this._autoCompacted[streamSid] = true;
          // Schedule on next tick so the stream's done handler fully
          // unwinds first (runCompact's per-session streaming check
          // needs to see streaming === false). Pass streamSid explicitly
          // so a mid-stream tab switch doesn't redirect the compact
          // onto the user's current (often unrelated) session.
          this.$nextTick(() => {
            const zh = this.lang === "zh";
            this.toast(zh ? `上下文 ${Math.round(ctxPct)}%，自动压缩中…`
                          : `Context ${Math.round(ctxPct)}% — auto-compacting…`,
                       "info", 3000);
            this.runCompact(streamSid, { skipConfirm: true });
          });
        } else if (ctxPct >= 85 && ctxPct < 95 && !this._ctxWarned[streamSid]) {
          this._ctxWarned[streamSid] = true;
          this.toast(
            this.t("ctx.window_warn", { pct: Math.round(ctxPct) }),
            "warn", 6000,
            { label: this.t("ctx.window_warn_action"), onClick: () => this.runCompact(streamSid, { skipConfirm: true }) },
          );
        }
        // Pass the backend's `cancelled` flag through to _markDone so it
        // can skip the green-dot unread cue for user-cancelled turns.
        // The on-screen `done` handler runs only when the FE is still
        // subscribed at completion time (typical when user did NOT click
        // stop — stop closes the ES). The relevant case for this branch
        // is page-reload-then-reconnect picking up a turn that finished
        // after being cancelled before reload.
        es.close(); _markDone(!!d.cancelled); _stopTimer();
        this.refreshSessions();
        if (this.currentId === streamSid) {
          this.$nextTick(() => this.highlightCode(".chat-body"));
        }
        // Auto-drain the next queued message, if any. nextTick lets
        // _markDone's streaming=false propagate before _isBusy() reads it.
        // If an auto-compact was just triggered above (ctx >= 95%), the
        // drain will hit st.compacting=true and bail; the compact-finally
        // path will pick it up. If the queue's tab isn't the active one,
        // drain bails too and activateTab handles it on return.
        this.$nextTick(() => this._drainPendingQueue(streamSid));
        // If this turn left an SDK background task running (its card is still
        // ⏳), start polling /active so the server's continuation turn (card
        // flip + model auto-continue) surfaces live when the task finishes.
        // Re-evaluated here after every turn — incl. a continuation's own
        // `done`, which (having flipped the card) leaves no running card, so
        // _ensureBgContPoller no-ops and the existing poller self-stops.
        this.$nextTick(() => this._ensureBgContPoller(streamSid));
      });
      es.addEventListener("error", ev => {
        flushRender();
        // Two distinct error paths share this handler:
        //   1. Server-sent `event: error` (well-formed JSON in ev.data) —
        //      a real turn failure (vendor 401, quota, 30-min timeout).
        //      Retrying the same SSE won't help; mark the user msg failed
        //      so the ↻ button shows, and the user can edit + resend.
        //   2. Transport-level disconnect (ev.data undefined → JSON.parse
        //      throws) — network blip, sleep/wake, server restart, etc.
        //      The backend's TurnBroadcast survives client disconnect, so
        //      we can transparently re-subscribe via _checkActiveTurn().
        //      Capped at 3 attempts with exponential backoff so a truly
        //      dead backend doesn't loop forever.
        let serverError = null;
        let errKind = "unknown";
        let errCta = "retry";
        let errRetryable = true;
        try {
          const d = JSON.parse(ev.data);
          if (d && d.error) serverError = d.error;
          if (d && d.kind) errKind = d.kind;
          if (d && typeof d.cta === "string") errCta = d.cta;
          if (d && typeof d.retryable === "boolean") errRetryable = d.retryable;
        } catch (_) {
          // ev.data missing → transport-level. Fall through to auto-retry.
        }
        const markUserFailed = () => {
          for (let i = streamState.messages.length - 1; i >= 0; i--) {
            const m = streamState.messages[i];
            if (m.role === "user") {
              m._failed = true;
              // Stash the classification so the FE can render a useful
              // CTA button under the failed bubble (Open Settings on auth,
              // Switch model on quota, Compact on cross-vendor signature).
              m._error_kind = errKind;
              m._error_cta = errCta;
              m._error_retryable = errRetryable;
              m._error_text = serverError || "";
              break;
            }
          }
        };

        // Benign: "no active turn" means a RECONNECT raced a turn that already
        // finished on the server (and aged past the grace-keep TTL, so it's not
        // in _recent_turns either). The reply IS persisted in the session JSONL
        // — this is NOT a failure. Toasting + markUserFailed here would wrongly
        // flag the wrong user bubble (e.g. the first drained item) as failed.
        // Instead: silently reload history (surfacing the finished reply) and
        // let the queue drain continue.
        if (serverError === "no active turn") {
          es.close(); _markDone(); _stopTimer();
          this.loadSession(streamSid).then(() => {
            this.$nextTick(() => this._drainPendingQueue(streamSid));
          });
          return;
        }

        if (serverError) {
          this.toast(this._humanizeStreamError(serverError), "error", 6000);
          markUserFailed();
          es.close(); _markDone(); _stopTimer();
          // Pause auto-drain — same context likely fails the next message
          // too (quota / auth / cross-vendor signature). The failed user
          // bubble surfaces a "resume queue (N)" CTA so the user can
          // explicitly continue after fixing the root cause.
          if (streamState.pendingQueue && streamState.pendingQueue.length > 0) {
            // Optimistic — the server also pauses the queue in the turn's
            // finally (Task 3) when an errored turn has items waiting. Show
            // the banner now, reconcile with server truth a beat later.
            streamState._queuePaused = true;
            setTimeout(() => this._syncQueueFromServer(streamSid), 800);
          }
          return;
        }

        // ---- Transport-level: auto-retry path ----
        const MAX_ATTEMPTS = 3;
        const attempts = (streamState._reconnectAttempts = (streamState._reconnectAttempts || 0) + 1);

        // Always close the old ES — leaving it triggers the browser's own
        // auto-reconnect (every ~3 s) on top of ours, which would race.
        try { es.close(); } catch (_) {}

        if (attempts > MAX_ATTEMPTS) {
          // Given up. Surface manual retry UI.
          this.toast(this.lang === "zh"
                      ? "和 Muse 的连接断开了，重试一下"
                      : "Lost connection to Muse — try again",
                      "error");
          markUserFailed();
          _markDone(); _stopTimer();
          if (streamState.pendingQueue && streamState.pendingQueue.length > 0) {
            // Optimistic — the server also pauses the queue in the turn's
            // finally (Task 3) when an errored turn has items waiting. Show
            // the banner now, reconcile with server truth a beat later.
            streamState._queuePaused = true;
            setTimeout(() => this._syncQueueFromServer(streamSid), 800);
          }
          return;
        }

        // Exponential backoff: 800 ms, 1.6 s, 3.2 s. _checkActiveTurn
        // confirms the backend turn is still in flight before opening a
        // fresh SSE — if the turn finished cleanly while we were
        // disconnected, it loads the session view from disk instead, so
        // the user sees the completed reply rather than an in-progress
        // bubble that never resolves.
        const delay = 800 * Math.pow(2, attempts - 1);
        setTimeout(async () => {
          // User switched tabs / cancelled / page already navigated away?
          // Don't surprise them by re-opening a stream they no longer want.
          if (this.currentId !== streamSid) { _markDone(); _stopTimer(); this.streaming = false; return; }
          // streamState.streaming is still true from initial send(); use
          // it as the in-flight gate _checkActiveTurn checks internally.
          try {
            const r = await fetch(`/api/chat/sessions/${streamSid}/active`,
                                    { headers: this.hdr() });
            if (!r.ok) throw new Error("active probe failed");
            const d = await r.json();
            if (!d.active) {
              // Backend turn already finished while we were disconnected.
              // Refresh session from disk to pick up the completed reply.
              _markDone(); _stopTimer();
              if (this.currentId === streamSid) this.loadSession(streamSid);
              streamState._reconnectAttempts = 0;
              return;
            }
            // Re-subscribe via the existing reconnect plumbing.
            // streaming flag must be cleared first or send() bails as
            // "already streaming."
            streamState.streaming = false;
            this.streaming = false;
            // Anchor the elapsed timer to the backend turn's real start so
            // the footer resumes from the true start, not from reconnect.
            this.send({ reconnect: true, startedAt: d.started_at });
          } catch (_e) {
            // Probe failed — try again on next error tick (counter will
            // continue incrementing until MAX_ATTEMPTS). Force a fresh
            // error event by manufacturing one: easiest is to schedule
            // another setTimeout that mimics this branch but bypasses
            // the EventSource. Cheaper: just bump the counter and let
            // the next real error fire normally. Bail here.
            _markDone(); _stopTimer();
            if (attempts >= MAX_ATTEMPTS) {
              this.toast(this.lang === "zh"
                          ? "和 Muse 的连接断开了，重试一下"
                          : "Lost connection to Muse — try again",
                          "error");
              markUserFailed();
            } else {
              // Schedule next retry ourselves since no new ES error will fire.
              setTimeout(() => {
                if (this.currentId !== streamSid) return;
                streamState.streaming = false;
                this.streaming = false;
                this.send({ reconnect: true });
              }, 800 * Math.pow(2, attempts));
            }
          }
        }, delay);
      });
      es.addEventListener("cancelled", () => {
        flushRender();
        this.toast(this.lang === "zh" ? "已中断" : "Interrupted", "warn", 2000);
        es.close(); _markDone(); _stopTimer();
        // User explicitly stopped — pause the queue too. Auto-draining
        // here would be surprising (they cancelled for a reason, almost
        // never "just this one but please send the rest"). The paused
        // banner gives an explicit Resume.
        if (streamState.pendingQueue && streamState.pendingQueue.length > 0) {
          // Optimistic — the server pauses on an explicit interrupt too
          // (broadcast.cancelled → finally pauses the queue). Reconcile after.
          streamState._queuePaused = true;
          setTimeout(() => this._syncQueueFromServer(streamSid), 800);
        }
      });
      // NOTE: errors are owned exclusively by the addEventListener("error")
      // handler above (rich classification + exponential-backoff transparent
      // reconnect). A redundant `es.onerror` here used to also fire on the
      // same `error` event and — because addEventListener runs first and
      // intentionally closes the ES for a transparent reconnect — would see
      // readyState === CLOSED and call _markDone() prematurely, flipping the
      // turn to "done" (input re-enabled, footer stamped, unread dot) ~800ms
      // before the reconnect restored streaming. Removing it stops that flicker.
    },
    stop() {
      // Two-stage stop:
      //   1. If the pending queue is non-empty, pop the TAIL (the
      //      most-recently enqueued message) and toast what was removed.
      //      The current streaming turn is left alone — the assumption
      //      is "I just typed something I want to take back, but keep
      //      the reply that's already running."
      //   2. Once the queue is empty, the same button interrupts the
      //      in-flight turn (the original stop behaviour).
      // The button title swaps to communicate which action will fire.
      const sid = this.currentId;
      const st = this._ensureTabState(sid);
      if (st && st.pendingQueue && st.pendingQueue.length > 0) {
        const lastIdx = st.pendingQueue.length - 1;
        const removed = st.pendingQueue[lastIdx];
        // Server-backed delete of the tail item (DELETE by id → re-sync).
        // removePendingQueueItem reads the item at lastIdx, so don't splice
        // the mirror first — let the re-sync inside it update the display.
        this.removePendingQueueItem(sid, lastIdx);
        const preview = (removed.text || "").trim().slice(0, 40);
        this.toast(this.lang === "zh"
                    ? `已撤回队列最后一条：${preview}…`
                    : `Removed last queued: ${preview}…`,
                    "info", 2200);
        return;
      }
      // Queue empty — interrupt the active turn (original behaviour).
      // Backend uses SDK's client.interrupt() — keeps the client / CLI
      // subprocess alive so the next message continues the same
      // conversation without reloading CLAUDE.md / MCP / system prompt.
      if (st.es) { try { st.es.close(); } catch {} st.es = null; }
      st.streaming = false;
      this.streaming = false; this.es = null;
      if (st._streamTimer) { clearInterval(st._streamTimer); st._streamTimer = null; }
      this._streamTimer = null; this.streamElapsed = 0;
      // Token via header (not query) so it stays out of access / proxy logs
      // and history. /interrupt accepts header-or-query backend-side.
      fetch("/api/chat/interrupt?session_id=" + encodeURIComponent(sid),
            { method: "POST", headers: this.hdr() });
    },

    // ====== ask_user_question UI helpers ======
    // Defensive label/description extraction. The backend now normalizes
    // option objects to `{label, description}` (see ask_user_question.py
    // _normalize_questions), but we keep this fallback so a frontend
    // running against an older backend, or a future malformed payload,
    // doesn't render buttons with empty text.
    askOptionLabel(opt) {
      if (opt == null) return "";
      if (typeof opt === "string") return opt;
      return String(opt.label || opt.text || opt.name || opt.value || "");
    },
    askOptionDesc(opt) {
      if (opt == null || typeof opt === "string") return "";
      return String(opt.description || opt.desc || opt.detail || "");
    },
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
      const q = msg.questions?.[qIdx];
      if (!q) return false;
      const cur = msg.pendingAnswers?.[q.question];
      if (q.multiSelect) return Array.isArray(cur) && cur.includes(optionLabel);
      return cur === optionLabel;
    },
    _allAskQuestionsAnswered(msg) {
      const qs = msg.questions || [];
      if (!qs.length) return false;
      return qs.every(q => {
        const v = msg.pendingAnswers?.[q.question];
        if (q.multiSelect) return Array.isArray(v) && v.length > 0;
        return v != null;
      });
    },
    // "Other" free-text fallback. The MCP tool only lets the model give
    // 2-4 fixed buttons; when none fit, the user opens this and types
    // a regular reply. The typed text is sent as the answer for EVERY
    // pending question on the card (typical case is one question; for
    // multi-question cards the model gets the same custom reply for
    // each Q, which is fine — it's only meaningful when no other
    // option fits, so duplicating beats forcing the user to type N
    // separate replies).
    openAskOther(msg) {
      if (msg.submitted) return;
      msg.askOtherOpen = true;
      if (msg.askOtherText == null) msg.askOtherText = "";
      // Focus the textarea on next tick — Alpine has to apply the x-show
      // toggle first or the element isn't in the DOM yet.
      this.$nextTick(() => {
        const ta = document.querySelector(".ask-question .ask-other-textarea");
        if (ta && typeof ta.focus === "function") ta.focus();
      });
    },
    cancelAskOther(msg) {
      msg.askOtherOpen = false;
    },
    async submitAskOther(msg) {
      if (msg.submitted) return;
      const text = (msg.askOtherText || "").trim();
      if (!text) {
        this.toast(this.lang === "zh" ? "请输入回复" : "Please enter a reply",
                    "warn", 2000);
        return;
      }
      msg.submitted = true;
      // Use the typed text as the answer for every question on the card.
      // Backend's _normalize_questions has already canonicalized q.question
      // into the keying we need.
      const answers = {};
      for (const q of (msg.questions || [])) {
        answers[q.question] = text;
      }
      try {
        const r = await fetch(
          `/api/chat/answer/${encodeURIComponent(this.currentId)}/${encodeURIComponent(msg.id)}`,
          {
            method: "POST",
            headers: { ...this.hdr(), "Content-Type": "application/json" },
            body: JSON.stringify({ answers }),
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

    openLightbox(src, alt) {
      if (!src) return;
      this.lightbox = { show: true, src, alt: alt || "" };
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
        // Desktop-only focus — see post-send refocus note: a mobile
        // programmatic focus re-arms body.kb-open without a paired blur
        // and leaves a blank band under the composer during the retry's
        // streaming reply. send() below clears the input either way.
        if (ta) { this.autoGrow(ta); if (!this._isMobileLayout()) ta.focus(); }
        this.send();
      });
    },

    onUserBubbleClick(m) {
      // On desktop the edit button in msg-actions is the primary UI.
      // On touch / hover-none devices (phones, tablets) msg-actions is
      // display:none, so we make the bubble itself tappable for editing.
      if (!window.matchMedia("(hover: none)").matches) return;
      if (m._editing) return;  // already in edit mode — let textarea handle it
      this.startEditMessage(m);
    },

    startEditMessage(m) {
      if (!m || m.role !== "user") return;
      if (this._isBusy(this.currentId)) {
        this.toast(
          this.lang === "zh" ? "等当前回复完成后再编辑" : "Wait for the current reply to finish",
          "warn", 2000
        );
        return;
      }
      // Close any other open edit first (only one inline editor at a time).
      (this.messages || []).forEach(msg => { if (msg !== m && msg._editing) msg._editing = false; });
      m._editText = m.text || "";
      m._editing = true;
    },

    cancelEditMessage(m) {
      if (!m) return;
      m._editing = false;
      m._editText = "";
    },

    commitEditMessage(m) {
      if (!m) return;
      const newText = (m._editText || "").trim();
      if (!newText) return;
      m._editing = false;
      // Truncate everything from this user message onwards (inclusive) —
      // the edited text is sent as a fresh turn, so the old branch
      // (original message + all replies that followed) is discarded from
      // the in-memory view. The JSONL on disk is NOT modified; a reload
      // will show both branches, which is acceptable for now.
      const msgs = this.messages;
      const idx = msgs.indexOf(m);
      if (idx >= 0) msgs.splice(idx);
      this.input = newText;
      this.$nextTick(() => {
        const ta = this.$refs.chatInput;
        if (ta) this.autoGrow(ta);
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
      if (/already in use/i.test(s))
        return zh ? "session 还被上一次的 CLI 占着 — 等几秒再试或切回原模型" : "Session still locked by previous CLI — wait a moment or switch model back";
      if (/Command failed with exit code|ProcessError/i.test(s))
        return zh ? "CLI 子进程异常退出（看 systemctl --user logs muselab）" : "CLI subprocess exited unexpectedly (check service logs)";
      if (/thinking.*signature|cross.*vendor/i.test(s))
        return zh ? "跨厂商切换模型遇到 thinking-signature 问题 — 新建会话或压缩历史" : "Cross-vendor thinking-signature mismatch — new chat or compact history";
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
        () => this.errToast("copy", this.lang === "zh" ? "需要 HTTPS" : "HTTPS required")
      );
    },

    escape(s) {
      return String(s).replace(/[&<>"']/g, c => _HTML_ESCAPE_MAP[c]);
    },
    // Render a user message: HTML-escape + convert raw `\n` into <br>.
    // Replaces the prior `x-text + white-space: pre-wrap` approach,
    // which caused fit-content to compute max-content per-line — so a
    // multi-line user message could never grow wider than its longest
    // single line, leaving an asymmetric gap vs. muse's continuous
    // markdown blocks. With <br> the bubble's max-content collapses
    // to the longest CONTINUOUS line (still subject to the same forced
    // break, but for typical single-paragraph user input this lines
    // up with muse's bubble at the same max-width edge).
    userTextHtml(text) {
      return this.escape(text || "").replace(/\n/g, "<br>");
    },

    // ===== command palette =====
    openPalette() {
      this.palette.query = "";
      this.palette.activeIndex = 0;
      this.palette.fileResults = [];
      this.palette.fileQuery = "";
      this.palette.messageResults = [];
      this.palette.messageQuery = "";
      this.palette.show = true;
      this.$nextTick(() => {
        const el = document.querySelector(".cmd-palette-input");
        if (el) el.focus();
      });
    },
    closePalette() { this.palette.show = false; },
    // Cross-session full-text message search. Mirrors _fetchPaletteFiles
    // shape — debounced from palette input, race-safe via query echo
    // check. Server caps at 30 hits.
    async _fetchPaletteMessages() {
      const q = this.palette.query.trim();
      if (q.length < 2) {
        this.palette.messageResults = [];
        this.palette.messageQuery = "";
        return;
      }
      if (q === this.palette.messageQuery) return;
      this.palette.messageQuery = q;
      this.palette.messageLoading = true;
      try {
        const r = await fetch(
          "/api/chat/search?q=" + encodeURIComponent(q) + "&limit=20",
          { headers: this.hdr() });
        if (!r.ok) { this.palette.messageResults = []; return; }
        const data = await r.json();
        if (this.palette.query.trim() === q) {
          this.palette.messageResults = data.hits || [];
        }
      } catch {
        this.palette.messageResults = [];
      } finally {
        this.palette.messageLoading = false;
      }
    },
    // Jump to a session and scroll to a specific message uuid. Used by
    // the palette's message-search results. If the session isn't open
    // yet, openTab handles loading; we wait until the messages are
    // rendered before scrolling.
    async _jumpToMessage(sid, uuid) {
      await this.openTab(sid);
      // openTab fires loadSession async — give it a tick or two to render.
      for (let i = 0; i < 20; i++) {
        await new Promise(r => setTimeout(r, 50));
        const target = document.querySelector(
          `.msg[data-uuid="${CSS.escape(uuid)}"]`);
        if (target) {
          target.scrollIntoView({ behavior: "smooth", block: "center" });
          target.classList.add("msg-highlight");
          setTimeout(() => target.classList.remove("msg-highlight"), 2400);
          return;
        }
      }
    },
    // Fetch files matching the current palette query against the whole
    // archive (not just loaded tree rows). Called from the palette input's
    // debounced @input. Idempotent — skips if the query hasn't changed.
    async _fetchPaletteFiles() {
      const q = this.palette.query.trim();
      if (q.length < 2) {
        this.palette.fileResults = [];
        this.palette.fileQuery = "";
        return;
      }
      if (q === this.palette.fileQuery) return;
      this.palette.fileQuery = q;
      this.palette.fileLoading = true;
      try {
        const r = await fetch(
          "/api/files/search?q=" + encodeURIComponent(q) + "&limit=30",
          { headers: this.hdr() });
        if (!r.ok) { this.palette.fileResults = []; return; }
        const data = await r.json();
        // Race: only commit if the user hasn't typed something else since
        // we kicked off this request.
        if (this.palette.query.trim() === q) {
          this.palette.fileResults = (data.entries || []).filter(n => !n.is_dir);
        }
      } catch {
        this.palette.fileResults = [];
      } finally {
        this.palette.fileLoading = false;
      }
    },
    // Build the item list freshly each render — cheap (few hundred entries
    // at most) and keeps logic out of x-show templates. Item shape:
    //   { type, label, hint, run }
    paletteItems() {
      const zh = this.lang === "zh";
      const q = this.palette.query.trim().toLowerCase();
      const items = [];

      // 1) Quick actions — always available
      const actions = [
        { type: "act", label: zh ? "新建会话" : "New session",
          hint: "Ctrl+T", run: () => this.newSession() },
        { type: "act", label: zh ? "打开设置" : "Open settings",
          hint: "⚙", run: () => this.openSettings() },
        { type: "act", label: zh ? "切换主题（深/浅）" : "Toggle theme",
          hint: "", run: () => this.toggleTheme() },
        { type: "act", label: zh ? "切换语言到 English" : "Switch language to 中文",
          hint: "", run: () => this.setLang(zh ? "en" : "zh") },
        { type: "act", label: zh ? "刷新文件树" : "Refresh file tree",
          hint: "", run: () => this.reloadTree() },
        { type: "act", label: zh ? "压缩当前会话历史" : "Compact session history",
          hint: "", run: () => this.runCompact() },
        { type: "act", label: zh ? "退出登录" : "Logout",
          hint: "", run: () => this.logout() },
      ];
      items.push(...actions);

      // 2) Open sessions — switch to any session
      for (const s of (this.sessions || [])) {
        items.push({
          type: "session",
          label: s.name || "(untitled)",
          hint: zh ? "会话" : "session",
          run: () => this.activateTab(s.id),
          _searchExtra: (s.first_prompt || "").slice(0, 100),
        });
      }

      // 3) Files — server-side search across the whole archive (not
      // limited to the loaded tree view). Results in palette.fileResults
      // are pre-filtered server-side by name match; we still pass them
      // through the substring scorer below so they get ordered together
      // with action / session matches.
      for (const n of (this.palette.fileResults || [])) {
        if (n.is_dir) continue;
        items.push({
          type: "file",
          label: n.name,
          hint: n.path,
          run: () => this.openFile(n),
        });
      }

      // 4) Cross-session message hits — pre-filtered server-side via
      // /api/chat/search. Each item already matched `q`, so we mark
      // _searchExtra with the full snippet to make sure the substring
      // scorer below keeps them.
      for (const h of (this.palette.messageResults || [])) {
        items.push({
          type: "message",
          label: h.snippet,
          hint: (h.name || "(untitled)") + " · " + (h.role || ""),
          run: () => this._jumpToMessage(h.sid, h.uuid),
          _searchExtra: h.snippet,
        });
      }

      // Fuzzy filter — substring match over label + hint + _searchExtra.
      // Empty query returns first 30 items (so opening the palette without
      // typing still shows something useful — most-recent sessions, etc).
      if (!q) return items.slice(0, 30);
      const matched = [];
      for (const it of items) {
        const hay = (it.label + " " + (it.hint || "") + " " + (it._searchExtra || "")).toLowerCase();
        const i = hay.indexOf(q);
        if (i >= 0) matched.push({ it, score: i + (it.type === "act" ? -100 : 0) });
      }
      matched.sort((a, b) => a.score - b.score);
      return matched.slice(0, 40).map(m => m.it);
    },
    paletteMove(delta) {
      const list = this.paletteItems();
      if (!list.length) return;
      this.palette.activeIndex =
        (this.palette.activeIndex + delta + list.length) % list.length;
    },
    paletteRun(item) {
      if (!item || !item.run) return;
      this.closePalette();
      // Defer the run so the modal close transition can paint before any
      // heavy action (e.g. activateTab triggering loadSession's fetch).
      this.$nextTick(() => { try { item.run(); } catch (e) { console.error(e); } });
    },
    onPaletteEnter() {
      const list = this.paletteItems();
      const idx = Math.min(this.palette.activeIndex, list.length - 1);
      this.paletteRun(list[idx]);
    },

    paletteIcon(type) {
      // Tiny svg id mapping; falls back to a dot if unknown.
      return ({ act: "#i-settings", session: "#i-file-text",
                 file: "#i-file", message: "#i-search" })[type] || "#i-circle";
    },

    // ===== scheduler drawer =====
    async openScheduler() {
      this.scheduler.show = true;
      await this.loadSchedulerTasks();
      await this.loadSchedulerHistory();
      // Opening the drawer = user has seen unread results. Server-side
      // ack so the badge clears on this AND any other tab.
      if (this.scheduler.unreadCount > 0) await this.ackSchedulerUnread();
      // First open ever, or after a reset — populate the draft's model
      // with whatever the user has selected in the chat UI so new tasks
      // don't silently default to an SDK fallback model.
      if (!this.scheduler.draft.editingId && !this.scheduler.draft.model) {
        this.scheduler.draft.model = this.model || "";
      }
    },
    closeScheduler() { this.scheduler.show = false; },
    _resetSchedDraft() {
      this.scheduler.draft = {
        editingId: null,
        name: "", prompt: "", model: this.model || "",
        kind: "daily",
        times: [{ hour: 9, minute: 0 }],
        weekdays: [1, 2, 3, 4, 5],
        day: 1,
        onceDate: "",
        // 2026-05-28: "fresh" default for new tasks — matches cronjob
        // mental model where each fire is independent. Existing tasks
        // edited via editSchedTask hydrate to whatever they were saved as
        // (or fallback to "reuse" if the task predates this field).
        session_mode: "fresh",
      };
    },
    // ---- multi-time helpers (used by the daily-only time list) ----
    // Pad a (hour, minute) pair into the "HH:MM" string that
    // <input type="time"> expects as its `value`.
    padTime(h, m) {
      return String(h ?? 0).padStart(2, "0") + ":"
           + String(m ?? 0).padStart(2, "0");
    },
    // Append a new time slot to the draft. Defaults the new slot to a copy
    // of the last slot (usually what the user wants when adding "another"
    // time) rather than 00:00 which would feel random. Capped at 24 — same
    // ceiling as the backend Pydantic schema.
    addDraftTime() {
      const arr = this.scheduler.draft.times;
      if (arr.length >= 24) return;
      const last = arr[arr.length - 1] || { hour: 9, minute: 0 };
      arr.push({ hour: last.hour, minute: last.minute });
    },
    // Remove a time slot by index. Refuses to remove the last one —
    // there must always be at least one fire time, otherwise the
    // backend schedule has no hh:mm to fall back to.
    removeDraftTime(i) {
      const arr = this.scheduler.draft.times;
      if (arr.length <= 1) return;
      arr.splice(i, 1);
    },
    // Update a slot from an <input type="time"> change event. Value comes
    // as "HH:MM" or empty (cleared). Empty falls back to 00:00 rather
    // than leaving NaN in the data — backend would reject NaN with 422.
    updateDraftTime(i, val) {
      const arr = this.scheduler.draft.times;
      if (!arr[i]) return;
      const [hh, mm] = (val || "00:00").split(":");
      arr[i].hour = Number(hh) || 0;
      arr[i].minute = Number(mm) || 0;
    },
    // Load an existing task into the draft form. The same form template
    // then becomes "edit mode" because draft.editingId is set; the save
    // button switches to PATCH and a Cancel button appears.
    editSchedTask(t) {
      if (!t) return;
      const s = t.schedule || {};
      // Hydrate `times`: prefer the multi-slot list when present (saved by
      // newer tasks); otherwise synthesize a single-slot list from the
      // legacy (hour, minute) pair so pre-multi-time tasks edit fine.
      let times = [];
      if (Array.isArray(s.times) && s.times.length) {
        times = s.times
          .filter(x => x && typeof x.hour === "number" && typeof x.minute === "number")
          .map(x => ({ hour: x.hour, minute: x.minute }));
      }
      if (!times.length) {
        times = [{
          hour: (typeof s.hour === "number") ? s.hour : 9,
          minute: (typeof s.minute === "number") ? s.minute : 0,
        }];
      }
      this.scheduler.draft = {
        editingId: t.id,
        name: t.name || "",
        prompt: t.prompt || "",
        model: t.model || "",
        kind: s.kind || "daily",
        times,
        weekdays: Array.isArray(s.weekdays) ? s.weekdays.slice() : [1, 2, 3, 4, 5],
        day: (typeof s.day === "number") ? s.day : 1,
        onceDate: (s.kind === "once" && s.year && s.month && s.day)
          ? `${s.year}-${String(s.month).padStart(2, "0")}-${String(s.day).padStart(2, "0")}`
          : "",
        // Old tasks lack the field entirely — fall back to "reuse" so
        // their original behavior is preserved across edits. Without
        // this an edit would silently flip them to fresh mode and leak
        // their bound session.
        session_mode: t.session_mode || "reuse",
      };
      // Scroll the form into view — list rows are below it so the user
      // is otherwise looking at empty form they can't see.
      this.$nextTick(() => {
        const el = document.querySelector(".sched-create");
        if (el && typeof el.scrollIntoView === "function") {
          el.scrollIntoView({ behavior: "smooth", block: "start" });
        }
      });
    },
    cancelEditSched() { this._resetSchedDraft(); },
    async loadSchedulerTasks() {
      this.scheduler.loading = true;
      try {
        const r = await fetch("/api/scheduler/tasks", { headers: this.hdr() });
        if (r.ok) {
          const d = await r.json();
          this.scheduler.tasks = d.tasks || [];
          this.scheduler.unreadCount = d.unread_count || 0;
        }
      } finally {
        this.scheduler.loading = false;
      }
    },
    async loadSchedulerHistory() {
      const r = await fetch("/api/scheduler/history?limit=30", { headers: this.hdr() });
      if (r.ok) {
        const d = await r.json();
        this.scheduler.history = d.history || [];
        this.scheduler.unreadCount = d.unread_count || 0;
      }
    },
    // Lazy "show all past runs of this task" expander. Closed by default
    // (the task card list would get too tall if every task pre-rendered
    // its history). First open triggers a fetch; subsequent opens reuse
    // the cached `runs` until the user explicitly refreshes (which
    // currently only happens through closeScheduler/loadSchedulerTasks).
    async toggleTaskRuns(tid) {
      const cur = this.scheduler.taskRuns[tid];
      if (cur && cur.open) {
        // Collapse — keep cached runs around so the next open is instant.
        this.scheduler.taskRuns[tid] = { ...cur, open: false };
        return;
      }
      // First open or re-open. Show the entry expanded immediately with
      // a loading spinner, then fill in the data.
      this.scheduler.taskRuns[tid] = {
        open: true,
        loading: !(cur && cur.runs),
        runs: cur?.runs || [],
      };
      try {
        const r = await fetch(
          `/api/scheduler/tasks/${encodeURIComponent(tid)}/history?limit=100`,
          { headers: this.hdr() });
        if (r.ok) {
          const d = await r.json();
          this.scheduler.taskRuns[tid] = {
            open: true, loading: false, runs: d.history || [],
          };
        } else {
          this.scheduler.taskRuns[tid] = {
            open: true, loading: false, runs: cur?.runs || [],
          };
        }
      } catch (_) {
        this.scheduler.taskRuns[tid] = {
          open: true, loading: false, runs: cur?.runs || [],
        };
      }
    },
    async createSchedTask() {
      const d = this.scheduler.draft;
      if (!d.name.trim() || !d.prompt.trim()) {
        this.toast(this.lang === "zh"
          ? "任务名 / prompt 不能为空" : "Name and prompt are required",
          "warn", 2500);
        return;
      }
      // Build the schedule dict per kind. Backend's ScheduleIn validates
      // ranges + ignores fields irrelevant to the chosen kind.
      // tz_offset_minutes is east-positive (Beijing=+480, NYC=-240); JS
      // reports east-negative so we flip the sign. Sent on every create/
      // edit so the backend fires at the user's local hh:mm regardless of
      // where the server clock thinks it is — fixes the Docker/UTC case
      // where "daily 09:00" fired at 17:00 Beijing time.
      //
      // Time slots: we always send (hour, minute) = times[0] so the
      // Pydantic schema's required fields are satisfied (keeps the
      // single-time path identical to before). For daily with multiple
      // slots, also send the full `times` array — sorted + deduped by
      // (h, m) so display order is stable and "08:00 / 08:00" collapses
      // before hitting the backend.
      const sortedTimes = [...(d.times || [{ hour: 9, minute: 0 }])]
        .filter(t => Number.isFinite(t.hour) && Number.isFinite(t.minute))
        .sort((a, b) => (a.hour - b.hour) || (a.minute - b.minute))
        .filter((t, i, arr) => i === 0
          || t.hour !== arr[i - 1].hour || t.minute !== arr[i - 1].minute);
      const firstTime = sortedTimes[0] || { hour: 9, minute: 0 };
      const sched = {
        kind: d.kind,
        hour: Number(firstTime.hour),
        minute: Number(firstTime.minute),
        tz_offset_minutes: -new Date().getTimezoneOffset(),
      };
      // Prefer the IANA tz name (DST-aware on the backend via ZoneInfo) so a
      // "daily 09:00" task keeps firing at 09:00 wall-clock across DST
      // transitions instead of drifting an hour. tz_offset_minutes above
      // stays as the legacy fallback for any browser without Intl support.
      try {
        const ianaTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
        if (ianaTz) sched.tz = ianaTz;
      } catch (_) { /* keep the offset-only fallback */ }
      // Only attach `times` for daily with >1 slot — keeps weekly/monthly/
      // once and single-time daily payloads byte-identical to the pre-
      // multi-time format, so nothing about old behavior changes.
      if (d.kind === "daily" && sortedTimes.length > 1) {
        sched.times = sortedTimes.map(t => ({
          hour: Number(t.hour), minute: Number(t.minute),
        }));
      }
      if (d.kind === "weekly") {
        if (!d.weekdays.length) {
          this.toast(this.lang === "zh"
            ? "至少选一天" : "Pick at least one weekday", "warn", 2500);
          return;
        }
        sched.weekdays = d.weekdays.slice();
      } else if (d.kind === "monthly") {
        sched.day = Number(d.day);
      } else if (d.kind === "once") {
        if (!d.onceDate) {
          this.toast(this.lang === "zh"
            ? "选个日期" : "Pick a date", "warn", 2500);
          return;
        }
        const [y, m, dy] = d.onceDate.split("-").map(Number);
        sched.year = y; sched.month = m; sched.day = dy;
      }
      const isEdit = !!d.editingId;
      const url = isEdit
        ? "/api/scheduler/tasks/" + encodeURIComponent(d.editingId)
        : "/api/scheduler/tasks";
      let r;
      try {
        r = await fetch(url, {
          method: isEdit ? "PATCH" : "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({
            name: d.name.trim(),
            prompt: d.prompt.trim(),
            schedule: sched,
            model: d.model || "",
            session_mode: d.session_mode || "fresh",
          }),
        });
      } catch (e) {
        // Network-level failure (offline / flaky mobile) — without this the
        // throw becomes an unhandledrejection and the user gets no feedback.
        this.errToast(isEdit ? "save" : "create", String((e && e.message) || e));
        return;
      }
      if (!r.ok) {
        const err = await r.text();
        const verb = isEdit
          ? (this.lang === "zh" ? "保存失败：" : "Save failed: ")
          : (this.lang === "zh" ? "创建失败：" : "Create failed: ");
        this.toast(verb + err, "error", 4000);
        return;
      }
      this._resetSchedDraft();
      await this.loadSchedulerTasks();
      this.toast(
        isEdit
          ? (this.lang === "zh" ? "已保存" : "Saved")
          : (this.lang === "zh" ? "任务已创建" : "Task created"),
        "success", 2000);
    },
    toggleDraftWeekday(w) {
      const wds = this.scheduler.draft.weekdays;
      const i = wds.indexOf(w);
      if (i >= 0) wds.splice(i, 1);
      else wds.push(w);
    },
    fmtSchedule(s) {
      // Human-readable summary of a schedule dict — appears next to the
      // task name in the list.
      if (!s) return "";
      const zh = this.lang === "zh";
      const hh = String(s.hour).padStart(2, "0") + ":"
                + String(s.minute).padStart(2, "0");
      if (s.kind === "daily") {
        // Multi-time daily: show all slots up to 4 inline; collapse beyond
        // that into "每天 N 个时段（首个 HH:MM）" so a 12-slot list doesn't
        // overflow the row.
        const arr = Array.isArray(s.times) ? s.times : null;
        if (arr && arr.length > 1) {
          const fmt = (t) => String(t.hour).padStart(2, "0") + ":"
                          + String(t.minute).padStart(2, "0");
          if (arr.length <= 4) {
            return (zh ? "每天 " : "Daily ") + arr.map(fmt).join(", ");
          }
          return zh
            ? `每天 ${arr.length} 个时段（首个 ${fmt(arr[0])}）`
            : `Daily ${arr.length} times (first ${fmt(arr[0])})`;
        }
        return (zh ? "每天 " : "Daily ") + hh;
      }
      if (s.kind === "weekly") {
        const names = zh
          ? ["一", "二", "三", "四", "五", "六", "日"]
          : ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
        const days = (s.weekdays || []).sort().map(w => names[w]).join(
          zh ? "、" : " ");
        return (zh ? "每周" : "Weekly ") + days + " " + hh;
      }
      if (s.kind === "monthly") {
        return zh ? `每月 ${s.day} 日 ${hh}` : `Monthly day ${s.day} ${hh}`;
      }
      if (s.kind === "once") {
        return zh
          ? `${s.year}-${String(s.month).padStart(2,"0")}-${String(s.day).padStart(2,"0")} ${hh}`
          : `Once ${s.year}-${String(s.month).padStart(2,"0")}-${String(s.day).padStart(2,"0")} ${hh}`;
      }
      return hh;
    },
    async deleteSchedTask(t) {
      const zh = this.lang === "zh";
      let ok;
      // Confirm-text varies by mode — fresh keeps all historical run
      // sessions, reuse removes the single bound session. Default to
      // "reuse" wording for ancient tasks that lack the field entirely
      // (these are the ones that DO have a bound session that'll get
      // deleted), matching scheduler.delete_task's fallback.
      const mode = t.session_mode || "reuse";
      const body = mode === "fresh"
        ? (zh
            ? `确定删除「${t.name}」？历史运行产生的 session 不会被删除（可在会话列表中手动清理）。`
            : `Delete "${t.name}"? Past-run sessions are NOT removed — clean them up in the sessions list if you want.`)
        : (zh
            ? `确定删除「${t.name}」？关联的 [定时] 会话会一并删除。`
            : `Delete "${t.name}"? The bound [Scheduled] session is removed too.`);
      try {
        ok = await this.confirm({
          title: zh ? "删除任务" : "Delete task",
          body,
          danger: true,
          okText: zh ? "删除" : "Delete",
        });
      } catch (e) {
        this.toast(zh ? "确认弹窗异常：" + (e && e.message)
                       : "Confirm error: " + (e && e.message),
                    "error", 4000);
        return;
      }
      if (!ok) return;
      try {
        const r = await fetch("/api/scheduler/tasks/" + encodeURIComponent(t.id),
          { method: "DELETE", headers: this.hdr() });
        if (!r.ok) {
          // Surface the actual HTTP status + body so we stop the "delete
          // did nothing" mystery (previously: silent return on r.ok=false).
          let body = "";
          try { body = await r.text(); } catch (_) {}
          this.toast(
            (zh ? "删除失败 HTTP " : "Delete failed HTTP ")
              + r.status + (body ? ": " + body.slice(0, 200) : ""),
            "error", 5000);
          return;
        }
        if (this.scheduler.draft.editingId === t.id) this._resetSchedDraft();
        await this.loadSchedulerTasks();
        this.toast(zh ? "任务已删除" : "Task deleted", "success", 2000);
      } catch (e) {
        // Network-level failure — typically iOS Safari losing the request
        // mid-flight on flaky 4G. Show the user what happened.
        this.toast(
          (zh ? "删除请求失败：" : "Delete network error: ")
            + (e && (e.name + (e.message ? " — " + e.message : ""))),
          "error", 5000);
      }
    },
    async toggleSchedEnabled(t) {
      let r;
      try {
        r = await fetch("/api/scheduler/tasks/" + encodeURIComponent(t.id), {
          method: "PATCH",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: !t.enabled }),
        });
      } catch (e) {
        // Network failure on the enable/disable toggle — surface it instead
        // of letting the rejection escape with no UI feedback.
        this.errToast("save", String((e && e.message) || e));
        return;
      }
      if (r.ok) await this.loadSchedulerTasks();
    },
    // Out-of-schedule run. Backend dispatches a background task; we poll
    // history once and again after a short delay so the new entry shows
    // up without waiting for the next periodic refresh.
    async runSchedTaskNow(t) {
      if (!t || !t.id || this.schedRunning[t.id]) return;
      this.schedRunning[t.id] = true;
      try {
        const r = await fetch(
          "/api/scheduler/tasks/" + encodeURIComponent(t.id) + "/run",
          { method: "POST", headers: this.hdr() });
        if (!r.ok) throw new Error(await r.text());
        this.toast(this.lang === "zh" ? "已触发，等待结果…" : "Triggered — awaiting result…",
                    "info", 2000);
        // Poll history a couple of times to surface the new entry; backend
        // appends synchronously in _execute_task's finally so a few-second
        // delay catches even slow LLM turns.
        // IMPORTANT: each poll MUST swallow its own error AND the final one
        // MUST clear schedRunning in a `finally` — a throwing
        // loadSchedulerHistory used to skip the `= false` line, wedging the
        // button :disabled forever (every later click then hit the
        // `schedRunning[t.id]` guard and silently no-op'd).
        setTimeout(() => this.loadSchedulerHistory().catch(() => {}), 1500);
        setTimeout(async () => {
          try {
            await this.loadSchedulerHistory();
          } catch (_) {
            /* history refresh failed — still release the button below */
          } finally {
            this.schedRunning[t.id] = false;
          }
        }, 8000);
      } catch (e) {
        this.toast((this.lang === "zh" ? "触发失败: " : "Trigger failed: ")
                    + ((e && e.message) || e), "error", 4000);
        this.schedRunning[t.id] = false;
      }
    },
    retrySchedHistory(h) {
      if (!h || !h.task_id) return;
      const task = this.scheduler.tasks.find(t => t.id === h.task_id);
      if (task) this.runSchedTaskNow(task);
    },
    // Delete a single history row. Optimistic UI: remove from
    // scheduler.history immediately so the row disappears even if the
    // network is slow; if the DELETE fails, refetch to restore truth.
    // No confirm dialog — single rows are cheap to delete-by-accident
    // (one click of "重试" on the same task brings it back) and the
    // confirm modal popping for every × would be noisy on phones.
    async deleteSchedHistoryEntry(h) {
      if (!h || h.ts == null) return;
      const before = this.scheduler.history.slice();
      this.scheduler.history = this.scheduler.history.filter(
        x => !(x.ts === h.ts && x.task_id === h.task_id));
      try {
        const r = await fetch(
          `/api/scheduler/history/${encodeURIComponent(h.ts)}`
            + `?task_id=${encodeURIComponent(h.task_id || "")}`,
          { method: "DELETE", headers: this.hdr() });
        if (!r.ok) throw new Error("HTTP " + r.status);
      } catch (e) {
        // Restore + re-sync from server (other tabs / pruning could
        // have changed it in the meantime).
        this.scheduler.history = before;
        this.toast(this.lang === "zh" ? "删除失败" : "Delete failed", "error", 2500);
        this.loadSchedulerHistory().catch(() => {});
      }
    },
    // Wipe ALL history. Behind a confirm — this is destructive and
    // there's no undo. Unread badge is left alone (independent flag);
    // user can dismiss the badge separately by closing+reopening the
    // drawer (which already calls ackSchedulerUnread).
    async clearAllSchedHistory() {
      const zh = this.lang === "zh";
      if (!this.scheduler.history.length) return;
      const ok = await this.confirm({
        title: zh ? "清空运行记录" : "Clear history",
        body: zh
          ? `将删除全部 ${this.scheduler.history.length} 条最近运行记录。任务本身和绑定的会话不受影响，只是不再显示在这个列表里。无法撤销。`
          : `Will delete all ${this.scheduler.history.length} recent-run entries. Tasks themselves and bound sessions are untouched — only the list display is cleared. Cannot be undone.`,
        okText: zh ? "清空" : "Clear",
        danger: true,
      });
      if (!ok) return;
      const before = this.scheduler.history.slice();
      this.scheduler.history = [];
      try {
        const r = await fetch("/api/scheduler/history",
          { method: "DELETE", headers: this.hdr() });
        if (!r.ok) throw new Error("HTTP " + r.status);
      } catch (e) {
        this.scheduler.history = before;
        this.toast(this.lang === "zh" ? "清空失败" : "Clear failed", "error", 2500);
      }
    },
    async ackSchedulerUnread() {
      const r = await fetch("/api/scheduler/ack", {
        method: "POST", headers: this.hdr(),
      });
      if (r.ok) {
        const d = await r.json();
        this.scheduler.unreadCount = d.unread_count || 0;
      }
    },
    async fetchSchedulerUnread() {
      // Called from the heartbeat — keeps the bell badge live without
      // requiring the user to open the drawer. Also detects a tick-up
      // since last poll → triggers foreground vibration (if user opted in).
      try {
        const r = await fetch("/api/scheduler/tasks", { headers: this.hdr() });
        if (r.ok) {
          const d = await r.json();
          const next = d.unread_count || 0;
          if (next > this._lastSeenUnread && this.notifyEnabled) {
            // 3-pulse "task done" pattern. navigator.vibrate is a no-op
            // (returns false) on devices without a vibration motor, so
            // it's safe to call unconditionally.
            try { navigator.vibrate?.([120, 60, 120]); } catch {}
          }
          this._lastSeenUnread = next;
          this.scheduler.unreadCount = next;
        }
      } catch {}
    },
    saveNotifyPrefs() {
      try {
        localStorage.setItem("muselab_notify_enabled",
          this.notifyEnabled ? "1" : "0");
      } catch {}
    },
    // Single entry point for the one notification switch. ON does best-
    // effort across capabilities: (1) request Notification permission,
    // (2) subscribe to Web Push so background events come through even
    // when muselab is closed. Vibration is automatic — `notifyEnabled`
    // is the gate fetchSchedulerUnread() checks. We treat "some capability
    // worked" as success; on most desktops vibration is a no-op and push
    // is the meaningful part, on iOS-without-PWA push fails silently and
    // only foreground vibration works — but the toggle still reflects
    // "you'll be notified to whatever extent your device allows".
    async onNotifyToggle(ev) {
      const wantOn = ev?.target?.checked ?? this.notifyEnabled;
      if (wantOn) {
        // Best-effort push subscribe. Even if it fails (browser unsupported,
        // permission denied, iOS-without-PWA, …), foreground vibrate still
        // works, so we keep the switch ON unless the user explicitly toggles
        // it off. pushSubscribe() already toasts its own error reason.
        const pushOk = await this.pushSubscribe();
        this.notifyEnabled = true;
        if (!pushOk) {
          // Surface the partial-success state so the user understands why
          // they may not get background pushes — but doesn't think the
          // whole switch is broken.
          this.toast(this.lang === "zh"
            ? "已开启前台提醒；后台推送不可用（详见上一条）"
            : "Foreground reminders on; background push unavailable (see prior toast)",
            "warn", 4500);
        }
      } else {
        await this.pushUnsubscribe();
        this.notifyEnabled = false;
      }
      this.saveNotifyPrefs();
    },
    async pushSubscribe() {
      // Browser feature checks upfront so we can give a clearer error
      // than "TypeError: Cannot read property 'subscribe' of undefined".
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
        this.toast(this.lang === "zh"
          ? "此浏览器不支持 Web Push" : "This browser doesn't support Web Push",
          "warn", 3500);
        return false;
      }
      try {
        const perm = await Notification.requestPermission();
        if (perm !== "granted") {
          this.toast(this.lang === "zh"
            ? "未授权通知 — 在浏览器设置里允许后重试"
            : "Notification permission denied", "warn", 4000);
          return false;
        }
        // Make sure the SW is installed + activated before we touch
        // pushManager — pushManager.subscribe on an installing worker
        // throws on Firefox.
        const reg = await navigator.serviceWorker.register("/sw.js");
        await navigator.serviceWorker.ready;
        // Public key arrives as urlsafe-b64. PushManager wants raw bytes.
        const r = await fetch("/api/push/vapid-public", { headers: this.hdr() });
        if (!r.ok) throw new Error("vapid fetch failed: " + r.status);
        const { public_key } = await r.json();
        const sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: this._urlsafeB64ToBytes(public_key),
        });
        const sr = await fetch("/api/push/subscribe", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify(sub.toJSON()),
        });
        if (!sr.ok) throw new Error("subscribe POST failed: " + sr.status);
        this.toast(this.lang === "zh"
          ? "已开启推送通知" : "Push notifications enabled",
          "success", 2500);
        return true;
      } catch (e) {
        this.toast((this.lang === "zh" ? "开启失败：" : "Push subscribe failed: ")
          + (e.message || e), "error", 5000);
        return false;
      }
    },
    async pushUnsubscribe() {
      try {
        if (!("serviceWorker" in navigator)) return;
        const reg = await navigator.serviceWorker.getRegistration("/sw.js");
        if (!reg) return;
        const sub = await reg.pushManager.getSubscription();
        if (!sub) return;
        const endpoint = sub.endpoint;
        await sub.unsubscribe();
        await fetch("/api/push/unsubscribe", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint }),
        });
      } catch (e) {
        // Silent — even if the cleanup fails, the local subscription is gone.
      }
    },
    _urlsafeB64ToBytes(s) {
      const pad = "=".repeat((4 - (s.length % 4)) % 4);
      const b64 = (s + pad).replace(/-/g, "+").replace(/_/g, "/");
      const raw = atob(b64);
      const buf = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);
      return buf;
    },
    loadNotifyPrefs() {
      // New key first; fall back to the 2026-05 vibrate/push pair so
      // existing users don't get silently flipped to OFF on upgrade.
      // Migration rule: either old flag → new switch ON. After one save
      // the old key is gone; next read just hits the new key.
      try {
        const v = localStorage.getItem("muselab_notify_enabled");
        if (v === "0" || v === "1") {
          this.notifyEnabled = v === "1";
          return;
        }
        // Legacy shape — `{"vibrate": bool, "push": bool}`.
        const p = JSON.parse(localStorage.getItem("muselab_notify") || "{}");
        if (p && (p.vibrate === true || p.push === true)) {
          this.notifyEnabled = true;
          this.saveNotifyPrefs();
          localStorage.removeItem("muselab_notify");
        }
      } catch {}
    },
    async openSchedTaskSession(t) {
      // Jump straight to the muselab session bound to this scheduled
      // task. Use openTab — NOT activateTab — because the bound session
      // may not be in openTabIds yet (user hasn't manually opened it).
      // activateTab only switches currentId; without push to openTabIds
      // the tab strip wouldn't show this session and the user would see
      // messages with no visible tab label.
      this.closeScheduler();
      const sid = (t && t.session_id) || t;
      if (!sid) return;
      await this.openTab(sid);
    },
    fmtSchedTime(ts) {
      if (!ts) return "—";
      const d = new Date(ts * 1000);
      const pad = n => String(n).padStart(2, "0");
      const today = new Date();
      const sameDay = d.toDateString() === today.toDateString();
      const hh = pad(d.getHours()) + ":" + pad(d.getMinutes());
      if (sameDay) return (this.lang === "zh" ? "今天 " : "today ") + hh;
      return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hh}`;
    },
  };
}
