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


# Candidate top-level method declarations inside the Alpine x-data object.
# Calls can appear at the same four-space indentation in the boot IIFE, so the
# test below also parses through the matching `)` and requires `{` after it.
_METHOD_DEF = re.compile(
    r"^    (?:async\s+|static\s+|\*\s*)?([a-zA-Z_][a-zA-Z0-9_]*)\s*\("
)


def _is_method_definition(source: str, open_paren: int) -> bool:
    """Return whether the matching `)` is followed by a method body `{`."""
    depth = 0
    quote = ""
    escaped = False
    for i in range(open_paren, len(source)):
        char = source[i]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[i + 1:].lstrip().startswith("{")
    return False


def test_app_js_has_no_duplicate_method_definitions():
    """Guard against silently shadowed methods in app.js.

    Real bug, 2026-05-17: two `closeChatTab(id)` definitions coexisted —
    JS kept only the second, so the toolbar's close button (wired to the
    first) silently broke. This test would have caught it instantly."""
    text = (FRONTEND / "app.js").read_text(encoding="utf-8")

    names = []
    lines = text.splitlines(keepends=True)
    offsets = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    for line, offset in zip(lines, offsets):
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
        open_paren = offset + m.end() - 1
        if _is_method_definition(text, open_paren):
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


def test_empty_chat_keeps_only_the_file_mention_hint():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    i18n = (FRONTEND / "i18n" / "index.js").read_text(encoding="utf-8")
    start = html.index('<div class="brand-tips"')
    end = html.index("</div>", html.index("</div>", start) + 1)
    hints = html[start:end]

    assert "chat.empty_tip2" in hints
    for removed in ("empty_tip1", "empty_tip1b", "empty_tip3", "empty_tip4"):
        assert f"chat.{removed}" not in hints
        assert f'"chat.{removed}"' not in i18n


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
    assert "_done_memory_receipt = _persistable_memory_recall(" in chat
    assert "memory_recall=_done_memory_receipt" in chat
    assert '"/api/memory/items/" + encodeURIComponent(item.id)' in app
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
    assert "const reveal = opts.reveal === true" in open_file
    assert "const keepCurrentEditor = this.editing" in open_file
    assert open_file.index("this._confirmLoseEdits()") < open_file.index(
        "if (reveal && !this._isMobileLayout()")
    assert "if (this._isMobileLayout() && reveal) this.setMobileTab(\"preview\")" in open_file
    assert "if (reveal && !this._isMobileLayout()" in open_file
    assert 'this.previewSurface !== "terminal" || reveal' in open_file
    assert 'this.desktopFullPane = ""' in open_file
    assert "this.previewOpen = true" in open_file
    assert "this._schedulePreviewViewRestore(cachedPath, loadSeq)" in open_file
    restore_call = "{ preview: !!(_restored && _restored.preview), reveal: false }"
    assert app.count(restore_call) == 2
    assert html.count('@click="switchTab(t.path, { reveal: true })"') == 2
    assert '@click="openByPath(h.path, { reveal: true })"' in html
    assert "this.switchTab(path, { reveal: true })" in app
    assert app.count("await this.switchTab(path, { reveal: true });") == 2
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


def test_preview_selection_quote_attachment_and_side_question_are_safely_wired():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert "PREVIEW_QUOTE_MAX_CHARS: 6000" in app
    assert "this._initPreviewSelection();" in app
    assert '_previewSelectionHost(node)' in app
    assert '_chatSelectionHost(node)' in app
    assert 'el.closest(".markdown, pre.text, .xlsx-preview")' in app
    assert 'pane.dataset.tid !== this.currentId' in app
    assert 'message.classList.contains("user")' in app
    assert 'message.classList.contains("assistant")' in app
    assert "sessionId: this.currentId" in app
    assert 'snapshot.source === "chat"' in app
    assert '"引用自我的消息："' in app
    assert '"引用自 Muse 回复："' in app
    assert 'this.previewMode === "md"' in app
    assert 'this.previewMode === "text"' in app
    assert 'this.previewMode === "csv"' in app
    assert 'this.previewMode === "xlsx"' in app
    assert 'host.closest(".editor-live-preview")' in app
    assert 'previewMode === "html"' not in app[
        app.index("_previewSelectionHost(node)"):
        app.index("_syncPreviewSelection()")
    ]
    assert 'previewMode === "pdf"' not in app[
        app.index("_previewSelectionHost(node)"):
        app.index("_syncPreviewSelection()")
    ]

    assert 'class="preview-selection-popover"' in html
    assert '@click="quotePreviewSelection()"' in html
    assert '@click="openPreviewSelectionAsk()"' in html
    assert '@submit.prevent="sendPreviewSelectionQuestion()"' in html
    assert 'x-ref="previewQuoteInput"' in html
    assert ':href="previewQuoteSourceIcon()"' in html
    assert 'class="selection-quote-chip"' in html
    assert '@click="removePendingQuote(i)"' in html
    assert "将展开为独立侧问" in html
    assert "独立多轮 · 可网页搜索" in html
    assert '@click="openPreviewSelectionAskSession()"' in html
    assert 'x-ref="previewSelectionPopover"' in html
    assert '@pointerdown="startPreviewQuoteDrag($event)"' in html
    assert '@pointermove="movePreviewQuoteDrag($event)"' in html
    assert '@pointerup="finishPreviewQuoteDrag($event)"' in html
    assert 'class="preview-selection-drag-grip"' in html
    assert ".preview-selection-popover" in css
    assert "position: fixed" in css[css.index(".preview-selection-popover"):][0:300]
    assert ".preview-selection-popover.is-dragged" in css
    assert "touch-action: none" in css
    assert "_clampPreviewQuotePosition(left, top, width, height)" in app
    assert "startPreviewQuoteDrag(ev)" in app
    assert "movePreviewQuoteDrag(ev)" in app
    assert "finishPreviewQuoteDrag(ev)" in app
    assert "window.visualViewport.addEventListener" in app
    assert "new ResizeObserver" in app
    scroll_intent_start = app.index("\n    _userScrollIntent() {")
    scroll_intent_end = app.index("\n    scrollToBottom(", scroll_intent_start)
    scroll_intent = app[scroll_intent_start:scroll_intent_end]
    assert "this.dismissPreviewQuote(false)" in scroll_intent
    assert "this.dismissPreviewQuote(true)" not in scroll_intent

    quote_start = app.index("quotePreviewSelection()")
    quote_end = app.index("\n\n    removePendingQuote", quote_start)
    quote = app[quote_start:quote_end]
    assert "draft.pendingQuotes.push" in quote
    assert "draft.input" not in quote
    assert "_insertComposerTextAtCaret" not in app

    send_start = app.index("async send(opts = {})")
    send_end = app.index("\n    async stop(", send_start)
    send = app[send_start:send_end]
    assert 'const hasDetachedText = typeof opts.detachedText === "string"' in send
    assert 'const composerInput = hasDetachedText ? opts.detachedText' in send
    assert 'const composerImages = hasDetachedText ? []' in send
    assert 'const composerDocs = hasDetachedText ? []' in send
    assert 'const composerQuotes = hasDetachedText ? []' in send
    assert "this._composerPromptText(composerInput, composerQuotes)" in send
    assert "opts.permissionMode || inheritedPermission" in send
    assert "if (hasDetachedText) return;" in send
    assert "if (!hasDetachedText) this._cancelMentionLookup();" in send
    assert "if (hasDetachedText || isReconnect || resumed" in send
    assert "if (!hasDetachedText && !isReconnect && !resumed)" in send
    ask_start = app.index("async sendPreviewSelectionQuestion()")
    ask_end = app.index("\n\n    // A11y:", ask_start)
    ask = app[ask_start:ask_end]
    assert "_createPreviewSelectionAskSession" in app
    assert "/fork`" in app
    assert 'fetch("/api/chat/sessions"' in app
    assert "sessionId: target.id" in ask
    assert "detachedText: prompt" in ask
    assert 'permissionMode: "default"' in ask
    assert "this.dismissPreviewQuote(true)" not in ask
    assert "newSession" not in ask


def test_side_question_stays_floating_and_is_hidden_from_activity_center():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    chat = (BACKEND / "chat.py").read_text(encoding="utf-8")
    sessions = (BACKEND / "sessions.py").read_text(encoding="utf-8")

    transient_start = app.index("\n    dismissTransientPreviewQuote(") + 1
    transient_end = app.index("\n    dismissPreviewQuote(", transient_start)
    transient = app[transient_start:transient_end]
    assert 'this.previewQuote.mode === "ask"' in transient
    assert "return false" in transient

    selection_start = app.index("_initPreviewSelection()")
    selection_end = app.index("\n    _previewQuoteElement()", selection_start)
    selection = app[selection_start:selection_end]
    assert "if (!inPopover) this.dismissTransientPreviewQuote(false)" in selection
    assert "if (!inPopover && this.previewQuote.show) this.dismissPreviewQuote" not in selection
    for owner_change in (
        'this.$watch("currentId"',
        'this.$watch("selected"',
        "onPreviewViewportScroll()",
        "async openFile(n, opts = {})",
    ):
        start = app.index(owner_change)
        window = 1800 if owner_change.startswith("async openFile") else 700
        assert "dismissTransientPreviewQuote" in app[start:start + window]

    create_start = app.index("async _createPreviewSelectionAskSession(")
    create_end = app.index("\n    previewSelectionAskMessages()", create_start)
    create = app[create_start:create_end]
    assert create.count("activity_hidden: true") >= 3
    assert create.count('runtime_profile: "side_question"') >= 3
    assert "sendPreviewSelectionFollowup()" in app
    assert "previewSelectionAskConversation()" in app
    assert "WebSearch or WebFetch" in app
    assert "preview-selection-followup" in html
    assert "activity_hidden: bool = False" in chat
    assert "broadcast.activity_hidden = bool" in chat
    assert "if broadcast.activity_hidden:" in chat
    assert "and not broadcast.activity_hidden" in chat
    assert '"activity_hidden": bool(m.get("activity_hidden", False))' in sessions


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
    ime_start = app.index("    _isImeComposingEvent(ev) {")
    ime_end = app.index("\n    },", ime_start)
    ime = app[ime_start:ime_end]
    recent_start = app.index("    _isRecentImeCommitEnter(ev) {")
    recent_end = app.index("\n    },", recent_start)
    recent = app[recent_start:recent_end]
    consume_start = app.index("    _consumeRecentImeCommitEnter(ev) {")
    consume_end = app.index("\n    },", consume_start)
    consume = app[consume_start:consume_end]
    assert "this._hasExplicitImeSignal(ev)" in ime
    assert "target._museImeComposing" in ime
    assert "target._museImeEndedAt" not in ime
    assert "target._museImeEndedAt" in recent
    assert "target._museImeCommitEnterDown" in recent
    assert "eventAt - endedAt <= 1000" in recent
    assert "this._consumeRecentImeCommitEnter(ev)" in helper
    assert consume.index("ev.preventDefault()") < consume.index("return true")
    composing_branch = helper[helper.index("this._isImeComposingEvent(ev)"):]
    assert composing_branch.index("return false") \
        < composing_branch.index("ev.preventDefault()")
    assert "_museImeOriginalForceModelUpdate" not in app
    assert "target._x_forceModelUpdate = value =>" not in app
    assert "_syncChatInputDom(value = this.input, options = {})" in app
    assert "target._museImeComposing" in app
    assert "ev.inputType === \"insertCompositionText\"" in app
    assert "this._finishImeComposition(target)" in app
    assert "if (this.input !== target.value) this.input = target.value" in app
    assert "ev.isComposing === true" in app
    assert "onImeEnterKeyup(ev)" in app
    assert '@keyup.enter="onImeEnterKeyup($event)"' in html

    assert '@keydown.enter="confirmModalOnEnter($event)"' in html
    assert '@keydown.enter="commitRenameTabOnEnter($event)"' in html
    assert '@keydown.enter="pickerCommitInlineRenameOnEnter($event)"' in html
    assert '@keydown.enter="onEnter($event)"' in html
    assert '@compositionstart="onImeCompositionStart($event)"' in html
    assert '@compositionend="onImeCompositionEnd($event)"' in html
    assert '@beforeinput="onChatBeforeInput($event)"' in html
    assert '@blur="onChatInputBlur($event)"' in html
    assert 'x-effect="_syncChatInputDom(input, { target: $el })"' in html
    assert 'x-model="input"' not in html
    assert 'x-model.unintrusive="input"' not in html
    assert '@keydown.enter.prevent="commitRenameTab()"' not in html
    assert '@keydown.enter.prevent="pickerCommitInlineRename()"' not in html
    assert '@keydown.enter.prevent.stop="onEnter($event)"' not in html


