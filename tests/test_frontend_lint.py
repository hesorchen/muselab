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
    assert 'this.mobileTab !== "preview"' in app
    assert "preview: !this._isMobileLayout()" in node_click
    assert html.count("@click=\"setMobileTab('") == 3


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

    assert "await this._changeWorkspaceSurface(path)" in switch
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

    assert "if (this._sessionListPullPromise) return this._sessionListPullPromise" in app
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


def test_interrupted_turn_is_dismissed_only_after_open():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async _checkInterruptedTurns()")
    end = app.index("\n    // 10s heartbeat", start)
    recovery = app[start:end]

    click_at = recovery.index("onClick: async () =>")
    open_at = recovery.index("await this.openTab(turn.sid)", click_at)
    dismiss_at = recovery.index("/dismiss", open_at)
    assert click_at < open_at < dismiss_at
    assert recovery.count("/dismiss") == 1


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
    effort_end = app.index("\n    async onThinkingChange", effort_start)
    effort = app[effort_start:effort_end]
    thinking_start = app.index("async onThinkingChange()")
    thinking_end = app.index("\n    modelGroups()", thinking_start)
    thinking = app[thinking_start:thinking_end]

    assert "const prior = st[tailKey] || Promise.resolve()" in serialize
    assert "Promise.resolve(prior).catch(() => {}).then(work)" in serialize
    assert "const sid = this.currentId" in effort
    assert "++st._effortPatchSeq" in effort
    assert "this.tabState[sid] !== st" in effort
    assert "st._effortPatchSeq !== seq" in effort
    assert '"_effortPatchTail"' in effort
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
    assert "name: \"\", model: newM, cwd: ownerWorkspace" in model
    assert "this._conversationWorkspaceIsCurrent(ownerWorkspace) && this.currentId === sid" in model


def test_conversation_fork_is_explicit_and_keeps_edit_and_model_switch_separate():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert '@click="menuFork(tabCtxMenu && tabCtxMenu.id)"' in html
    assert '@click.stop="forkConversation(tid, m.uuid)"' in html
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


def test_activity_center_groups_failed_tasks_as_recent():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert 'states: ["waiting_approval", "paused"]' in app
    assert 'states: ["completed", "failed", "cancelled"], readOnly: true' in app
    assert 'item.state === "failed"' in app


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
    assert "_drainPendingQueue(streamSid)" in done
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
    assert "_markDone(true)" in app[cancelled_start:cancelled_end]
    mark_done_start = app.index(
        "const _markDone = (cancelled = false, backgroundPending = false)")
    mark_done_end = app.index("\n      };", mark_done_start)
    assert "streamState._stopping = false" in app[
        mark_done_start:mark_done_end]
    assert "this.isTabStreaming(this.currentId)" in app


def test_background_task_gap_keeps_composer_busy_and_footer_running():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    i18n = (FRONTEND / "i18n" / "index.js").read_text(encoding="utf-8")

    assert "backgroundActive: false" in app
    assert "backgroundTaskCount: 0" in app
    assert "st.backgroundActive || st.compacting" in app
    assert "st.streaming || st.backgroundActive || st.compacting" in app
    assert "d.background && d.attachable === false" in app
    assert "background_tasks_pending" in app
    assert "_stopTimer(backgroundPending > 0)" in app
    assert "pane.streaming || pane.backgroundActive" in html
    assert "class=\"background-task-strip\"" in html
    assert "isTabRunning(tid)" in html
    assert "isTabBackgroundActive(tid)" in html
    assert "background-task-strip" in css
    assert '"chat.background_running": "后台任务运行中"' in i18n
    assert "if (streamState.es === es) streamState.es = null" in app
    assert "d.background && d.attachable === false" in app
    assert "continuation: !!d.continuation" in app
    assert "(m.role === 'assistant' && m.uuid)" in html
    assert 'x-show="m.role === \'assistant\' && m.uuid' in html


