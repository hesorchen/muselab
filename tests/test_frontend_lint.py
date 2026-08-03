"""Frontend static lint — narrow but high-value checks for bug classes that
already shipped once. These read frontend/ as plain text; no JS runtime
needed.

Why this exists: JS object literals silently shadow earlier definitions when
the same key appears twice. We hit this in the multi-tab sprint
(2026-05-17) — a second `closeChatTab(...)` was added below the first one
and the upper definition was lost without any warning. The duplicate sat
undiscovered until a button stopped working. Pytest is the cheapest
guard."""
from __future__ import annotations
import re
from collections import Counter
from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
# A few checks in here span the FE/BE seam (e.g. a field the backend must emit
# for a binding to have anything to render), so the backend source is read the
# same source-text way rather than importing it.
BACKEND = Path(__file__).resolve().parents[1] / "backend"


# Match top-level method definitions inside the Alpine x-data object:
#     methodName(args) {
#     async methodName(args) {
#     *gen(args) {
# - Exactly 4 spaces of indent (the component's outer indent level).
# - Strips optional `async ` / `static ` / `*` prefix so it doesn't capture
#   the keyword as the name. Without this, `async closeChatTab` matched as
#   `async` and missed the real collision.
# - Excludes arrow assignments (`const foo = () =>`) and `function ` decls.
# `(?!\{)` negative lookahead excludes calls like `_report({ ... })` where
# the open paren is immediately followed by a `{` (object literal arg). A
# real method def starts with `name(arg…)` or `name()`, never `name({`.
_METHOD_DEF = re.compile(
    r"^    (?:async\s+|static\s+|\*\s*)?([a-zA-Z_][a-zA-Z0-9_]*)\s*\((?!\{)"
)


def test_app_js_has_no_duplicate_method_definitions():
    """Guard against silently shadowed methods in app.js.

    Real bug, 2026-05-17: two `closeChatTab(id)` definitions coexisted —
    JS kept only the second, so the toolbar's close button (wired to the
    first) silently broke. This test would have caught it instantly."""
    text = (FRONTEND / "app.js").read_text(encoding="utf-8")

    names = []
    for line in text.splitlines():
        m = _METHOD_DEF.match(line)
        if not m:
            continue
        name = m.group(1)
        # Skip JS keywords that legitimately appear in the same column shape
        # (if/for/while/switch/return/etc.) — not method defs.
        if name in {
            "if", "for", "while", "switch", "return", "throw", "catch",
            "do", "else", "function", "case",
        }:
            continue
        names.append(name)

    dupes = [n for n, c in Counter(names).items() if c > 1]
    assert not dupes, (
        f"Duplicate method definitions in app.js: {dupes}. "
        "JS keeps only the LAST one — the earlier definitions are dead "
        "code and any caller wired to them silently breaks. Rename or "
        "merge the duplicates."
    )


def test_i18n_zh_en_key_parity():
    """Both language sections in i18n/index.js must define the same set of
    keys. A missing translation causes `t('foo.bar')` to fall back to the
    key literal — exposed to users as 'foo.bar' on screen. We hit this
    historically when a quick zh-only addition landed without the en
    mirror; the English UI showed raw keys until a user reported it."""
    text = (FRONTEND / "i18n" / "index.js").read_text(encoding="utf-8")
    # The file has shape `window.MUSELAB_STRINGS = { zh: {...}, en: {...} };`
    # — split it at the top-level "zh:" / "en:" labels. The blocks are
    # several hundred lines but contain no nested object literals that look
    # like another language label, so a greedy "until next label" works.
    zh_match = re.search(r"\bzh:\s*\{(.*?)\n  \},\s*en:", text, re.S)
    en_match = re.search(r"\ben:\s*\{(.*?)\n  \},?\s*\};", text, re.S)
    assert zh_match, "couldn't find zh: { ... } block in i18n/index.js"
    assert en_match, "couldn't find en: { ... } block in i18n/index.js"
    zh_keys = set(re.findall(r'"([\w.]+)"\s*:', zh_match.group(1)))
    en_keys = set(re.findall(r'"([\w.]+)"\s*:', en_match.group(1)))
    only_zh = zh_keys - en_keys
    only_en = en_keys - zh_keys
    assert not only_zh and not only_en, (
        f"i18n key drift between zh and en. "
        f"only in zh: {sorted(only_zh)[:8]}; "
        f"only in en: {sorted(only_en)[:8]}. "
        f"Add the missing translations or `t()` will leak raw keys to "
        f"users on the side that's missing them."
    )