def test_stale_ime_recovery_requires_five_second_plain_enter():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    mark_start = app.index("    _markLocalImeComposition(target, restart = false) {")
    mark = app[mark_start:app.index("\n    },", mark_start)]
    explicit_start = app.index("    _hasExplicitImeSignal(ev) {")
    explicit = app[explicit_start:app.index("\n    },", explicit_start)]
    recover_start = app.index(
        "    _recoverStaleImeCompositionOnPlainEnter(ev) {")
    recover = app[recover_start:app.index("\n    },", recover_start)]
    claim_start = app.index("    _claimNonImeEnter(ev) {")
    claim = app[claim_start:app.index("\n    },", claim_start)]

    assert "IME_STALE_AFTER_MS: 5000" in app
    assert "target._museImeStartedAt = Date.now()" in mark
    assert "restart || !target._museImeComposing || startedAt <= 0" in mark
    assert "ev.isComposing === true" in explicit
    assert "ev.keyCode === 229" in explicit
    assert "ev.which === 229" in explicit
    assert 'ev.key === "Process"' in explicit
    assert 'ev.key === "Enter"' in recover
    assert "ev.shiftKey || ev.ctrlKey || ev.metaKey || ev.altKey" in recover
    assert "this._hasExplicitImeSignal(ev)" in recover
    assert "Date.now() - startedAt < this.IME_STALE_AFTER_MS" in recover
    assert "this._finishImeComposition(target)" in recover
    assert "target._museImeStartedAt = 0" in recover
    assert claim.index("this._recoverStaleImeCompositionOnPlainEnter(ev)") \
        < claim.index("this._isImeComposingEvent(ev)")


def test_side_question_textareas_share_dom_local_ime_guard():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert index.count('@compositionstart="onLocalImeCompositionStart($event)"') >= 2
    assert index.count('@compositionend="onLocalImeCompositionEnd($event)"') >= 2
    assert index.count('@beforeinput="onLocalImeBeforeInput($event)"') >= 2
    assert index.count('@input="onLocalImeInput($event)"') >= 2
    for name in ("onPreviewQuoteAskEnter(ev)", "onPreviewQuoteFollowupEnter(ev)"):
        start = app.index(name)
        block = app[start:app.index("\n    },", start)]
        assert "this._consumeRecentImeCommitEnter(ev)" in block
        assert "this._isImeComposingEvent(ev)" in block
        assert "ev.preventDefault()" in block


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


def test_desktop_layout_is_files_chat_preview_with_one_canonical_chat_dom():
    """Chat is the flexible center; preview is the optional right rail."""
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    i18n = (FRONTEND / "i18n" / "index.js").read_text(encoding="utf-8")

    assert html.count('<aside class="pane chat"') == 1
    assert html.count('<section class="pane preview"') == 1
    assert ".layout > .pane.files { order: 1; }" in css
    assert ".layout > .files-resizer { order: 2; }" in css
    assert ".layout > .pane.chat { order: 3; }" in css
    assert ".layout > .preview-resizer { order: 4; }" in css
    assert ".layout > .pane.preview { order: 5;" in css

    preview_start = html.index('<section class="pane preview"')
    preview_head = html.index("<header", preview_start)
    assert "'pane-hidden': !previewOpen" in html[preview_start:preview_head]
    chat_start = html.index('<aside class="pane chat"')
    chat_head = html.index("<header", chat_start)
    assert "pane-hidden" not in html[chat_start:chat_head]

    assert "previewOpen: true" in app
    assert "previewWidth: 440" in app
    assert "togglePreviewPane()" in app
    assert "rightOpen: true" not in app
    assert "rightWidth: 440" not in app
    assert "leftOpen: this.leftOpen, previewOpen: this.previewOpen" in app
    assert "leftWidth: this.leftWidth, previewWidth: this.previewWidth" in app
    assert 'leftWidth: 340' in app
    assert 'schema: 9' in app
    assert 'Math.abs(p.leftWidth - 280) <= 1' in app
    assert 'else if (typeof p.rightWidth === "number")' in app
    assert 'if (next === "preview") this.previewOpen = true;' in app
    assert "btn.hide_preview" in i18n and "btn.show_preview" in i18n


def test_external_file_drop_is_global_root_by_default_and_directory_explicit():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    listener_start = app.index("// Document-level OS-file-drag detection")
    listener_end = app.index("// HTML preview bridge", listener_start)
    listeners = app[listener_start:listener_end]
    assert 'document.addEventListener("drop", (e) =>' in listeners
    assert "this._externalFileDropTarget(e.target)" in listeners
    assert "this._uploadFilesToDir(dropTarget.dir, files)" in listeners
    assert "e.stopPropagation()" in listeners
    assert "}, true);" in listeners

    target_start = app.index("_externalFileDropTarget(target)")
    target_end = app.index("\n    async upload(ev)", target_start)
    target = app[target_start:target_end]
    assert '.closest(".filelist li.dir[data-path]")' in target
    assert 'dir: row ? String(row.dataset.path || "") : ""' in target

    attach_start = app.index("async onAttachDrop(ev)")
    attach_end = app.index("\n    async onImagePaste", attach_start)
    attach = app[attach_start:attach_end]
    assert 'this._uploadFilesToDir("", files)' in attach
    assert "_attachFile" not in attach

    drop_start = app.index("async onDrop(ev, n)")
    drop_end = app.index("\n    // Parallel-upload", drop_start)
    tree_drop = app[drop_start:drop_end]
    assert 'this._uploadFilesToDir(n.is_dir ? n.path : "", files)' in tree_drop
    assert 'class="global-file-drop-overlay"' in html
    assert "上传到工作区根目录" in html
    assert ".global-file-drop-overlay" in css
    assert "pointer-events: none" in css

    nav = html[html.index('<nav class="mobile-tab-bar"'):
               html.index("</nav>", html.index('<nav class="mobile-tab-bar"'))]
    # Keep the primary conversation action in the centre of the mobile nav.
    assert nav.index("mobileTab==='files'") < nav.index(
        "mobileTab==='chat'") < nav.index("mobileTab==='preview'")


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
    assert "workspaceSurfaceTransition: false" in app
    assert "const switchSeq = ++this._workspaceSwitchSeq" in switch
    assert "this.workspaceSurfaceTransition = true" in switch
    assert "await this.$nextTick()" in switch
    assert "this.workspaceSurfaceTransition = false" in switch
    assert "const previousMobileTab = this.mobileTab" in switch
    assert "this.setMobileTab(previousMobileTab)" in switch
    assert 'class="workspace-switch-shield"' in html
    assert 'x-show="workspaceSurfaceTransition"' in html
    assert ':aria-busy="workspaceSurfaceTransition"' in html
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


def test_session_rename_patches_activity_without_reloading_chat():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    helper_start = app.index("_applyRenamedSession(sid, name) {")
    helper = app[helper_start:app.index("\n    async pickerCommitInlineRename()", helper_start)]
    modal_start = app.index("async renameSession() {")
    modal = app[modal_start:app.index("\n    // ===== settings modal", modal_start)]

    assert "item.session_name = name" in helper
    assert "this.activity.events = [...this.activity.events]" in helper
    assert app.count("this._applyRenamedSession(") == 2
    assert "refreshSessions" not in modal
    assert "loadSession" not in helper
    assert "fetchActivity" not in helper


def test_file_tree_metadata_is_adaptive_and_human_readable():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert 'const units = ["B", "KB", "MB", "GB", "TB"]' in app
    assert "fmtRelativeMtime(ts)" in app
    assert "fileMetaTitle(meta)" in app
    assert 'class="tree-trailing"' in html
    assert 'class="pane-fileinfo-meta"' in html
    assert "fileBreadcrumb(selected)" in html
    assert ".filelist li:hover .size { opacity: 0; }" in css
    assert "@container (max-width: 250px)" in css
    assert "font-variant-numeric: tabular-nums" in css


def test_file_header_keeps_theme_and_hidden_toggles_as_direct_actions():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    files_head_start = html.index('<aside class="pane files"')
    files_head_end = html.index("</header>", files_head_start)
    files_head = html[files_head_start:files_head_end]

    assert files_head.count('@click="toggleTheme()"') == 1
    assert files_head.count('@click="toggleHidden()"') == 1
    assert 'class="icon-btn files-keep-mobile files-theme-toggle"' in files_head
    assert 'class="icon-btn files-hidden-toggle"' in files_head
    assert "filesToolsOpen" not in html
    assert ".files-tools" not in css
    assert ".files-theme-mobile" not in css


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
    assert app.count('replace(/^ducc:/i, "")') >= 2
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
    assert 'x-text="currentForkSource()?.name || \'\'"' in html

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


def test_chat_file_urls_are_routed_through_workspace_preview():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    helper_start = app.index("_localFileUrlPath(href)")
    helper_end = app.index("\n    // Rewrite author-relative", helper_start)
    helper = app[helper_start:helper_end]
    linkify_start = app.index("_linkifyFilePaths(rootEl)")
    linkify_end = app.index("\n    // Delegated click handler", linkify_start)
    linkify = app[linkify_start:linkify_end]
    click_start = app.index("onChatClick(ev)")
    click_end = app.index("\n    // Fallback resolver", click_start)
    click = app[click_start:click_end]

    sanitize_start = app.index("window.DOMPurify.sanitize(raw")
    sanitize_end = app.index("\n      // Restore protected math", sanitize_start)
    sanitize = app[sanitize_start:sanitize_end]

    assert 'if (!/^file:/i.test(String(href || ""))) return null' in helper
    assert 'url.hostname !== "localhost"' in helper
    assert "decodeURIComponent(url.pathname" in helper
    assert "ALLOWED_URI_REGEXP" in sanitize
    assert "file|mailto|tel" in sanitize
    assert "const localFilePath = this._localFileUrlPath(href)" in linkify
    assert 'a.removeAttribute("href")' in linkify
    assert 'a.classList.add("file-link")' in linkify
    assert 'a.setAttribute("href", "#")' in linkify
    assert "const localFilePath = this._localFileUrlPath(href)" in click
    assert "href = localFilePath" in click
    assert "this.openByPathToasted(p)" in click