def test_attachment_uploads_have_deadlines_and_never_log_filenames():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert app.count("() => uploadController.abort(), 5 * 60 * 1000") == 2
    assert app.count("signal: uploadController.signal") == 2
    assert app.count("clearTimeout(uploadTimeout)") == 2
    assert "[muselab][upload]" not in app


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
    assert "const clearSubmittedComposer = () =>" in send
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
    assert "_captureComposerState(id = this.currentId)" in app
    assert "_activateComposerState(id)" in app
    assert "this._activateComposerState(id)" in app
    assert attach.index("const ownerSid = this.currentId") < attach.index(
        "await this._maybeCompressImage")
    assert "ownerDraft.pendingImages.push(raw)" in attach
    assert "ownerDraft.pendingDocs.push(raw)" in attach
    assert "this.tabState[ownerSid] === ownerState" in attach
    assert "ownerSid" in editor and "ownerEntry" in editor
    assert "ownerState.draft.pendingImages.includes(entry)" in editor
    assert "ownerState.draft.pendingImages.push(entry)" in image_gen
    assert "this.tabState[ownerSid] !== ownerState" in image_gen


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


def test_tab_disposal_aborts_uploads_and_drops_memory_only_draft():
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
    assert "_mountedMessageCap() { return this._isMobileLayout() ? 60 : 300; }" in app
    assert "_historyCacheCap() { return this._isMobileLayout() ? 120 : 800; }" in app
    assert "const budget = this._isMobileLayout() ? 1 : this._MAX_RESIDENT_PANES" in app
    assert "_MAX_RESIDENT_PANES: 4" in app
    assert "? (_coldEarly ? 8 : 15)" in app
    assert ": (_coldEarly ? 30 : 60)" in app
    assert "if (this._isMobileLayout() && histLen > 60 && shouldFollow)" in app
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
    assert "return st._loadedOffset > 0" in app
    assert "historyTruncated(sid)" in app and "return false" in app[
        app.index("historyTruncated(sid)"):app.index("async renameSession()")]

    pane_start = html.index('<div class="msg-pane"')
    pane_end = html.index("<!-- /P1 per-tab message panes", pane_start)
    pane = html[pane_start:pane_end]
    assert ':data-tid="tid"' in pane
    assert 'x-for="(m, i) in paneMsgs" :key="m._k"' in pane
    assert "pane.streaming" in pane
    assert "pane.streamElapsed" in pane
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
    assert "async createTerminal(profileId)" in app
    assert "async renameTerminal(row)" in app
    assert "async closeTerminal(id" in app
    assert "async terminateAllTerminals()" in app
    assert "async saveTerminalProfile()" in app
    assert "async deleteTerminalProfile()" in app
    assert "openTerminalManagerFromChat()" in app
    assert "terminalMobileKey(text)" in app
    assert "this._terminalSend(text)" in app
    assert "_terminalDataIsMouseReport(data)" in app
    assert "_terminalHandleInput(data, term = this._terminal)" in app
    assert 'term.buffer?.active?.type !== "alternate"' in app
    assert '"\\x1b[?1000l\\x1b[?1002l\\x1b[?1003l\\x1b[?1005l"' in app
    assert "let replayActive = false" in app
    assert "let replayWritesPending = 0" in app
    assert "if (replayActive || replayWritesPending) return" in app
    assert 'message.type === "replay_start"' in app
    assert 'message.type === "replay_end"' in app
    assert "if (this._terminal) this._terminal.focus()" in app
    assert "_attachTerminalTouchScroll(host, term)" in app
    assert 'host.addEventListener("touchmove", onMove, captureActive)' in app
    assert "capture: true, passive: false" in app
    assert "if (event.cancelable) event.preventDefault()" in app
    assert "event.stopPropagation()" in app
    assert "term.scrollLines(lines)" in app
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
    assert 'class="icon-btn chat-terminal-btn"' in html
    assert 'class="icon-btn terminal-manager-btn preview-keep-mobile"' in html
    assert 'data-terminal-key="backslash"' in html
    assert "@click=\"terminalMobileKey('\\\\')\"" in html
    assert ".pane.chat .pane-head .chat-terminal-btn { display: inline-flex; }" in css
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
    assert "this._fileEventsWorkspace === workspace" in app
    assert 'es.addEventListener("changes"' in app
    assert 'es.addEventListener("resync"' in app
    assert "this._workspaceIsCurrent(ownerWorkspace)" in app
    assert "this._refreshParentInTree(path, ownerWorkspace)" in app
    assert "const delay = this._isMobileLayout() ? 650 : 250" in app
    assert 'document.visibilityState !== "visible"' in app
    assert "this._stopFileEvents(true)" in app
    assert 'if (t === "files") this.$nextTick(() => this._flushFileTreeDirty())' in app


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
