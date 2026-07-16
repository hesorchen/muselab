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


def test_workspace_switch_changes_conversation_only_and_keeps_archive_surface():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async switchWorkspace(path)")
    end = app.index("\n    closeWorkspaceBrowser()", start)
    switch = app[start:end]

    assert "this.activeWorkspace = path" in switch
    assert "await this.fetchContextInfo()" in switch
    assert "_changeWorkspaceSurface" not in switch
    assert "_confirmLoseEdits" not in switch
    assert "loadRoot()" not in switch
    assert "loadTrash()" not in switch
    assert "_clearPreviewState" not in switch


def test_session_history_and_workspace_use_distinct_icons():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    history_start = html.index('class="chat-tab-history-btn"')
    history_end = html.index("</button>", history_start)
    workspace_start = html.index('class="workspace-picker-btn"')
    workspace_end = html.index("</button>", workspace_start)

    assert '#i-history' in html[history_start:history_end]
    assert '#i-folder' not in html[history_start:history_end]
    assert '#i-folder' in html[workspace_start:workspace_end]


def test_workspace_file_requests_reject_late_previous_owner_results():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    def method(start_marker: str, end_marker: str) -> str:
        start = app.index(start_marker)
        return app[start:app.index(end_marker, start)]

    trash = method("async loadTrash()", "\n    openTrashModal()")
    meta = method("async loadSelectedMeta(path)", "\n    // Format a unix-seconds")
    children = method("async fetchChildren(path, opts = {})", "\n    toggleHidden()")
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


def test_model_fork_keeps_workspace_and_does_not_hijack_a_new_active_tab():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async onModelChange()")
    end = app.index("\n    // ===== Effort knob", start)
    model = app[start:end]

    assert "const sid = this.currentId" in model
    assert "const ownerWorkspace = this.currentWorkspacePath()" in model
    assert "name: \"\", model: newM, cwd: ownerWorkspace" in model
    assert "this._conversationWorkspaceIsCurrent(ownerWorkspace) && this.currentId === sid" in model


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
    assert "this._scrollToUserMsg(m, sid)" in jump
    assert "this.tabState[sid] !== st || sid !== this.currentId" in jump
    assert "if (this.currentId !== sid) return" in palette_jump
    assert "body && body.querySelector" in palette_jump


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