def test_activity_center_groups_by_attention_order_and_read_state():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    activity_state = app[app.index("activity: {"):app.index("_activityEtags:")]
    assert 'view: "groups"' in activity_state

    group_button = html.index("setActivityView('groups')")
    status_button = html.index("setActivityView('status')")
    timeline_button = html.index("setActivityView('timeline')")
    assert group_button < status_button < timeline_button

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
    assert "ACTIVITY_TIMELINE_CAP: 15" in app
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
    custom_sort = app.index("const aManual = Number.isFinite")
    assert "if (aManual !== bManual) return aManual ? 1 : -1" in app[custom_sort:]
    assert "Number(a.group_order) - Number(b.group_order)" in app[custom_sort:]
    assert 'group.key === "custom:__ungrouped__"' in app[custom_sort:]
    assert "this._activityAppliedSeq = ++this._activityRequestSeq" in app
    assert '"/api/activity/events-ticket"' in app
    assert "new EventSource(" in app
    assert 'this.activity.view === "timeline"' in app
    assert 'key === "timeline") return true' in app
    assert "setActivityView('timeline')" in html
    assert "setActivityView('groups')" in html
    assert 'activity.view === "groups"' in app
    assert "activityCustomGroupSections()" in app
    assert 'key: "custom:__ungrouped__"' in app
    assert '"/api/activity/groups"' in app
    assert '"/api/activity/groups/order"' in app
    assert '}/group`' in app
    assert 'class="activity-groups-toolbar"' in html
    assert 'class="activity-group-editor"' in html
    assert "'is-group-board': activity.view === 'groups'" in html
    assert 'class="activity-row-group"' in html
    assert '@dragstart="onActivityDragStart($event, item)"' in html
    assert '@dragover.stop.prevent="onActivityRowDragOver($event, group, item)"' in html
    assert '@drop.stop.prevent="onActivityRowDrop(group, item)"' in html
    assert '@dragstart.stop="onActivityGroupOrderDragStart($event, group)"' in html
    assert '@drop.stop.prevent="group.custom && onActivityLaneDrop(group)"' in html
    assert "before_event_id" in app
    assert 'groupOrder: ["__ungrouped__"]' in app
    assert 'result.push("__ungrouped__")' in app
    assert 'x-show="!group.builtin"' in html
    assert "|| group?.builtin) return" in app
    assert "async persistActivityGroupOrder(next, previous)" in app
    assert "const task = prior.catch(() => {}).then(run)" in app
    assert "json: { ids: requestedOrder }" in app
    assert "incomingRevision < this._activityRevision" in app
    assert "if (this.activitySearchQuery() || group?.builtin) return false" in app
    update_start = app.index("_applyActivityUpdate(payload)")
    update_end = app.index("\n    async _startActivityEvents()", update_start)
    update = app[update_start:update_end]
    assert update.index("revision && revision <= this._activityRevision") < update.index(
        "this.applyActivityGroupPayload(payload)")
    assert ".activity-group.is-custom.is-drag-over" in css
    assert ".activity-group.is-custom.is-group-drop-before" in css
    assert ".activity-row-wrap.drop-before::before" in css
    assert ".activity-move-menu" in css
    assert "const pinRank = Number(!!b.pinned) - Number(!!a.pinned)" in app
    assert "async toggleActivityPin(item)" in app
    assert 'method: "PATCH", json: { pinned: target }' in app
    assert '@click.stop="toggleActivityPin(item)"' in html
    assert 'x-show="activity.view === \'timeline\'"' in html
    assert ".activity-pin.active" in css
    assert ".activity-modal { width:700px" in css
    assert ".modal.activity-modal.is-group-board" in css
    assert "width:min(1120px,calc(100vw - 64px))" in css
    assert "height:auto" in css
    assert "flex:1 1 auto" in css
    assert "grid-template-columns:repeat(auto-fit,minmax(300px,1fr))" in css
    assert "grid-auto-rows:300px" in css
    assert ".activity-body.is-group-board > .activity-group.is-custom" in css
    assert "ACTIVITY_CUSTOM_GROUP_CAP: 50" in app
    assert "if (group?.custom) return this.ACTIVITY_CUSTOM_GROUP_CAP" in app
    assert "max-height:min(82vh,820px)" in css
    assert "min-height:min(56vh,520px)" in css
    assert "scrollbar-gutter:stable" in css

    # The left marker is unread/action state, not a permanent failure marker.
    assert "activityIsUnreadResult(item) ? ' is-unread'" in html
    assert ".activity-row.failed.is-unread .activity-state-dot" in css
    assert ".activity-row.failed .activity-state-dot,.activity-row.waiting_approval" not in css


def test_activity_center_searches_loaded_sessions_before_group_caps():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    activity_state = app[app.index("activity: {"):app.index("_activityEtags:")]
    match_start = app.index("activityMatchesSearch(item)")
    match_end = app.index("\n    activitySearchResultCount", match_start)
    matcher = app[match_start:match_end]
    all_start = app.index("activityAllEvents(group)")
    all_end = app.index("\n    activityEvents(group)", all_start)
    all_events = app[all_start:all_end]
    count_start = app.index("activityGroupCount(group)")
    count_end = app.index("\n    activityVisibleGroups()", count_start)
    counts = app[count_start:count_end]

    assert 'query: ""' in activity_state
    for field in (
        "session_name", "task_summary", "session_id", "thread_id",
        "workspace", "workspace_name", "state", "status_detail",
    ):
        assert f"item?.{field}" in matcher
    assert "this.activityStateLabel(item?.state)" in matcher
    assert ".filter(item => this.activityMatchesSearch(item))" in all_events
    assert all_events.index("activityMatchesSearch") < all_events.index(".sort(")
    assert "this.activitySearchQuery()" in counts
    assert 'role="search"' in html
    assert 'x-model="activity.query"' in html
    assert "activitySearchResultCount() + ' 条匹配'" in html
    assert '@keydown.escape.stop.prevent="clearActivitySearch()"' in html
    assert 'class="activity-search-clear"' in html
    assert 'class="hint-row activity-search-empty"' in html
    assert "没有匹配的会话" in html
    assert "group.custom && !activitySearchQuery()" in html
    assert ".activity-searchbar" in css
    assert ".activity-search-empty" in css


def test_memory_recall_details_use_a_root_fixed_portal():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    trigger = html.index('class="memory-recall-trace"')
    portal = html.index('class="memory-recall-global"')
    activity = html.index('class="modal activity-modal"')
    assert trigger < portal < activity
    assert 'class="memory-recall-popover"' not in html
    assert "document.querySelector(\".memory-recall-global\")" in app
    assert '"position:fixed"' in app
    assert "_queueMemoryRecallPosition()" in app
    assert 'cache: "no-store"' in app
    assert ".memory-recall-global {" in css
    portal_css = css[css.index(".memory-recall-global {"):]
    portal_css = portal_css[:portal_css.index("}")]
    assert "position: fixed" in portal_css
    assert "z-index: 880" in portal_css


def test_chat_header_exposes_authenticated_session_todo_board():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert "sessionTodoOpen: false" in app
    assert 'sessionTodoDraft: ""' in app
    assert 'sessionTodoEditId: ""' in app
    assert 'sessionTodoEditDraft: ""' in app
    assert "userTodos: []" in app
    assert "_globalUserTodoStorageKey()" in app
    assert 'return "muselab.userTodos.global"' in app
    assert "addSessionUserTodo()" in app
    assert "toggleSessionUserTodo(id)" in app
    assert "deleteSessionUserTodo(id)" in app
    assert "startSessionTodoEdit(item, ev)" in app
    assert "saveSessionTodoEdit(id = this.sessionTodoEditId, restoreFocus = false)" in app
    assert "cancelSessionTodoEdit(restoreFocus = false)" in app
    assert "sessionTodosForPriority(priority)" in app
    assert "sessionTodoIndicatorPriority()" in app
    assert 'item.priority === "high"' in app
    assert 'item.priority === "medium"' in app
    assert "onSessionTodoDragStart(ev, item)" in app
    assert "onSessionTodoDrop(ev, priority" in app
    assert "onSessionTodoPointerDown(ev, item)" in app
    assert "onSessionTodoPointerMove(ev)" in app
    assert "onSessionTodoPointerEnd(ev)" in app
    assert "onSessionTodoGripKeydown(ev, item)" in app
    assert 'priority: "medium"' in app
    todo_start = app.index("\n    _globalUserTodoStorageKey() {")
    todo_impl = app[todo_start:app.index("taskLogLine(m)", todo_start)]
    assert "this.currentId" not in todo_impl
    assert "_ensureTabState" not in todo_impl
    assert "tabState" not in todo_impl
    assert 'startsWith("muselab.userTodos.")' in todo_impl
    assert "TodoWrite" not in todo_impl
    assert "TaskCreate" not in todo_impl
    assert "TaskUpdate" not in todo_impl
    activity = html.index('class="activity-center-btn"')
    button = html.index('class="session-todo-btn icon-btn"', activity)
    assert activity < button
    assert '@click="toggleSessionTodoBoard()"' in html
    todo_button = html[button:html.index("</button>", button)]
    assert 'x-show="sessionTodoIndicatorPriority()"' in todo_button
    assert ':class="\'is-\' + sessionTodoIndicatorPriority()"' in todo_button
    assert "sessionTodoCount(true) + '/' + sessionTodoCount()" not in todo_button
    assert 'class="modal session-todo-modal"' in html
    assert "'待办事项'" in html
    assert 'x-show="sessionTodoOpen"' in html
    assert '@click.self="closeSessionTodoBoard()"' in html
    assert '@submit.prevent="addSessionUserTodo()"' in html
    assert "['high','medium','low']" in html
    assert '@dblclick.prevent="startSessionTodoEdit(item, $event)"' in html
    assert ':draggable="sessionTodoEditId !== item.id"' in html
    assert 'class="session-todo-edit"' in html
    assert '@keydown.enter="if (_claimNonImeEnter($event)) saveSessionTodoEdit(item.id, true)"' in html
    assert "cancelSessionTodoEdit(true)" in html
    assert '@compositionstart="onLocalImeCompositionStart($event)"' in html
    assert '@compositionend="onLocalImeCompositionEnd($event)"' in html
    assert '@keydown="onLocalImeKeydown($event)"' in html
    assert '@blur="saveSessionTodoEdit(item.id)"' in html
    assert '@dragstart="onSessionTodoDragStart($event, item)"' in html
    assert '@drop="onSessionTodoDrop($event, priority)"' in html
    assert '@drop.stop="onSessionTodoDrop($event, priority, item.id)"' in html
    assert '@pointerdown="onSessionTodoPointerDown($event, item)"' in html
    assert '@pointermove.window="onSessionTodoPointerMove($event)"' in html
    assert '@keydown="onSessionTodoGripKeydown($event, item)"' in html
    assert 'class="session-todo-priority-select"' not in html
    assert 'class="session-todo-move"' not in html
    assert '@click="toggleSessionUserTodo(item.id)"' in html
    assert '@click="deleteSessionUserTodo(item.id)"' in html
    assert '@keydown.tab="trapDialogFocus($event, \'session-todo\')"' in html
    assert ".session-todo-modal" in css
    assert ".modal.session-todo-modal" in css
    assert "width:min(1040px,calc(100vw - 64px))" in css
    assert "height:min(76vh,720px)" in css
    assert ".session-todo-board" in css
    assert ".session-todo-lane.is-high" in css
    assert ".session-todo-badge.is-high" in css
    assert ".session-todo-badge.is-medium" in css
    assert ".session-todo-modal .session-todo-edit" in css
    assert ".session-todo-compose" in css
    assert "@media (max-width:720px)" in css


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