def test_frontend_positions_muselab_as_a_workspace_agent_workbench():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    constants = (FRONTEND / "data" / "constants.js").read_text(encoding="utf-8")
    i18n = (FRONTEND / "i18n" / "index.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    combined = "\n".join((app, constants, i18n, html, css))

    assert '"empty.preview_tagline": "MuseLab · 本地 Agent 工作台"' in i18n
    assert '"empty.preview_tagline": "MuseLab · Local Agent Workbench"' in i18n
    assert '"onboard.no_provider_title":' in i18n
    assert 'class="muse-mascot' in html
    assert app.count("greek:") == 9

    for dead_symbol in (
        "MUSELAB_INSPIRE_PROMPTS",
        "onboardingSubdirs",
        "SKILL_TRIGGERS",
        "skillSuggestions",
        "onboardingPrompts",
        "shuffleInspirePrompts",
        "useSuggestedPrompt",
        "claudeMdChipTitle",
        "openClaudeMdHelp",
        "museOpener",
        "pickMascotAndAsk",
        "startOrganize",
    ):
        assert dead_symbol not in combined
    for stale_copy in ("未配档案", "真正懂你", "health/foo.md", "archive root"):
        assert stale_copy not in combined.lower()

    # Compatibility/security/file-type semantics are intentionally not part of
    # the positioning cleanup.
    assert "archive_root" in app
    assert '"archive_project"' in app
    assert "noarchive" in html
    assert 'data-ext="archive"' in css


def test_memory_center_and_chat_recall_trace_are_wired():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    chat = (BACKEND / "chat.py").read_text(encoding="utf-8")
    api = (BACKEND / "api_memory.py").read_text(encoding="utf-8")

    assert "loadMemorySettings()" in app
    assert "saveMemorySettings()" in app
    assert "memorySkillAction(item, action)" in app
    assert "memorySaveMessage(message)" in app
    assert "_startMemoryMonitor()" in app
    assert "muselab_memory_artifacts_seen" in app
    assert 'data-page="memory"' in index
    assert "Skill 只能生成候选" in index
    assert "memoryRecall" in index
    assert "_done_memory_recall = mem0.pop_recall_trace(session_id)" in chat
    assert '"memory_recall": _done_memory_recall' in chat
    assert '@router.post("/skills/{artifact_id}/approve")' in api


def test_image_generation_history_prompt_actions_are_wired():
    """History prompt actions need both Alpine handlers and template wiring."""
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert "copyImageGenPrompt(job)" in app
    assert "reuseImageGenPrompt(job)" in app
    assert '@click="copyImageGenPrompt(job)"' in index
    assert '@click="reuseImageGenPrompt(job)"' in index
    assert 'x-ref="imageGenPrompt"' in index


def test_preview_tabs_persist_reading_positions_and_html_frames():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    start = app.index("async openFile(n, opts = {})")
    end = app.index("\n    async csvLoadPage", start)
    open_file = app[start:end]

    assert "this._capturePreviewViewState(this.selected)" in open_file
    assert "this._schedulePreviewViewRestore(cachedPath, loadSeq)" in open_file
    assert "this.csvLoadPage(targetView.csvOffset)" in open_file
    assert 'x-ref="previewBody"' in html
    assert '@scroll.passive="onPreviewViewportScroll()"' in html
    assert 'd.__muselab === "preview-scroll"' in app
    assert '__muselab: "preview-scroll-restore"' in app
    assert "HTML_PREVIEW_CACHE_MAX: 4" in app
    assert "next.length >= this.HTML_PREVIEW_CACHE_MAX" in app
    assert 'x-for="entry in htmlPreviewFrames" :key="entry.path"' in html
    assert ':src="rawUrl(entry.path, {preview:true})"' in html
    assert "this._htmlPreviewMessageOwner(e.source)" in app


def test_mobile_preview_captures_before_hiding_and_pins_tree_taps():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    tab_start = app.index("setMobileTab(next)")
    tab_end = app.index("\n    // The queue is authoritative", tab_start)
    mobile_tab = app[tab_start:tab_end]
    click_start = app.index("async onNodeClick(ev, n)")
    click_end = app.index("\n    // ===== multi-select helpers", click_start)
    node_click = app[click_start:click_end]

    assert mobile_tab.index("this._capturePreviewViewState(ownerPath)") < (
        mobile_tab.index("this.mobileTab = next")
    )
    assert mobile_tab.index(
        "this._restorePreviewViewState(ownerPath, ownerLoadSeq)"
    ) < mobile_tab.index("this.mobileTab = next")
    assert "this._schedulePreviewViewRestore(ownerPath, ownerLoadSeq)" in mobile_tab
    assert mobile_tab.index("this.messagesReady = false") < mobile_tab.index(
        "this.mobileTab = next"
    )
    assert "this._afterPaint(() => {" in mobile_tab
    assert 'this.mobileTab !== "chat"' in mobile_tab
    assert 'this.mobileTab !== "preview"' in app
    assert "preview: !this._isMobileLayout()" in node_click
    assert html.count("@click=\"setMobileTab('") == 3


def test_terminal_restore_and_reconnect_do_not_hijack_mobile_tab():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    restore_start = app.index("async fetchTerminals(")
    restore_end = app.index("\n    async createTerminal", restore_start)
    restore = app[restore_start:restore_end]
    terminal_start = app.index("async openTerminal(")
    terminal_end = app.index("\n    _teardownTerminalView", terminal_start)
    terminal = app[terminal_start:terminal_end]

    assert "this._restorePendingMobileTab();" in app
    assert "this.openTerminal(this.activeTerminalId, { reveal: false })" in restore
    assert "async openTerminal(id, { reconnect = false, reveal = true } = {})" in terminal
    assert 'if (reveal && this._isMobileLayout()) this.setMobileTab("preview")' in terminal
    assert "this.openTerminal(id, { reconnect: true, reveal: false })" in terminal


def test_primary_mobile_surfaces_keep_native_touch_scrolling():
    css = (FRONTEND / "styles.css").read_text()
    marker = "contract explicit on every primary mobile surface."
    start = css.index(marker)
    end = css.index("}", css.index(".terminal-manager-pop", start))
    contract = css[start:end]

    for selector in (".filelist", ".preview-body:not(.terminal-active)",
                     ".chat-body", ".terminal-manager-pop"):
        assert selector in contract
    assert "min-height: 0" in contract
    assert "overflow-y: auto" in contract
    assert "-webkit-overflow-scrolling: touch" in contract
    assert "touch-action: manipulation" in contract
    assert "touch-action: pan-y" not in contract


def test_enter_submission_waits_for_ime_composition():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    helper_start = app.index("_claimNonImeEnter(ev)")
    helper_end = app.index("\n    },", helper_start)
    helper = app[helper_start:helper_end]
    ime_start = app.index("_isImeComposingEvent(ev)")
    ime_end = app.index("\n    },", ime_start)
    ime = app[ime_start:ime_end]
    assert "ev.isComposing" in ime
    assert "ev.keyCode === 229" in ime
    assert "ev.which === 229" in ime
    assert 'ev.key === "Process"' in ime
    assert helper.index("return false") < helper.index("ev.preventDefault()")

    assert '@keydown.enter="confirmModalOnEnter($event)"' in html
    assert '@keydown.enter="commitRenameTabOnEnter($event)"' in html
    assert '@keydown.enter="pickerCommitInlineRenameOnEnter($event)"' in html
    assert '@keydown.enter="onEnter($event)"' in html
    assert '@keydown.enter.prevent="commitRenameTab()"' not in html
    assert '@keydown.enter.prevent="pickerCommitInlineRename()"' not in html
    assert '@keydown.enter.prevent.stop="onEnter($event)"' not in html


def test_chat_arrow_keys_walk_user_input_history_and_restore_draft():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    start = app.index("_chatInputHistory()")
    end = app.index("\n    _cancelMentionLookup()", start)
    history = app[start:end]

    assert "st._earlierMessages" in history
    assert "st._laterMessages" in history
    assert 'm.role === "user"' in history
    assert "draft._historyIndex = index - 1" in history
    assert "draft._historyIndex = index + 1" in history
    assert "draft._historyDraft = this.input" in history
    assert "const originalDraft = draft._historyDraft" in history
    assert "this._resetChatInputHistory(draft)" in history
    assert 'this.input.includes("\\n")' in history
    assert "this._isImeComposingEvent(ev)" in history
    assert '@keydown.up="onChatArrowUp($event)"' in html
    assert '@keydown.down="onChatArrowDown($event)"' in html


def test_pane_popups_escape_clipping_but_stay_below_global_overlays():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    files_start = html.index('<aside class="pane files"')
    files_end = html.index("<header", files_start)
    files = html[files_start:files_end]
    assert "'pane-floating-layer': workspaceMenuOpen" in files

    preview_start = html.index('<section class="pane preview"')
    preview_end = html.index("<header", preview_start)
    preview = html[preview_start:preview_end]
    for state in ("terminalManagerOpen", "editorTabPickerOpen",
                  "previewTabCtxMenu"):
        assert state in preview

    chat_start = html.index('<aside class="pane chat"')
    chat_end = html.index("<header", chat_start)
    chat = html[chat_start:chat_end]
    for state in ("sessionPickerOpen", "tabCtxMenu", "ctxBreakdown.show",
                  "composerSettingsOpen", "mentionShow", "slashShow"):
        assert state in chat

    layer_start = css.index(".pane.pane-floating-layer")
    layer_end = css.index("}", layer_start)
    layer = css[layer_start:layer_end]
    assert "z-index: 150" in layer
    assert "overflow: visible" in layer

    # Pane-local floating content must clear navigation, while every true
    # application overlay remains above it.
    assert "height: 48px !important; z-index: 100" in css
    assert "position: fixed; inset: 0; z-index: 200" in css
    assert "position: fixed; z-index: 800" in css
    assert "position: fixed; inset: 0; z-index: 900" in css


def test_multi_workspace_ui_and_folder_browser_are_wired_end_to_end():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    backend = (FRONTEND.parent / "backend" / "workspaces.py").read_text(
        encoding="utf-8")

    assert "cwd: seedCwd" in app
    assert "primaryWorkspacePath()" in app
    assert "fileWorkspacePath()" in app
    assert 'headers["X-Muselab-Workspace"] = encodeURIComponent(cwd)' in app
    assert '"&workspace=" + encodeURIComponent(this.fileWorkspacePath())' in app
    assert "workspace-picker" in html
    assert 'class="workspace-info-btn"' in html
    assert "workspace.help" in html
    assert "workspaceOpenTabIds()" in html
    assert "chat-grid" not in html
    assert "_workspacePreviewTabs(surface = {})" in app
    assert "async _refreshSessionsAfterWorkspaceRegistryChange()" in app
    assert 'class="modal workspace-browser-modal"' in html
    assert ':data-workspace-path="directory.path"' in html
    assert 'class="btn-primary workspace-browser-confirm"' in html
    assert "async addWorkspacePathManually()" in app
    assert '@router.get("/browse"' in backend
    assert ".workspace-browser-modal" in css
    mobile = css[css.index("@media (max-width: 720px)", css.index(
        ".workspace-browser-modal")):]
    assert "height: 100dvh" in mobile


def test_workspace_picker_supports_mouse_and_touch_reordering():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert 'class="workspace-picker-drag" draggable="true"' in html
    assert '@dragstart="onWorkspaceDragStart($event, workspace.path)"' in html
    assert '@pointerdown.stop="onWorkspacePointerDown($event, workspace.path)"' in html
    assert '@pointermove.window="onWorkspacePointerMove($event)"' in html
    assert 'fetch("/api/chat/workspaces/order"' in app
    assert 'localStorage.setItem("muselab_workspace_order_v1"' in app
    assert "touch-action: none" in css


def test_workspace_switch_moves_files_preview_and_conversation_together():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    start = app.index("async switchWorkspace(path)")
    end = app.index("\n    closeWorkspaceBrowser()", start)
    switch = app[start:end]

    assert "const surfaceReady = Promise.resolve(" in switch
    assert "this._changeWorkspaceSurface(path)" in switch
    assert "await this._pullWorkspaceSessions(path)" in switch
    assert "await this._ensureSessionLoaded(target.id)" in switch
    assert "await Promise.all([surfaceReady, targetReady])" in switch
    assert "_pullAllSessions()" not in switch
    target_ready = switch[switch.index("const targetReady"):switch.index(
        "const [surfaceOk, target]")]
    assert "this.currentId =" not in target_ready
    assert "this.openTab(" not in target_ready
    assert switch.index("await Promise.all([surfaceReady, targetReady])") < switch.index(
        "await this.openTab(target.id)")
    assert "return this.currentWorkspacePath()" in app
    assert "workspaceSurfaces: this.workspaceSurfaces" in app
    files_start = html.index('<aside class="pane files"')
    files_end = html.index("</aside>", files_start)
    chat_start = html.index('<aside class="pane chat"')
    chat_end = html.index("</aside>", chat_start)
    assert "files-head-workspace" in html[files_start:files_end]
    assert "activity-center-btn" not in html[files_start:files_end]
    assert "workspace-picker" not in html[chat_start:chat_end]
    assert "activity-center-btn" in html[chat_start:chat_end]


def test_workspace_switch_uses_scoped_session_window_and_merges_it():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    pull_start = app.index("_mergeWorkspaceSessionList(raw, path)")
    pull_end = app.index("\n    async _refreshSessionsAfterWorkspaceRegistryChange()", pull_start)
    pull = app[pull_start:pull_end]

    assert 'workspace_only: "1"' in pull
    assert 'limit: "20"' in pull
    assert "const remembered = this.workspaceLastSession[path]" in pull
    assert "headers: this.conversationHdr(path)" in pull
    assert "this._mergeWorkspaceSessionList" in pull
    assert "olderTarget" not in pull

    ensure_start = app.index("async _ensureSessionLoaded(sid)")
    ensure_end = app.index("\n    async loadSession(sid", ensure_start)
    ensure = app[ensure_start:ensure_end]
    assert "const canonicalBehind = st._loaded" in ensure
    assert "updated > seen" in ensure
    assert "canonicalBehind ? { quiet: true } : {}" in ensure
    assert "this._applySessionList([...incoming, ...otherWorkspaces])" in pull
    assert "this._optimisticMetas && this._optimisticMetas[session.id]" in pull
    assert "return { ok: true, sessions }" in pull
    assert "this.sessions =" not in pull


def test_boot_uses_workspace_registry_cache_without_blocking_revalidation():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    boot_start = app.index("async _bootApp()")
    boot_end = app.index("\n    // Start the always-on", boot_start)
    boot = app[boot_start:boot_end]

    assert "const restoredWorkspaceRegistry = this._restoreSessionWorkspaceCache()" in boot
    assert "restoreCache: false" in boot
    assert "if (!restoredWorkspaceRegistry) await workspaceRegistryReady" in boot
    assert "if (restoredWorkspaceRegistry)" in boot
    assert "return this._changeWorkspaceSurface(result.fallback)" in boot
    assert "!this._workspaceIsCurrent(result.requested)" in boot

    restore_start = app.index("    _restoreSessionWorkspaceCache() {")
    restore_end = app.index("\n    async fetchSessionWorkspaces(", restore_start)
    restore = app[restore_start:restore_end]
    assert "10 * 60_000" in restore
    assert "return this.sessionWorkspaces.length > 0" in restore

    fetch_start = app.index("async fetchSessionWorkspaces({")
    fetch_end = app.index("\n    async _pullAllSessions()", fetch_start)
    fetch_workspaces = app[fetch_start:fetch_end]
    assert "const requestSeq = ++this._workspaceRegistrySeq" in fetch_workspaces
    assert "if (requestSeq !== this._workspaceRegistrySeq) return false" in fetch_workspaces


def test_chat_refresh_and_stats_requests_run_concurrently():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    refresh_start = app.index("async refreshChat()")
    refresh_end = app.index("\n    // ===== prefs =====", refresh_start)
    refresh = app[refresh_start:refresh_end]
    stats_start = app.index("async fetchStats()")
    stats_end = app.index("\n    async fetchCodexRateLimit", stats_start)
    stats = app[stats_start:stats_end]

    assert "void Promise.resolve(this.fetchStats())" in refresh
    assert "await Promise.all([" in refresh
    assert "this.fetchContextInfo()" in refresh
    assert "this.refreshSessions()" in refresh
    assert "this._reloadSessionCoalesced(sid, { quiet: true })" in refresh
    assert refresh.index("this.fetchContextInfo()") < refresh.index(
        "this._reloadSessionCoalesced")
    assert "await Promise.allSettled([" in stats
    assert "this.fetchMcp()" in stats
    assert "this.fetchRateLimit()" in stats
    assert "providers," in stats

    reconcile_start = app.index("_reconcileOpenSession(next)")
    reconcile_end = app.index("\n    // Field-level equality", reconcile_start)
    reconcile = app[reconcile_start:reconcile_end]
    assert "st._seenUpdated = Math.max" not in reconcile
    assert "const stillBehind" in reconcile
    assert "st._pendingExternalUpdate = true" in reconcile
    assert "st._reconcileRetryTimer = setTimeout" in reconcile


def test_session_history_and_workspace_use_distinct_icons():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    history_start = html.index('class="chat-tab-history-btn"')
    history_end = html.index("</button>", history_start)
    workspace_start = html.index('class="workspace-picker-btn"')
    workspace_end = html.index("</button>", workspace_start)

    assert '#i-history' in html[history_start:history_end]
    assert '#i-folder' not in html[history_start:history_end]
    assert '#i-hard-drive' in html[workspace_start:workspace_end]


def test_workspace_file_requests_reject_late_previous_owner_results():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    def method(start_marker: str, end_marker: str) -> str:
        start = app.index(start_marker)
        return app[start:app.index(end_marker, start)]

    trash = method("async loadTrash()", "\n    openTrashModal()")
    meta = method("async loadSelectedMeta(path)", "\n    // Format a unix-seconds")
    children = method("async fetchChildren(path, opts = {})", "\n    async toggleHidden()")
    upload = method("async _syncUploadedFiles(", "\n    onPreviewTabDragStart")
    save = method("async saveEdit()", "\n    // ===== @ mention")
    palette = method("async _fetchPaletteFiles()", "\n    // Build the item list")

    assert "const loadSeq = ++this._trashLoadSeq" in trash
    assert "ownerWorkspace === this.fileWorkspacePath()" in trash
    assert trash.count("if (!isOwner()) return") >= 2
    assert "const loadSeq = ++this._selectedMetaSeq" in meta
    assert "ownerWorkspace === this.fileWorkspacePath()" in meta
    assert "opts.ownerWorkspace || this.fileWorkspacePath()" in children
    assert "this._workspaceIsCurrent(ownerWorkspace)" in children
    assert "stale.staleWorkspace = true" in children
    assert "parsed.detail || parsed.error" in children
    assert "error.detail = parsedDetail" in children
    assert "_uniqueFileNodes(nodes)" in app
    assert "ownerWorkspace = this.fileWorkspacePath()" in upload
    assert "if (!this._workspaceIsCurrent(ownerWorkspace)) return" in upload
    assert save.index("if (!sameOwner) return") < save.index(
        "this._previewCacheDel(savePath)")
    assert "const requestSeq = ++this._paletteFileSeq" in palette
    assert "requestSeq === this._paletteFileSeq" in palette


def test_file_tree_uses_a_bounded_viewport_window():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert "fileTreeViewport: { start: 0, end: 80 }" in app
    assert "fileTreeWindowRows()" in app
    assert "onFileTreeScroll(ev)" in app
    assert '_positionFileTreePath(path, block = "nearest")' in app
    assert 'x-for="n in fileTreeWindowRows()"' in html
    assert '@scroll.passive="onFileTreeScroll($event)"' in html
    assert html.count("filelist-virtual-spacer") == 2
    assert ".filelist li.filelist-virtual-spacer" in css


def test_hidden_toggle_collapses_before_reloading_root():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async toggleHidden()")
    method = app[start:app.index("\n    async onNodeClick", start)]

    collapse = method.index("this.expanded = new Set()")
    load = method.index("await this.loadRoot()")
    assert collapse < load
    assert "this._pendingExpanded = []" in method
    assert "this.childCache = {}" in method
    assert "this._scheduleFileTreeViewportSync(true)" in method


def test_context_upload_remembers_the_workspace_that_opened_picker():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async ctxUploadHandler(ev)")
    handler = app[start:app.index("\n    async doRename", start)]

    assert "this._ctxUploadWorkspace = this.currentWorkspacePath()" in app
    assert "const ownerWorkspace = this._ctxUploadWorkspace" in handler
    assert "!this._workspaceIsCurrent(ownerWorkspace)" in handler


def test_session_poll_and_revision_reconciliation_are_resilient():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("_reconcileOpenSession(next)")
    end = app.index("\n    _sessionsEqual", start)
    reconcile = app[start:end]

    assert "const sharedResult = await this._sessionListPullPromise" in app
    assert "await this._pullSessionListOnce(false, requestedIds.join(\",\"))" in app
    assert "async _pullSessionListOnce(" in app
    assert "signal: controller.signal" in app
    assert "if (r.status === 304)" in app
    assert "this._reconcileOpenSession(this.sessions)" in app
    assert "for (const sid of (this.currentId ? [this.currentId] : []))" in reconcile
    assert "const baseline = st._seenUpdated" in reconcile
    assert "_reconcileTargetUpdated" in reconcile
    assert "const stillBehind" in reconcile
    assert "st._pendingExternalUpdate = true" in reconcile
    assert "_sessionsInitialized: false" in app
    assert "if (this._sessionInitPromise) return this._sessionInitPromise" in app
    assert "await this.initSessions({ skipRefresh: true })" in app
    assert "this._sessionsInitialized = true" in app


def test_optimistic_session_is_registered_before_first_turn():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    helper_start = app.index("_registerOptimisticSession(meta)")
    helper_end = app.index("\n    newSession(options = {})", helper_start)
    helpers = app[helper_start:helper_end]
    send_start = app.index("async send(opts = {})")
    send_end = app.index("\n    async stop()", send_start)
    send = app[send_start:send_end]

    assert "_sessionRegistrationPromises: {}" in app
    assert "this._sessionRegistrationPromises[id]" in helpers
    assert "async _ensureSessionRegistered(id)" in helpers
    assert helpers.count("this._registerOptimisticSession(meta)") >= 2
    ensure_at = send.index("await this._ensureSessionRegistered(sendSid)")
    push_at = send.index("this._appendLiveMessage(sendState")
    ticket_at = send.index('fetch("/api/chat/stream/start"')
    clear_at = send.index("clearSubmittedComposer();")
    assert ensure_at < push_at < ticket_at
    assert ensure_at < clear_at
    assert "新会话未能保存" in send


def test_silent_stream_recovers_without_manual_refresh():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async _recoverStalledStream(sid = this.currentId)")
    end = app.index("\n    _retireStaleSessionStream", start)
    recovery = app[start:end]

    assert "Date.now() - observedActivity < 18_000" in recovery
    assert "d.events_so_far" in recovery
    assert "st._serverActiveObserved = true" in recovery
    assert "await this.send({" in recovery
    assert "reconnect: true" in recovery
    assert "this._retireStaleSessionStream(sid, st)" in recovery
    assert "await this.loadSession(sid, { quiet: true })" in recovery
    assert "this._recoverStalledStream(streamSid)" in app


def test_stream_reconnect_is_pinned_to_backend_turn_identity():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    send_start = app.index("async send(opts = {})")
    send_end = app.index("\n    async stop()", send_start)
    send = app[send_start:send_end]

    assert "const expectedTurnId = isReconnect" in send
    assert "turn_id: expectedTurnId" in send
    assert '"&turn_id=" + encodeURIComponent(expectedTurnId)' in send
    assert "streamState.es !== es" in send
    assert "ownedTurnId !== eventTurnId" in send
    assert "eventSeq <= (Number(streamState.lastEventSeq) || 0)" in send
    assert "sessionId: streamSid" in send
    assert "turnId: d.turn_id || streamState.activeTurnId || \"\"" in send


def test_interrupted_turn_is_dismissed_after_open_or_manual_close():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    helper_start = app.index("async _dismissInterruptedTurn(sid)")
    helper_end = app.index("\n    // Toast any turns", helper_start)
    helper = app[helper_start:helper_end]
    start = app.index("async _checkInterruptedTurns()")
    end = app.index("\n    // 10s heartbeat", start)
    recovery = app[start:end]

    assert "/api/chat/interrupted-turns/${encodeURIComponent(sid)}/dismiss" in helper
    click_at = recovery.index("onClick: async () =>")
    open_at = recovery.index("await this.openTab(turn.sid)", click_at)
    dismiss_at = recovery.index("await this._dismissInterruptedTurn(turn.sid)", open_at)
    assert click_at < open_at < dismiss_at
    assert "onDismiss: () => this._dismissInterruptedTurn(turn.sid)" in recovery
    assert 'dismissToast(t.id, true)' in html
    assert "userInitiated && toast?.action?.onDismiss" in app


def test_mobile_keyboard_watchdog_clears_stale_pwa_viewport_inset():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("_mobileKeyboardInset()")
    end = app.index("\n    // Attach iOS-style pull-to-refresh", start)
    keyboard = app[start:end]

    assert "window.innerHeight - vv.height - vv.offsetTop" in keyboard
    assert "this._mobileKeyboardPollTimer = setInterval" in keyboard
    assert "() => this._syncMobileKeyboardViewport(), 400" in keyboard
    assert 'style.setProperty("--kb-inset", "0px")' in keyboard
    assert "if (wasOpen) this._scheduleMobileRootReset()" in keyboard
    assert "window.scrollTo(0, 0)" in keyboard
    assert 'document.addEventListener("visibilitychange"' in keyboard


def test_session_delete_confirms_once_and_disposes_browser_runtime():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    picker_start = app.index("async pickerDeleteSession(sid, ev)")
    picker_end = app.index("\n    // One-click bulk clear", picker_start)
    picker = app[picker_start:picker_end]
    dispose_start = app.index("_disposeTabRuntime(id)")
    dispose_end = app.index("\n    async removeWorkspace", dispose_start)
    dispose = app[dispose_start:dispose_end]
    delete_start = app.index("async deleteSessionById(sid, { confirmed = false } = {})")
    delete_end = app.index("\n    // ===== Versions", delete_start)
    delete = app[delete_start:delete_end]
    current_start = app.index("async deleteSession()")
    current_end = app.index("\n    // ===== file tree", current_start)
    current = app[current_start:current_end]

    assert "deleteSessionById(sid, { confirmed: true })" in picker
    assert "if (!confirmed)" in delete
    assert "if (!response.ok)" in delete
    assert delete.index("this._disposeTabRuntime(sid)") > delete.index("if (!response.ok)")
    assert "deleteSessionById(cur.id, { confirmed: true })" in current
    assert "const ownedEs = st.es" in dispose
    assert "this.es === ownedEs" in dispose
    assert "this._stopBgContPoller(id)" in dispose
    assert "delete this._sessionLoadPromises[id]" in dispose
    assert "delete this.tabState[id]" in dispose


def test_session_setting_writes_keep_their_tab_owner_and_order():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    serialize_start = app.index("async _serializeTabSettingPatch")
    serialize_end = app.index("\n    async onEffortChange", serialize_start)
    serialize = app[serialize_start:serialize_end]
    effort_start = app.index("async onEffortChange()")
    effort_end = app.index("\n    async onServiceTierChange", effort_start)
    effort = app[effort_start:effort_end]
    tier_start = app.index("async onServiceTierChange", effort_end)
    tier_end = app.index("\n    async onThinkingChange", tier_start)
    tier = app[tier_start:tier_end]
    thinking_start = app.index("async onThinkingChange()")
    thinking_end = app.index("\n    modelGroups()", thinking_start)
    thinking = app[thinking_start:thinking_end]

    assert "const prior = st[tailKey] || Promise.resolve()" in serialize
    assert "Promise.resolve(prior).catch(() => {}).then(work)" in serialize
    assert "const sid = this.currentId" in effort
    assert "++st._effortPatchSeq" in effort
    assert "this.tabState[sid] !== st" in effort
    assert "st._effortPatchSeq !== seq" in effort
    assert "_serializeRuntimeSettingPatch" in effort
    assert "st._runtimeSettingsGeneration += 1" in effort
    assert "if (this.workspaceSwitching)" in effort
    assert "this._conversationWorkspaceIsCurrent(ownerWorkspace)" in effort
    assert "service_tier: compatibleTier" in effort
    assert "++st._serviceTierPatchSeq" in effort
    assert effort.index("await this._ensureSessionRegistered(sid)") < effort.index(
        "this._serializeRuntimeSettingPatch"
    )
    assert "const sid = this.currentId" in tier
    assert "++st._serviceTierPatchSeq" in tier
    assert "this.tabState[sid] !== st" in tier
    assert "st._serviceTierPatchSeq !== seq" in tier
    assert "_serializeRuntimeSettingPatch" in tier
    assert "st._runtimeSettingsGeneration += 1" in tier
    assert "if (this.workspaceSwitching)" in tier
    assert "this._conversationWorkspaceIsCurrent(ownerWorkspace)" in tier
    assert "effort: compatibleEffort" in tier
    assert "++st._effortPatchSeq" in tier
    assert tier.index("await this._ensureSessionRegistered(sid)") < tier.index(
        "this._serializeRuntimeSettingPatch"
    )
    assert '"_runtimeSettingPatchTail"' in serialize
    assert "while (this.tabState[sid] === st)" in serialize
    assert "const sid = this.currentId" in thinking
    assert "++st._thinkingPatchSeq" in thinking
    assert "this.tabState[sid] !== st" in thinking
    assert '"_thinkingPatchTail"' in thinking


def test_model_switch_new_session_keeps_workspace_and_does_not_hijack_active_tab():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async onModelChange()")
    end = app.index("\n    // ===== Effort knob", start)
    model = app[start:end]

    assert "const sid = this.currentId" in model
    assert "const ownerWorkspace = this.currentWorkspacePath()" in model
    assert "if (this.workspaceSwitching)" in model
    assert "const ownsSelection = () => !this.workspaceSwitching" in model
    assert "await this._ensureSessionRegistered(sid)" in model
    assert "ownerState._modelPatchSeq !== seq" in model
    assert "ownerState._modelExpected = expected" in model
    assert "name: \"\", model: newM, cwd: ownerWorkspace" in model
    assert "this._conversationWorkspaceIsCurrent(ownerWorkspace) && this.currentId === sid" in model


def test_effort_and_fast_controls_follow_per_model_capabilities():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    i18n = (FRONTEND / "i18n" / "index.js").read_text(encoding="utf-8")

    start = app.index("_normalizeEffort(value)")
    end = app.index("\n    _supportsThinking", start)
    capabilities = app[start:end]
    assert '["auto", "low", "medium", "high", "xhigh", "max", "ultra"]' in capabilities
    assert "Array.isArray(meta.effort_levels)" in capabilities
    assert "meta.supports_fast === true" in capabilities
    assert "this._isClaudeModel(model)" in capabilities
    assert 'level !== "ultra"' in capabilities
    assert 'level !== "xhigh" || this._isClaudeXHighModel(model)' in capabilities

    assert html.count('x-show="_showEffortControl(model)"') == 2
    assert html.count('x-show="_showFastControl(model)"') == 2
    assert "|| level === selected" in app
    assert '|| this._normalizeEffort(this.effort) !== "auto"' in capabilities
    assert '|| this._normalizeServiceTier(this.serviceTier) === "fast"' in capabilities
    assert "onServiceTierChange(serviceTier !== 'fast')" in html
    assert "onServiceTierChange($event.target.checked)" in html
    assert html.count('x-model="effort"') == 2
    assert i18n.count('"effort.ultra":') == 2
    assert i18n.count('"service_tier.label": "Fast"') == 2


def test_runtime_setting_writes_gate_send_and_restore_per_session():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    send_start = app.index("async send(opts = {})")
    send = app[send_start:]
    assert send.count("await this._awaitRuntimeSettingPatches(sendSid, sendState)") >= 3
    assert "runtimeSettingsPending()" in html
    model_control = html[html.index('<select x-model="model"'):
                         html.index("</select>", html.index('<select x-model="model"'))]
    assert ':disabled="workspaceSwitching"' in model_control
    assert html.count('<select x-model="effort"') == 2
    for marker in [
        '<select x-model="effort"',
        '<input type="checkbox" :checked="serviceTier === \'fast\'"',
        '<button type="button"\n                    class="chat-toolbar-fast"',
    ]:
        start = html.index(marker)
        end = html.index(">", start)
        assert ':disabled="workspaceSwitching"' in html[start:end]

    activate_start = app.index("_activateTabState(id)")
    activate_end = app.index("\n    _paneElement", activate_start)
    activate = app[activate_start:activate_end]
    assert "this.effort = st.effort" in activate
    assert "this.serviceTier = st.serviceTier" in activate

    switch_start = app.index("async switchSession()")
    switch_end = app.index("\n    _afterPaint", switch_start)
    switch = app[switch_start:switch_end]
    assert "curState._effortExpected" in switch
    assert "curState._serviceTierExpected" in switch


def test_new_and_model_switched_sessions_start_with_clean_runtime_settings():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    register_start = app.index("_registerOptimisticSession(meta)")
    register_end = app.index("\n    async _ensureSessionRegistered", register_start)
    register = app[register_start:register_end]
    assert "effort: this._normalizeEffort(meta.effort)" in register
    assert "service_tier: this._normalizeServiceTier(meta.service_tier)" in register
    assert "this._retainExpectedSessionSettings({" in register

    new_start = app.index("newSession(options = {})")
    new_end = app.index("\n    // ===== tabs =====", new_start)
    new_session = app[new_start:new_end]
    assert 'effort: "auto"' in new_session
    assert 'service_tier: ""' in new_session
    assert "st.effort = meta.effort" in new_session
    assert "st.serviceTier = meta.service_tier" in new_session

    model_start = app.index("async onModelChange()")
    model_end = app.index("\n    // ===== Effort knob", model_start)
    model = app[model_start:model_end]
    assert "this.onEffortChange()" not in model
    assert 'effort: "auto", service_tier: ""' in model
    cancel = model[model.index("if (!ok)"):model.index("try {", model.index("if (!ok)"))]
    assert "effort" not in cancel
    assert "serviceTier" not in cancel


def test_conversation_fork_is_explicit_and_keeps_edit_and_model_switch_separate():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert '@click="menuFork(tabCtxMenu && tabCtxMenu.id)"' in html
    assert '@click.stop="forkConversation(tid, turnForkMessageId(paneMsgs, i))"' in html
    assert "turnForkMessageId(paneMsgs, i)" in app
    assert 'class="fork-origin-banner"' in html
    assert 'x-text="currentForkSource().name"' in html

    start = app.index("async forkConversation(id, upToMessageId = \"\")")
    end = app.index("\n    async menuFork", start)
    fork = app[start:end]
    assert "/api/chat/sessions/${encodeURIComponent(id)}/fork" in fork
    assert "up_to_message_id: upToMessageId || null" in fork
    assert "const st = this._ensureTabState(newId)" in fork
    assert "await this.openTab(newId)" in fork

    model_start = app.index("async onModelChange()")
    model_end = app.index("\n    // ===== Effort knob", model_start)
    edit_start = app.index("commitEditMessage(m)")
    edit_end = app.index("\n    },", edit_start)
    assert "/fork" not in app[model_start:model_end]
    assert "/fork" not in app[edit_start:edit_end]

    assert ".turn-fork-btn" in css
    assert ".fork-origin-banner" in css


def test_history_jump_keeps_the_session_that_owned_the_click():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("_scrollToUserMsg(m, ownerSid = this.currentId)")
    end = app.index("\n    // Short preview text", start)
    jump = app[start:end]
    palette_start = app.index("async _jumpToMessage(sid, uuid)")
    palette_end = app.index("\n    // Fetch files matching", palette_start)
    palette_jump = app[palette_start:palette_end]

    assert "const sid = ownerSid" in jump
    assert "body && body.querySelector" in jump
    assert "document.querySelector(" not in jump
    assert "await this._loadAroundMessage(sid, uuid)" in jump
    assert "this.tabState[sid] !== st || sid !== this.currentId" in jump
    assert "if (this.currentId !== sid) return" in palette_jump
    assert "body && body.querySelector" in palette_jump
    assert "await this._loadAroundMessage(sid, uuid)" in palette_jump


def test_failed_transcript_refresh_preserves_last_good_messages():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async loadSession(sid, opts = {})")
    end = app.index("\n    // Warm OPEN-but-inactive tabs", start)
    load = app[start:end]
    failed = load[
        load.index("if (!r.ok) {"):
        load.index("const s = this._retainExpectedSessionSettings(await r.json())")
    ]

    assert "return false" in failed
    assert "st.messages.length = 0" not in failed
    assert "this.messages = st.messages" not in failed


def test_stale_session_read_cannot_overwrite_new_runtime_settings():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    state_start = app.index("_blankTabState()")
    state_end = app.index("\n    // ===== Per-session message queue", state_start)
    state = app[state_start:state_end]
    load_start = app.index("async loadSession(sid, opts = {})")
    load_end = app.index("\n    // Warm OPEN-but-inactive tabs", load_start)
    load = app[load_start:load_end]

    assert "_runtimeSettingsGeneration: 0" in state
    assert "const runtimeSettingsGenerationAtLoad" in load
    assert "const runtimeSettingsStillCurrent" in load
    assert "runtimeSettingsStillCurrent ? s.effort : st.effort" in load
    assert "runtimeSettingsStillCurrent ? s.service_tier : st.serviceTier" in load
    assert "st._modelExpected" in load
    assert "const resolvedModel = String(" in load
    assert "loadedMeta.model = resolvedModel" in load
    assert "if (resolvedModel) this.model = resolvedModel" in load
    assert "loadedMeta.effort = resolvedEffort" in load
    assert "loadedMeta.service_tier = resolvedServiceTier" in load


def test_runtime_setting_expected_values_expire_and_accept_remote_truth():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    retain_start = app.index("_retainExpectedSessionSettings(meta)")
    retain_end = app.index("\n    _retainExpectedSessionActivity", retain_start)
    retain = app[retain_start:retain_end]
    effort_start = app.index("async onEffortChange()")
    effort_end = app.index("\n    async onServiceTierChange", effort_start)
    tier_start = effort_end + 1
    tier_end = app.index("\n    async onThinkingChange", tier_start)

    assert "SESSION_SETTING_EXPECTED_TTL_MS: 15_000" in app
    assert '["_modelExpected", "_runtimeSettingPatchTail", "model"' in retain
    assert '["_effortExpected", "_runtimeSettingPatchTail", "effort"' in retain
    assert '["_serviceTierExpected", "_runtimeSettingPatchTail", "service_tier"' in retain
    assert "now - expectedAt" in retain
    assert "> this.SESSION_SETTING_EXPECTED_TTL_MS" in retain
    assert "st[expectedKey] = null" in retain
    assert "at: Date.now()" in app[effort_start:effort_end]
    assert "at: Date.now()" in app[tier_start:tier_end]


def test_chat_file_link_fallback_prefers_suffix_then_unique_basename():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    resolver_start = app.index("async _findChatFileCandidates(")
    resolver_end = app.index("\n    // List-choice variant", resolver_start)
    resolver = app[resolver_start:resolver_end]
    open_start = app.index("async openByPathToasted(path)")
    open_end = app.index("\n    // Open a background-task result", open_start)
    open_path = app[open_start:open_end]

    assert '"&exact=true&limit=200"' in resolver
    assert "const exactNameMatches" in resolver
    assert "const suffixMatches" in resolver
    assert "suffixMatches.length ? suffixMatches : exactNameMatches" in resolver
    assert "this._findChatFileCandidates(path, name, requestHeaders)" in open_path
    assert "matches.length === 1" in open_path
    assert "matches.length > 1" in open_path
    assert "await this.chooseOne({" in open_path


def test_activity_center_groups_by_attention_order_and_read_state():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    activity_state = app[app.index("activity: {"):app.index("_activityEtags:")]
    assert 'view: "timeline"' in activity_state

    review = app.index('{ key: "review"')
    running = app.index('{ key: "running"', review)
    failed = app.index('{ key: "failed"', running)
    history = app.index('{ key: "history"', failed)
    assert review < running < failed < history

    assert 'key === "review") return item.state === "completed" && !item.read' in app
    assert '["running", "waiting_approval", "paused"].includes(item.state)' in app
    assert 'key === "failed") return item.state === "failed"' in app
    assert 'item.state === "completed" && !!item.read' in app
    assert 'item.state === "cancelled"' in app
    assert "ACTIVITY_GROUP_CAP: 5" in app
    assert "ACTIVITY_TIMELINE_CAP: 10" in app
    assert 'group?.key === "timeline"' in app
    assert "this.activityGroupCap(group)" in app
    assert "activityHiddenCount(group)" in app
    assert '"/api/activity?limit=500"' in app
    assert "r.status === 304 && !opts.summaryOnly && !this.activity.events.length" in app
    assert 'cache: "reload"' in app
    assert "opts.summaryOnly && this._activityFetchPromises.events" in app
    assert "!opts.summaryOnly && this._activityFetchPromises.summary" in app
    assert "const rank = (activeRank[a.state] ?? 9)" in app
    assert "return this.activityEventTimestamp(b)" in app
    assert "this._activityAppliedSeq = ++this._activityRequestSeq" in app
    assert '"/api/activity/events-ticket"' in app
    assert "new EventSource(" in app
    assert 'this.activity.view === "timeline"' in app
    assert 'key === "timeline") return true' in app
    assert "setActivityView('timeline')" in html
    assert "const pinRank = Number(!!b.pinned) - Number(!!a.pinned)" in app
    assert "async toggleActivityPin(item)" in app
    assert 'method: "PATCH", json: { pinned: target }' in app
    assert '@click.stop="toggleActivityPin(item)"' in html
    assert 'x-show="activity.view === \'timeline\'"' in html
    assert ".activity-pin.active" in css

    # The left marker is unread/action state, not a permanent failure marker.
    assert "activityIsUnreadResult(item) ? ' is-unread'" in html
    assert ".activity-row.failed.is-unread .activity-state-dot" in css
    assert ".activity-row.failed .activity-state-dot,.activity-row.waiting_approval" not in css


def test_activity_center_uses_two_compact_numberless_status_dots():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    start = html.index('class="activity-center-btn"')
    button = html[start:html.index("</button>", start)]

    assert 'class="activity-running"' in button
    assert 'x-show="activity.summary.running"' in button
    assert 'class="activity-unread"' in button
    assert 'x-show="activity.summary.unread"' in button
    assert "x-text=" not in button

    def compact_rule(selector: str) -> str:
        pos = css.index(selector)
        return re.sub(r"\s+", "", css[pos:css.index("}", pos)])

    running = compact_rule(".activity-center-btn .activity-running {")
    unread = compact_rule(".activity-center-btn .activity-unread {")
    assert "width:10px" in running and "height:10px" in running
    assert "background:var(--c-running)" in running
    assert "width:10px" in unread and "height:10px" in unread
    assert "min-width" not in unread and "padding" not in unread
    assert "background:var(--c-success)" in unread


def test_task_rows_force_targeted_session_lookup_and_activate_the_linked_workspace():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    deep_start = app.index("async _openSessionFromDeeplink(id, workspace = \"\")")
    deep_end = app.index("\n    // Close a tab.", deep_start)
    deep = app[deep_start:deep_end]
    assert "this.sessionWorkspaces.some(w => w.path === workspace)" in deep
    assert "await this._changeWorkspaceSurface(workspace)" in deep
    # A normal in-flight list poll may swallow extraIds; the shared list helper
    # must deterministically follow it with an id-bearing request.
    assert "await this._pullSessionList(false, id)" in deep
    assert "if (!this.sessions.find(s => s.id === id)) return false" in deep
    assert "await this.activateTab(id)" in deep
    assert "if (this.currentId !== id) return false" in deep
    assert 'this.setMobileTab("chat")' in deep

    activity_start = app.index("async openActivityEvent(item)")
    activity_end = app.index("\n    async ackAllActivity", activity_start)
    activity = app[activity_start:activity_end]
    assert "item.session_id || item.thread_id" in activity
    assert "item.workspace || item.cwd || \"\"" in activity
    assert "await this._openSessionFromDeeplink(" in activity
    assert "if (!opened)" in activity
    assert '@click="openActivityEvent(item)"' in html

    pull_start = app.index("async _pullSessionList(conditional = false, extraIds = \"\")")
    pull_end = app.index("\n    async _pullSessionListOnce", pull_start)
    pull = app[pull_start:pull_end]
    assert "const sharedResult = await this._sessionListPullPromise" in pull
    assert "requestedIds.every(id => this.sessions.some(s => s.id === id))" in pull
    assert "await this._pullSessionListOnce(false, requestedIds.join(\",\"))" in pull

    # Scheduler run-history rows have the same out-of-window failure mode and
    # therefore must not bypass the guarded helper with a bare openTab().
    assert '@click="openSchedRunSession(run)"' in html
    sched_start = app.index("async openSchedRunSession(run)")
    sched_end = app.index("\n    fmtSchedTime", sched_start)
    sched = app[sched_start:sched_end]
    assert "await this._openSessionFromDeeplink(" in sched
    assert "run.workspace || run.cwd || \"\"" in sched


def test_bounded_stream_resync_waits_for_canonical_history_without_retry_loop():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    handler_start = app.index('es.addEventListener("resync"')
    handler_end = app.index('es.addEventListener("error"', handler_start)
    handler = app[handler_start:handler_end]
    error_end = app.index('es.addEventListener("cancelled"', handler_end)
    error = app[handler_end:error_end]
    helper_start = app.index("_scheduleCanonicalStreamReload(sid, st")
    helper_end = app.index("\n    _retireStaleSessionStream", helper_start)
    helper = app[helper_start:helper_end]

    assert '"cancelled", "resync"' in app
    assert 'const streamMobile = this._isMobileLayout()' in app
    assert 'mobile: streamMobile' in app
    assert '"&mobile=" + (streamMobile ? "1" : "0")' in app
    assert 'if (!streamMobile)' in app
    assert 'if (!final && streamMobile && acc.length > 32 * 1024)' in app
    assert 'if (streamMobile && reason === "replay_truncated")' in handler
    assert 'streamState._canonicalResyncPending = true' in handler
    assert "this._scheduleCanonicalStreamReload(streamSid, streamState)" in handler
    assert "if (streamState._canonicalResyncPending) return" in error
    assert "/active" in helper
    assert "this.loadSession(sid" in helper
    assert "31 * 60_000" in helper


def test_stream_done_errors_share_failed_message_state_and_actions():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    helper_start = app.index("const markUserFailed = (errorText, kind, cta, retryable)")
    done_start = app.index('es.addEventListener("done"', helper_start)
    error_start = app.index('es.addEventListener("error"', done_start)
    done = app[done_start:error_start]
    error = app[error_start:app.index('es.addEventListener("cancelled"', error_start)]

    assert "markUserFailed(_detail, d.kind, d.cta, d.retryable)" in done
    assert "if (d.is_error)" in done
    assert "_drainPendingQueue(streamSid, completedTurnId)" in done
    assert "d.turn_id || streamState.activeTurnId || expectedTurnId" in done
    assert "turnId === completedTurnId" in app
    assert "this._attachToServerTurn(" in app
    assert "markUserFailed(serverError, errKind, errCta, errRetryable)" in error


def test_frontend_recognizes_model_route_and_full_context_window_errors():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("_humanizeStreamError(raw)")
    humanizer = app[start:app.index("copyMsg(m)", start)]
    hint_start = app.index("errorFixHint(m)")
    hints = app[hint_start:app.index("findToolUseFor", hint_start)]

    assert "unknown provider" in humanizer
    assert "context window" in humanizer
    assert "input exceeds" in humanizer
    assert "unknown provider" in hints
    assert "context window" in hints


def test_compact_http_failure_parses_detail_and_never_shows_success():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async runCompact")
    compact = app[start:app.index("onChatArrowUp", start)]
    failure = compact[compact.index("if (!r.ok)"):compact.index("// Reload the compacted", compact.index("if (!r.ok)"))]

    assert "JSON.parse(raw)" in failure
    assert "body.detail" in failure
    assert "_humanizeStreamError(detail)" in failure
    assert "压缩完成" not in failure


def test_workspace_switch_disables_composer_and_gates_programmatic_user_send():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    start = app.index("async send(opts = {})")
    send = app[start:app.index("// ====== ask_user_question", start)]

    assert "if (this.workspaceSwitching && !opts.reconnect && !opts.resumedItem) return" in send
    assert ':disabled="!availableModels.length || workspaceSwitching"' in html
    assert 'multiple style="display:none" :disabled="workspaceSwitching"' in html
    assert ':disabled="workspaceSwitching || !availableModels.length' in html
    assert ':disabled="workspaceSwitching || !!(tabState[currentId]' in html


def test_stop_control_interrupts_session_and_never_removes_queue_items():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    start = app.index("async stop() {")
    stop = app[start:app.index("// ====== ask_user_question", start)]

    assert 'x-show="isTabStreaming(currentId)"' in html
    assert "chat-toolbar-stop" in html
    assert ':title="_isBusy(currentId) ? t(\'queue.button_hint\')' in html
    assert "撤回队尾" not in html
    assert "removePendingQueueItem" not in stop
    assert "if (st._stopping) return" in stop
    assert "const r = await fetch(" in stop
    assert "if (!r.ok) throw" in stop
    assert 'String(item).startsWith(sid + "@")' in stop
    assert "const timeout = setTimeout(() => controller.abort(), 15000)" in stop
    assert "waitForTerminalEvent = !!st.streaming" in stop
    assert "this._retireStaleSessionStream(sid, st)" not in stop
    assert "if (st._renderStreamingHtml) st._renderStreamingHtml()" not in stop
    assert "if (!didInterrupt)" in stop
    assert "if (!waitForTerminalEvent || !st.streaming)" in stop
    cancelled_start = app.index('es.addEventListener("cancelled"')
    cancelled_end = app.index("\n      });", cancelled_start)
    assert "_markDone(true, false, true)" in app[cancelled_start:cancelled_end]
    mark_done_start = app.index("const _markDone = (")
    mark_done_end = app.index("\n      };", mark_done_start)
    assert "streamState._stopping = false" in app[
        mark_done_start:mark_done_end]
    assert "this.isTabStreaming(this.currentId)" in app


def test_background_task_gap_stops_footer_without_blocking_composer():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    i18n = (FRONTEND / "i18n" / "index.js").read_text(encoding="utf-8")

    assert "backgroundActive: false" in app
    assert "backgroundTaskCount: 0" in app
    # A pending background task keeps its card alive but must NOT keep the
    # completed turn's footer timer or make the composer busy: the backend pump
    # owns the session stream now, so task completion arrives independently.
    assert "|| (st && st.compacting));" in app
    assert "if (st && (st.streaming || st.compacting)) return true;" in app
    assert "if (status.background) return false;" in app
    assert "d.background && d.attachable === false" in app
    assert "background_tasks_pending" in app
    assert "_stopTimer();" in app
    assert "_continuationAwaitingReaction: false" in app
    # The turn footer must key off a REAL streaming turn. It used to also
    # suppress itself on backgroundActive, which hid the completion timestamp
    # of every turn that finished while a background task was still pending.
    assert "pane && pane.streaming && i === paneMsgs.length - 1" in html
    assert "pane.streaming || pane.backgroundActive" not in html
    # The "background task running · messages will queue" strip is gone: it
    # told the user they could not keep talking, which is no longer true.
    assert "background-task-strip" not in html
    assert "background-task-strip" not in css
    assert "isTabRunning(tid)" in html
    assert "isTabBackgroundActive(tid)" in html
    assert '"chat.background_running": "后台任务运行中"' in i18n
    assert "if (streamState.es === es) streamState.es = null" in app
    assert "d.background && d.attachable === false" in app
    assert "continuation: !!d.continuation" in app
    # A canonical boundary anywhere in a tool-ending turn still earns the
    # tail-mounted footer and fork affordance. The `!streaming` guard came with
    # the separator restyle: the footer no longer hosts streaming dots.
    assert "(turnForkMessageId(paneMsgs, i) && !(pane && pane.streaming))" in html
    assert 'x-show="turnForkMessageId(paneMsgs, i)' in html
    assert 'Object.prototype.hasOwnProperty.call(s, "turn_active")' in app
    assert "return !!s.turn_active" in app
    assert "return !!(s && s.background_active)" in app


def test_attachment_uploads_have_deadlines_and_never_log_filenames():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert app.count("() => uploadController.abort(), 5 * 60 * 1000") == 2
    assert app.count("signal: uploadController.signal") == 2
    assert app.count("clearTimeout(uploadTimeout)") == 2
    assert "[muselab][upload]" not in app


def test_html_preview_uses_path_bound_ticket_not_api_token():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    open_start = app.index('} else if (["html", "htm"].includes(ext))')
    open_branch = app[open_start:app.index(
        'else if (["png", "jpg"', open_start)]
    raw_start = app.index("rawUrl(p, opts = {})")
    raw = app[raw_start:app.index("async reloadPreview()", raw_start)]
    preview_branch = raw[raw.index("if (opts.preview)"):
                         raw.index('return "/api/files/raw?path="', raw.index(
                             'return "/api/files/raw?path="') + 1)]

    assert open_branch.index("_mintPreviewTicket") < open_branch.index(
        'this.previewMode = "html"')
    assert '"&ticket="' in preview_branch
    assert '"&token="' not in preview_branch
    assert 'if (!ticket) return "about:blank"' in preview_branch


def test_turn_finalization_repairs_whole_pane_and_cache_bytes():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert 'this.highlightCode(".chat-body", pane ? [pane] : null)' in app
    assert 'finalEl ? [finalEl] : []' not in app
    assert "_mdCacheDelete(text)" in app
    rerender_start = app.index("_rerenderMathMessages()")
    rerender = app[rerender_start:app.index(
        "// Path-shaped strings", rerender_start)]
    assert ".delete(m.text)" not in rerender
    assert ".delete(this.rawText)" not in rerender


def test_send_pins_owner_waits_before_enqueue_and_blocks_failed_attachments():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async send(opts = {})")
    send = app[start:app.index("// ====== ask_user_question", start)]

    assert "const sendSid = opts.sessionId || this.currentId" in send
    assert "const sendState = this._ensureTabState(sendSid)" in send
    assert "const sendMeta = (this.sessions || []).find(s => s.id === sendSid)" in send
    assert "sendSid === this.currentId" in send
    assert "const sendDraft = sendState.draft" in send
    assert "const composerImages = sendDraft.pendingImages.slice()" in send
    assert "const composerDocs = sendDraft.pendingDocs.slice()" in send
    assert "const clearSubmittedComposer = ({ preserveForHandshake = false } = {}) =>" in send
    assert "removeOwned(sendDraft.pendingImages" in send
    busy_branch = "await this._confirmSessionBusy(sendSid, sendState)"
    assert send.index("while (ownsSendDraft() && stillUploading())") < send.rindex(busy_branch)
    assert send.index("failedAttachments.length") < send.rindex(busy_branch)
    assert "this._enqueueMessage(sendSid" in send
    assert "async _confirmSessionBusy(sid" in app
    assert "failed and were skipped" not in send


def test_composer_draft_is_per_session_and_async_actions_pin_owner():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    blank_start = app.index("_blankTabState()")
    blank = app[blank_start:app.index("_ensureTabState(id)", blank_start)]
    attach_start = app.index("async _attachFile(file)")
    attach = app[attach_start:app.index("async onAttachPicked", attach_start)]
    editor_start = app.index("async openImageEditor(i)")
    editor = app[editor_start:app.index("openImageGen()", editor_start)]
    image_gen_start = app.index("async attachGeneratedImage")
    image_gen = app[image_gen_start:app.index("imageGenStatusLabel", image_gen_start)]

    assert 'draft: {' in blank
    assert 'input: ""' in blank
    assert 'pendingImages: []' in blank
    assert 'pendingDocs: []' in blank
    assert '_sendWaitingForUpload: false' in blank
    assert '_activated: false' in blank
    assert "_captureComposerState(id = this.currentId, { persist = true } = {})" in app
    assert "_activateComposerState(id)" in app
    assert "this._activateComposerState(id)" in app
    assert "if (st.draft._activated === false) return" in app
    assert attach.index("const ownerSid = this.currentId") < attach.index(
        "await this._maybeCompressImage")
    assert "ownerDraft.pendingImages.push(raw)" in attach
    assert "ownerDraft.pendingDocs.push(raw)" in attach
    assert "this.tabState[ownerSid] === ownerState" in attach
    assert "ownerSid" in editor and "ownerEntry" in editor
    assert "ownerState.draft.pendingImages.includes(entry)" in editor
    assert "ownerState.draft.pendingImages.push(entry)" in image_gen
    assert "this.tabState[ownerSid] !== ownerState" in image_gen


def test_chat_draft_survives_refresh_and_failed_stream_start():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    send_start = app.index("async send(opts = {})")
    send = app[send_start:app.index("\n    async stop()", send_start)]

    assert '_chatDraftStoreKey: "muselab_chat_drafts_v1"' in app
    assert "_consumePersistedChatDraft(id)" in app
    assert "this.tabState[id].draft.input = this._consumePersistedChatDraft(id)" in app
    assert 'window.addEventListener("pagehide"' in app
    assert "this._schedulePersistChatDraft(this.currentId, this.input" in app
    assert "this._stageChatRecoveryDraft(sendSid, composerText)" in send
    assert "clearSubmittedComposer({ preserveForHandshake: true })" in send
    assert "this._commitChatRecoveryDraft(sendSid, composerText)" in send
    assert "const rollbackUnstartedSend = () =>" in send
    assert "restoreSubmittedComposer(true)" in send
    assert "restoreOwned(sendDraft.pendingImages, composerImages)" in send
    assert "restoreOwned(sendDraft.pendingDocs, composerDocs)" in send
    assert "this._markDone(streamSid)" not in send
    assert send.count("restoreSubmittedComposer(false)") >= 2
    assert "this._deletePersistedChatDraft(sid)" in app
    assert "const previousDraft = previousState && previousState.draft" in app
    assert "landingState.draft.input = this._mergeChatDraftText" in app


def test_symlink_outside_workspace_has_actionable_tree_error():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("_fileTreeOpenError(path, error)")
    helper = app[start:app.index("\n    async expand(n, opts = {})", start)]
    expand_start = app.index("async expand(n, opts = {})")
    expand = app[expand_start:app.index("\n    collapse(n)", expand_start)]

    assert 'detail === "path escapes root"' in helper
    assert "不在当前工作区或已添加的工作区中" in helper
    assert "先把目标目录添加为工作区" in helper
    assert "outside the current or registered workspaces" in helper
    assert "this._fileTreeOpenError(n.path, e)" in expand


def test_queue_edit_does_not_borrow_active_composer_and_prompt_menu_is_removed():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    queue_start = app.index("async editPendingQueueItem")
    queue_edit = app[queue_start:app.index("async resumeQueueDrain", queue_start)]

    assert "draft.input = text" in queue_edit
    assert "draft.pendingImages.splice" in queue_edit
    assert "draft.pendingDocs.splice" in queue_edit
    assert "if (sid !== this.currentId) return" not in queue_edit
    assert "menuEditPrompt" not in app
    assert "editSessionPrompt" not in app


def test_tab_disposal_aborts_memory_only_uploads_and_drops_runtime_state():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("_disposeTabRuntime(id)")
    dispose = app[start:app.index("async removeWorkspace", start)]

    assert "draft._uploadControllers" in dispose
    assert "controller.abort()" in dispose
    assert 'draft.input = ""' in dispose
    assert "draft.pendingImages.splice(0)" in dispose
    assert "draft.pendingDocs.splice(0)" in dispose
    assert "delete this.tabState[id]" in dispose


def test_active_stream_owns_messages_and_continuation_reconciles_canonical_history():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    load_start = app.index("async loadSession(sid, opts = {})")
    load = app[load_start:app.index("// Warm OPEN-but-inactive tabs", load_start)]
    send_start = app.index("async send(opts = {})")
    send = app[send_start:app.index("async stop()", send_start)]

    assert "if (st.streaming || st.es) return false" in load
    assert "this.tabState[sid] !== st || st.streaming || st.es" in load
    reveal_start = app.index("async _revealMessagesChunked(sid, st, visible)")
    reveal = app[reveal_start:app.index("async _fillDeferredHead", reveal_start)]
    assert "this.tabState[sid] !== st || st.streaming || st.es" in reveal
    assert "const ownsCurBubble = () =>" in send
    assert "this._containsPaneMessage(streamState, curBubble)" in send
    assert "if (ownsCurBubble()) curBubble.error" in send
    assert "this._reconcileCompletedContinuation(" in send
    assert "streamSid, streamState, continuationFinalText" in send
    assert "const loaded = await this.loadSession(sid, { quiet: true })" in app
    assert "expectedText" in app
    assert "const stillOwned = () => this.tabState[sid] === ownerState" in app
    assert "if (!isContinuation)" in send
    assert "all = this._preserveCanonicalMessageIdentity(st, all)" in load
    assert "delete canonicalFields._k" in app
    assert "matched._k = mountedKey" in app
    canonical_start = app.index("_scheduleCanonicalStreamReload(sid, st")
    canonical_end = app.index("\n    _retireStaleSessionStream", canonical_start)
    canonical_reload = app[canonical_start:canonical_end]
    assert "this.loadSession(sid, { quiet: true })" in canonical_reload
    assert "quiet: sid === this.currentId" not in canonical_reload


def test_done_immediately_stamps_tool_tail_and_quietly_adopts_fork_boundary():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    append_start = app.index("_appendLiveMessage(st, m)")
    append_end = app.index("\n    _mountedMessageCap", append_start)
    append = app[append_start:append_end]
    # Every live role can become the visual turn tail. Predeclaring these keys
    # makes done-time assignments reactive on tool/status rows too.
    assert 'hasOwnProperty.call(m, "uuid")' in append
    assert 'hasOwnProperty.call(m, "forkUuid")' in append
    assert 'hasOwnProperty.call(m, "ts")' in append
    assert 'hasOwnProperty.call(m, "elapsed")' in append

    mark_start = app.index("const _markDone = (")
    mark_end = app.index("\n      const markUserFailed", mark_start)
    mark = app[mark_start:mark_end]
    assert "if (tailCandidate && tailCandidate !== stampedAssistant)" in mark
    assert "_stamp(tailCandidate)" in mark
    assert "if (!m.ts) m.ts = _now" in mark
    assert "if (!m.elapsed && _elapsed >= 1) m.elapsed = _elapsed" in mark
    assert "const completedAtMs = Number(meta.completedAtMs)" in mark
    assert "const durationMs = Number(meta.durationMs)" in mark
    assert "tailCandidate.forkUuid = assistantUuid" in mark
    # `done.assistant_uuid` is only an early fork boundary. No live message
    # may adopt it as canonical identity before the quiet history reload.
    assert ".uuid = assistantUuid" not in mark

    done_start = app.index('es.addEventListener("done"')
    done_end = app.index('es.addEventListener("error"', done_start)
    done = app[done_start:done_end]
    assert "const completedFinalText = ownsCurBubble()" in done
    assert "} else if (!d.cancelled) {" in done
    assert "this._reconcileCompletedTurn(" in done
    assert "streamSid, streamState, completedFinalText" in done
    assert "assistantUuid: d.assistant_uuid" in done
    assert "completedAtMs: d.completed_at_ms" in done
    assert "durationMs: d.duration_ms" in done

    reconcile_start = app.index("_reconcileCompletedTurn(sid, ownerState")
    reconcile_end = app.index(
        "\n    _reconcileCompletedContinuation", reconcile_start)
    reconcile = app[reconcile_start:reconcile_end]
    assert '"/api/chat/sessions/" + sid + "/active"' in reconcile
    assert "!!(await activeResponse.json()).active" in reconcile
    assert '"/api/chat/sessions/" + sid + "?tail=80"' in reconcile
    assert "m && m.role !== \"user\" && m.uuid" in reconcile
    assert "m.role === \"assistant\" && m.uuid" in reconcile
    assert "const loaded = await this.loadSession(sid, { quiet: true })" in reconcile
    assert "attempt < 30" in reconcile
    assert "Math.min(2000, 250 + attempt * 100)" in reconcile

    continuity_start = app.index("_messageContinuitySignatures(m)")
    continuity_end = app.index(
        "\n    _preserveCanonicalMessageIdentity", continuity_start)
    continuity = app[continuity_start:continuity_end]
    assert "forkUuid" not in continuity
    fork_start = app.index("turnForkMessageId(paneMsgs, i)")
    fork_end = app.index("\n    // Normalize a model-emitted path", fork_start)
    fork = app[fork_start:fork_end]
    assert "if (message.forkUuid) return message.forkUuid" in fork

    preserve_start = app.index("_preserveCanonicalMessageIdentity(st, incoming)")
    preserve_end = app.index("\n    _assignLiveKey", preserve_start)
    preserve = app[preserve_start:preserve_end]
    assert 'mountedKey.includes(":live:")' in preserve
    assert "if (liveFields.ts) matched.ts = liveFields.ts" in preserve
    assert "if (liveFields.elapsed) matched.elapsed = liveFields.elapsed" in preserve
    assert "const canonicalTail = result[result.length - 1]" in preserve
    assert "canonicalTail.elapsed = liveFooter.elapsed" in preserve

    assert "(turnForkMessageId(paneMsgs, i) && !(pane && pane.streaming))" in html
    assert '@click.stop="forkConversation(tid, turnForkMessageId(paneMsgs, i))"' in html


def test_background_settlement_pauses_footer_until_reaction_starts():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    send_start = app.index("async send(opts = {})")
    send = app[send_start:app.index("async stop()", send_start)]

    assert "_setContinuationAwaitingReaction(true)" in send
    assert "_setContinuationAwaitingReaction(false)" in send
    assert "background_tasks_pending" in send
    assert "_continuationAwaitingReaction: false" in app
    assert "!(pane._continuationAwaitingReaction)" in html


def test_terminal_event_immediately_settles_tab_activity_snapshot():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "_sessionActivityExpected: null" in app
    assert "_retainExpectedSessionActivity(meta)" in app
    assert ".map(meta => this._retainExpectedSessionActivity(meta))" in app
    assert "_setSessionActivityExpectation(tid, backgroundActive = false)" in app
    done_start = app.index("const _markDone = (")
    done = app[done_start:app.index("const markUserFailed =", done_start)]
    assert "if (authoritativeTerminal)" in done
    assert "this._setSessionActivityExpectation(" in done
    assert "_markDone(!!d.cancelled, backgroundPending, true, {" in app


def test_scheduler_uses_activity_completion_instead_of_fixed_history_polling():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("_applyScheduledActivity(item)")
    live = app[start:app.index("_applyActivityUpdate(payload)", start)]
    run_start = app.index("async runSchedTaskNow(t)")
    run = app[run_start:app.index("retrySchedHistory(h)", run_start)]

    assert "item.kind !== \"scheduled\"" in live
    assert "this.loadSchedulerHistory()" in live
    assert "this.loadSchedulerTasks()" in live
    assert "setTimeout(() => this.loadSchedulerHistory()" not in run


def test_workspace_gate_does_not_destroy_retry_or_edit_before_send_rejects():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    retry_start = app.index("retryFailedMessage(m)")
    retry = app[retry_start:app.index("onUserBubbleClick", retry_start)]
    edit_start = app.index("commitEditMessage(m)")
    edit = app[edit_start:app.index("_humanizeStreamError", edit_start)]

    assert retry.index("if (this.workspaceSwitching) return") < retry.index(
        "this.messages.splice")
    assert edit.index("this.workspaceSwitching") < edit.index("msgs.splice")


def test_queue_sync_keeps_older_success_when_newer_read_fails():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async _syncQueueFromServer(sid)")
    sync = app[start:app.index("_currentQueueLen", start)]

    assert "_queueAppliedSeq: 0" in app
    assert "seq < st._queueAppliedSeq" in sync
    assert "st._queueAppliedSeq = seq" in sync
    assert "st._queueSyncSeq !== seq" not in sync


def test_long_chat_state_is_per_tab_bounded_and_generation_safe():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    blank = app[app.index("_blankTabState()"):
                app.index("_ensureTabState(id)", app.index("_blankTabState()"))]
    assert "messagesReady: true" in blank
    assert "messagesLoading: false" in blank
    assert 'historyGeneration: ""' in blank
    assert '_historyOrder: "normal"' in blank
    assert "_hasServerLater: false" in blank
    assert "_laterMessages: []" in blank
    assert "_nextLiveKey: 1" in blank
    assert "_mountedMessageCap() { return this._isMobileLayout() ? 36 : 300; }" in app
    assert "histLen >= Math.ceil(this._mountedMessageCap() / 2)" in app
    assert "_historyCacheCap() { return this._isMobileLayout() ? 120 : 800; }" in app
    assert "const budget = this._isMobileLayout() ? 1 : this._MAX_RESIDENT_PANES" in app
    assert "_MAX_RESIDENT_PANES: 4" in app
    assert "? (_coldEarly ? 8 : 15)" in app
    assert ": (_coldEarly ? 30 : 60)" in app
    assert "&& histLen >= Math.ceil(this._mountedMessageCap() / 2)" in app
    assert "if (cst && cst.streaming) continue" not in app
    assert '"&history_generation="' in app
    assert "if (r.status === 409)" in app
    assert '"?around_uuid="' in app
    assert "full: true" not in app

    around_start = app.index("async _loadAroundMessage(sid, uuid")
    around_end = app.index("// Outline click", around_start)
    around = app[around_start:around_end]
    assert "return this._loadAroundMessage(sid, uuid, false)" in around
    assert 'this._capMountedWindow(st, "around", uuid)' in around
    assert "st._hasServerLater = !!data.has_later" in around
    assert 'st._historyOrder = data.history_order === "normal" ? "normal" : "full"' in around

    older_start = app.index("async _fetchOlderWindow(sid)")
    older_end = app.index("async _fetchLaterWindow(sid)", older_start)
    older = app[older_start:older_end]
    later_end = app.index("// Per-message placeholder height", older_end)
    newer = app[older_end:later_end]
    assert 'st._historyOrder === "full" ? "&full=1" : ""' in older
    assert 'st._historyOrder === "full" ? "&full=1" : ""' in newer
    assert "st._loadedOffset + this._allPaneMessages(st).length" in newer
    assert "< st._total" in newer

    cap_start = app.index("_capMountedWindow(st, direction")
    cap_end = app.index("// Pop the next batch", cap_start)
    cap = app[cap_start:cap_end]
    assert 'direction === "around"' in cap
    assert 'direction === "older"' in cap
    assert "st._laterMessages.splice(st._laterMessages.length - drop, drop)" in cap
    assert "_captureMessageAnchor(scrollEl, m)" in cap
    assert "_restoreMessageAnchor(scrollEl, anchor)" in cap
    # "Load earlier" keys off the server cursor first…
    assert "if (st._loadedOffset > 0) return true;" in app
    # …and, at cursor 0, off stranded pre-chain history (post-/compact), which
    # is reached by crossing orders rather than by decrementing the cursor.
    assert "return this._preCompactReachable(st);" in app
    assert 'st._historyOrder !== "full" && (st._preTotal || 0) > 0' in app
    switch_start = app.index("async _switchToFullOrder(sid)")
    switch_end = app.index("async loadEarlierMessages(sid)", switch_start)
    switch = app[switch_start:switch_end]
    # The crossing must go through around_uuid; offset arithmetic across the
    # two orders mis-seats the window (full keeps sidechains, normal doesn't).
    assert "this._loadAroundMessage(sid, anchorUuid)" in switch
    assert "&full=1" not in switch
    assert "_preTotal" not in switch.split("_preCompactReachable")[-1]
    assert "historyTruncated(sid)" in app and "return false" in app[
        app.index("historyTruncated(sid)"):app.index("async renameSession()")]

    pane_start = html.index('<div class="msg-pane"')
    pane_end = html.index("<!-- /P1 per-tab message panes", pane_start)
    pane = html[pane_start:pane_end]
    assert ':data-tid="tid"' in pane
    assert 'x-for="(m, i) in paneMsgs" :key="m._k"' in pane
    assert "pane.streaming" in pane
    assert "pane.streamElapsed" in pane
    # Elapsed reads through a null-guarded pane. `pane` resolves to null while
    # a closing tab's pane is torn down, and an unguarded property read there
    # throws inside the Alpine effect.
    assert "fmtStreamElapsed((pane && pane.streamElapsed) || 0)" in pane
    assert 'x-text="fmtStreamElapsed(pane.streamElapsed)"' not in pane
    assert "messages.length" not in re.sub(r"<!--.*?-->", "", pane, flags=re.S)
    assert "streaming &&" not in re.sub(r"<!--.*?-->", "", pane, flags=re.S).replace(
        "pane.streaming &&", "")


def test_long_stream_switches_to_plain_preview_and_final_rich_render():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert "acc.length > 32 * 1024" in app
    assert "curBubble._streamPlain = true" in app
    assert "curBubble._streamPlain = false" in app
    assert "_streamRichRenderCount" in app
    assert "_streamPlainRenderCount" in app
    assert "}, 1000);" in app
    assert 'class="stream-plain" x-text="m.text || \'\'"' in html
    assert 'x-show="!m._streamPlain" x-html="m.html || \'\'"' in html
    assert "if (this.atBottom) this.scrollToBottom(false)" in app
    assert "if (this.atBottom) this._capLiveMessages" not in app
    assert "const maxChunk = this._isMobileLayout() ? 4 : 12" in app
    assert "const frameBudgetMs = this._isMobileLayout() ? 6 : 12" in app
    assert "performance.now() - started >= frameBudgetMs" in app
    composer_start = css.index(
        ".chat-input {", css.index("VSCode-Claude style bottom input area"))
    chat_input = css[composer_start:css.index("}", composer_start)]
    assert "flex-shrink: 0" in chat_input
    assert ".chat-input-wrap { padding: 0; }" in css
    assert ".chat-toolbar.has-stop .chat-toolbar-ring" in css
    assert ".chat-toolbar-rl { display: none !important; }" in css
    assert ":class=\"{ 'has-stop': isTabStreaming(currentId) }\"" in html


def test_terminal_preview_has_local_renderer_and_management_wiring():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    vendor = FRONTEND / "vendor" / "xterm"

    for filename in ("xterm.js", "xterm.css", "addon-fit.js",
                     "xterm-LICENSE.txt", "addon-fit-LICENSE.txt"):
        assert (vendor / filename).is_file()
    create_start = app.index("async createTerminal(profileId)")
    create_end = app.index("\n    editTerminalProfile(", create_start)
    create_terminal = app[create_start:create_end]
    loader_start = app.index("async _loadTerminalLib()")
    loader_end = app.index("\n    _terminalTheme()", loader_start)
    terminal_loader = app[loader_start:loader_end]
    # A transient mobile asset failure must be retried with a fresh DOM node,
    # and the backend PTY may only be allocated after the renderer is ready.
    assert create_terminal.index("await this._loadTerminalLib()") < \
        create_terminal.index('this.api("/api/terminals"')
    assert "const ownerWorkspace = this.currentWorkspacePath()" in create_terminal
    assert "const requestHeaders = this.fileHdr(ownerWorkspace)" in create_terminal
    assert create_terminal.count("if (!isOwner()) return") >= 3
    assert "for (let attempt = 0; attempt < 2; attempt += 1)" in terminal_loader
    assert "await Promise.allSettled([" in terminal_loader
    assert "node.remove()" in terminal_loader
    assert 'meta[name="muselab-asset-version"]' in terminal_loader
    assert "const timeoutMs = 15000" in terminal_loader
    assert "const deadline = Date.now() + 30000" in terminal_loader
    assert "async createTerminal(profileId)" in app
    assert "async renameTerminal(row)" in app
    assert "async closeTerminal(id" in app
    assert "async terminateAllTerminals()" in app
    assert "async saveTerminalProfile()" in app
    assert "async deleteTerminalProfile()" in app
    # The chat header must NOT grow a second terminal entry point. The preview
    # header's terminal-manager button is the only one; a duplicate in the chat
    # header competes for the most contested row in the mobile layout.
    assert "openTerminalManagerFromChat()" not in app
    assert "terminalMobileKey(text)" in app
    assert "this._terminalSend(text)" in app
    assert "_terminalDataIsMouseReport(data)" in app
    assert "_terminalDataIsReplayReply(data)" in app
    assert "_terminalTextInputDelta(before, after)" in app
    assert "_attachTerminalImeFallback(term)" in app
    assert "_terminalNormalizeImeData(data, state, term)" in app
    assert 'textarea.addEventListener("keydown", onKeyDown, true)' in app
    assert 'textarea.addEventListener("input", onInput)' in app
    assert "Number(event.keyCode || event.which || 0) === 229" in app
    assert "_terminalHandleInput(data, term = this._terminal)" in app
    assert 'term.buffer?.active?.type !== "alternate"' in app
    assert '"\\x1b[?1000l\\x1b[?1002l\\x1b[?1003l\\x1b[?1005l"' in app
    assert "let replayActive = false" in app
    assert "let replayWritesPending = 0" in app
    assert "&& this._terminalDataIsReplayReply(data)) return" in app
    assert 'message.type === "replay_start"' in app
    assert 'message.type === "replay_end"' in app
    assert "if (this._terminal) this._terminal.focus()" in app
    assert "_attachTerminalTouchScroll(host, term)" in app
    assert "_attachTerminalSelectionCopy(host, term)" in app
    assert 'term.onSelectionChange(() =>' in app
    assert 'host.addEventListener("mousedown", onMouseDown, true)' in app
    assert 'document.addEventListener("mouseup", onMouseUp, true)' in app
    assert "if (this._terminalSelectionCleanup) this._terminalSelectionCleanup()" in app
    assert "_terminalLegacyCopy(text)" in app
    assert 'document.execCommand("copy")' in app
    assert "Stop xterm from encoding Ctrl+V as \\x16" in app
    assert "Clipboard access is restricted; press Ctrl+V or Ctrl+Shift+V" in app
    assert 'host.addEventListener("touchmove", onMove, captureActive)' in app
    assert "capture: true, passive: false" in app
    assert "if (event.cancelable) event.preventDefault()" in app
    assert "event.stopPropagation()" in app
    assert "term.scrollLines(lines)" in app
    assert 'new WheelEvent("wheel"' in app
    assert "this._terminalTouchWheelDispatching = true" in app
    assert "if (this._terminalTouchWheelDispatching)" in app
    assert "this._terminalSuppressMouseUntil = performance.now() + 500" in app
    assert "this._terminalSuppressMouseUntil = performance.now() + 800" in app
    assert "if (this._terminalTouchCleanup) this._terminalTouchCleanup()" in app
    assert "TERMINAL_SCROLLBACK_MOBILE: 3000" in app
    assert "TERMINAL_SCROLLBACK_DESKTOP: 10000" in app
    assert "? this.TERMINAL_SCROLLBACK_MOBILE" in app
    assert "cursorBlink: true" in app
    assert 'cursorStyle: "bar"' in app
    assert "minimumContrastRatio: 4.5" in app
    assert "term.parser.registerCsiHandler(" in app
    assert '{ final: "q", intermediates: " " }' in app
    assert "term.options.cursorBlink = true" in app
    assert "term.onWriteParsed(" in app
    assert 'path === this.selected && this.previewSurface === "file"' in app
    assert "profile_id: selectedProfileId" in app
    assert "const select = this.$refs.terminalProfileSelect" in app
    assert "new WebSocket(" in app
    assert "ticketResponse.data.ticket" in app
    assert 'x-ref="terminalHost"' in html
    assert "terminal-manager-pop" in html
    assert 'class="terminal-manager-backdrop"' in html
    assert 'class="terminal-manager-dismiss"' in html
    assert "'pane-floating-layer': terminalManagerOpen" in html
    assert 'class="icon-btn chat-terminal-btn"' not in html
    assert 'class="icon-btn terminal-manager-btn preview-keep-mobile"' in html
    assert 'data-terminal-key="backslash"' in html
    assert "@click=\"terminalMobileKey('\\\\')\"" in html
    assert "chat-terminal-btn" not in css
    assert ".pane.preview > .pane-head > .btn-primary { display: none; }" in css
    assert ".pane.preview .pane-head .btn-primary { display: none; }" not in css
    layer_start = css.index(".pane.pane-floating-layer")
    layer_end = css.index("}", layer_start)
    layer = css[layer_start:layer_end]
    assert "z-index: 150" in layer
    assert "overflow: visible" in layer
    assert ".terminal-host .xterm-viewport { touch-action: none; }" in css
    mobile_sheet = css[css.index(".terminal-manager-backdrop {",
                                 css.index("@media", css.index("Real PTY terminal preview"))):]
    assert "position: fixed; inset: 0; z-index: 1790" in mobile_sheet
    assert "position: fixed; top: auto; left: 0; right: 0; bottom: 0" in mobile_sheet
    assert "max-height: min(78dvh, 680px)" in mobile_sheet
    manager = html[html.index('<div class="terminal-manager"'):
                   html.index('<button x-show="previewSurface', html.index('<div class="terminal-manager"'))]
    assert manager.index("terminal-manager-head") < manager.index("terminal-launch-row")
    assert manager.index("terminal-create-btn") < manager.index("terminal-launch-row")
    assert "lang==='zh'?'+ 新建终端':'+ New terminal'" in manager
    assert 'x-model="terminalProfileId"' in html
    assert 'x-ref="terminalProfileSelect"' in html
    assert '@click="createTerminal($refs.terminalProfileSelect.value)"' in html
    assert 'class="terminal-manager-profile"' in html
    assert 'x-model="terminalProfileEditor.command"' in html
    preview_start = html.index('<section class="pane preview"')
    preview_head = html[preview_start:
                        html.index('<div class="tab-bar"', preview_start)]
    assert preview_head.index('href="#i-search"') < preview_head.index(
        'class="terminal-manager"') < preview_head.index('@click="reloadPreview()"')
    assert "lang==='zh'?'已连接':'Connected'" not in preview_head
    assert 'x-show="terminalConnection!==\'connected\'"' in preview_head


def test_terminal_ansi_palettes_are_distinct_and_readable():
    """Light terminal themes must not regress to pale dark-mode ANSI colors."""
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    palette_start = app.index("const TERMINAL_ANSI_THEMES")
    palette_end = app.index("\n});", palette_start)
    source = app[palette_start:palette_end]
    expected = {
        "black", "brightBlack", "red", "brightRed", "green", "brightGreen",
        "yellow", "brightYellow", "blue", "brightBlue", "magenta",
        "brightMagenta", "cyan", "brightCyan", "white", "brightWhite",
    }
    palettes: dict[str, dict[str, str]] = {}
    for theme in ("dark", "light", "eyecare"):
        match = re.search(
            rf"{theme}: Object\.freeze\(\{{(.*?)\}}\),",
            source,
            re.S,
        )
        assert match, f"missing terminal ANSI palette for {theme}"
        colors = dict(re.findall(
            r'(\w+): "(#[0-9a-fA-F]{6})"',
            match.group(1),
        ))
        assert set(colors) == expected
        palettes[theme] = colors

    assert palettes["dark"] != palettes["light"] != palettes["eyecare"]
    backgrounds = {"light": "#ffffff", "eyecare": "#f5f0e0"}

    def luminance(hex_color: str) -> float:
        channels = [
            int(hex_color[index:index + 2], 16) / 255
            for index in (1, 3, 5)
        ]
        linear = [
            value / 12.92 if value <= 0.04045
            else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def contrast(first: str, second: str) -> float:
        lighter, darker = sorted(
            (luminance(first), luminance(second)),
            reverse=True,
        )
        return (lighter + 0.05) / (darker + 0.05)

    for theme, background in backgrounds.items():
        failures = {
            name: round(contrast(color, background), 2)
            for name, color in palettes[theme].items()
            if contrast(color, background) < 4.5
        }
        assert not failures, f"{theme} ANSI colors below 4.5:1: {failures}"

    terminal_rule = css[css.index(".preview-body.terminal-active"):
                        css.index("}", css.index(".preview-body.terminal-active"))]
    assert "background: var(--c-bg-0)" in terminal_rule
    assert "selectionForeground:" in app
    assert "cursorAccent: background" in app
    assert "extendedAnsi[22 - 16]" in app
    assert "extendedAnsi[52 - 16]" in app
    assert 'if (this.theme !== "dark")' in app
    assert 'value("--c-diff-add-bg")' in app
    assert 'value("--c-diff-del-bg")' in app


def test_diff_surfaces_use_theme_tokens_and_readable_edges():
    """Diff rows should be calm solid washes, not dark-theme alpha overlays."""
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    expected = {
        "dark": {
            "add_bg": "#193125", "add_edge": "#4ade80",
            "del_bg": "#351f24", "del_edge": "#f87171",
        },
        "light": {
            "add_bg": "#c7e5d0", "add_edge": "#1f6333",
            "del_bg": "#f2c9c6", "del_edge": "#8d3333",
        },
        "eyecare": {
            "add_bg": "#c6d8b8", "add_edge": "#355d31",
            "del_bg": "#dfb9aa", "del_edge": "#6f3629",
        },
    }

    def luminance(hex_color: str) -> float:
        channels = [
            int(hex_color[index:index + 2], 16) / 255
            for index in (1, 3, 5)
        ]
        linear = [
            value / 12.92 if value <= 0.04045
            else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def contrast(first: str, second: str) -> float:
        lighter, darker = sorted(
            (luminance(first), luminance(second)),
            reverse=True,
        )
        return (lighter + 0.05) / (darker + 0.05)

    for theme, colors in expected.items():
        assert contrast(colors["add_edge"], colors["add_bg"]) >= 4.5, theme
        assert contrast(colors["del_edge"], colors["del_bg"]) >= 4.5, theme
        for name, value in colors.items():
            token = name.replace("_", "-")
            assert f"--c-diff-{token}: {value}" in css

    assert ".diff-ins { background: var(--c-diff-add-bg); }" in css
    assert ".diff-del { background: var(--c-diff-del-bg); }" in css
    assert "border-left: 3px solid var(--c-diff-add-edge)" in css
    assert "border-left: 3px solid var(--c-diff-del-edge)" in css
    deleted_text = css[css.index(".diff-del .diff-text"):
                       css.index("}", css.index(".diff-del .diff-text"))]
    assert "color: var(--c-fg-1)" in deleted_text
    assert "line-through" not in deleted_text
    assert 'html[data-theme="light"] .diff-body-cr .diff-line.diff-ins' not in css

    # Fenced Markdown `diff` blocks are a separate highlight.js path from the
    # Edit tool card above. Both must use the same theme tokens.
    assert ".markdown pre code.hljs .hljs-addition" in css
    assert ".bubble pre code.hljs .hljs-addition" in css
    assert "background-color: var(--c-diff-add-bg) !important" in css
    assert ".markdown pre code.hljs .hljs-deletion" in css
    assert ".bubble pre code.hljs .hljs-deletion" in css
    assert "background-color: var(--c-diff-del-bg) !important" in css

    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    theme_start = app.index("const link = document.getElementById(\"hljs-theme\")")
    theme_end = app.index("// CodeMirror:", theme_start)
    theme_switch = app[theme_start:theme_end]
    assert 'this.theme === "dark"' in theme_switch
    assert theme_switch.index("highlight-theme.css") < (
        theme_switch.index("highlight-theme-light.css")
    )


def test_file_tree_live_events_are_workspace_scoped_and_mobile_batched():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "new EventSource(`/api/files/events?${params.toString()}`)" in app
    assert '"/api/files/events-ticket"' in app
    assert "new URLSearchParams({ ticket, workspace })" in app
    assert "token: this.token, workspace" not in app
    assert "this._fileEventsWorkspace === workspace" in app
    assert 'es.addEventListener("changes"' in app
    assert 'es.addEventListener("resync"' in app
    assert "this._workspaceIsCurrent(ownerWorkspace)" in app
    assert "params.set(\"cursor\", String(cursor))" in app
    assert "_queueWorkspaceEventPayload(payload, workspace)" in app
    assert "WORKSPACE_EVENT_BATCH_MOBILE_MS: 250" in app
    assert "workspaceGeneration = this._workspaceGeneration(ownerWorkspace)" in app
    assert "_flushWorkspaceEventBatch(\n          ownerWorkspace, workspaceGeneration," in app
    assert "this._applyFileTreeDelta(fresh)" in app
    assert "path, ownerWorkspace, workspaceGeneration" in app
    assert "this._fileEventsGeneration === workspaceGeneration" in app
    assert "readyWorkspaceId !== expectedWorkspaceId" in app
    assert "_recoverWorkspaceRegistrationMismatch(" in app
    assert "const delay = this._isMobileLayout() ? 650 : 250" in app
    assert "if (!this._fileTreeIsVisible())" in app
    assert "this._stopFileEvents(true)" in app
    assert 'if (t === "files") this._flushFileTreeDirty()' not in app


def test_workspace_cache_uses_delta_without_blocking_or_copying_hidden_bursts():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    cache = (FRONTEND / "modules" / "persistent-cache.mjs").read_text(
        encoding="utf-8")
    makefile = (FRONTEND.parent / "Makefile").read_text(encoding="utf-8")

    change_start = app.index("async _changeWorkspaceSurface(path)")
    change_end = app.index("\n    async switchWorkspace(path)", change_start)
    change = app[change_start:change_end]
    assert "this.loadRoot({ runtimeSnapshot: !!runtime })" in change
    assert "coldTreeOk = await treeReady" in change
    assert "Promise.allSettled" not in change
    assert "await terminalRefresh" not in change
    assert "this.fetchTerminals({ restore: true })" in change
    assert "{ preview: !!tab.preview, reveal: false }" in change
    assert "void treeReady.then(startFileEvents)" in change
    assert "this._fileTreeDirty = treeOk !== true" in change
    assert "this._scheduleWorkspaceSyncRetry(path)" in change
    assert "\n      await refresh;" not in change

    capture_start = app.index("_captureWorkspaceSurface(path")
    capture = app[capture_start:change_start]
    assert "visible: this.visible || []" in capture
    assert "childCache: this.childCache || {}" in capture
    assert "visible: (this.visible || []).map" not in capture

    sync_start = app.index("async _syncWorkspaceTree(")
    sync_end = app.index("\n    _enqueueWorkspaceSync(", sync_start)
    sync = app[sync_start:sync_end]
    assert '`/api/files/delta?${query}`' in sync
    assert 'fetch("/api/files/bootstrap", {' in sync
    assert 'method: "POST"' in sync
    assert 'show_hidden: !!this.showHidden' in sync
    assert 'parents: expandedParents' in sync
    assert "[404, 405, 501].includes(response.status)" in sync
    assert 'ownerHeaders["X-Muselab-Workspace"]' in sync
    fallback = sync[sync.index("if ([404, 405, 501].includes(response.status))"):]
    assert fallback.index("treeSeq !== this._treeLoadSeq") < fallback.index(
        '`/api/files/bootstrap${query ? `?${query}` : ""}`')
    assert "{ headers: ownerHeaders }" in fallback
    assert "this.fileHdr()" not in fallback
    assert '`/api/files/bootstrap${query ? `?${query}` : ""}`' in sync
    assert 'params.set("show_hidden", "true")' in sync
    assert "const hasCursor = hydrated && cursor != null" in sync
    assert "void task.then(release, release)" in app
    assert "task.finally(" not in app

    delta_start = app.index("_applyFileTreeDelta(changes)")
    delta_end = app.index("\n    async _syncWorkspaceTree(", delta_start)
    delta = app[delta_start:delta_end]
    assert delta.index('part => part.startsWith(".")') < delta.index(
        "const nodes = new Map()")
    assert "const nodes = new Map()" in delta
    assert "const finalNodes = new Map()" in delta
    assert "const affectedParents = new Set()" in delta
    assert "!knownParents.has(parent)" in delta
    assert "findIndex(" not in delta
    assert ".splice(" not in delta
    assert "(this.visible || []).map(node => ({ ...node }))" not in delta
    assert "if (visibleChanged)" in delta
    assert "const nextExpanded = new Set(" in delta
    assert "this._pendingExpanded = Array.from(nextExpanded)" in delta

    persist_start = app.index("_scheduleWorkspaceTreePersist(")
    persist_end = app.index("\n    _materializeFileSnapshot(", persist_start)
    persist = app[persist_start:persist_end]
    assert "WORKSPACE_TREE_PERSIST_DEBOUNCE_MS: 1500" in app
    assert "window.requestIdleCallback" in persist
    assert 'const token = Symbol("workspace-tree-persist")' in persist
    assert "current.token !== token" in persist
    assert 'if (this.previewSurface === "terminal") return' not in persist
    assert "Clone only after the trailing debounce" in persist
    assert 'const neededParents = new Set(["", ...expanded])' in persist
    assert "Object.entries(source.childCache || {}).map" not in persist
    assert "workspaceId: this._workspaceRegistryId(ownerWorkspace)" in persist
    assert "await cache.deleteWorkspaceSnapshot(ownerWorkspace)" in persist

    assert 'const DB_NAME = "muselab-persistent-cache-v1"' in cache
    assert 'const WORKSPACES = "workspaces"' in cache
    assert "getWorkspaceSnapshot(owner)" in cache
    assert "putWorkspaceSnapshot(owner, snapshot)" in cache
    assert "deleteWorkspaceSnapshot(owner)" in cache
    assert "session-tail" not in cache
    assert "db.onversionchange = () => {" in cache
    assert "databasePromise = undefined" in cache
    assert "db.close()" in cache
    assert "persistent-cache.mjs" in makefile

    boot_start = app.index("async _bootApp()")
    boot_end = app.index("\n    // Start the always-on", boot_start)
    boot = app[boot_start:boot_end]
    assert "Promise.resolve(this.loadRoot()).catch(() => false)" in boot
    assert "this._startLiveConnections({ fileEvents: false })" in boot
    assert boot.index("this._startLiveConnections({ fileEvents: false })") < boot.index(
        "await this.fetchContextInfo()")
    assert "await rootReady" not in boot

    load_start = app.index("loadRoot({\n      runtimeSnapshot = false")
    load_end = app.index("\n    reloadTree(options = {})", load_start)
    load = app[load_start:load_end]
    assert "this._enqueueWorkspaceSync(" in load
    assert "_loadRootNow({" in app
    assert "treeSeq === this._treeLoadSeq" in load
    assert "workspaceGeneration: generation" in load

    purge_start = app.index("async _purgeWorkspaceTreeState(path)")
    purge_end = app.index("\n    async _recoverWorkspaceRegistrationMismatch(", purge_start)
    purge = app[purge_start:purge_end]
    assert "this._workspaceEpochs.set(" in purge
    assert "this._workspaceRuntimeCaches.delete(ownerWorkspace)" in purge
    assert "this._workspaceTreeCursors.delete(ownerWorkspace)" in purge
    assert "this._workspaceTreeCacheTimers.delete(ownerWorkspace)" in purge
    assert "this._workspaceSyncChains.delete(ownerWorkspace)" in purge
    assert "this._clearWorkspaceSyncRetry(ownerWorkspace)" in purge
    assert "this._clearWorkspaceEventBatch(ownerWorkspace)" in purge
    assert "this._childFetches.delete(key)" in purge
    assert "await cache.deleteWorkspaceSnapshot(ownerWorkspace)" in purge

    remove_start = app.index("async removeWorkspace(path)")
    remove_end = app.index("\n    _registerOptimisticSession", remove_start)
    remove = app[remove_start:remove_end]
    assert remove.index("if (!response.ok)") < remove.index(
        "await this._purgeWorkspaceTreeState(path)")
    assert remove.index("await this._purgeWorkspaceTreeState(path)") < remove.index(
        "await this.fetchSessionWorkspaces()")


def test_chat_code_blocks_have_copy_button_with_clipboard_fallback():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    assert "this._attachCopyBtn(el);" in app
    assert 'btn.className = "code-copy-btn"' in app
    assert "await navigator.clipboard.writeText(raw)" in app
    assert 'document.execCommand("copy")' in app
    assert "pre.has-copy-btn .code-copy-btn" in css
    assert "@media (hover: none)" in css


def test_chat_send_and_stop_buttons_are_icon_only_but_accessible():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    toolbar_start = html.index('class="btn-primary chat-toolbar-send chat-toolbar-queue"')
    toolbar_end = html.index("</button>", html.index(
        'class="btn-danger chat-toolbar-send chat-toolbar-stop"', toolbar_start,
    )) + len("</button>")
    buttons = html[toolbar_start:toolbar_end]
    assert 'x-text="t(\'btn.send\')"' not in buttons
    assert 'x-text="t(\'btn.stop\')"' not in buttons
    assert ':aria-label="_isBusy(currentId) ? t(\'queue.button_hint\') : t(\'btn.send\')"' in buttons
    assert ':aria-label="t(\'btn.stop\')"' in buttons
    assert ".chat-toolbar-send { width: 44px; padding: 0; }" in css
    assert ".chat-toolbar-send > span:nth-child(2)" not in css


def test_ctx_breakdown_maps_sdk_theme_tokens_to_real_colors():
    """SDK `color` is a THEME TOKEN NAME, not a CSS color.

    Real bug, 2026-07-25: get_context_usage() returns colors like
    "promptBorder" / "inactive" / "claude" / "warning" /
    "purple_FOR_SUBAGENTS_ONLY". The old ctxCategoryColor() returned them
    verbatim, so every swatch and bar got `background: promptBorder` — an
    invalid declaration browsers drop. The whole visualisation rendered
    blank, and the hashed-hue fallback was unreachable because `cat.color`
    is always truthy.
    """
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    for token in ("promptBorder", "inactive", "claude", "warning",
                  "purple_FOR_SUBAGENTS_ONLY"):
        assert f"{token}:" in app, f"theme token {token} left unmapped"
    start = app.index("    ctxCategoryColor(cat) {")
    body = app[start:app.index("\n    },", start)]
    # The bug was returning cat.color unconditionally. Any passthrough must
    # now be gated on it actually being a color.
    assert "if (cat && cat.color) return cat.color;" not in body
    assert "this.ctxColorTokens[raw]" in body
    assert "this._isCssColor(raw)" in body
    assert 'CSS.supports("color", v)' in app


def test_ctx_breakdown_excludes_free_space_and_deferred_from_the_stack():
    """Free space is the remainder, deferred rows aren't in totalTokens.

    Real bug, 2026-07-25: `categories` was rendered raw. "Free space"
    (114.8K = 57% of a 200K window) became the largest bar and flattened
    every real category, and the two "(deferred)" rows pushed the sum to
    208,235 against a 200,000 window — a stack overflowing its own track by
    4%. The SDK's own gridRows (the CLI /context grid) omits both.
    """
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    # Nothing may iterate the raw category list any more.
    assert "ctxBreakdown.data.categories" not in html
    assert 'x-for="cat in ctxBreakdown.view.used"' in html
    assert 'x-for="cat in ctxBreakdown.view.deferred"' in html
    start = app.index("    _ctxBuildView(data) {")
    body = app[start:app.index("\n    },", start)]
    assert "/free\\s*space/i.test(name)" in body
    assert "/\\(\\s*deferred\\s*\\)/i.test(name)" in body
    # Deferred must be summed separately, never folded into usedTotal.
    assert "deferredTotal: sum(deferred)" in body
    assert "usedTotal: sum(used)" in body


def test_ctx_breakdown_drilldown_uses_normalised_labels():
    """memoryFiles keys its name as `path`, skills wraps skillFrontmatter.

    Real bug, 2026-07-25: the template read `item.name` for every child
    list, so expanding "Memory files" — the 2nd largest real category —
    listed 14 rows with blank labels and `undefined-<tokens>` keys.
    """
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert 'x-text="item.name"' not in html
    assert 'x-text="item.label"' in html
    assert 'key="item.title + \'-\' + item.tokens"' in html
    start = app.index("    _ctxChildrenFor(data, name) {")
    body = app[start:app.index("\n    },", start)]
    assert "f.path || f.name" in body          # memoryFiles: path, not name
    assert "sk.skillFrontmatter" in body       # skills: object, not array
    assert "data.mcpTools" in body
    assert "sort((a, b) => b.tokens - a.tokens)" in body


def test_ctx_breakdown_rows_survive_the_x_for_single_root_rule():
    """Alpine x-for needs ONE root element per iteration.

    The row <div> and its drill-down <template x-if> used to be siblings
    inside the same x-for, which is not a single root. Each iteration now
    wraps both in a container.
    """
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    assert ".ctx-breakdown-swatch.hollow" in css
    assert ".ctx-breakdown-submeta" in css
    # Both x-for bodies (counted + deferred) open a wrapper <div> immediately.
    for marker in ('x-for="cat in ctxBreakdown.view.used"',
                   'x-for="cat in ctxBreakdown.view.deferred"'):
        # The stacked bar also iterates `.used`, and its body is a single
        # <div class="ctx-breakdown-stack-seg"> — already one root.
        for start in _all_indices(html, marker):
            body = html[start:start + 400]
            assert ("<div>" in body) or ("ctx-breakdown-stack-seg" in body), (
                f"x-for at {start} must wrap its iteration in a single root")


def _all_indices(hay: str, needle: str) -> list[int]:
    out, i = [], hay.find(needle)
    while i != -1:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def test_ctx_breakdown_rows_show_a_percent_column_not_a_dead_inline_bar():
    """The per-row bar was inert markup and is gone.

    `.ctx-breakdown-bar-fill` was a <span> carrying only `height: 100%`, and
    width/height do not apply to non-replaced inline boxes (CSS 2.1 §10.3.1,
    §10.6.1) — so every row bar rendered as an empty grey track for its whole
    life. The stacked bar at the top works because its segments are <div>s.
    Rather than resurrect a control that triple-encoded one number (swatch +
    bar + numeral) and flattened four of five rows into near-empty tracks, the
    column is now the share-of-window figure.
    """
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert "ctx-breakdown-bar" not in html
    assert "ctx-breakdown-bar-fill" not in css
    assert ".ctx-breakdown-pct" in css
    assert 'x-text="ctxPctLabel(cat)"' in html
    # Variable precision, or Skills (79 tokens) and System tools (10.3K) both
    # print "0%" — the exact flattening the bars already did.
    assert 'if (p < 0.1) return "<0.1%";' in app
    assert 'if (p < 10) return p.toFixed(1) + "%";' in app
    # Deferred rows get an EMPTY pct cell: their tokens are not in totalTokens,
    # so a share-of-window figure there would read as occupancy.
    assert '<span class="ctx-breakdown-pct"></span>' in html
    # Rows are a figure list now, not a table: no per-row rule.
    assert "border-bottom: 1px dashed var(--c-border);\n}" not in css.split(
        ".ctx-breakdown-row {")[1][:200]


def test_composer_settings_panel_escapes_the_overflow_hidden_composer():
    """The gear panel must not live inside .chat-input-wrap.

    Real bug, 2026-07-25: the panel was a child of .chat-toolbar-more, which
    sits inside .chat-input-wrap — and that wrap sets `overflow: hidden` to
    clip the textarea + toolbar to its rounded frame. A panel popping upward
    from in there is CLIPPED at the wrap's top edge, not merely stacked
    under something, so its z-index was irrelevant. With the zh effort label
    wrapping to 3 lines the panel ran ~176px against ~46px of room: the user
    saw one bare select and nothing else.
    """
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    panel = html.index('class="chat-toolbar-more-pop"')
    wrap = html.index('<div class="chat-input-wrap"')
    chat_input = html.index('<div class="chat-input">')
    assert chat_input < panel < wrap, (
        "the settings panel must be anchored to .chat-input, before (and thus "
        "outside) the overflow:hidden .chat-input-wrap"
    )
    # .chat-input-wrap keeps its clipping — the panel moved, the frame didn't.
    # (There are several `.chat-input-wrap {` blocks; the media-query one only
    # zeroes padding, so check every block rather than the first.)
    blocks = [css[m.end():css.index("}", m.end())]
              for m in re.finditer(r"\.chat-input-wrap \{", css)]
    assert any("overflow: hidden;" in b for b in blocks), (
        "the clip that made this bug is gone — if that was deliberate, this "
        "test and the panel's hoisting should be revisited together"
    )
    # Anchor must no longer be the button's own box.
    assert ".chat-toolbar-more { display: none; }" in css
    # Desktop re-gate: hoisting it out of the group's display:none exposed it
    # on viewports where the gear itself is hidden.
    assert "@media not all and (pointer: coarse)" in css
    assert ".chat-toolbar-more-pop { display: none !important; }" in css


def test_effort_field_uses_a_short_label_not_the_tooltip_prose():
    """`effort.title` is tooltip copy, not a field caption.

    The localized string explains the model-dependent levels and Ultra's
    proactive delegation semantics. Rendered as a visible <span> label it wraps
    to multiple lines and becomes the largest contributor to panel height.
    Mirrors the existing thinking.label / thinking.title split.
    """
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    i18n = (FRONTEND / "i18n" / "index.js").read_text(encoding="utf-8")
    assert "x-text=\"t('effort.title')\"" not in html
    assert "x-text=\"t('effort.label')\"" in html
    # Present in BOTH dictionaries, and actually short.
    assert i18n.count('"effort.label": "Effort",') == 2
    for key in ("effort.label", "effort.title"):
        assert i18n.count(f'"{key}":') == 2, f"{key} missing from a locale"
    assert "Ultra = 最大推理" in i18n
    assert "Ultra combines maximum reasoning" in i18n


def test_running_state_is_pinned_to_the_scroll_viewport_not_the_last_message():
    """A long agentic turn must show "still running" at every scroll position.

    2026-07-25 report ("这种界面很让人困惑啊 为什么没有footer？"): a screenful of
    tool cards with no time, no state, no boundary. Three separately-reasonable
    rules composed into that: (1) one footer per TURN, on its tail message;
    (2) the HH:MM stamp only lands in the `done` handler, so a running turn has
    no `ts` anywhere; (3) the pulsing avatar marks the turn's FIRST message,
    which in a 40-block turn is far off-screen. Net: the only evidence of life
    was the composer's stop button.
    """
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    pane_start = html.index('<div class="msg-pane"')
    pane_end = html.index("<!-- /P1 per-tab message panes", pane_start)
    pane = html[pane_start:pane_end]
    # Lives INSIDE the pane: panes are resident per tab, and a single shared
    # bar would report the active tab's state under every one of them.
    assert 'class="turn-running-bar"' in pane
    # The gate also excludes the pending-bubble case — see
    # test_running_bar_and_pending_bubble_are_mutually_exclusive.
    assert 'x-show="pane && pane.streaming' in pane

    block = css[css.index(".turn-running-bar {"):]
    block = block[:block.index("}")]
    assert "position: sticky" in block
    assert "bottom: 0" in block
    # Opaque, or message text shows through as it scrolls underneath.
    assert "background: var(--c-bg-1)" in block


def test_turn_footer_is_a_separator_and_no_longer_hosts_streaming_dots():
    """The footer became the turn boundary; the dots moved to the sticky bar.

    Keeping both would pulse two sets of dots ~20px apart whenever the user
    happened to be scrolled to the bottom.
    """
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    footer = html[html.index('<div class="turn-footer"'):]
    footer = footer[:footer.index("</div>")]
    assert "thinking-dots" not in footer
    assert "stream-elapsed" not in footer
    # Bracketing hairlines are what make it read as a boundary rather than a
    # caption hanging off the last card.
    assert ".turn-footer::before," in css
    assert ".turn-footer::after {" in css
    # The 40px avatar-column indent went with the dots — a boundary spans the
    # full width.
    assert "padding: 4px 0 6px 40px;" not in css
    assert "padding: 2px 0 3px 34px;" not in css


def test_per_message_timestamps_are_plumbed_but_only_shown_on_expand():
    """`mts` is the transcript wall-clock, kept distinct from the turn `ts`.

    Populating `ts` per message instead would break _markDone: it walks
    backwards looking for a bubble that ALREADY has `ts` to decide it has left
    the current turn, so it would abort on the first block it saw.
    """
    chat = (BACKEND / "chat.py").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert "def _transcript_ts_ms(entry: dict) -> int | None:" in chat
    # Both raw-entry loaders stamp it; the pure-SDK loader can't (SessionMessage
    # has no timestamp field) and consumers must tolerate its absence.
    assert chat.count("_transcript_ts_ms(e)") == 2
    assert 'entry["mts"] = mts_by_uuid[u]' in chat
    assert '__slots__ = ("uuid", "type", "message", "mts")' in chat

    # Shown only on an EXPANDED tool card — 30+ stamped cards per turn costs
    # more attention than it returns, and the separator already answers "when".
    assert 'class="msg-time"' in html
    stamp = html[html.index('class="msg-time"'):]
    stamp = stamp[:stamp.index("</div>")]
    assert "isMsgExpanded(i, m, false, pane, paneMsgs)" in stamp
    assert "m.role === 'tool_use'" in stamp


def test_queue_paused_flag_cannot_outlive_its_items():
    """An empty queue must never stay paused.

    2026-07-25: a 30-min-cap abort paused a 2-item queue; the user deleted both
    items; `paused` survived; two fresh messages then sat forever because
    dequeue_message returns None while paused — through several completed
    turns, with no banner (it was gated on `!streaming`) and no error. The
    stale flag also blocks _save_queue's empty-file cleanup, so sessions/ had
    zombie {items: [], paused: true} files up to a week old.
    """
    sessions = (BACKEND / "sessions.py").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    remove = sessions[sessions.index("def remove_queue_item("):]
    remove = remove[:remove.index("def clear_queue(")]
    assert 'if not data["items"]:' in remove
    assert 'data["paused"] = False' in remove

    # Paused beats streaming: "a turn is running" no longer implies "it will
    # drain when the turn ends".
    assert "(!streaming || tabState[currentId]._queuePaused)" in html
    # And the bubble itself says so — the banner is easy to scroll past.
    assert 'class="queued-paused-badge"' in html


def test_compact_summary_stays_collapsed_until_tapped():
    """A compact summary must never unfurl itself.

    The original "啥也没有" fix (2026-07-25) bundled two changes: render the
    summary body at all, and default it open while it was the pane's last
    bubble. The second half backfired — a compact summary sits last from the
    moment it lands until the first muse-side msg of the NEXT turn arrives, so
    both the explicit `i === paneMsgs.length - 1` hint AND isMsgExpanded's
    "streaming last block" fallback unfurled 10-20k chars on top of a turn the
    user was trying to watch. Only an explicit tap opens it now; the pill's
    state-tracking label carries the affordance instead.
    """
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")

    block = html[html.index('x-if="m._is_compact_summary"'):]
    block = block[:block.index("</template>")]
    # No default-open hint on any of the four call sites (pill click, aria,
    # label, body).
    assert "paneMsgs.length - 1" not in block
    assert block.count("isMsgExpanded(i, m, false, pane, paneMsgs)") == 4
    assert "toggleMsgExpanded(m, i, false)" in block
    # toggleMsgExpanded's defaultOpen must mirror isMsgExpanded's, else the
    # first tap computes the wrong "current" state and visibly does nothing.

    # And the fallback rule opts compact summaries out before it can fire.
    fn = js[js.index("isMsgExpanded(i, m, defaultOpen"):]
    fn = fn[:fn.index("toggleMsgExpanded(")]
    opt_out = fn.index("m._is_compact_summary")
    assert opt_out < fn.index("if (defaultOpen) return true;")
    assert opt_out < fn.index("streaming && i === msgs.length - 1")


def test_running_bar_and_pending_bubble_are_mutually_exclusive():
    """Only one live-state indicator at a time.

    The sticky .turn-running-bar shows dots + elapsed + model. So does the
    "Muse 正在思考…" pending bubble, and the bar pins itself a few px below it —
    2026-07-25 screenshot showed "运行中 · 9m27s · Opus 5" stacked directly on
    "Muse 正在思考… · Opus 5 · 9m27s". The pending bubble renders when there is
    no muse-side msg yet; the bar must require the inverse.
    """
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    bar = html[html.index('class="turn-running-bar"'):]
    bar = bar[:bar.index("</div>")]
    assert "paneMsgs[paneMsgs.length - 1].role !== 'user'" in bar
    assert "paneMsgs.length" in bar

    # The pending bubble's own gate, unchanged — the two conditions are
    # complements, so exactly one renders.
    assert ("streaming && (!messages.length\n"
            "                                       || messages[messages.length-1]"
            ".role === 'user')") in html


def test_auto_compact_drives_the_same_ui_as_a_manual_one():
    """A backend preflight compact must not masquerade as a slow turn.

    2026-07-25: a 186229/200000 session auto-compacted for 9m19s behind the
    generic "Muse 正在思考…" bubble and then died on "/compact ended without a
    ResultMessage". The manual compact path has had a 📦 bubble + ctx-meter
    shimmer since 2026-05-22, all driven by the per-tab `compacting` flag —
    the automatic path now sets the same flag rather than growing a parallel
    UI that would drift.
    """
    chat = (BACKEND / "chat.py").read_text(encoding="utf-8")
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    # Backend: both phases, on every exit path (ok, sdk failure, and the
    # "context didn't shrink" verification failure).
    pre = chat[chat.index("async def _preflight_compact_if_needed("):]
    pre = pre[:pre.index("async def event_gen():")]
    assert '_emit_compact(emit, "start"' in pre
    assert pre.count('_emit_compact(\n                emit, "end"') \
        + pre.count('_emit_compact(emit, "end"') == 3

    # The emitter is injected, not closed over: merge_q is one scope deeper.
    assert "async def _emit_side(evt: dict) -> None:" in chat
    assert "await _preflight_compact_if_needed(_emit_side)" in chat
    # A UI cue must never kill the turn it describes.
    helper = chat[chat.index("async def _emit_compact("):]
    helper = helper[:helper.index("async def _preflight_compact_if_needed(")]
    assert "except Exception as e:" in helper
    assert "if emit is None:" in helper

    # Frontend: same flag, written per-tab (a background tab's compact must not
    # animate the tab you're looking at).
    h = js[js.index('es.addEventListener("compact_progress"'):]
    h = h[:h.index('es.addEventListener("ask_user_question"')]
    assert "streamState.compacting = true;" in h
    assert "streamState.compacting = false;" in h
    assert "this.compacting" not in h

    # Cleared on stream teardown too — a turn that dies inside /compact may
    # never deliver phase:"end", and a stuck flag also blocks queue drain.
    done = js[js.index("const _markDone = ("):]
    done = done[:done.index("this._setBackgroundTaskActive(")]
    assert "streamState.compacting = false;" in done

    # Exactly one placeholder bubble: the auto-compact fires while the last msg
    # is still the user's, which is also the generic pending bubble's trigger.
    assert ("&& !(tabState[currentId] && tabState[currentId].compacting)\"\n"
            "               class=\"msg assistant\"") in html


def test_concise_mode_hides_exactly_three_card_classes():
    """Concise chat mode is a subtraction, and a narrow one.

    2026-07-26, user-selected by circling them in a screenshot: the generic
    tool bubble, its 改动预览 diff strip, and the tool_result. Everything that
    carries content or needs an action stays visible — TodoWrite, Task/Agent,
    the Task* log lines, ExitPlanMode's plan card, ask_user_question,
    permission_request. Hiding that last pair would leave the backend awaiting
    an answer the user cannot see, i.e. a silent deadlock.
    """
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    fn = js[js.index("conciseHidesToolUse(m) {"):]
    fn = fn[:fn.index("shouldHideToolResult(m) {")]
    # Only tool_use is this predicate's business.
    assert 'm.role !== "tool_use"' in fn
    for keep in ("TodoWrite", "Task", "Agent", "ExitPlanMode"):
        assert keep in fn
    assert "isTaskTool(m)) return false" in fn
    # Failures survive concise mode, on both the call and the result side.
    assert "if (m.is_error) return false;" in fn
    res = js[js.index("shouldHideToolResult(m) {"):]
    res = res[:res.index("isMsgRenderable(")]
    assert res.index("if (m.is_error) return false;") \
        < res.index("if (this.conciseChat) return true;")

    # BOTH x-if gates: the diff strip is a second template on the same message,
    # so gating only the generic bubble would leave the red/green strip behind.
    assert html.count("&& !conciseHidesToolUse(m)") == 2
    assert "['Edit','MultiEdit','Write'].includes(m.name)" in html

    # isMsgRenderable must agree with the x-if gates, or the wrapper survives
    # around a body that no longer renders — the 30-40px blank-gap bug.
    rend = js[js.index("isMsgRenderable(m, i, paneMsgs"):]
    rend = rend[:rend.index("// True iff this Edit/Write/MultiEdit")]
    assert "if (this.conciseHidesToolUse(m)) return false;" in rend
    # ...but the turn-tail short-circuit still precedes it, so the turn
    # separator survives on turns that end in a hidden tool card.
    assert rend.index("if (isTurnTail) return true;") \
        < rend.index("conciseHidesToolUse")


def test_concise_mode_is_a_device_preference_and_defaults_off():
    """Off by default, remembered per device.

    The problem it solves is "the phone screen is small", so a desktop reading
    the same session should still get the full detail — hence localStorage
    rather than a per-session setting. Default off because the cards it removes
    are the only surface on which you can notice the agent touching a file you
    did not expect.
    """
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    i18n = (FRONTEND / "i18n" / "index.js").read_text(encoding="utf-8")

    # Declared on the data object, not merely assigned in init(): the x-if
    # gates read it during the very first paint.
    assert "\n    conciseChat: false," in js
    assert 'localStorage.getItem("muselab_concise_chat") === "1"' in js
    assert 'localStorage.setItem("muselab_concise_chat",' in js

    # Toggle lives in the composer gear panel, inside the hoisted popover.
    pop = html[html.index('class="chat-toolbar-more-pop"'):]
    pop = pop[:pop.index("<!-- / slash command palette -->")]
    assert 't(\'concise.label\')' in pop
    assert '@change="toggleConciseChat()"' in pop

    # Both languages, label + tooltip, and the tooltip states the exception.
    assert i18n.count('"concise.label"') == 2
    assert i18n.count('"concise.title"') == 2
    assert "失败的工具仍会显示" in i18n
    assert "Failed tools still show" in i18n


def test_stop_aborts_stream_ticket_before_backend_turn_exists():
    """A Stop click during POST /stream/start must prevent the later turn."""
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")

    state = js[js.index("_stopping: false,"):]
    state = state[:state.index("streamingModel:", 0)]
    assert "_streamStartController: null" in state
    assert "_cancelBeforeStream: false" in state

    ticket = js[js.index("const streamStartController = new AbortController()"):]
    ticket = ticket[:ticket.index("const es = new EventSource(url)")]
    assert "signal: streamStartController.signal" in ticket
    assert "if (streamState._cancelBeforeStream)" in ticket

    stop = js[js.index("async stop() {"):]
    stop = stop[:stop.index("// ====== ask_user_question UI helpers")]
    assert "if (st._streamStartController && !st.es)" in stop
    assert "st._streamStartController.abort()" in stop
    assert "st.streaming = false" in stop