def test_memory_center_shortcut_sits_immediately_after_activity_center():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    activity_start = html.index('class="activity-center-btn"')
    activity_end = html.index("</button>", activity_start) + len("</button>")
    memory_start = html.index('@click="openMemoryCenter()"', activity_end)
    skills_start = html.index('@click="toggleSkillsDrawer()"', activity_end)

    assert activity_end < memory_start < skills_start
    shortcut = html[memory_start - 100:html.index("</button>", memory_start)]
    assert 'href="#i-brain"' in shortcut
    assert "打开记忆中心" in shortcut
    assert 'async openSettings(activePage = "")' in app
    assert 'activePage === "memory"' in app
    assert 'async openMemoryCenter(tab = "")' in app
    assert 'await this.openSettings("memory")' in app


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
    assert "if (d.is_error && !d.cancelled)" in done
    assert "const queueBlockingError = !!d.is_error && !isContinuation" in done
    assert "if (queueBlockingError)" in done
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


def test_context_recovery_opens_branch_and_never_reuses_attachment_ids():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    adopt_start = app.index("async _adoptRecoveredSession")
    adopt = app[adopt_start:app.index("async runCompact", adopt_start)]
    compact_start = app.index("async runCompact")
    compact = app[compact_start:app.index("onChatArrowUp", compact_start)]
    send_start = app.index("async send(opts = {})")
    send = app[send_start:app.index("async stop()", send_start)]
    error_start = send.index('es.addEventListener("error"')
    error = send[error_start:send.index(
        'es.addEventListener("cancelled"', error_start)]

    assert "recovered_session || payload.session" in adopt
    assert "this.currentId === sourceSid" in adopt
    assert "await this.openTab(newId, shouldFocus)" in adopt
    assert "compactResult.recovered_session" in compact
    assert "if (!recoveredSession)" in compact
    assert "const contextRecoveryAttempted = !!opts.contextRecoveryAttempted" in send
    assert "isReconnect && !!sendState._contextRecoveryAttempted" in send
    assert "errorMeta.recovered_session" in error
    assert "&& !contextRecoveryAttempted" in error
    assert "const attachmentRecovery = attachIds.length > 0" in error
    assert "!isReconnect && !isContinuation && !resumed" in error
    assert "this._contextRecoveryAutoSent[recoveryId]" in error
    assert "detachedText: text" in error
    assert "detachedDisplayText: composerInput || text" in error
    assert "contextRecoveryAttempted: true" in error
    assert "recoveryDraft = composerInput || text" in error
    assert "pendingImages" not in error[error.index("if (hasContextRecovery)"):]
    assert "pendingDocs" not in error[error.index("if (hasContextRecovery)"):]


def test_workspace_switch_disables_composer_and_gates_programmatic_user_send():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    start = app.index("async send(opts = {})")
    send = app[start:app.index("// ====== ask_user_question", start)]
    textarea = html[html.index('<textarea x-ref="chatInput"'):]
    textarea = textarea[:textarea.index("</textarea>")]

    assert "if (this.workspaceSwitching && !opts.reconnect && !opts.resumedItem) return" in send
    assert ':disabled="workspaceSwitching || !availableModels.length"' in textarea
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
    assert "tabState[currentId]?._stopping" in html
    assert "等待上一条任务完成中断" in html
    assert "sendButtonHint(currentId)" in html
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
    cancelled = app[cancelled_start:cancelled_end]
    assert "_markDone(true, false, true, {" in cancelled
    assert 'turnStatus: "cancelled"' in cancelled
    assert "d.snapshot_ready" in cancelled
    assert "streamState._seenUpdated = undefined" in cancelled
    assert "quiet: true" in cancelled
    assert "probeActive: false" in cancelled
    mark_done_start = app.index("const _markDone = (")
    mark_done_end = app.index("\n      };", mark_done_start)
    assert "streamState._stopping = false" in app[
        mark_done_start:mark_done_end]
    assert "this.isTabStreaming(this.currentId)" in app


def test_background_task_gap_rolls_foreground_onto_detached_successor():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    i18n = (FRONTEND / "i18n" / "index.js").read_text(encoding="utf-8")

    assert "backgroundActive: false" in app
    assert "backgroundTaskCount: 0" in app
    helper_start = app.index("    turnHasPendingBackground(")
    helper_end = app.index("\n    turnFooterTime(", helper_start)
    helper = app[helper_start:helper_end]
    assert "isLatest" in helper
    assert "pane.backgroundActive || pane.inheritedBackgroundTaskCount > 0" in helper
    assert "&& !pane.streaming" in helper
    assert "!m._failed && !m._interrupted" in helper
    assert 'this.turnFooterStatus(m, pane) === "completed"' in helper
    assert "turn_status =" not in helper
    # A pending background task keeps its card alive without extending the
    # completed turn's footer timer. A fresh foreground send transparently
    # rolls onto an isolated successor instead of waiting for that task.
    assert "st.compacting || st.backgroundActive || st._draining" in app
    assert "st.streaming || st.compacting || st.backgroundActive" in app
    assert "st._draining || (st.pendingQueue && st.pendingQueue.length)" in app
    assert "if (status.background) return true;" in app
    assert "async _handoffBackgroundSession(" in app
    assert "/continue-detached`" in app
    assert "_stateForDetachedSuccessor(" in app
    assert "_backgroundRolloverAttempted" in app
    assert "owner_session_id" in app
    # A second browser may refresh its list after the first browser hid the
    # predecessor. Its still-open tab must trust the local watcher flag and
    # probe /active instead of attempting a turn on the missing source row.
    assert "&& !(st && st.backgroundActive)" in app
    assert "d.background && d.attachable === false" in app
    assert "background_tasks_pending" in app
    poller_start = app.index("    _ensureBgContPoller(sid) {")
    poller_end = app.index("\n    _stopBgContPoller(", poller_start)
    assert "let ticksLeft = 1810;" in app[poller_start:poller_end]
    assert "_stopTimer();" in app
    assert "_continuationAwaitingReaction: false" in app
    # The turn footer must key off a REAL streaming turn. It used to also
    # suppress itself on backgroundActive, which hid the completion timestamp
    # of every turn that finished while a background task was still pending.
    assert "pane && pane.streaming && i === paneMsgs.length - 1" in html
    assert "pane.streaming || pane.backgroundActive" not in html
    assert "i === paneMsgs.length - 1" in html
    assert "t('chat.main_response_completed')" in html
    assert "t('chat.background_running_count'" in html
    assert 'class="queued-background-wait"' not in html
    assert "t('queue.waiting_background_tasks'" not in html
    # The "background task running · messages will queue" strip is gone: it
    # told the user they could not keep talking, which is no longer true.
    assert "background-task-strip" not in html
    assert "background-task-strip" not in css
    assert "isTabRunning(tid)" in html
    assert "isTabBackgroundActive(tid)" in html
    assert '"chat.background_running": "后台任务运行中"' in i18n
    assert '"chat.main_response_completed": "主回复完成"' in i18n
    hint_start = app.index("    sendButtonHint(sid) {")
    hint_end = app.index("\n    async _confirmSessionBusy", hint_start)
    assert "this.isTabBackgroundActive(sid)" not in app[hint_start:hint_end]
    assert '"queue.waiting_background_tasks": "等待 {count} 个后台任务"' in i18n
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


def test_inherited_task_poller_waits_for_durable_agent_projection():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("    _ensureInheritedTaskPoller(")
    end = app.index("\n    // Poll /active + re-subscribe", start)
    poller = app[start:end]

    assert "status.runtime_background_tasks_pending" in poller
    assert "?? status.background_tasks_pending" in poller
    assert "status.runtime_continuation_pending" in poller
    assert "status.runtime_ui_revision" in poller
    assert "st.runtimeUiRevision === desiredRevision" in poller
    assert "reported !== 0" in poller
    assert "status.active || st.streaming" not in poller
    assert "if (st.streaming || st.es || st.compacting) return;" in poller
    assert "quiet: true, probeActive: false" in poller
    assert 'st.inheritedBackgroundTaskCount = 0' in poller
    assert "if (stopped || inFlight) return;" in poller
    assert "epoch !== tickEpoch" in poller
    # Single-flight must not turn one half-open /active request into a
    # permanently wedged poller. The owning tick aborts on the normal session
    # request timeout and releases its controller in finally.
    assert "const controller = new AbortController();" in poller
    assert "Number(this._sessionListTimeoutMs) || 8000" in poller
    assert "signal: controller.signal" in poller
    assert "clearTimeout(timeout);" in poller
    assert "activeController.abort()" in poller
    # Every canonical adoption joins the existing per-session single-flight.
    # A revision-changing terminal tick performs that merge once, not once for
    # the bubble and immediately again for the completed task overlay.
    assert "this._reloadSessionCoalesced(childSid" in poller
    assert "this.loadSession(childSid" not in poller
    assert "loadedCanonicalThisTick = true" in poller
    assert "adoptedRevision && !loadedCanonicalThisTick" in poller
    # Only a newly adopted durable reply marks an off-screen successor unread.
    # The later terminal-overlay reload must not badge the tab, and the current
    # tab is already visibly consuming the bubble.
    adoption_start = poller.index("if (!adoptedRevision")
    adoption_end = poller.index("if (Number.isFinite(reported)", adoption_start)
    adoption = poller[adoption_start:adoption_end]
    assert "const continuationEventIdsBefore = new Set(" in adoption
    assert 'message.display_kind === "runtime_continuation"' in adoption
    assert "message.runtime_event_id" in adoption
    assert "const hasNewRuntimeContinuation" in adoption
    assert adoption.index("continuationEventIdsBefore = new Set") < adoption.index(
        "this._reloadSessionCoalesced(childSid")
    assert adoption.index("this._reloadSessionCoalesced(childSid") < adoption.index(
        "const hasNewRuntimeContinuation")
    assert "revisionBeforeAdoption !== desiredRevision" in adoption
    assert "&& hasNewRuntimeContinuation" in adoption
    assert "childSid !== this.currentId" in adoption
    assert "if (adoptedRevision" in adoption
    assert "st.unread = true;" in adoption
    assert poller.count("st.unread = true;") == 1
    assert poller.index("if (continuationPending) return;") < poller.index(
        'st.inheritedBackgroundTaskCount = 0')

    # task_notification is transport/card state only. A transient toast or an
    # unread dot at this point races ahead of (and duplicates) the durable Agent
    # reply; revision adoption above is the sole user-visible completion owner.
    notification_start = app.index(
        'es.addEventListener("task_notification", ev => {')
    notification_end = app.index(
        'es.addEventListener("rate_limit", ev => {', notification_start)
    notification = app[notification_start:notification_end]
    assert "applyTaskStatus(d.tool_use_id" in notification
    assert "_noteBackgroundTaskSettled" not in notification
    assert ".unread" not in notification
    assert "this.toast(" not in notification
    assert "_noteBackgroundTaskSettled" not in app

    # A reloaded successor never ran the live handoff initializer. Its normal
    # active probe must rebuild inherited ownership from durable session meta
    # and the overlay aggregate, then arm the same poller.
    check_start = app.index("    async _checkActiveTurn(sid) {")
    check_end = app.index("\n    // Hover-prefetch", check_start)
    check = app[check_start:check_end]
    assert "d.runtime_background_tasks_pending" in check
    assert "d.runtime_continuation_pending" in check
    assert "d.runtime_ui_revision" in check
    assert "sessionMeta.runtime_predecessor" in check
    assert "Math.max(0, Math.floor(inheritedPending))" in check
    assert "this._ensureInheritedTaskPoller(sid, predecessorSid)" in check


def test_runtime_ui_revision_rejects_out_of_order_session_response():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("    async loadSession(sid, opts = {}) {")
    end = app.index("\n    // Warm OPEN-but-inactive tabs", start)
    load = app[start:end]

    baseline = 'const runtimeUiRevisionAtLoad = String(st.runtimeUiRevision || "");'
    response = 'const loadedRuntimeUiRevision = String(s.runtime_ui_revision || "");'
    current = 'const currentRuntimeUiRevision = String(st.runtimeUiRevision || "");'
    stale_guard = (
        "if (currentRuntimeUiRevision !== runtimeUiRevisionAtLoad\n"
        "            && currentRuntimeUiRevision !== loadedRuntimeUiRevision)"
    )
    assert baseline in load
    assert response in load
    assert current in load
    assert stale_guard in load
    assert load.index(stale_guard) < load.index("st.messages.splice(")
    assert load.index(stale_guard) < load.index(
        "st.runtimeUiRevision = loadedRuntimeUiRevision")

    # The session-list reconciler and inherited-task poller share the same
    # coalescer, shrinking the remaining concurrency surface before the
    # revision guard handles direct legacy callers.
    reconcile_start = app.index("    _reconcileOpenSession(next) {")
    reconcile_end = app.index("\n    // Field-level equality", reconcile_start)
    reconcile = app[reconcile_start:reconcile_end]
    assert "await this._reloadSessionCoalesced(" in reconcile
    assert "await this.loadSession(sid, { quiet: true })" not in reconcile


def test_runtime_continuation_history_identity_footer_and_fork_guards():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    continuity_start = app.index("    _messageContinuitySignatures(m) {")
    continuity_end = app.index(
        "\n    _preserveCanonicalMessageIdentity", continuity_start)
    continuity = app[continuity_start:continuity_end]
    runtime_guard = continuity[continuity.index(
        'if (m.display_kind === "runtime_continuation")'):]
    runtime_guard = runtime_guard[:runtime_guard.index("const continuityIds")]
    assert 'push("runtime-event", m.runtime_event_id)' in runtime_guard
    assert 'push("text", m.text)' not in runtime_guard

    preserve_start = app.index(
        "    _preserveCanonicalMessageIdentity(st, incoming) {")
    preserve_end = app.index("\n    _assignLiveKey", preserve_start)
    preserve = app[preserve_start:preserve_end]
    assert 'canonicalTail.display_kind !== "runtime_continuation"' in preserve

    fork_start = app.index("    turnForkMessageId(paneMsgs, i) {")
    fork_end = app.index("\n    // Normalize a model-emitted path", fork_start)
    fork = app[fork_start:fork_end]
    assert 'tail.display_kind === "runtime_continuation"' in fork
    assert "tail.forkable === false" in fork
    assert 'next.display_kind !== "runtime_continuation"' in fork
    assert "paneMsgs[i + 1].display_kind === 'runtime_continuation'" in html
    # Runtime continuations have no preceding user row, but are still a new
    # assistant turn. Their stable data-message-key exempts them from the
    # adjacent non-user rule that hides avatars inside one ordinary turn.
    avatar_boundary = (
        ':not([data-message-key*="runtime-continuation:"]) .msg-avatar'
    )
    assert css.count(avatar_boundary) == 2


def test_detached_rollover_preserves_migrated_queue_fifo():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    handoff_start = app.index("    async _handoffBackgroundSession(")
    handoff_end = app.index("\n    _ensureInheritedTaskPoller(", handoff_start)
    handoff = app[handoff_start:handoff_end]
    send_start = app.index("    async send(opts = {}) {")
    send_end = app.index("\n    // ====== ask_user_question", send_start)
    send = app[send_start:send_end]

    assert "await this._syncQueueFromServer(childSid)" in handoff
    assert "Number(payload.queue_migrated) > 0" in handoff
    assert "payload.target_queue_depth" in handoff
    assert "?? payload.queue_depth" in handoff
    assert "?? payload.queue_pending" in handoff
    assert "childState.pendingQueue.length > 0" in handoff
    assert "return { sessionId: childSid, queuePending, rolledOver: true };" in handoff
    # Ordinary messages commit to the source's durable queue before the
    # expensive transcript fork. The backend migrates that accepted item in
    # FIFO order; the browser handoff is non-blocking presentation work.
    busy = send[send.index("const confirmedBusy ="):]
    busy = busy[:busy.index("// Push to the SENDING tab's messages array")]
    assert "await this._enqueueMessage(sendSid" in busy
    assert "clearSubmittedComposer();" in busy
    assert "this._scheduleBackgroundHandoff(sendSid, sendState);" in busy
    assert "await this._handoffBackgroundSession" not in busy
    assert busy.index("await this._enqueueMessage(sendSid") < busy.index(
        "this._scheduleBackgroundHandoff(sendSid, sendState);"
    )
    # Slash controls never enter the durable message queue. Their per-command
    # busy policy is owned by the shared dispatcher, while this send path has a
    # single delegation point and therefore cannot overtake migrated prompts
    # through a second handoff implementation.
    slash_start = send.index("// Slash controls must stay responsive")
    slash_end = send.index("// Keep the ownership token primitive", slash_start)
    slash = send[slash_start:slash_end]
    assert "await this._dispatchSlash(" in slash
    assert "_enqueueMessage" not in slash
    assert "_handoffBackgroundSession" not in slash
    assert "_confirmSessionBusy" not in slash


def test_composer_send_has_a_per_session_reentry_guard():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    send_start = app.index("    async send(opts = {}) {")
    send_end = app.index("\n    // ====== ask_user_question", send_start)
    send = app[send_start:send_end]

    assert "_composerSubmitting: false" in app
    assert "_composerSubmitToken: null" in app
    assert "if (sendState._composerSubmitToken" in send
    assert "sendState._composerSubmitting = true;" in send
    assert "} finally {" in send
    assert "owner._composerSubmitting = false;" in send
    assert send.index("sendState._composerSubmitting = true;") < send.index(
        "await this._awaitRuntimeSettingPatches(sendSid, sendState)"
    )
    assert "if (ev.repeat) return;" in app
    assert "tabState[currentId]?._composerSubmitting" in html
    assert ':aria-busy="!!tabState[currentId]?._composerSubmitting"' in html
    enqueue_start = app.index("    async _enqueueMessage(sid, item) {")
    enqueue_end = app.index("\n    // Post-turn / on-activate hook", enqueue_start)
    enqueue = app[enqueue_start:enqueue_end]
    assert "accepted = await r.json()" in enqueue
    assert "this._syncQueueFromServer(sid);" in enqueue
    assert "await this._syncQueueFromServer(sid);" not in enqueue

    # A done event with detached work rolls over proactively, and an in-flight
    # queue POST transfers its primitive composer claim to that successor.
    assert "if (backgroundPending > 0 && !isContinuation && !d.cancelled)" in send
    assert "this._scheduleBackgroundHandoff(streamSid, streamState);" in send
    assert "_backgroundSuccessorSid: \"\"" in app
    assert "child._composerSubmitToken = sourceState._composerSubmitToken || null;" in app
    assert "const successorState = () =>" in send
    assert "successorState()," in send


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
    assert "const composerImages = hasDetachedText ? [] : sendDraft.pendingImages.slice()" in send
    assert "const composerDocs = hasDetachedText ? [] : sendDraft.pendingDocs.slice()" in send
    assert "const clearSubmittedComposer = ({ preserveForHandshake = false } = {}) =>" in send
    assert "removeOwned(ownerDraft.pendingImages" in send
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
    assert "this._stageChatRecoveryDraft(ownerSid, composerInput)" in send
    assert "clearSubmittedComposer({ preserveForHandshake: true })" in send
    assert "this._commitChatRecoveryDraft(sendSid, composerInput)" in send
    assert "const rollbackUnstartedSend = (restoreDraft = true) =>" in send
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

    assert "draft.input = displayText" in queue_edit
    assert "draft.pendingQuotes.splice" in queue_edit
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
    assert "const surfaceTerminalError = detail =>" in send
    assert "surfaceTerminalError(_detail)" in send
    assert "surfaceTerminalError(serverError)" in send
    assert "closeAsst();" in send[send.index("const surfaceTerminalError = detail =>"):]
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
    # A continuation can emit its task-complete toast and then race out of the
    # grace-kept /active slot. Both terminal fallbacks reconcile an already-
    # rendered pane and must never take the cold-load skeleton/clear path.
    no_active_start = send.index('if (serverError === "no active turn")')
    no_active_end = send.index("// ---- Transport-level", no_active_start)
    assert "this.loadSession(streamSid, { quiet: true })" in send[
        no_active_start:no_active_end]
    transport_finished_start = send.index("if (!d.active)", no_active_end)
    transport_finished_end = send.index(
        "if (d.background && d.attachable === false)", transport_finished_start)
    assert "this.loadSession(streamSid, { quiet: true })" in send[
        transport_finished_start:transport_finished_end]


def test_fork_banner_and_message_template_are_null_and_key_safe():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert "currentForkSource()?.name || ''" in html
    assert 'x-for="(m, i) in paneMsgs" :key="m._k"' in html


def test_history_keys_prefer_backend_block_identity_without_local_dup_suffixes():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    key_start = app.index("    _historyMessageKey(sid, m) {")
    key_end = app.index("    _messageContinuitySignatures(m) {", key_start)
    history_keys = app[key_start:key_end]

    assert "if (m && m.block_id) return sid + \":block:\" + m.block_id;" in history_keys
    assert '":dup:"' not in history_keys
    assert "const seen = new Map()" not in history_keys


def test_history_store_normalizes_canonical_blocks_and_prunes_session_windows():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("    _historyStoreKey(sid, m) {")
    end = app.index("    _messageContinuitySignatures(m) {", start)
    store = app[start:end]
    dispose_start = app.index("    _disposeTabRuntime(id) {")
    dispose_end = app.index("    _startWorkspaceDrag", dispose_start)
    dispose = app[dispose_start:dispose_end]
    cap_start = app.index("    _capHistoryCache(st, direction = \"newer\") {")
    cap_end = app.index("    _captureMessageAnchor", cap_start)
    cap = app[cap_start:cap_end]

    assert "_messagesById: new Map()" in app
    assert "_sessionWindows: new Map()" in app
    assert "sessionKeys.add(storeKey)" in store
    assert "this._messagesById.get(storeKey)" in store
    assert "this._messagesById.set(storeKey, created)" in store
    assert "Object.assign(existing, m" in store
    assert 'existing.body_state === "loaded"' in store
    assert 'm.body_state === "unloaded"' in store
    assert "this._sessionWindows.set(sid, retained)" in store
    assert "this._messagesById.delete(storeKey)" in store
    assert "this._dropSessionMessageStore(id)" in dispose
    assert "this._syncSessionMessageStore(st)" in cap


def test_large_history_bodies_load_by_stable_block_reference():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    loader_start = app.index("    async _loadMessageBody(m) {")
    loader_end = app.index("    async toggleMsgExpanded", loader_start)
    loader = app[loader_start:loader_end]
    toggle_start = loader_end
    toggle_end = app.index("    // Rendered markdown", toggle_start)
    toggle = app[toggle_start:toggle_end]

    assert 'm.body_state !== "unloaded"' in loader
    assert '"/blocks/" + encodeURIComponent(m.body_ref)' in loader
    assert 'Object.assign(m, loaded' in loader
    assert 'body_state: "loaded"' in loader
    assert "await this._loadMessageBody(m)" in toggle
    assert "m.body_available && m.body_state !== 'loaded'" in html
    assert "加载完整正文" in html


def test_render_key_hot_paths_use_pane_index_without_full_scans_or_transport_duplication():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    append_start = app.index("    _appendLiveMessage(st, m) {")
    append_end = app.index("\n    // Keep the phone DOM", append_start)
    append_body = app[append_start:append_end]
    assert "_assignLiveKey(st, m)" in append_body
    assert "_allPaneMessages" not in append_body
    assert "_rebuildPaneMessageRenderKeys" not in append_body

    activate_start = app.index("    _activateTabState(id) {")
    activate_end = app.index("\n    // P1 (chat-perf-redesign)", activate_start)
    activate_body = app[activate_start:activate_end]
    assert "_ensurePaneMessageRenderKeys(id)" in activate_body
    assert "_allPaneMessages" not in activate_body

    capture_start = app.index("(function installErrorCapture() {")
    capture_end = app.index("\n})();", capture_start)
    capture = app[capture_start:capture_end]
    assert capture.count('navigator.sendBeacon("/api/log/client-error"') == 1
    assert capture.count('fetch("/api/log/client-error"') == 1
    assert '_deliverClientErrorRecord(rec, telemetry, "[muse-telemetry]", "warn")' in capture
    assert "const wireRecord = _clientErrorWireRecord(rec);" in capture
    wire_helper = capture[
        capture.index("function _clientErrorWireRecord"):
        capture.index("function _deliverClientErrorRecord")
    ]
    for private_field in ("rec.message", "rec.stack", "rec.filename", "rec.url", "rec.ua"):
        assert private_field not in wire_helper


def test_render_key_regression_boundaries_keep_state_and_owners_consistent():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    older_start = app.index("async _fetchOlderWindow(sid)")
    older_end = app.index("async _fetchLaterWindow(sid)", older_start)
    older = app[older_start:older_end]
    assert older.index("const data = await r.json()") < older.index(
        "if (this.tabState[sid] !== st) return 0")
    assert older.index("if (this.tabState[sid] !== st) return 0") < older.index(
        "this._historyEnvelopes")
    assert older.index("if (this.tabState[sid] !== st) return 0") < older.index(
        "this._ensurePaneMessageRenderKeys")

    earlier_start = app.index("async loadEarlierMessages(sid)")
    earlier_end = app.index("async loadLaterMessages(sid)", earlier_start)
    earlier = app[earlier_start:earlier_end]
    assert "await this._fetchOlderWindow(sid);\n        if (this.tabState[sid] !== st) return;" in earlier
    assert "requestAnimationFrame(() => r()) : setTimeout(r, 0)));\n          if (this.tabState[sid] !== st) return;" in earlier
    assert earlier.index("st.messages.unshift(...batch)") < earlier.index(
        "this._ensurePaneMessageRenderKeys(sid)")
    assert earlier.index("this._ensurePaneMessageRenderKeys(sid)") < earlier.index(
        'this._capMountedWindow(st, "older")')

    rebuild_start = app.index("_rebuildPaneMessageRenderKeys(tid, messages = null)")
    rebuild_end = app.index("_ensurePaneMessageRenderKeys(tid)", rebuild_start)
    rebuild = app[rebuild_start:rebuild_end]
    assert "source.splice(i--, 1)" in rebuild
    assert 'new Map([["duplicate", duplicateOccurrences]])' in rebuild
    assert "occurrenceIssues" in rebuild

    send_start = app.index("async send(opts = {})")
    send_end = app.index("async stop()", send_start)
    send = app[send_start:send_end]
    reconnect = send[send.index("} else if (!isContinuation) {"):
                     send.index("// (isContinuation:")]
    assert "const removed = sendState.messages.splice(lastUserIdx + 1)" in reconnect
    assert "this._releasePaneMessageRenderKeys(sendState" in reconnect

    retry_start = app.index("retryFailedMessage(m)")
    retry_end = app.index("onUserBubbleClick", retry_start)
    assert "this._removePaneMessage(st, m)" in app[retry_start:retry_end]
    edit_start = app.index("commitEditMessage(m)")
    edit_end = app.index("_humanizeStreamError", edit_start)
    assert "this._truncatePaneMessagesFrom(st, m)" in app[edit_start:edit_end]
    truncate_start = app.index("_truncatePaneMessagesFrom(st, m)")
    truncate_end = app.index("_appendLiveMessage(st, m)", truncate_start)
    truncate = app[truncate_start:truncate_end]
    assert "st._hasServerLater = false" in truncate
    assert "this._releasePaneMessageRenderKeys(st" in truncate


def test_render_key_owned_arrays_mutate_only_at_audited_boundaries():
    """New pane-array mutations must explicitly join the render-key audit."""
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    method = "<top-level>"
    mutation_methods = set()
    method_re = re.compile(
        r"^    (?:async )?([A-Za-z_$][\w$]*)\([^)]*\) \{$"
    )
    mutation_re = re.compile(
        r"\b(?:st|sendState|ownerState|newSt|child)\."
        r"(?:messages|_earlierMessages|_laterMessages)"
        r"(?:\.(?:push|unshift|splice|pop|shift)\s*\(|\.length\s*=|\s*=)"
    )
    for line in app.splitlines():
        declaration = method_re.match(line)
        if declaration:
            method = declaration.group(1)
        if mutation_re.search(line):
            mutation_methods.add(method)

    assert mutation_methods == {
        "_capHistoryCache",
        "_capLiveMessages",
        "_capMountedWindow",
        "_ensureTabState",
        "_fetchLaterWindow",
        "_fetchOlderWindow",
        "_loadAroundMessage",
        "_reloadHistoryTailAfterConflict",
        "_revealMessagesChunked",
        "_scrollToUserMsg",
        "_stateForDetachedSuccessor",
        "loadEarlierMessages",
        "loadLaterMessages",
        "loadSession",
        "newSession",
        "onModelChange",
        "returnToLatest",
        "send",
    }


def test_render_key_telemetry_has_one_disposable_trailing_flush():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    report_start = app.index("_flushPaneRenderKeyTelemetry(tid, st)")
    report_end = app.index("_nextPaneRenderRepairKey", report_start)
    report = app[report_start:report_end]
    dispose_start = app.index("_disposeTabRuntime(id)")
    dispose_end = app.index("async removeWorkspace", dispose_start)
    dispose = app[dispose_start:dispose_end]

    assert "flushTimer: null" in app
    assert "|| telemetry.flushTimer) return" in report
    assert "telemetry.flushTimer = setTimeout(() =>" in report
    assert "if (this.tabState[tid] !== st) return" in report
    assert "this._flushPaneRenderKeyTelemetry(tid, st)" in report
    assert "clearTimeout(st._renderKeyTelemetry.flushTimer)" in dispose
    assert "st._renderKeyTelemetry.flushTimer = null" in dispose


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
    assert "streamSid, streamState, d.is_error ? \"\" : completedFinalText" in done
    assert "String(d.assistant_uuid || \"\")" in done
    assert "assistantUuid: d.assistant_uuid" in done
    assert "completedAtMs: d.completed_at_ms" in done
    assert "durationMs: d.duration_ms" in done

    reconcile_start = app.index("    _reconcileCompletedTurn(\n      sid, ownerState")
    reconcile_end = app.index(
        "\n    _reconcileCompletedContinuation", reconcile_start)
    reconcile = app[reconcile_start:reconcile_end]
    assert '"/api/chat/sessions/" + sid + "/active"' in reconcile
    assert "activity.active && !activity.background" in reconcile
    assert '"/api/chat/sessions/" + sid + "?tail=80"' in reconcile
    assert "m && m.role !== \"user\" && m.uuid" in reconcile
    assert "m.role === \"assistant\" && m.uuid" in reconcile
    assert "m && m.uuid === expectedAssistantUuid" in reconcile
    assert "const loaded = await this.loadSession(sid, { quiet: true })" in reconcile
    assert "attempt < 30" in reconcile
    assert "Math.min(2000, 250 + attempt * 100)" in reconcile

    continuity_start = app.index("_messageContinuitySignatures(m)")
    continuity_end = app.index(
        "\n    _preserveCanonicalMessageIdentity", continuity_start)
    continuity = app[continuity_start:continuity_end]
    assert 'if (role === "assistant") continuityIds.push(m.forkUuid)' in continuity
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


def test_any_result_rendered_in_current_visible_turn_is_auto_acknowledged():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    helper_start = app.index("_ackViewedActivity(")
    helper_end = app.index("\n    ackCurrentActivity", helper_start)
    helper = app[helper_start:helper_end]
    send_start = app.index("async send(opts = {})")
    send = app[send_start:app.index("async stop()", send_start)]
    done_start = send.index('es.addEventListener("done"')
    error_start = send.index('es.addEventListener("error"')
    done = send[done_start:error_start]
    error = send[
        error_start:send.index('es.addEventListener("cancelled"', error_start)
    ]

    assert 'document.visibilityState !== "visible"' in helper
    assert "this.currentId === sid" in helper
    assert "this.tabState[this.currentId] === streamState" in helper
    assert "this._ackViewedActivity(" in done
    assert "this._ackViewedActivity(" in error
    assert "isContinuation" in send
    assert "backgroundPending" in send


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
    retry_start = app.index("\n    retryFailedMessage(m) {")
    retry = app[retry_start:app.index("\n    onUserBubbleClick", retry_start)]
    edit_start = app.index("\n    commitEditMessage(m) {")
    edit = app[edit_start:app.index("\n    _humanizeStreamError", edit_start)]

    assert retry.index("if (this.workspaceSwitching) return") < retry.index(
        "this._removePaneMessage")
    assert edit.index("this.workspaceSwitching") < edit.index(
        "this._truncatePaneMessagesFrom")


def test_queue_sync_keeps_older_success_when_newer_read_fails():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("async _syncQueueFromServer(sid)")
    sync = app[start:app.index("_currentQueueLen", start)]

    assert "_queueAppliedSeq: 0" in app
    assert "_queueRevision: 0" in app
    assert "seq < st._queueAppliedSeq" in sync
    assert "revision < (Number(st._queueRevision) || 0)" in sync
    assert 'cache: "no-store"' in sync
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
    backgrounds = {
        "light": ["#ffffff"],
        # All three curated eyecare surface levels share this ANSI palette.
        "eyecare": ["#fbf9f1", "#f5f0e0", "#faedce"],
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

    for theme, theme_backgrounds in backgrounds.items():
        for background in theme_backgrounds:
            failures = {
                name: round(contrast(color, background), 2)
                for name, color in palettes[theme].items()
                if contrast(color, background) < 4.5
            }
            assert not failures, (
                f"{theme}@{background} ANSI colors below 4.5:1: {failures}"
            )

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


def test_eyecare_intensity_is_curated_persisted_and_content_safe():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    i18n = (FRONTEND / "i18n" / "index.js").read_text(encoding="utf-8")

    assert "eyecareLevel: 2" in app
    assert 'localStorage.getItem("muselab_eyecare_level")' in app
    assert "Number.isInteger(savedEyecareLevel)" in app
    assert 'this._setLS("muselab_eyecare_level", String(next))' in app
    assert "!Number.isInteger(next) || next < 1 || next > 3" in app
    assert 'setAttribute("data-eyecare-level", String(this.eyecareLevel))' in app
    assert 'getPropertyValue("--c-bg-0")' in app
    assert "setEyecareLevel(level)" in app
    assert 'const order = ["light", "dark", "eyecare"]' in app
    assert "if (this._terminal) this._terminal.options.theme = this._terminalTheme()" in app

    row_start = html.index('class="settings-row eyecare-level-row"')
    row_end = html.index('<div class="settings-row">', row_start)
    row = html[row_start:row_end]
    assert 'x-show="theme === \'eyecare\'"' in row
    assert 'role="group"' in row
    assert row.count(':aria-pressed="eyecareLevel===') == 3
    for level in (1, 2, 3):
        assert f'@click="setEyecareLevel({level})"' in row

    for key in (
        "set.label.eyecare_level", "set.eyecare.soft",
        "set.eyecare.balanced", "set.eyecare.deep", "set.eyecare.hint",
    ):
        assert i18n.count(f'"{key}"') == 2

    expected = {
        1: {
            "--c-bg-0": "#fbf9f1", "--c-bg-1": "#f8f5ec",
            "--c-bg-2": "#f3efe6", "--c-assistant-bg": "#f8f5ec",
            "--c-user-bg": "#e8f0e3", "--c-tool-bg": "#f6f0e2",
            "--c-thinking-bg": "#f1f1e8",
        },
        3: {
            "--c-bg-0": "#faedce", "--c-bg-1": "#f5e9cc",
            "--c-bg-2": "#f1e4c7", "--c-assistant-bg": "#f5e9cc",
            "--c-user-bg": "#e0ebcf", "--c-tool-bg": "#f5e5c4",
            "--c-thinking-bg": "#ede4ca",
        },
    }
    blocks = {}
    for level, tokens in expected.items():
        match = re.search(
            rf'html\[data-theme="eyecare"\]\[data-eyecare-level="{level}"\] '
            r"\{(.*?)\n\}",
            css,
            re.S,
        )
        assert match, f"missing eyecare level {level}"
        blocks[level] = match.group(1)
        assert "filter:" not in blocks[level]
        for token, value in tokens.items():
            assert f"{token}: {value}" in blocks[level]
    assert 'data-eyecare-level="2"' not in css

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
            (luminance(first), luminance(second)), reverse=True,
        )
        return (lighter + 0.05) / (darker + 0.05)

    # Endpoint backgrounds keep body/muted text readable; level 2 is already
    # covered by the long-standing eyecare palette tests above.
    for colors in expected.values():
        assert contrast("#3d3526", colors["--c-bg-0"]) >= 4.5
        assert contrast("#5e5447", colors["--c-bg-1"]) >= 4.5
        assert contrast("#6b6050", colors["--c-bg-2"]) >= 4.5
        assert contrast("#2f6a2f", colors["--c-bg-2"]) >= 4.5
        assert contrast("#92500a", colors["--c-bg-2"]) >= 4.5
        assert contrast("#3a5a30", colors["--c-user-bg"]) >= 4.5
        assert contrast("#6f5226", colors["--c-tool-bg"]) >= 4.5
        assert contrast("#5a6a4a", colors["--c-thinking-bg"]) >= 4.5


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
    assert "_fileEventsReconnectFailures: 0" in app
    assert "_nextFileEventsReconnectDelay()" in app
    assert "500 * Math.pow(2, this._fileEventsReconnectFailures - 1)" in app
    assert "this._fileEventsReconnectFailures = 0" in app
    assert "this._nextFileEventsReconnectDelay()" in app
    assert "1500" not in app[
        app.index("async _startFileEvents()"):
        app.index("\n    _queueFileChanges", app.index("async _startFileEvents()"))
    ]
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
    assert "return false" in fallback
    assert '`/api/files/bootstrap${query ? `?${query}` : ""}`' not in sync
    assert "legacy full-snapshot GET" in sync
    assert "Array.isArray(payload.truncated_parents)" in sync
    assert "payload.children_per_parent_limit" in sync
    assert "snapshotHasTruncatedParents" in sync
    assert "this._workspaceTreeCursors.delete(ownerWorkspace)" in sync
    truncated_cursor = sync.index("if (snapshotHasTruncatedParents)")
    delta_cursor = sync.index("} else if (nextCursor != null)", truncated_cursor)
    assert truncated_cursor < delta_cursor
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
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    toolbar_start = html.index('class="btn-primary chat-toolbar-send chat-toolbar-queue"')
    toolbar_end = html.index("</button>", html.index(
        'class="btn-danger chat-toolbar-send chat-toolbar-stop"', toolbar_start,
    )) + len("</button>")
    buttons = html[toolbar_start:toolbar_end]
    assert 'x-text="t(\'btn.send\')"' not in buttons
    assert 'x-text="t(\'btn.stop\')"' not in buttons
    assert "tabState[currentId]?._stopping" in buttons
    assert buttons.count("sendButtonHint(currentId)") == 2
    send_hint_start = app.index("    sendButtonHint(sid) {")
    send_hint_end = app.index("\n    async _confirmSessionBusy", send_hint_start)
    send_hint = app[send_hint_start:send_hint_end]
    assert "this.isTabBackgroundActive(sid)" not in send_hint
    assert 'this.t("chat.background_queue_hint")' not in send_hint
    assert 'this.t("queue.button_hint")' in send_hint
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
    bounded subagent capacity. Rendered as a visible <span> label it wraps
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
    assert "Ultra uses maximum reasoning" in i18n


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


def test_turn_footer_falls_back_to_transcript_time_and_shows_model_and_state():
    """Historic/tool-ending turns must not render as an empty separator."""
    chat = (BACKEND / "chat.py").read_text(encoding="utf-8")
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    footer = html[html.index('<div class="turn-footer"'):]
    footer = footer[:footer.index('<button class="turn-fork-btn"')]
    assert "turnFooterTime(m, pane)" in footer
    assert "turnFooterElapsed(m, pane)" in footer
    assert "turnFooterStatus(m, pane)" in footer
    assert 'class="turn-model"' in footer
    assert 'class="turn-status"' in footer
    assert "modelLabel(turnFooterModel(m, pane, tid))" in footer
    assert 'entry["model"] = model_name' in chat
    assert 'entry["turn_status"] = turn_status' in chat
    assert "turn_status=_activity_status" in chat
    assert "def _complete_turn_footer_metadata(" in chat
    assert 'tail["turn_status"] = status' in chat

    mark_start = app.index("const _markDone = (")
    mark_end = app.index("\n      const markUserFailed", mark_start)
    mark = app[mark_start:mark_end]
    assert "if (!m.model && completedModel) m.model = completedModel" in mark
    assert 'm.turn_status === "running"' in mark
    assert "m.turn_status = turnStatus" in mark
    assert "if (!m.memoryRecall && memoryRecall)" in mark
    assert "if (!tailCandidate && lastUserCandidate && turnStatus)" in mark
    assert "m.role !== 'user' || m.turn_status || m._interrupted || m._failed" in footer


def test_queue_controls_validate_mutations_and_block_send_during_interrupt():
    """A failed DELETE must not become an editable duplicate."""
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    chat = (BACKEND / "chat.py").read_text(encoding="utf-8")

    helper_start = app.index("async _runQueueMutation(")
    helper_end = app.index("\n    async _enqueueMessage", helper_start)
    helper = app[helper_start:helper_end]
    assert "if (!r.ok) throw" in helper
    assert "queueActionBusy(sid, key)" in helper

    edit_start = app.index("async editPendingQueueItem(")
    edit_end = app.index("\n    async resumeQueueDrain", edit_start)
    edit = app[edit_start:edit_end]
    assert "if (!r) return;" in edit
    assert edit.index("if (!r) return;") < edit.index("draft.input = displayText")

    send_start = app.index("async send(opts = {})")
    send_end = app.index("\n    // ====== ask_user_question", send_start)
    send = app[send_start:send_end]
    assert "if (sendState._stopping && !opts.reconnect && !opts.resumedItem)" in send
    assert "tabState[currentId]?._stopping" in html
    assert "queueActionBusy(currentId, 'edit:' + q.id)" in html
    assert "queueActionBusy(currentId, 'remove:' + q.id)" in html
    assert "sess.pause_queue_if_nonempty(session_id)" in chat


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


def test_midturn_reconnect_storm_guards_are_in_place():
    """Guard the 2026-08-04 mid-turn flicker fix.

    Measured symptom: ~60 full SSE teardown+replay cycles in 20-30 s while a
    turn was running (60 POST /stream/start, 63 ?tail=300 quiet reloads, 382
    /active probes). No transport error was involved — the driver was a closed
    loop: _reconcileOpenSession saw `active:true` in the session list, quiet-
    reloaded the transcript, loadSession's tail probed /active, that reconnected
    and replayed the whole turn, `done` refreshed the list, repeat. Each of the
    asserts below removes one edge of that loop; losing any one re-opens it.
    """
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")

    # 1. A live session-list row alone must not trigger a transcript re-read.
    #    `cur.active` stays true for the whole life of a turn AND of any
    #    background task, so it cannot mean "there is new content".
    reconcile = js[js.index("    _reconcileOpenSession(next) {"):]
    reconcile = reconcile[:reconcile.index("\n    _sessionsEqual(")]
    assert "const backgroundOnly = !!cur.background_active && !cur.turn_active;" in reconcile
    assert "const needsRefresh = st._pendingExternalUpdate || visibleNewer;" in reconcile
    assert "messageCountChanged || turnCountChanged" in reconcile
    assert "!!cur.active || st._pendingExternalUpdate" not in reconcile
    # Attaching to a server-side turn is a separate, pane-preserving path.
    assert "hasTurnActivityFlag ? !!cur.turn_active" in reconcile
    assert "!!cur.active && !cur.background_active" in reconcile
    assert "if (wantsAttach && st._loaded) this._checkActiveTurn(sid);" in reconcile
    # 2. A HEALTHY transport is never retired on one stale `active:false` tick.
    assert "const transportDead = !st.es" in reconcile
    assert "if (transportDead) this._retireStaleSessionStream(sid, st);" in reconcile

    # 3. Quiet reconciliation loads must not re-probe /active (that probe is
    #    what turned every poll-driven reload into a full-turn replay).
    load = js[js.index("    async loadSession(sid, opts = {}) {"):]
    assert "const probeActive = opts.probeActive !== undefined" in load
    assert "if (probeActive) this._checkActiveTurn(sid);" in load

    # 4. Every reconnect source goes through one shared rate brake.
    assert "_allowReconnect(sid, turnId) {" in js
    gate = js[js.index("    _allowReconnect(sid, turnId) {"):]
    gate = gate[:gate.index("\n    _reconcileOpenSession(")]
    assert "if (last && now - last < MIN_GAP_MS) return false;" in gate
    assert ">= BURST_MAX" in gate
    # Refusal falls back to the flicker-free path: wait out the turn, then
    # quiet-load canonical history.
    assert "this._scheduleCanonicalStreamReload(sid, st);" in gate
    check = js[js.index("    async _checkActiveTurn(sid) {"):]
    check = check[:check.index("\n    // Hover-prefetch")]
    assert "if (!this._allowReconnect(sid, d.turn_id)) return;" in check
    recover = js[js.index("    async _recoverStalledStream(sid = this.currentId) {"):]
    recover = recover[:recover.index("\n    _scheduleCanonicalStreamReload(")]
    assert "if (!this._allowReconnect(sid, d.turn_id || st.activeTurnId)) return false;" in recover

    # 5. The MAX_ATTEMPTS ceiling must stay reachable: a fresh turn is the only
    #    place the counter resets. Every reconnect opens its EventSource
    #    successfully, so resetting in es.onopen (or on retire) made the cap
    #    unreachable and let the loop run forever.
    assert js.count("_reconnectAttempts = 0") == 1
    fresh = js[js.index("        streamState._sessionActivityExpected = null;"):]
    fresh = fresh[:fresh.index("streamState.streaming = true;")]
    assert "streamState._reconnectAttempts = 0;" in fresh
    onopen = js[js.index("      es.onopen = () => {"):]
    onopen = onopen[:onopen.index("      };")]
    assert "_reconnectAttempts" not in onopen

    # 6. Turn completion refreshes the list quietly instead of via
    #    refreshSessions(), which also drives _recoverStalledStream — i.e. it
    #    wired a second reconnect probe into the turn-completion path.
    done = js[js.index("        streamState._seenUpdated = undefined;"):]
    done = done[:done.index("        if (this.currentId === streamSid) {")]
    assert "this._syncSessionListQuiet();" in done
    assert "this.refreshSessions();" not in done

    # 7. The catch-up retry is bounded. An unbounded 250 ms self-retry is a hot
    #    loop whenever the transcript never reaches the list's target revision,
    #    and every round costs a full ?tail= reload of the visible pane.
    assert "st._reconcileRetryN = retries + 1;" in reconcile
    assert "&& retries < 6" in reconcile
    assert "Math.min(2000, 250 * (retries + 1))" in reconcile


def test_turn_busy_race_falls_back_to_durable_queue():
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    send = js[js.index("    async send(opts = {}) {"):]
    busy = send[send.index('if (serverError && errKind === "turn_busy"'):]
    busy = busy[:busy.index("\n\n        if (serverError) {")]

    assert "await this._enqueueMessage(streamSid" in busy
    assert "!isReconnect && !resumed" in busy
    assert "rollbackUnstartedSend(false);" in busy
    assert "restoreSubmittedComposer" not in busy
    assert busy.index("es.close()") < busy.index(
        "await this._enqueueMessage(streamSid"
    )
    assert "streamState._busyQueueHandoff === es" in busy
    assert "if (streamState.es === es) rollbackUnstartedSend(false);" in busy
    assert busy.index("streamState._busyQueueHandoff = es;") < busy.index(
        "await this._enqueueMessage(streamSid"
    )
    assert "this._removePaneMessage(streamState, sentUserBubble);" in busy


def test_slash_registry_has_core_commands_aliases_and_busy_policies():
    constants = (FRONTEND / "data" / "constants.js").read_text(encoding="utf-8")
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    start = constants.index("window.MUSELAB_SLASH_CMDS = [")
    registry = constants[start:constants.index("\n];", start)]

    # The first implementation is retained for a later UX redesign, but its
    # entry point is intentionally closed: slash-prefixed text is ordinary chat.
    assert "window.MUSELAB_SLASH_ENABLED = false" in constants
    assert "SLASH_ENABLED: window.MUSELAB_SLASH_ENABLED === true" in app
    assert 'x-show="SLASH_ENABLED && slashShow"' in html
    assert 'if (this.SLASH_ENABLED && text.startsWith("/"))' in app
    assert "if (this.SLASH_ENABLED && isComposerSubmission)" in app

    # Do not freeze the total command count: local conveniences may grow. These
    # eight names are the stable product contract, and aliases resolve through
    # the same records instead of becoming duplicate command implementations.
    for name in (
        "context", "compact", "model", "permission",
        "mcp", "stop", "usage", "effort",
    ):
        assert re.search(rf'\bname:\s*"{name}"', registry)
    assert 'name: "permission", aliases: ["permissions"]' in registry
    assert 'name: "usage", aliases: ["cost"]' in registry

    for name in ("context", "mcp", "usage"):
        assert re.search(
            rf'name:\s*"{name}"[^\n]*policy:\s*"readonly"', registry,
        )
    assert re.search(r'name:\s*"stop"[^\n]*policy:\s*"immediate"', registry)
    for name in ("compact", "model", "permission", "effort"):
        assert re.search(
            rf'name:\s*"{name}"[^\n]*policy:\s*"stateful"', registry,
        )


def test_slash_palette_supports_aliases_and_second_stage_arguments():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    resolve = app[app.index("    _resolveSlashCommand(rawName) {"):]
    resolve = resolve[:resolve.index("\n    _slashCommandResults(")]
    assert "command.name === name" in resolve
    assert "(command.aliases || []).includes(name)" in resolve

    arguments = app[app.index("    _slashArgumentResults(command, rawQuery) {"):]
    arguments = arguments[:arguments.index("\n    _refreshSlashPalette(")]
    assert "this.availableModels" in arguments
    assert 'command.argKind === "permission"' in arguments
    assert "this.effortChoices(this.model)" in arguments
    assert 'command.argKind === "session"' in arguments
    assert "rows.filter(item => item._search.includes(query))" in arguments

    refresh = app[app.index("    _refreshSlashPalette(prefix = this.input) {"):]
    refresh = refresh[:refresh.index("\n    _setSlashComposerValue(")]
    assert "this._slashCommandResults(raw)" in refresh
    assert "this._slashArgumentResults(command" in refresh
    # A miss keeps the popup shell mounted so its no-result row is reachable.
    assert "this.slashShow = true" in refresh
    assert ':key="c._key"' in html
    assert "slashStage === 'argument' ? t('slash.no_arg')" in html


def test_slash_enter_tab_pointer_and_send_share_one_dispatcher():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    pick = app[app.index("    pickSlash(i = this.slashIdx) {"):]
    pick = pick[:pick.index("\n    onSlashTab(")]
    # Choosing a command with arguments opens stage two; choosing an argument
    # and choosing an argument-free command both enter the same dispatcher.
    assert "if (command.argKind)" in pick
    assert "this._setSlashComposerValue(`/${command.name} `, true)" in pick
    assert pick.count("this._dispatchSlash(") == 2

    tab = app[app.index("    onSlashTab(ev) {"):]
    tab = tab[:tab.index("\n    _slashDraftHasAttachments(")]
    assert "this._isImeComposingEvent(ev)" in tab
    assert tab.index("this._isImeComposingEvent(ev)") < tab.index("ev.preventDefault()")
    assert "this.pickSlash()" in tab

    enter = app[app.index("    onEnter(ev) {"):]
    enter = enter[:enter.index("\n    _captureChatPosition(")]
    assert enter.index("this._claimNonImeEnter(ev)") < enter.index("this.slashShow")
    # Slash selection wins before the mobile newline policy, while a composing
    # Enter still exits through _claimNonImeEnter without touching the palette.
    assert enter.index("this.pickSlash()") < enter.index("this._isMobileLayout()")

    send = app[app.index("    async send(opts = {}) {"):]
    slash_start = send.index("// Slash controls must stay responsive")
    slash_end = send.index("// Keep the ownership token primitive", slash_start)
    slash = send[slash_start:slash_end]
    assert "await this._dispatchSlash(" in slash
    assert "_runSlash(" not in slash
    assert "_runSlashHandler(" not in slash

    assert '@keydown.tab="onSlashTab($event)"' in html
    assert '@keydown.tab.prevent=' not in html
    assert '@mousedown.prevent="pickSlash(i)"' in html


def test_slash_dispatcher_centralizes_busy_and_attachment_policy():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("    async _dispatchSlash(rawCommand, rawArg, opts = {}) {")
    dispatcher = app[start:app.index("\n    async _runSlashHandler(", start)]

    assert "const command = this._resolveSlashCommand(rawCommand)" in dispatcher
    assert 'if (command.policy === "stateful")' in dispatcher
    assert "await this._confirmSessionBusy(sid, st)" in dispatcher
    # Read-only and immediate commands skip the stateful-only branch; all
    # commands still share re-entry, error, and exact-draft clearing rules.
    assert dispatcher.count("_confirmSessionBusy(") == 1
    assert "await this._runSlashHandler(" in dispatcher
    assert "if (this._slashDispatching && ownsDispatchLock) return false" in dispatcher

    attach = dispatcher.index("this._slashDraftHasAttachments(sid)")
    busy = dispatcher.index('command.policy === "stateful"')
    run = dispatcher.index("await this._runSlashHandler(")
    clear = dispatcher.index('ownerState.draft.input = ""')
    assert attach < busy < run < clear
    assert "ownerState.draft.input === submitted" in dispatcher
    assert "this.currentId === sid && this.input === submitted" in dispatcher
    assert 'this._setChatInput("")' in dispatcher
    attachment_helper = app[app.index("    _slashDraftHasAttachments("):start]
    for field in ("pendingImages", "pendingDocs", "pendingQuotes"):
        assert field in attachment_helper


def test_slash_handlers_use_existing_safe_control_flows():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = app.index("    async _runSlashHandler(cmd, arg) {")
    handler = app[start:app.index("\n    // Compatibility entry point", start)]

    context = handler[handler.index('case "context"'):handler.index('case "compact"')]
    compact = handler[handler.index('case "compact"'):handler.index('case "model"')]
    model = handler[handler.index('case "model"'):handler.index('case "permission"')]
    permission = handler[
        handler.index('case "permission"'):handler.index('case "mcp"')
    ]
    mcp = handler[handler.index('case "mcp"'):handler.index('case "stop"')]
    stop = handler[handler.index('case "stop"'):handler.index('case "usage"')]
    usage = handler[handler.index('case "usage"'):handler.index('case "effort"')]
    effort = handler[handler.index('case "effort"'):handler.index('case "help"')]

    assert "await this.showCtxBreakdown()" in context
    assert "await this.runCompact()" in compact
    assert "/sessions/${this.currentId}/compact" not in compact
    assert "this.availableModels" in model
    assert "await this.onModelChange()" in model
    assert "this._normalizePermissionMode(value)" in permission
    assert "await this.onPermissionChange()" in permission
    assert "this.toggleMcpDrawer()" in mcp and "await this.fetchMcp()" in mcp
    assert "await this.stop()" in stop
    assert "await this.fetchStats()" in usage
    assert "this._normalizeEffort(arg)" in effort
    assert "this._effortAllowed(value, this.model)" in effort
    assert "await this.onEffortChange()" in effort

    # Backward compatibility is a thin delegate, not a policy bypass.
    compat = app[app.index("    async _runSlash(cmd, arg) {"):]
    compat = compat[:compat.index("\n    // Inject a synthetic assistant bubble")]
    assert "return this._dispatchSlash(cmd, arg)" in compat


def test_slash_controls_run_before_provider_gate_without_consuming_attachments():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    send = app[app.index("    async send(opts = {}) {"):]
    slash_start = send.index("// Slash controls must stay responsive")
    provider_gate = send.index("// No-provider gate", slash_start)
    slash_end = send.index("// Keep the ownership token primitive", slash_start)
    slash = send[slash_start:slash_end]

    assert "if (this.SLASH_ENABLED && isComposerSubmission)" in slash
    assert "sendState.draft && sendState.draft.input" in slash
    assert "await this._dispatchSlash(" in slash
    # The send path does not clear or move attachment arrays. Dispatcher refusal
    # leaves the exact tab-owned draft intact for the user to edit or resend.
    for destructive in (
        "pendingImages.splice", "pendingDocs.splice", "pendingQuotes.splice",
        "clearSubmittedComposer", "ownerState.draft.input = \"\"",
    ):
        assert destructive not in slash
    assert slash_start < provider_gate
