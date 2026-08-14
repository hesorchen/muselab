"""Browser regressions for file-tree and preview async ownership.

These cases need Alpine + a real DOM: static source assertions cannot prove
that aborts, reactive tab state, and editor buffers settle on the right owner.
"""
from __future__ import annotations

from collections import Counter
import json
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="install with: uv add --group dev pytest-playwright")
from playwright.sync_api import Page, expect  # noqa: E402


def _login(page: Page, base: str, token: str) -> None:
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_selector(".login, .chat-tabs-list", state="visible", timeout=5000)
    if page.locator(".login").is_visible():
        page.fill('.login input[type="password"]', token)
        page.keyboard.press("Enter")
    expect(page.locator(".chat-tabs-list")).to_be_visible(timeout=5000)
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')?._x_dataStack?.[0];
          return app && app.authed && app.appReady && app._sessionsInitialized;
        }"""
    )


def _select_rendered_preview_text(page: Page) -> str:
    # Alpine can publish ``previewMode`` one render tick before the Markdown
    # body is mounted.  Wait for the actual selectable surface so callers do
    # not race an otherwise healthy preview on slower CI runners.
    page.wait_for_function(
        """() => Array.from(document.querySelectorAll(
          '.pane.preview .markdown, .pane.preview pre.text, '
          + '.pane.preview .xlsx-preview'
        )).some(el => el.getClientRects().length && el.textContent.trim())"""
    )
    return page.evaluate(
        """() => {
          const roots = Array.from(document.querySelectorAll(
            '.pane.preview .markdown, .pane.preview pre.text, '
            + '.pane.preview .xlsx-preview'
          )).filter(el => el.getClientRects().length && el.textContent.trim());
          const root = roots[0];
          if (!root) throw new Error('no visible selectable preview');
          const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
            acceptNode(node) {
              return node.data.trim().length >= 8
                ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
            },
          });
          const node = walker.nextNode();
          if (!node) throw new Error('no preview text node');
          const leading = node.data.length - node.data.trimStart().length;
          const length = Math.min(32, node.data.trim().length);
          const range = document.createRange();
          range.setStart(node, leading);
          range.setEnd(node, leading + length);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          document.dispatchEvent(new Event('selectionchange'));
          return selection.toString();
        }"""
    )


def test_desktop_chat_is_center_primary_pane_and_preview_is_right_rail(
        page: Page, backend_url, auth_token):
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)

    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.desktopFullPane = '';
          app.leftOpen = true;
          app.previewOpen = true;
          app.leftWidth = 280;
          app.previewWidth = 440;
          await new Promise(resolve => app.$nextTick(resolve));
          await new Promise(resolve => requestAnimationFrame(resolve));

          const files = document.querySelector('.pane.files');
          const chat = document.querySelector('.pane.chat');
          const preview = document.querySelector('.pane.preview');
          const chatNode = chat;
          const rect = node => {
            const r = node.getBoundingClientRect();
            return {left: r.left, right: r.right, width: r.width};
          };
          const before = {
            files: rect(files), chat: rect(chat), preview: rect(preview),
            orders: [files, chat, preview].map(node => getComputedStyle(node).order),
          };

          app.togglePreviewPane();
          await new Promise(resolve => app.$nextTick(resolve));
          await new Promise(resolve => requestAnimationFrame(resolve));
          const hidden = {
            previewDisplay: getComputedStyle(preview).display,
            chat: rect(chat),
            sameChatNode: document.querySelector('.pane.chat') === chatNode,
          };

          app.togglePreviewPane();
          await new Promise(resolve => app.$nextTick(resolve));
          await new Promise(resolve => requestAnimationFrame(resolve));
          const restored = {
            previewDisplay: getComputedStyle(preview).display,
            chat: rect(chat), preview: rect(preview),
            sameChatNode: document.querySelector('.pane.chat') === chatNode,
          };
          const prefs = JSON.parse(localStorage.getItem('muselab_prefs') || '{}');
          localStorage.setItem('muselab_prefs', JSON.stringify({
            schema: 8, leftWidth: 280, previewWidth: 440,
          }));
          app.leftWidth = 280;
          app.loadPrefs();
          const fileWidthMigration = app.leftWidth;
          localStorage.setItem('muselab_prefs', JSON.stringify({
            schema: 7, rightOpen: false, rightWidth: 512,
          }));
          app.previewOpen = true;
          app.previewWidth = 440;
          app.loadPrefs();
          const migration = {
            previewOpen: app.previewOpen,
            previewWidth: app.previewWidth,
          };
          app.previewOpen = false;
          app.toggleDesktopFull('preview');
          await new Promise(resolve => app.$nextTick(resolve));
          await new Promise(resolve => requestAnimationFrame(resolve));
          const previewFullscreen = {
            previewOpen: app.previewOpen,
            preview: rect(preview),
            chatDisplay: getComputedStyle(chat).display,
          };
          app.toggleDesktopFull('preview');
          await new Promise(resolve => app.$nextTick(resolve));
          app.toggleDesktopFull('chat');
          await new Promise(resolve => app.$nextTick(resolve));
          await new Promise(resolve => requestAnimationFrame(resolve));
          const chatFullscreen = {
            chat: rect(chat),
            previewDisplay: getComputedStyle(preview).display,
            sameChatNode: document.querySelector('.pane.chat') === chatNode,
          };
          app.toggleDesktopFull('chat');
          return {
            before, hidden, restored, prefs, migration, fileWidthMigration,
            previewFullscreen, chatFullscreen,
          };
        }"""
    )

    before = result["before"]
    assert before["files"]["right"] <= before["chat"]["left"]
    assert before["chat"]["right"] <= before["preview"]["left"]
    assert before["chat"]["width"] > before["preview"]["width"]
    assert before["orders"] == ["1", "3", "5"]

    assert result["hidden"]["previewDisplay"] == "none"
    assert result["hidden"]["chat"]["width"] > before["chat"]["width"]
    assert result["hidden"]["sameChatNode"] is True
    assert result["restored"]["previewDisplay"] == "flex"
    assert result["restored"]["chat"]["right"] <= result["restored"]["preview"]["left"]
    assert result["restored"]["sameChatNode"] is True
    assert result["prefs"]["schema"] == 9
    assert result["prefs"]["previewOpen"] is True
    assert result["prefs"]["previewWidth"] == 440
    assert "rightOpen" not in result["prefs"]
    assert "rightWidth" not in result["prefs"]
    assert result["fileWidthMigration"] == 340
    assert result["migration"] == {"previewOpen": True, "previewWidth": 512}
    assert result["previewFullscreen"]["previewOpen"] is True
    assert result["previewFullscreen"]["preview"]["width"] == 1440
    assert result["previewFullscreen"]["chatDisplay"] == "none"
    assert result["chatFullscreen"]["chat"]["width"] == 1440
    assert result["chatFullscreen"]["previewDisplay"] == "none"
    assert result["chatFullscreen"]["sameChatNode"] is True


def test_refresh_restores_file_without_reopening_hidden_preview(
        page: Page, backend_url, auth_token):
    """Background file restoration preserves the user's hidden right rail."""
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)

    prepared = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const opened = await app.openFile({path: 'README.md', name: 'README.md'});
          app.desktopFullPane = '';
          app.previewOpen = false;
          app.savePrefs();
          return {
            opened,
            selected: app.selected,
            previewOpen: app.previewOpen,
            savedPreviewOpen: JSON.parse(
              localStorage.getItem('muselab_prefs') || '{}').previewOpen,
          };
        }"""
    )
    assert prepared == {
        "opened": True,
        "selected": "README.md",
        "previewOpen": False,
        "savedPreviewOpen": False,
    }

    # A fresh boot should reload the selected file in the background without
    # treating restoration as a user click that reveals the preview rail.
    _login(page, backend_url, auth_token)
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')?._x_dataStack?.[0];
          return app && app.selected === 'README.md' && !app.previewOpen;
        }"""
    )
    restored = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const preview = document.querySelector('.pane.preview');
          return {
            selected: app.selected,
            previewOpen: app.previewOpen,
            previewDisplay: getComputedStyle(preview).display,
          };
        }"""
    )
    assert restored == {
        "selected": "README.md",
        "previewOpen": False,
        "previewDisplay": "none",
    }

    # A plain internal load is layout-neutral by default. Only an explicitly
    # user-owned call with reveal:true may reopen the hidden rail.
    reveal_contract = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          await app.openFile({path: 'README.md', name: 'README.md'});
          const afterBackground = {
            previewOpen: app.previewOpen,
            previewDisplay: getComputedStyle(
              document.querySelector('.pane.preview')).display,
          };
          await app.reloadPreview();
          const afterReload = {
            previewOpen: app.previewOpen,
            previewDisplay: getComputedStyle(
              document.querySelector('.pane.preview')).display,
          };
          await app._maybeReloadPreview('README.md');
          const afterToolReload = {
            previewOpen: app.previewOpen,
            previewDisplay: getComputedStyle(
              document.querySelector('.pane.preview')).display,
          };
          await app.onNodeClick({}, {
            path: 'README.md', name: 'README.md', is_dir: false,
          });
          await new Promise(resolve => app.$nextTick(resolve));
          await new Promise(resolve => requestAnimationFrame(resolve));
          return {
            afterBackground,
            afterReload,
            afterToolReload,
            selected: app.selected,
            previewOpen: app.previewOpen,
            previewDisplay: getComputedStyle(
              document.querySelector('.pane.preview')).display,
          };
        }"""
    )
    assert reveal_contract == {
        "afterBackground": {
            "previewOpen": False,
            "previewDisplay": "none",
        },
        "afterReload": {
            "previewOpen": False,
            "previewDisplay": "none",
        },
        "afterToolReload": {
            "previewOpen": False,
            "previewDisplay": "none",
        },
        "selected": "README.md",
        "previewOpen": True,
        "previewDisplay": "flex",
    }


def test_os_file_drop_uses_whole_window_root_except_explicit_directory(
        page: Page, backend_url, auth_token):
    """Composer/tree blank/file rows are root; only a dir row is nested."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)

    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const uploads = [];
          const attachments = [];
          const internalMoves = [];
          app._uploadFilesToDir = async (dir, files) => {
            uploads.push({dir, names: Array.from(files, file => file.name)});
          };
          app._attachFile = async file => attachments.push(file.name);
          app.moveTreeItems = async (paths, dir) => {
            internalMoves.push({paths: Array.from(paths), dir});
          };

          const list = document.querySelector('.filelist');
          const dirRow = document.createElement('li');
          dirRow.className = 'dir';
          dirRow.dataset.path = 'drop-target-dir';
          dirRow.innerHTML = '<span class="name">drop-target-dir</span>';
          const dirNode = {is_dir: true, path: 'drop-target-dir'};
          dirRow.addEventListener('dragover', event => {
            event.preventDefault();
            app.onTreeNodeDragOver(event, dirNode);
          });
          dirRow.addEventListener('drop', event => {
            event.preventDefault();
            void app.onDrop(event, dirNode);
          });
          list.appendChild(dirRow);
          const fileRow = document.createElement('li');
          fileRow.className = 'file';
          fileRow.dataset.path = 'drop-target-dir/existing.txt';
          fileRow.innerHTML = '<span class="name">existing.txt</span>';
          list.appendChild(fileRow);

          const overlay = document.querySelector('.global-file-drop-overlay');
          const settle = () => new Promise(resolve => requestAnimationFrame(
            () => requestAnimationFrame(resolve)));
          const transfer = name => {
            const dt = new DataTransfer();
            dt.items.add(new File(['fixture'], name, {type: 'text/plain'}));
            return dt;
          };
          const enter = async (target, name) => {
            const dt = transfer(name);
            target.dispatchEvent(new DragEvent('dragenter', {
              bubbles: true, cancelable: true, dataTransfer: dt,
            }));
            target.dispatchEvent(new DragEvent('dragover', {
              bubbles: true, cancelable: true, dataTransfer: dt,
            }));
            await settle();
            return dt;
          };
          const drop = async (target, dt) => {
            target.dispatchEvent(new DragEvent('drop', {
              bubbles: true, cancelable: true, dataTransfer: dt,
            }));
            await settle();
          };

          const chat = document.querySelector('.chat-input-wrap');
          let dt = await enter(chat, 'chat-root.txt');
          const rootOverlay = {
            visible: !!overlay.getClientRects().length,
            directory: app.osFileDropOnDirectory,
            text: overlay.textContent.replace(/\\s+/g, ' ').trim(),
          };
          await drop(chat, dt);

          dt = await enter(list, 'tree-blank-root.txt');
          await drop(list, dt);

          dt = await enter(dirRow.querySelector('.name'), 'nested.txt');
          const dirOverlay = {
            visible: !!overlay.getClientRects().length,
            directory: app.osFileDropOnDirectory,
            dir: app.osFileDropDir,
            text: overlay.textContent.replace(/\\s+/g, ' ').trim(),
          };
          await drop(dirRow.querySelector('.name'), dt);

          dt = await enter(fileRow.querySelector('.name'), 'file-row-root.txt');
          await drop(fileRow.querySelector('.name'), dt);

          // The document capture route must ignore MuseLab's own tree drag.
          const internal = new DataTransfer();
          internal.setData(app._DRAG_MIME_INTERNAL, 'README.md');
          internal.setData('text/plain', 'README.md');
          app._dragSrcPath = 'README.md';
          dirRow.querySelector('.name').dispatchEvent(new DragEvent('dragover', {
            bubbles: true, cancelable: true, dataTransfer: internal,
          }));
          dirRow.querySelector('.name').dispatchEvent(new DragEvent('drop', {
            bubbles: true, cancelable: true, dataTransfer: internal,
          }));
          await settle();
          const internalOverlay = app.osFileDragging;
          dirRow.remove();
          fileRow.remove();
          return {
            uploads, attachments, internalMoves, internalOverlay,
            rootOverlay, dirOverlay,
            dragging: app.osFileDragging,
            dropDir: app.osFileDropDir,
          };
        }"""
    )

    assert result["uploads"] == [
        {"dir": "", "names": ["chat-root.txt"]},
        {"dir": "", "names": ["tree-blank-root.txt"]},
        {"dir": "drop-target-dir", "names": ["nested.txt"]},
        {"dir": "", "names": ["file-row-root.txt"]},
    ]
    assert result["attachments"] == []
    assert result["internalMoves"] == [
        {"paths": ["README.md"], "dir": "drop-target-dir"},
    ]
    assert result["internalOverlay"] is False
    assert result["rootOverlay"]["visible"] is True
    assert result["rootOverlay"]["directory"] is False
    assert ("工作区根目录" in result["rootOverlay"]["text"]
            or "workspace root" in result["rootOverlay"]["text"])
    assert result["dirOverlay"]["visible"] is True
    assert result["dirOverlay"]["directory"] is True
    assert result["dirOverlay"]["dir"] == "drop-target-dir"
    assert "/drop-target-dir" in result["dirOverlay"]["text"]
    assert result["dragging"] is False
    assert result["dropDir"] == ""
    assert errors == []


def test_file_metadata_layout_is_compact_and_human_readable(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.locator('.filelist li[data-path="README.md"]').click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.selected === 'README.md' && app.selectedMeta?.path === 'README.md';
        }"""
    )

    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.lang = 'zh';
          const now = Date.now();
          app.fileMetaClock = now;
          await new Promise(resolve => app.$nextTick(resolve));
          const row = document.querySelector('.filelist li[data-path="README.md"]');
          const size = row.querySelector('.size');
          const meta = document.querySelector('.pane-fileinfo-meta');
          return {
            sizes: [0, 1024, 1536, 1024 ** 3, 1024 ** 4].map(n => app.fmtSize(n)),
            relative: app.fmtRelativeMtime((now - 5 * 60_000) / 1000),
            breadcrumb: app.fileBreadcrumb('one/two/three/four/file.md'),
            rowHeight: row.getBoundingClientRect().height,
            sizeText: size.textContent.trim(),
            sizeFont: getComputedStyle(size).fontFamily,
            metaText: meta.textContent.replace(/\\s+/g, ' ').trim(),
            metaTitle: meta.title,
          };
        }"""
    )

    assert result["sizes"] == ["0 B", "1 KB", "1.5 KB", "1 GB", "1 TB"]
    assert result["relative"] == "5 分钟前"
    assert result["breadcrumb"] == "… › two › three › four"
    assert result["rowHeight"] >= 28
    assert result["sizeText"].endswith(" B")
    assert "mono" not in result["sizeFont"].lower()
    assert "B" in result["metaText"]
    assert "修改于" in result["metaTitle"]


def test_preview_selection_quotes_as_attachment_and_asks_in_side_session(
        page: Page, backend_url, auth_token):
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    page.locator('.filelist li[data-path="README.md"]').click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const visible = Array.from(document.querySelectorAll(
            '.pane.preview .markdown'
          )).some(el => el.getClientRects().length && el.textContent.trim());
          return app.selected === 'README.md' && app.previewMode === 'md' && visible;
        }"""
    )

    before = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.lang = 'zh';
          app._setChatInput('alphaomega');
          await new Promise(resolve => app.$nextTick(resolve));
          app._captureComposerState(app.currentId);
          const ta = app.$refs.chatInput;
          ta.setSelectionRange(5, 5);
          return {
            session: app.currentId,
            sessionCount: app.sessions.length,
            openTabs: [...app.openTabIds],
            messageCount: app.messages.length,
          };
        }"""
    )
    selected = _select_rendered_preview_text(page)
    expect(page.locator(".preview-selection-actions")).to_be_visible(timeout=3000)
    page.locator(".preview-selection-actions button").nth(0).click()
    expect(page.locator(".preview-selection-popover")).to_be_hidden()

    quoted = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const quote = app.pendingQuotes[0];
          return {
            input: app.input,
            draft: app.tabState[app.currentId].draft.input,
            quoteCount: app.pendingQuotes.length,
            quoteText: quote && quote.text,
            quotePath: quote && quote.path,
            session: app.currentId,
            sessionCount: app.sessions.length,
            openTabs: [...app.openTabIds],
            messageCount: app.messages.length,
          };
        }"""
    )
    assert quoted["input"] == quoted["draft"]
    assert quoted["input"] == "alphaomega"
    assert quoted["quoteCount"] == 1
    assert quoted["quoteText"] == selected
    assert quoted["quotePath"] == "README.md"
    assert quoted["session"] == before["session"]
    assert quoted["sessionCount"] == before["sessionCount"]
    assert quoted["openTabs"] == before["openTabs"]
    assert quoted["messageCount"] == before["messageCount"]

    page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          // Programmatic composer changes use the native-editor bridge so the
          // focused textarea and the reactive draft advance together.
          app._setChatInput('KEEP EXISTING DRAFT');
          app.pendingImages = [{id: 'keep-image'}];
          app.pendingDocs = [{id: 'keep-doc'}];
          app.pendingQuotes.splice(0);
          app._captureComposerState(app.currentId);
          window.__previewAskOriginalSend = app.send;
          window.__previewAskOriginalCreate = app._createPreviewSelectionAskSession;
          app._createPreviewSelectionAskSession = async (snapshot, question) => {
            const meta = {
              id: 'preview-side-question', name: 'Preview side question',
              model: app.model, permission: 'default', active: false,
              cwd: app.currentWorkspacePath(),
            };
            app.sessions = [meta, ...app.sessions.filter(s => s.id !== meta.id)];
            const st = app._ensureTabState(meta.id);
            st._loaded = true;
            window.__previewAskCreate = {snapshot, question};
            return meta;
          };
          app.send = async opts => {
            window.__previewAskOptions = window.__previewAskOptions || [];
            window.__previewAskOptions.push(JSON.parse(JSON.stringify(opts)));
            const st = app._ensureTabState(opts.sessionId);
            const turn = window.__previewAskOptions.length;
            if (turn === 1) st.messages.splice(0, st.messages.length);
            st.messages.push(
              {role: 'user', text: opts.detachedText,
               displayText: opts.detachedDisplayText},
              {role: 'assistant', text: `SIDE_ANSWER_MARKER_${turn}`});
            st.streaming = false;
            return true;
          };
          await new Promise(resolve => app.$nextTick(resolve));
        }"""
    )
    selected_for_ask = _select_rendered_preview_text(page)
    expect(page.locator(".preview-selection-actions")).to_be_visible(timeout=3000)
    page.locator(".preview-selection-actions button").nth(1).click()
    ask = page.locator(".preview-selection-ask")
    expect(ask).to_be_visible()
    ask.locator("textarea").fill("这段内容的核心是什么？")
    ask.locator('button[type="submit"]').click()
    expect(page.locator(".preview-selection-answer")).to_be_visible()
    expect(page.locator(".preview-selection-answer-body")).to_contain_text(
        "SIDE_ANSWER_MARKER_1"
    )
    followup = page.locator(".preview-selection-followup textarea")
    followup.fill("能再举一个例子吗？")
    page.locator(".preview-selection-followup-send").click()
    expect(page.locator(".preview-selection-conversation")).to_contain_text(
        "SIDE_ANSWER_MARKER_2"
    )
    expect(page.locator(".preview-selection-conversation")).to_contain_text(
        "能再举一个例子吗？"
    )

    asked = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const opts = window.__previewAskOptions;
          app.send = window.__previewAskOriginalSend;
          app._createPreviewSelectionAskSession = window.__previewAskOriginalCreate;
          return {
            opts,
            create: window.__previewAskCreate,
            input: app.input,
            draft: app.tabState[app.currentId].draft.input,
            images: app.pendingImages.map(item => item.id),
            docs: app.pendingDocs.map(item => item.id),
            quotes: app.pendingQuotes.length,
            session: app.currentId,
            sessionCount: app.sessions.length,
            openTabs: [...app.openTabIds],
            messageCount: app.messages.length,
            askSessionId: app.previewQuote.askSessionId,
            popover: app.previewQuote.show,
          };
        }"""
    )
    assert len(asked["opts"]) == 2
    first, followup_opts = asked["opts"]
    assert first["sessionId"] == followup_opts["sessionId"] == "preview-side-question"
    assert first["permissionMode"] == followup_opts["permissionMode"] == "default"
    assert "引用自 `README.md`" in first["detachedText"]
    assert selected_for_ask in first["detachedText"]
    assert "这段内容的核心是什么？" in first["detachedText"]
    assert first["detachedDisplayText"] == "这段内容的核心是什么？"
    assert "追问：" in followup_opts["detachedText"]
    assert "能再举一个例子吗？" in followup_opts["detachedText"]
    assert followup_opts["detachedDisplayText"] == "能再举一个例子吗？"
    assert asked["create"]["snapshot"]["sessionId"] == before["session"]
    assert asked["create"]["question"] == "这段内容的核心是什么？"
    assert asked["input"] == asked["draft"] == "KEEP EXISTING DRAFT"
    assert asked["images"] == ["keep-image"]
    assert asked["docs"] == ["keep-doc"]
    assert asked["quotes"] == 0
    assert asked["session"] == before["session"]
    assert asked["sessionCount"] == before["sessionCount"] + 1
    assert asked["openTabs"] == before["openTabs"]
    assert asked["messageCount"] == before["messageCount"]
    assert asked["askSessionId"] == "preview-side-question"
    assert asked["popover"] is True


def test_selection_side_session_forks_without_opening_or_switching_tab(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    source = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentId"
    )
    child = "11111111-2222-4333-8444-555555555555"
    fork_bodies: list[dict] = []

    def handle_fork(route) -> None:
        fork_bodies.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "id": child,
                "session_id": child,
                "name": "Source · 独立侧问：why",
                "model": "e2e-model",
                "permission": "bypassPermissions",
                "cwd": "/e2e-workspace",
                "forked_from": source,
                "forked_from_message_id": "assistant-boundary",
            }),
        )

    page.route(f"**/api/chat/sessions/{source}/fork", handle_fork)
    result = page.evaluate(
        """async arg => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.lang = 'zh';
          const beforeTabs = [...app.openTabIds];
          const meta = await app._createPreviewSelectionAskSession({
            source: 'chat', role: 'assistant', sessionId: arg.source,
            messageId: 'assistant-boundary', path: '', text: 'selected',
            truncated: false,
          }, 'why');
          return {
            id: meta.id,
            currentId: app.currentId,
            openTabs: [...app.openTabIds],
            beforeTabs,
            stateLoaded: app.tabState[meta.id]._loaded,
            statePermission: app.tabState[meta.id].permission,
          };
        }""",
        {"source": source},
    )

    assert fork_bodies == [{
        "up_to_message_id": "assistant-boundary",
        "title": fork_bodies[0]["title"],
        "activity_hidden": True,
        "runtime_profile": "side_question",
    }]
    assert "独立侧问" in fork_bodies[0]["title"]
    assert result["id"] == child
    assert result["currentId"] == source
    assert result["openTabs"] == result["beforeTabs"]
    assert child not in result["openTabs"]
    assert result["stateLoaded"] is True
    assert result["statePermission"] == "default"


def test_side_question_window_stays_floating_until_explicit_close(
        page: Page, backend_url, auth_token):
    page.set_viewport_size({"width": 1200, "height": 800})
    _login(page, backend_url, auth_token)
    page.locator('.filelist li[data-path="README.md"]').click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.selected === 'README.md' && app.previewMode === 'md';
        }"""
    )
    _select_rendered_preview_text(page)
    page.locator(".preview-selection-actions button").nth(1).click()
    popover = page.locator(".preview-selection-popover")
    textarea = page.locator(".preview-selection-ask:visible textarea")
    expect(textarea).to_be_visible()
    textarea.fill("FLOATING_QUESTION_DRAFT")

    # An ordinary click elsewhere used to close the window in capture phase.
    page.locator(".filelist").click(position={"x": 6, "y": 6})
    expect(popover).to_be_visible()
    expect(textarea).to_have_value("FLOATING_QUESTION_DRAFT")

    # File selection, chat-tab ownership and preview scrolling are all page
    # navigation, not an implicit close command for an independent question.
    state = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const source = app.currentId;
          const other = '22222222-3333-4444-8555-666666666666';
          app.sessions = [...app.sessions, {
            id: other, name: 'Other chat', model: app.model,
            permission: 'default', cwd: app.currentWorkspacePath(),
          }];
          app.openTabIds = [...app.openTabIds, other];
          app._ensureTabState(other)._loaded = true;
          app.selected = 'notes/a.md';
          app.currentId = other;
          app.onPreviewViewportScroll();
          await new Promise(resolve => app.$nextTick(resolve));
          const switched = {
            show: app.previewQuote.show,
            mode: app.previewQuote.mode,
            question: app.previewQuote.question,
          };
          app.currentId = source;
          app.openTabIds = app.openTabIds.filter(id => id !== other);
          app.sessions = app.sessions.filter(row => row.id !== other);
          delete app.tabState[other];
          await new Promise(resolve => app.$nextTick(resolve));
          return switched;
        }"""
    )
    assert state == {
        "show": True,
        "mode": "ask",
        "question": "FLOATING_QUESTION_DRAFT",
    }
    expect(popover).to_be_visible()

    page.locator(".preview-selection-ask:visible .preview-selection-close").click()
    expect(popover).to_be_hidden()


def test_selection_side_question_window_drags_by_header_and_stays_in_view(
        page: Page, backend_url, auth_token):
    page.set_viewport_size({"width": 1000, "height": 720})
    _login(page, backend_url, auth_token)
    page.locator('.filelist li[data-path="README.md"]').click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.selected === 'README.md' && app.previewMode === 'md';
        }"""
    )
    _select_rendered_preview_text(page)
    actions = page.locator(".preview-selection-actions")
    expect(actions).to_be_visible(timeout=3000)
    actions.locator("button").nth(1).click()

    popover = page.locator(".preview-selection-popover")
    form_head = page.locator(
        ".preview-selection-ask:not([style*='display: none']) "
        ".preview-selection-ask-head"
    )
    expect(form_head).to_be_visible()
    before = popover.bounding_box()
    head_box = form_head.bounding_box()
    assert before is not None and head_box is not None
    start_x = head_box["x"] + 32
    start_y = head_box["y"] + head_box["height"] / 2
    target_left = 80
    target_top = 120
    page.mouse.move(start_x, start_y)
    page.mouse.down()
    page.mouse.move(
        start_x + target_left - before["x"],
        start_y + target_top - before["y"],
        steps=8,
    )
    page.mouse.up()

    after = popover.bounding_box()
    assert after is not None
    assert abs(after["x"] - target_left) < 2
    assert abs(after["y"] - target_top) < 2
    state = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const el = document.querySelector('.preview-selection-popover');
          return {
            dragged: app.previewQuote.dragged,
            dragging: app.previewQuote.dragging,
            transform: getComputedStyle(el).transform,
            x: app.previewQuote.x,
            y: app.previewQuote.y,
          };
        }"""
    )
    assert state["dragged"] is True
    assert state["dragging"] is False
    assert state["transform"] == "none"
    assert abs(state["x"] - after["x"]) < 1
    assert abs(state["y"] - after["y"]) < 1

    # Pointer capture keeps the drag alive when the cursor leaves the header;
    # the position is clamped to a 12 px viewport margin in both directions.
    head_box = form_head.bounding_box()
    assert head_box is not None
    page.mouse.move(head_box["x"] + 32, head_box["y"] + 12)
    page.mouse.down()
    page.mouse.move(995, 715, steps=8)
    page.mouse.up()
    lower_right = popover.bounding_box()
    assert lower_right is not None
    assert lower_right["x"] + lower_right["width"] <= 988.5
    assert lower_right["y"] + lower_right["height"] <= 708.5

    head_box = form_head.bounding_box()
    assert head_box is not None
    page.mouse.move(head_box["x"] + 32, head_box["y"] + 12)
    page.mouse.down()
    page.mouse.move(1, 1, steps=8)
    page.mouse.up()
    upper_left = popover.bounding_box()
    assert upper_left is not None
    assert upper_left["x"] >= 11.5
    assert upper_left["y"] >= 11.5

    # The answer-state header remains the same drag handle after the form is
    # replaced by the compact branch response.
    page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = 'drag-side-answer';
          const state = app._ensureTabState(sid);
          state._loaded = true;
          state.messages.splice(0, state.messages.length,
            {role: 'user', text: 'drag prompt'},
            {role: 'assistant', text: 'DRAG ANSWER'});
          app.previewQuote.question = 'drag question';
          app.previewQuote.askPrompt = 'drag prompt';
          app.previewQuote.askSessionId = sid;
          await new Promise(resolve => app.$nextTick(resolve));
        }"""
    )
    answer = page.locator(".preview-selection-answer")
    answer_head = answer.locator(".preview-selection-ask-head")
    expect(answer_head).to_be_visible()
    answer_before = popover.bounding_box()
    answer_head_box = answer_head.bounding_box()
    assert answer_before is not None and answer_head_box is not None
    page.mouse.move(answer_head_box["x"] + 32, answer_head_box["y"] + 12)
    page.mouse.down()
    page.mouse.move(
        answer_head_box["x"] + 170,
        answer_head_box["y"] + 100,
        steps=8,
    )
    page.mouse.up()
    answer_after = popover.bounding_box()
    assert answer_after is not None
    assert answer_after["x"] > answer_before["x"] + 80
    assert answer_after["y"] > answer_before["y"] + 45

    answer.locator(".preview-selection-close").click()
    expect(popover).to_be_hidden()
    reset = page.evaluate(
        """() => {
          const q = document.querySelector('#app')._x_dataStack[0].previewQuote;
          return {x: q.x, y: q.y, dragged: q.dragged, dragging: q.dragging};
        }"""
    )
    assert reset == {"x": 0, "y": 0, "dragged": False, "dragging": False}


def test_selection_side_question_window_supports_touch_drag(
        page: Page, browser_name, backend_url, auth_token):
    if browser_name != "chromium":
        pytest.skip("touch input dispatch runs on Chromium")
    page.set_viewport_size({"width": 390, "height": 844})
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setTouchEmulationEnabled", {
        "enabled": True,
        "maxTouchPoints": 5,
    })
    _login(page, backend_url, auth_token)
    page.evaluate(
        """() => document.querySelector('#app')._x_dataStack[0]
          .setMobileTab('files')"""
    )
    page.locator('.filelist li[data-path="README.md"]').click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.mobileTab === 'preview' && app.selected === 'README.md'
            && app.previewMode === 'md';
        }"""
    )
    _select_rendered_preview_text(page)
    actions = page.locator(".preview-selection-actions")
    expect(actions).to_be_visible(timeout=3000)
    actions.locator("button").nth(1).click()
    popover = page.locator(".preview-selection-popover")
    head = page.locator(".preview-selection-ask .preview-selection-ask-head")
    expect(head).to_be_visible()
    before = popover.bounding_box()
    head_box = head.bounding_box()
    assert before is not None and head_box is not None
    start = {
        "x": head_box["x"] + 40,
        "y": head_box["y"] + head_box["height"] / 2,
    }
    target = {
        "x": start["x"],
        "y": min(760, start["y"] + 170),
    }
    cdp.send("Input.dispatchTouchEvent", {
        "type": "touchStart",
        "touchPoints": [{**start, "id": 1}],
    })
    cdp.send("Input.dispatchTouchEvent", {
        "type": "touchMove",
        "touchPoints": [{**target, "id": 1}],
    })
    cdp.send("Input.dispatchTouchEvent", {
        "type": "touchEnd",
        "touchPoints": [],
    })
    page.wait_for_function(
        """() => {
          const q = document.querySelector('#app')._x_dataStack[0].previewQuote;
          return q.dragged && !q.dragging;
        }"""
    )
    after = popover.bounding_box()
    assert after is not None
    assert after["y"] > before["y"] + 70
    assert after["x"] >= 11.5
    assert after["x"] + after["width"] <= 378.5
    assert after["y"] + after["height"] <= 832.5


def test_detached_preview_question_uses_send_pipeline_without_touching_draft(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    ticket_bodies: list[dict] = []

    def handle_ticket(route) -> None:
        ticket_bodies.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ticket": "preview-detached-ticket"}),
        )

    page.route("**/api/chat/stream/start", handle_ticket)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          class FakeEventSource extends EventTarget {
            constructor(url) {
              super();
              this.url = url;
              this.readyState = 0;
              setTimeout(() => {
                this.readyState = 1;
                if (this.onopen) this.onopen(new Event('open'));
              }, 0);
            }
            close() { this.readyState = 2; }
          }
          const originalEventSource = window.EventSource;
          const originalBusy = app._confirmSessionBusy;
          const originalRuntimeWait = app._awaitRuntimeSettingPatches;
          const originalCommit = app._commitChatRecoveryDraft;
          window.EventSource = FakeEventSource;
          app._confirmSessionBusy = async () => false;
          app._awaitRuntimeSettingPatches = async () => true;
          let recoveryCommits = 0;
          app._commitChatRecoveryDraft = () => { recoveryCommits += 1; };
          try {
            const sid = app.currentId;
            const image = {id: 'draft-image', uploading: false};
            const doc = {id: 'draft-doc', uploading: false};
            app.input = 'PRESERVE THIS DRAFT';
            app.pendingImages = [image];
            app.pendingDocs = [doc];
            app._captureComposerState(sid);
            const messageCount = app.messages.length;
            const sendResult = await app.send({
              sessionId: sid,
              detachedText: 'DETACHED PREVIEW QUESTION',
            });
            await new Promise(resolve => setTimeout(resolve, 20));
            const state = app.tabState[sid];
            return {
              sendResult: sendResult === undefined ? 'undefined' : sendResult,
              input: app.input,
              draft: state.draft.input,
              images: state.draft.pendingImages.map(item => item.id),
              docs: state.draft.pendingDocs.map(item => item.id),
              lastMessage: state.messages.at(-1)?.text,
              messageDelta: state.messages.length - messageCount,
              recoveryCommits,
              recovery: app._chatDraftRecord(sid),
            };
          } finally {
            if (app.es) app.es.close();
            window.EventSource = originalEventSource;
            app._confirmSessionBusy = originalBusy;
            app._awaitRuntimeSettingPatches = originalRuntimeWait;
            app._commitChatRecoveryDraft = originalCommit;
          }
        }"""
    )

    assert len(ticket_bodies) == 1
    assert ticket_bodies[0]["prompt"] == "DETACHED PREVIEW QUESTION"
    assert ticket_bodies[0]["image_ids"] == ""
    assert result["sendResult"] == "undefined"
    assert result["input"] == result["draft"] == "PRESERVE THIS DRAFT"
    assert result["images"] == ["draft-image"]
    assert result["docs"] == ["draft-doc"]
    assert result["lastMessage"] == "DETACHED PREVIEW QUESTION"
    assert result["messageDelta"] == 1
    assert result["recoveryCommits"] == 0
    assert result["recovery"]["text"] == "PRESERVE THIS DRAFT"
    assert result["recovery"]["pending"] == ""


def test_composer_quote_sends_context_without_rewriting_visible_text(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    ticket_bodies: list[dict] = []

    def handle_ticket(route) -> None:
        ticket_bodies.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ticket": "selection-quote-ticket"}),
        )

    page.route("**/api/chat/stream/start", handle_ticket)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          class FakeEventSource extends EventTarget {
            constructor(url) {
              super();
              this.url = url;
              this.readyState = 0;
              setTimeout(() => {
                this.readyState = 1;
                if (this.onopen) this.onopen(new Event('open'));
              }, 0);
            }
            close() { this.readyState = 2; }
          }
          const originalEventSource = window.EventSource;
          const originalBusy = app._confirmSessionBusy;
          const originalRuntimeWait = app._awaitRuntimeSettingPatches;
          window.EventSource = FakeEventSource;
          app._confirmSessionBusy = async () => false;
          app._awaitRuntimeSettingPatches = async () => true;
          try {
            const sid = app.currentId;
            app.lang = 'zh';
            app.input = 'VISIBLE QUESTION';
            app.pendingQuotes = [{
              id: 'quote-context', source: 'preview', role: '',
              sessionId: sid, messageId: '', path: 'README.md',
              text: 'SELECTED CONTEXT', truncated: false,
            }];
            app._captureComposerState(sid);
            const before = app.messages.length;
            const sendResult = await app.send();
            await new Promise(resolve => setTimeout(resolve, 20));
            const state = app.tabState[sid];
            const user = state.messages.slice(before).find(m => m.role === 'user');
            return {
              sendResult: sendResult === undefined ? 'undefined' : sendResult,
              input: app.input,
              draft: state.draft.input,
              pendingQuotes: state.draft.pendingQuotes.length,
              promptText: user && user.text,
              displayText: user && user.displayText,
              quoteText: user && user.selectionQuotes[0].text,
            };
          } finally {
            if (app.es) app.es.close();
            window.EventSource = originalEventSource;
            app._confirmSessionBusy = originalBusy;
            app._awaitRuntimeSettingPatches = originalRuntimeWait;
          }
        }"""
    )

    assert len(ticket_bodies) == 1
    prompt = ticket_bodies[0]["prompt"]
    assert "引用自 `README.md`" in prompt
    assert "SELECTED CONTEXT" in prompt
    assert prompt.endswith("VISIBLE QUESTION")
    assert result["sendResult"] == "undefined"
    assert result["input"] == result["draft"] == ""
    assert result["pendingQuotes"] == 0
    assert result["promptText"] == prompt
    assert result["displayText"] == "VISIBLE QUESTION"
    assert result["quoteText"] == "SELECTED CONTEXT"


def test_preview_selection_quote_fits_mobile_and_reveals_chat(
        page: Page, browser_name, backend_url, auth_token):
    if browser_name != "chromium":
        pytest.skip("touch media emulation runs on Chromium")
    page.set_viewport_size({"width": 390, "height": 844})
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setTouchEmulationEnabled", {
        "enabled": True,
        "maxTouchPoints": 5,
    })
    _login(page, backend_url, auth_token)
    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.lang = 'zh';
          app.input = '';
          app._captureComposerState(app.currentId);
          app.setMobileTab('files');
        }"""
    )
    page.locator('.filelist li[data-path="README.md"]').click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.mobileTab === 'preview' && app.selected === 'README.md'
            && app.previewMode === 'md';
        }"""
    )
    selected = _select_rendered_preview_text(page)
    actions = page.locator(".preview-selection-actions")
    expect(actions).to_be_visible(timeout=3000)
    geometry = actions.evaluate(
        """el => {
          const box = el.getBoundingClientRect();
          return {
            left: box.left,
            right: box.right,
            top: box.top,
            bottom: box.bottom,
            buttonHeights: Array.from(el.querySelectorAll('button'))
              .map(button => button.getBoundingClientRect().height),
          };
        }"""
    )
    assert geometry["left"] >= 0
    assert geometry["right"] <= 390
    assert geometry["top"] >= 0
    assert geometry["bottom"] <= 844
    assert min(geometry["buttonHeights"]) >= 38

    actions.locator("button").nth(0).click()
    page.wait_for_function(
        """expected => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.mobileTab === 'chat'
            && app.input === ''
            && app.pendingQuotes.length === 1
            && app.pendingQuotes[0].path === 'README.md'
            && app.pendingQuotes[0].text.includes(expected);
        }""",
        arg=selected,
    )
    result = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {
            mobileTab: app.mobileTab,
            input: app.input,
            quoteCount: app.pendingQuotes.length,
            quoteText: app.pendingQuotes[0]?.text || '',
            popover: app.previewQuote.show,
            chatDisplay: getComputedStyle(document.querySelector('.pane.chat')).display,
            chipDisplay: getComputedStyle(document.querySelector('.selection-quote-chip')).display,
          };
        }"""
    )
    assert result["mobileTab"] == "chat"
    assert result["popover"] is False
    assert result["chatDisplay"] == "flex"
    assert result["input"] == ""
    assert result["quoteCount"] == 1
    assert selected in result["quoteText"]
    assert result["chipDisplay"] in {"flex", "inline-flex"}


def test_file_metadata_stays_inside_mobile_header(
        page: Page, browser_name, backend_url, auth_token):
    if browser_name != "chromium":
        pytest.skip("touch media emulation runs on Chromium")
    page.set_viewport_size({"width": 390, "height": 844})
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setTouchEmulationEnabled", {
        "enabled": True,
        "maxTouchPoints": 5,
    })
    _login(page, backend_url, auth_token)
    page.evaluate(
        """() => document.querySelector('#app')._x_dataStack[0]
          .setMobileTab('files')"""
    )
    file_row_height = page.locator(
        '.filelist li[data-path="README.md"]'
    ).evaluate("row => row.getBoundingClientRect().height")
    page.locator('.filelist li[data-path="README.md"]').click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.selected === 'README.md' && !!app.selectedMeta;
        }"""
    )
    page.evaluate(
        """() => document.querySelector('#app')._x_dataStack[0]
          .setMobileTab('preview')"""
    )

    geometry = page.evaluate(
        """() => {
          const head = document.querySelector('.pane.preview .pane-head');
          const meta = head.querySelector('.pane-fileinfo-meta');
          const headBox = head.getBoundingClientRect();
          const metaBox = meta.getBoundingClientRect();
          return {
            headTop: headBox.top,
            headBottom: headBox.bottom,
            metaTop: metaBox.top,
            metaBottom: metaBox.bottom,
            metaHeight: metaBox.height,
            themeDisplay: getComputedStyle(
              document.querySelector('.files-theme-toggle')).display,
            hiddenDisplay: getComputedStyle(
              document.querySelector('.files-hidden-toggle')).display,
          };
        }"""
    )

    assert geometry["metaTop"] >= geometry["headTop"]
    assert geometry["metaBottom"] <= geometry["headBottom"]
    assert geometry["metaHeight"] < 20
    assert file_row_height >= 40
    assert geometry["themeDisplay"] != "none"
    assert geometry["hiddenDisplay"] == "none"


def test_directory_can_be_mentioned_from_search_and_tree_action(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const created = await fetch('/api/files/mkdir', {
            method: 'POST',
            headers: {...app.fileHdr(), 'Content-Type': 'application/json'},
            body: JSON.stringify({path: 'mention-folder'}),
          });
          if (!created.ok) throw new Error(await created.text());

          await app.fetchMention('mention-folder');
          const directory = app.mentionResults.find(
            item => item.path === 'mention-folder'
          );
          if (!directory) return {found: false};

          app.input = '查看 @mention-folder';
          app.mentionAnchor = app.input.lastIndexOf('@');
          const input = app.$refs.chatInput;
          input.value = app.input;
          input.setSelectionRange(app.input.length, app.input.length);
          app.pickMention(app.mentionResults.indexOf(directory));
          await new Promise(resolve => app.$nextTick(resolve));
          const pickerInput = app.input;

          app.input = '';
          app.ctxMenu = {show: true, x: 0, y: 0, node: directory, multi: 0};
          await app.ctxAction('mention');
          return {
            found: true,
            isDir: directory.is_dir,
            pickerInput,
            treeInput: app.input,
          };
        }"""
    )
    assert result == {
        "found": True,
        "isDir": True,
        "pickerInput": "查看 @mention-folder/ ",
        "treeInput": "@mention-folder/ ",
    }


def test_symlink_outside_workspace_error_is_actionable(
        page: Page, backend_url, auth_token):
    def reject_outside_link(route) -> None:
        route.fulfill(
            status=400,
            content_type="application/json",
            body='{"detail":"path escapes root"}',
        )

    page.route("**/api/files/list?path=outside-link*", reject_outside_link)
    _login(page, backend_url, auth_token)
    message = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.lang = 'zh';
          app.childCache = {};
          const node = {
            path: 'outside-link', name: 'outside-link', is_dir: true, depth: 0,
          };
          app.visible = [node];
          app.expanded = new Set();
          app.toasts = [];
          await app.expand(node);
          return app.toasts.at(-1)?.msg || '';
        }"""
    )
    assert "不在当前工作区或已添加的工作区中" in message
    assert "先把目标目录添加为工作区" in message
    assert "{\"detail\"" not in message


def test_chat_file_path_falls_back_to_unique_name_and_disambiguates(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const filename = 'f4_seq_features_16d_spec.md';
          const requested = `docs/${filename}`;
          const write = async (path, content) => {
            const response = await fetch('/api/files/write', {
              method: 'PUT',
              headers: {...app.fileHdr(), 'Content-Type': 'application/json'},
              body: JSON.stringify({path, content}),
            });
            if (!response.ok) throw new Error(await response.text());
          };

          app.toasts = [];
          const chooserCalls = [];
          const realChooseOne = app.chooseOne;
          app.chooseOne = async options => {
            chooserCalls.push(options.choices.map(choice => choice.value).sort());
            return options.choices.find(choice => choice.value.startsWith('src/'))?.value;
          };
          try {
            // The model supplied docs/<name>, while the only file lives under
            // a differently named directory. Unique basename is safe to open.
            await write(`test/${filename}`, 'UNIQUE_BASENAME_TARGET');
            await app.openByPathToasted(requested);
            const unique = {selected: app.selected, rawText: app.rawText};

            // Once another same-name file exists, never guess: expose both
            // complete paths and respect the explicit choice.
            await write(`src/${filename}`, 'DISAMBIGUATED_TARGET');
            await app.openByPathToasted(requested);
            const ambiguous = {selected: app.selected, rawText: app.rawText};

            // A full suffix match is stronger than basename-only candidates
            // and should open directly without another chooser.
            await write(`sandbox/docs/${filename}`, 'SUFFIX_TARGET');
            await app.openByPathToasted(requested);
            const suffix = {selected: app.selected, rawText: app.rawText};
            return {
              unique,
              ambiguous,
              suffix,
              chooserCalls,
              notFound: app.toasts.some(toast =>
                String(toast.msg || '').includes('文件不存在')
                || String(toast.msg || '').includes('Not found')),
            };
          } finally {
            app.chooseOne = realChooseOne;
          }
        }"""
    )

    assert result == {
        "unique": {
            "selected": "test/f4_seq_features_16d_spec.md",
            "rawText": "UNIQUE_BASENAME_TARGET",
        },
        "ambiguous": {
            "selected": "src/f4_seq_features_16d_spec.md",
            "rawText": "DISAMBIGUATED_TARGET",
        },
        "suffix": {
            "selected": "sandbox/docs/f4_seq_features_16d_spec.md",
            "rawText": "SUFFIX_TARGET",
        },
        "chooserCalls": [[
            "src/f4_seq_features_16d_spec.md",
            "test/f4_seq_features_16d_spec.md",
        ]],
        "notFound": False,
    }


def test_external_file_changes_refresh_tree_without_manual_reload(
        page: Page, backend_url, auth_token):
    """Direct API mutations stand in for Agent/terminal writes and deletes."""
    _login(page, backend_url, auth_token)
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app._fileEvents && app._fileEvents.readyState === EventSource.OPEN;
        }""",
        timeout=5000,
    )
    created = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const response = await fetch('/api/files/write', {
            method: 'PUT',
            headers: {...app.fileHdr(), 'Content-Type': 'application/json'},
            body: JSON.stringify({path: 'watch-live.txt', content: 'live\\n'}),
          });
          return response.ok;
        }"""
    )
    assert created
    page.wait_for_function(
        """() => document.querySelector('#app')._x_dataStack[0]
          .visible.some(node => node.path === 'watch-live.txt')""",
        timeout=5000,
    )

    deleted = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const response = await fetch('/api/files/delete', {
            method: 'DELETE',
            headers: {...app.fileHdr(), 'Content-Type': 'application/json'},
            body: JSON.stringify({path: 'watch-live.txt'}),
          });
          return response.ok;
        }"""
    )
    assert deleted
    page.wait_for_function(
        """() => !document.querySelector('#app')._x_dataStack[0]
          .visible.some(node => node.path === 'watch-live.txt')""",
        timeout=5000,
    )


def test_latest_file_open_owns_preview_and_network_failure_exits_loading(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const realFetch = window.fetch;
          const delayed = (body, delay, signal) => new Promise((resolve, reject) => {
            const timer = setTimeout(() => resolve(new Response(body, {status: 200})), delay);
            signal?.addEventListener('abort', () => {
              clearTimeout(timer);
              reject(new DOMException('Aborted', 'AbortError'));
            }, {once: true});
          });
          window.fetch = (url, init = {}) => {
            const s = String(url);
            if (s.includes('/api/files/read?path=race-a.txt')) {
              return delayed('OLD_A', 120, init.signal);
            }
            if (s.includes('/api/files/read?path=race-b.txt')) {
              return delayed('LATEST_B', 10, init.signal);
            }
            if (s.includes('/api/files/read?path=offline.txt')) {
              return Promise.reject(new TypeError('offline'));
            }
            return realFetch(url, init);
          };
          try {
            const first = app.openFile({path: 'race-a.txt', name: 'race-a.txt'});
            await new Promise(r => setTimeout(r, 5));
            const second = app.openFile({path: 'race-b.txt', name: 'race-b.txt'});
            await Promise.all([first, second]);
            const latest = {
              selected: app.selected, rawText: app.rawText,
              mode: app.previewMode, loading: app.previewMode === 'loading',
            };
            const ok = await app.openFile({path: 'offline.txt', name: 'offline.txt'});
            return {
              latest, ok, offlineMode: app.previewMode,
              offlineTitle: app.previewError?.title || '',
            };
          } finally {
            window.fetch = realFetch;
          }
        }"""
    )
    assert result["latest"] == {
        "selected": "race-b.txt",
        "rawText": "LATEST_B",
        "mode": "text",
        "loading": False,
    }
    assert result["ok"] is False
    assert result["offlineMode"] == "unsupported"
    assert result["offlineTitle"]


def test_terminal_surface_clicking_last_selected_file_tab_returns_to_file(
        page: Page, backend_url, auth_token):
    """The file underneath terminal remains selected but must still be clickable."""
    _login(page, backend_url, auth_token)
    page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.tabs = [];
          app._clearPreviewState();
          await app.openFile({path: 'notes/a.md', name: 'a.md'});
          await app.openFile({path: 'README.md', name: 'README.md'});
          app.previewSurface = 'terminal';
          await app.$nextTick();
        }"""
    )
    last_tab = page.locator('.pane.preview .tab[data-path="README.md"]')
    expect(last_tab).to_be_visible()
    last_tab.click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.previewSurface === 'file'
            && app.selected === 'README.md'
            && app.previewMode === 'md';
        }"""
    )


def test_rapid_csv_switch_aborts_old_page_and_commits_latest(page: Page,
                                                              backend_url,
                                                              auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const realFetch = window.fetch;
          const reply = (path, label, delay, signal) => new Promise((resolve, reject) => {
            const body = JSON.stringify({
              path, header: ['name'], rows: [[label]], offset: 0, limit: 200,
              total_rows: 1, has_header: true, delimiter: ',', cols_truncated: false,
            });
            const timer = setTimeout(() => resolve(new Response(body, {
              status: 200, headers: {'Content-Type': 'application/json'},
            })), delay);
            signal?.addEventListener('abort', () => {
              clearTimeout(timer);
              reject(new DOMException('Aborted', 'AbortError'));
            }, {once: true});
          });
          window.fetch = (url, init = {}) => {
            const s = String(url);
            if (s.includes('/api/files/csv?path=slow-a.csv')) {
              return reply('slow-a.csv', 'OLD', 120, init.signal);
            }
            if (s.includes('/api/files/csv?path=fast-b.csv')) {
              return reply('fast-b.csv', 'LATEST', 10, init.signal);
            }
            return realFetch(url, init);
          };
          try {
            const first = app.openFile({path: 'slow-a.csv', name: 'slow-a.csv'});
            await new Promise(r => setTimeout(r, 5));
            const second = app.openFile({path: 'fast-b.csv', name: 'fast-b.csv'});
            await Promise.all([first, second]);
            return {
              selected: app.selected, csvPath: app.csvPath,
              cell: app.csvData?.rows?.[0]?.[0], mode: app.previewMode,
              loading: app.csvLoading, offset: app.csvOffset,
            };
          } finally {
            window.fetch = realFetch;
          }
        }"""
    )
    assert result == {
        "selected": "fast-b.csv",
        "csvPath": "fast-b.csv",
        "cell": "LATEST",
        "mode": "csv",
        "loading": False,
        "offset": 0,
    }


def test_preview_tabs_restore_their_own_reading_positions(page: Page,
                                                           backend_url,
                                                           auth_token):
    """Switching files must restore each tab's shared-preview scroll owner."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const realFetch = window.fetch;
          const documents = {
            'scroll-a.txt': Array.from({length: 700}, (_, i) => `A line ${i}`).join('\\n'),
            'scroll-b.txt': Array.from({length: 700}, (_, i) => `B line ${i}`).join('\\n'),
          };
          const settle = () => new Promise(resolve => setTimeout(resolve, 320));
          window.fetch = (url, init = {}) => {
            const parsed = new URL(String(url), location.origin);
            if (parsed.pathname === '/api/files/read') {
              const path = parsed.searchParams.get('path');
              if (Object.prototype.hasOwnProperty.call(documents, path)) {
                return Promise.resolve(new Response(documents[path], {status: 200}));
              }
            }
            return realFetch(url, init);
          };
          try {
            app.tabs = [];
            app._clearPreviewState();
            await app.openFile({path: 'scroll-a.txt', name: 'scroll-a.txt'});
            await settle();
            const body = document.querySelector('.pane.preview .preview-body');
            const maxA = body.scrollHeight - body.clientHeight;
            body.scrollTop = Math.min(640, maxA);
            const savedA = body.scrollTop;

            await app.openFile({path: 'scroll-b.txt', name: 'scroll-b.txt'});
            await settle();
            const maxB = body.scrollHeight - body.clientHeight;
            body.scrollTop = Math.min(360, maxB);
            const savedB = body.scrollTop;

            await app.switchTab('scroll-a.txt');
            await settle();
            const restoredA = body.scrollTop;
            await app.switchTab('scroll-b.txt');
            await settle();
            const restoredB = body.scrollTop;
            return {
              maxA, maxB, savedA, savedB, restoredA, restoredB,
              viewA: app.tabs.find(t => t.path === 'scroll-a.txt')?.view?.scrollTop,
              viewB: app.tabs.find(t => t.path === 'scroll-b.txt')?.view?.scrollTop,
            };
          } finally {
            window.fetch = realFetch;
          }
        }"""
    )
    assert result["maxA"] > 640
    assert result["maxB"] > 360
    assert abs(result["restoredA"] - result["savedA"]) <= 2, result
    assert abs(result["restoredB"] - result["savedB"]) <= 2, result
    assert abs(result["viewA"] - result["savedA"]) <= 2, result
    assert abs(result["viewB"] - result["savedB"]) <= 2, result


def test_mobile_preview_restores_after_bottom_nav_and_keeps_tree_tabs(
    page: Page, backend_url, auth_token,
):
    """Mobile pane hiding must not turn a real scroll position into zero."""
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const realFetch = window.fetch;
          const documents = {
            'mobile-a.txt': Array.from({length: 700}, (_, i) => `A line ${i}`).join('\\n'),
            'mobile-b.txt': Array.from({length: 700}, (_, i) => `B line ${i}`).join('\\n'),
          };
          const settle = () => new Promise(resolve => setTimeout(resolve, 360));
          window.fetch = (url, init = {}) => {
            const parsed = new URL(String(url), location.origin);
            if (parsed.pathname === '/api/files/read') {
              const path = parsed.searchParams.get('path');
              if (Object.prototype.hasOwnProperty.call(documents, path)) {
                return Promise.resolve(new Response(documents[path], {status: 200}));
              }
            }
            return realFetch(url, init);
          };
          try {
            app.tabs = [];
            app._clearPreviewState();
            app.setMobileTab('files');
            await app.onNodeClick({}, {path: 'mobile-a.txt', name: 'mobile-a.txt'});
            await settle();
            const body = document.querySelector('.pane.preview .preview-body');
            body.scrollTop = 640;
            const savedA = body.scrollTop;

            app.setMobileTab('files');
            await settle();
            const hiddenTop = body.scrollTop;
            app.setMobileTab('preview');
            await settle();
            const restoredAfterNav = body.scrollTop;

            app.setMobileTab('files');
            await app.onNodeClick({}, {path: 'mobile-b.txt', name: 'mobile-b.txt'});
            await settle();
            body.scrollTop = 360;
            const savedB = body.scrollTop;
            await app.switchTab('mobile-a.txt');
            await settle();
            const restoredA = body.scrollTop;
            await app.switchTab('mobile-b.txt');
            await settle();
            const restoredB = body.scrollTop;
            return {
              savedA, savedB, hiddenTop, restoredAfterNav, restoredA, restoredB,
              tabs: app.tabs.map(t => ({path: t.path, preview: t.preview})),
            };
          } finally {
            window.fetch = realFetch;
          }
        }"""
    )
    assert result["hiddenTop"] == 0  # proves display:none really clamped the DOM
    assert result["restoredAfterNav"] == result["savedA"]
    assert result["restoredA"] == result["savedA"]
    assert result["restoredB"] == result["savedB"]
    assert result["tabs"] == [
        {"path": "mobile-a.txt", "preview": False},
        {"path": "mobile-b.txt", "preview": False},
    ]


def test_mobile_html_restore_overrides_report_smooth_scroll(
    page: Page, backend_url, auth_token,
):
    """A report's smooth-scroll CSS must not animate tab restoration."""
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.setMobileTab('files');
          await app.onNodeClick({}, {
            path: 'smooth-preview.html', name: 'smooth-preview.html', is_dir: false,
          });
        }"""
    )
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.selected === 'smooth-preview.html' && app.previewMode === 'html';
        }"""
    )
    iframe = page.locator(
        'iframe[data-preview-html-path="smooth-preview.html"]',
    )
    expect(iframe).to_be_visible(timeout=5000)
    page.wait_for_function(
        """() => {
          const frame = document.querySelector(
            'iframe[data-preview-html-path="smooth-preview.html"]');
          return frame && frame.src.includes('smooth-preview.html');
        }""",
        timeout=5000,
    )
    handle = iframe.element_handle()
    frame = handle.content_frame() if handle else None
    assert frame is not None
    frame.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(300)
    frame.evaluate("window.scrollTo({top: 1200, behavior: 'instant'})")
    page.wait_for_timeout(300)
    assert frame.evaluate("window.scrollY") == 1200

    page.evaluate("document.querySelector('#app')._x_dataStack[0].setMobileTab('files')")
    page.wait_for_timeout(80)
    frame.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
    page.wait_for_timeout(80)
    frame.evaluate(
        """() => {
          window.__muselabRestoreTrace = [];
          addEventListener('scroll', () => {
            window.__muselabRestoreTrace.push(window.scrollY);
          }, {passive: true});
        }"""
    )
    page.evaluate("document.querySelector('#app')._x_dataStack[0].setMobileTab('preview')")
    page.wait_for_timeout(300)
    result = frame.evaluate(
        """() => ({y: window.scrollY, trace: window.__muselabRestoreTrace})"""
    )
    assert result["y"] == 1200
    assert result["trace"]
    assert all(y == 1200 for y in result["trace"]), result


def test_html_preview_lru_reuses_four_live_frames_without_refetch(
    page: Page, backend_url, auth_token,
):
    """Recent reports keep their browsing contexts; the fifth evicts the LRU."""
    raw_requests: list[str] = []

    def record_raw_request(request) -> None:
        parsed = urlparse(request.url)
        if parsed.path != "/api/files/raw":
            return
        path = parse_qs(parsed.query).get("path", [""])[0]
        if path.startswith("cache-"):
            raw_requests.append(path)

    page.on("request", record_raw_request)
    _login(page, backend_url, auth_token)

    def open_report(path: str) -> None:
        page.evaluate(
            """async (path) => {
              const app = document.querySelector('#app')._x_dataStack[0];
              await app.openFile({path, name: path}, {preview: false});
            }""",
            path,
        )
        page.wait_for_function(
            """(path) => {
              const app = document.querySelector('#app')._x_dataStack[0];
              const frame = Array.from(document.querySelectorAll(
                'iframe[data-preview-html-path]'
              )).find(el => el.dataset.previewHtmlPath === path);
              return app.selected === path && app.previewMode === 'html'
                && frame && getComputedStyle(frame).display !== 'none';
            }""",
            arg=path,
        )
        page.wait_for_timeout(120)

    open_report("cache-0.html")
    first_frame = page.frame(url=lambda url: "path=cache-0.html" in url)
    assert first_frame is not None
    first_query = parse_qs(urlparse(first_frame.url).query)
    assert "token" not in first_query
    assert first_query.get("ticket", [""])[0].startswith("preview.")
    first_frame.wait_for_load_state("domcontentloaded")
    first_frame.locator("input").fill("kept-live-state")
    first_frame.evaluate("window.scrollTo({top: 700, behavior: 'instant'})")

    open_report("cache-1.html")
    open_report("cache-0.html")
    first_frame = page.frame(url=lambda url: "path=cache-0.html" in url)
    assert first_frame is not None
    assert first_frame.locator("input").input_value() == "kept-live-state"
    assert first_frame.evaluate("window.scrollY") == 700
    assert Counter(raw_requests)["cache-0.html"] == 1

    for i in (2, 3, 4):
        open_report(f"cache-{i}.html")

    residents = page.evaluate(
        """() => document.querySelector('#app')._x_dataStack[0]
          .htmlPreviewFrames.map(entry => entry.path)"""
    )
    assert residents == ["cache-0.html", "cache-2.html", "cache-3.html", "cache-4.html"]
    assert Counter(raw_requests)["cache-1.html"] == 1

    open_report("cache-1.html")
    residents = page.evaluate(
        """() => document.querySelector('#app')._x_dataStack[0]
          .htmlPreviewFrames.map(entry => entry.path)"""
    )
    assert residents == ["cache-2.html", "cache-3.html", "cache-4.html", "cache-1.html"]
    assert Counter(raw_requests)["cache-1.html"] == 2


def test_indexeddb_tree_hydrates_then_applies_owner_scoped_delta(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const cache = await import('/static/modules/persistent-cache.mjs');
          const suffix = `${Date.now()}-${Math.random()}`;
          const owner = `/persistent-tree-owner-${suffix}`;
          const other = `/persistent-tree-other-${suffix}`;
          const node = path => ({
            path, name: path.split('/').pop(), is_dir: false,
            size: 1, mtime: 1, depth: 0,
          });
          await cache.putWorkspaceSnapshot(owner, {
            showHidden: false, cursor: 1,
            visible: [node('stale.txt')], childCache: {}, expanded: [],
          });
          await cache.putWorkspaceSnapshot(other, {
            showHidden: false, cursor: 99,
            visible: [node('other-secret.txt')], childCache: {}, expanded: [],
          });
          app.activeWorkspace = owner;
          app.showHidden = false;
          app.visible = [];
          app.childCache = {};
          app.expanded = new Set();
          app._pendingExpanded = [];
          app._workspaceTreeCursors = new Map();
          const realFetch = window.fetch;
          const calls = [];
          window.fetch = async (url, init = {}) => {
            const parsed = new URL(String(url), location.origin);
            if (parsed.pathname === '/api/files/delta') {
              calls.push(`delta:${parsed.searchParams.get('cursor') || ''}`);
              return new Response(JSON.stringify({
                cursor: 2, has_more: false, resync: false,
                changes: [{seq: 2, type: 'added', ...node('fresh.txt')}],
              }), {status: 200, headers: {'Content-Type': 'application/json'}});
            }
            if (parsed.pathname === '/api/files/bootstrap') calls.push('bootstrap');
            if (parsed.pathname === '/api/files/list') calls.push('list');
            return realFetch(url, init);
          };
          try {
            const ok = await app.loadRoot();
            return {
              ok,
              calls,
              paths: app.visible.map(item => item.path).sort(),
              cursor: app._workspaceTreeCursors.get(owner),
              leaked: app.visible.some(item => item.path === 'other-secret.txt'),
            };
          } finally {
            window.fetch = realFetch;
            const pending = app._workspaceTreeCacheTimers.get(owner);
            if (pending?.timer) clearTimeout(pending.timer);
            if (pending?.idle != null && window.cancelIdleCallback) {
              window.cancelIdleCallback(pending.idle);
            }
            app._workspaceTreeCacheTimers.delete(owner);
          }
        }"""
    )
    assert result == {
        "ok": True,
        "calls": ["delta:1"],
        "paths": ["fresh.txt", "stale.txt"],
        "cursor": 2,
        "leaked": False,
    }


def test_workspace_remove_readd_rejects_old_path_generation(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const cache = await import('/static/modules/persistent-cache.mjs');
          const suffix = `${Date.now()}-${Math.random()}`;
          const owner = `/workspace-aba-${suffix}`;
          const primary = `/workspace-primary-${suffix}`;
          const oldId = `old-${suffix}`;
          const newId = `new-${suffix}`;
          const staleNode = {
            path: 'stale.txt', name: 'stale.txt', is_dir: false,
            size: 1, mtime: 1, depth: 0,
          };
          await cache.putWorkspaceSnapshot(owner, {
            workspaceId: oldId, showHidden: false, cursor: 41,
            visible: [staleNode], childCache: {}, expanded: [],
          });

          const originals = {
            fetch: window.fetch,
            persistentCache: app._persistentCache,
            confirm: app.confirm,
            changeWorkspace: app._changeWorkspaceSurface,
            fetchWorkspaces: app.fetchSessionWorkspaces,
            refreshSessions: app._refreshSessionsAfterWorkspaceRegistryChange,
            savePrefs: app.savePrefs,
          };
          let releaseRead;
          let readStartedResolve;
          let releaseChain;
          const readGate = new Promise(resolve => { releaseRead = resolve; });
          const readStarted = new Promise(resolve => { readStartedResolve = resolve; });
          const oldChain = new Promise(resolve => { releaseChain = resolve; });
          const facade = {
            ...cache,
            getWorkspaceSnapshot: async path => {
              const snapshot = await cache.getWorkspaceSnapshot(path);
              if (path === owner) {
                readStartedResolve();
                await readGate;
              }
              return snapshot;
            },
          };
          let registered = true;
          let staleOperationRan = false;
          let fileEventsClosed = false;
          const persistTimer = setTimeout(() => {}, 60_000);
          const retryTimer = setTimeout(() => {}, 60_000);
          const batchTimer = setTimeout(() => {}, 60_000);
          try {
            app._persistentCache = async () => facade;
            app.confirm = async () => true;
            app.savePrefs = () => {};
            app._refreshSessionsAfterWorkspaceRegistryChange = async () => true;
            app._changeWorkspaceSurface = async path => {
              app.activeWorkspace = path;
              return true;
            };
            app.fetchSessionWorkspaces = async () => {
              app.sessionWorkspaces = [
                {path: primary, name: 'primary', primary: true, id: 'primary-id'},
                ...(registered ? [{
                  path: owner, name: 'owner', primary: false,
                  id: registered === 'new' ? newId : oldId,
                }] : []),
              ];
              return true;
            };
            window.fetch = async (url, init = {}) => {
              const parsed = new URL(String(url), location.origin);
              if (parsed.pathname === '/api/chat/workspaces'
                  && init.method === 'DELETE') {
                registered = false;
                return new Response('{}', {status: 200});
              }
              if (parsed.pathname === '/api/chat/workspaces'
                  && init.method === 'POST') {
                registered = 'new';
                return new Response(JSON.stringify({
                  path: owner, name: 'owner', primary: false, id: newId,
                }), {status: 200, headers: {'Content-Type': 'application/json'}});
              }
              return originals.fetch(url, init);
            };

            app.sessionWorkspaces = [
              {path: primary, name: 'primary', primary: true, id: 'primary-id'},
              {path: owner, name: 'owner', primary: false, id: oldId},
            ];
            app.activeWorkspace = owner;
            app.visible = [];
            app.childCache = {};
            app.expanded = new Set();
            app._workspaceTreeCursors.set(owner, 41);
            app._workspaceRuntimeCaches.set(owner, {visible: [staleNode]});
            app._workspaceTreeCacheTimers.set(owner, {
              timer: persistTimer, idle: null, token: Symbol('old'),
            });
            app._workspaceSyncRetryTimers.set(owner, {
              timer: retryTimer, attempt: 0,
            });
            app._workspaceEventBatches.set(owner, {
              timer: batchTimer, payloads: [],
            });
            app._childFetches = new Map([[
              `${owner}${String.fromCharCode(0)}old-request`, Promise.resolve([]),
            ]]);
            app._fileEvents = {close: () => { fileEventsClosed = true; }};
            app._fileEventsWorkspace = owner;
            app._fileEventsGeneration = app._workspaceGeneration(owner);

            const treeSeq = app._treeLoadSeq;
            const oldGeneration = app._workspaceGeneration(owner);
            const staleHydrate = app._hydrateWorkspaceTree(
              owner, treeSeq, oldGeneration,
            );
            await readStarted;
            app._workspaceSyncChains.set(owner, oldChain);
            const staleTask = app._enqueueWorkspaceSync(
              owner,
              () => { staleOperationRan = true; return true; },
              oldGeneration,
            );

            await app.removeWorkspace(owner);
            const afterRemove = {
              runtime: app._workspaceRuntimeCaches.has(owner),
              cursor: app._workspaceTreeCursors.has(owner),
              persist: app._workspaceTreeCacheTimers.has(owner),
              retry: app._workspaceSyncRetryTimers.has(owner),
              batch: app._workspaceEventBatches.has(owner),
              chain: app._workspaceSyncChains.has(owner),
              child: Array.from(app._childFetches.keys()).some(
                key => key.startsWith(`${owner}${String.fromCharCode(0)}`)),
            };
            const entry = await app._registerWorkspacePath(owner);
            app.activeWorkspace = owner;
            app.visible = [];
            releaseRead();
            releaseChain();
            const hydrated = await staleHydrate;
            const staleTaskResult = await staleTask;
            let freshOperationRan = false;
            const freshTaskResult = await app._enqueueWorkspaceSync(
              owner,
              () => { freshOperationRan = true; return true; },
              app._workspaceGeneration(owner),
            );
            const stored = await cache.getWorkspaceSnapshot(owner);
            return {
              entryId: entry?.id,
              registryId: app._workspaceRegistryId(owner),
              generationChanged: app._workspaceGeneration(owner) !== oldGeneration,
              afterRemove,
              fileEventsClosed,
              hydrated,
              staleTaskResult,
              staleOperationRan,
              freshTaskResult,
              freshOperationRan,
              staleVisible: app.visible.some(node => node.path === 'stale.txt'),
              stored,
            };
          } finally {
            releaseRead();
            releaseChain();
            clearTimeout(persistTimer);
            clearTimeout(retryTimer);
            clearTimeout(batchTimer);
            app._clearWorkspaceSyncRetry(owner);
            app._clearWorkspaceEventBatch(owner);
            app._workspaceTreeCacheTimers.delete(owner);
            app._workspaceSyncChains.delete(owner);
            app._workspaceRuntimeCaches.delete(owner);
            app._workspaceTreeCursors.delete(owner);
            app._workspaceEpochs.delete(owner);
            await cache.deleteWorkspaceSnapshot(owner);
            window.fetch = originals.fetch;
            app._persistentCache = originals.persistentCache;
            app.confirm = originals.confirm;
            app._changeWorkspaceSurface = originals.changeWorkspace;
            app.fetchSessionWorkspaces = originals.fetchWorkspaces;
            app._refreshSessionsAfterWorkspaceRegistryChange = originals.refreshSessions;
            app.savePrefs = originals.savePrefs;
          }
        }"""
    )
    assert result["entryId"] == result["registryId"]
    assert result["registryId"].startswith("new-")
    assert result == {
        "entryId": result["registryId"],
        "registryId": result["entryId"],
        "generationChanged": True,
        "afterRemove": {
            "runtime": False,
            "cursor": False,
            "persist": False,
            "retry": False,
            "batch": False,
            "chain": False,
            "child": False,
        },
        "fileEventsClosed": True,
        "hydrated": False,
        "staleTaskResult": False,
        "staleOperationRan": False,
        "freshTaskResult": True,
        "freshOperationRan": True,
        "staleVisible": False,
        "stored": None,
    }


def test_sse_ready_workspace_id_mismatch_forces_cold_tree_recovery(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const cache = await import('/static/modules/persistent-cache.mjs');
          const suffix = `${Date.now()}-${Math.random()}`;
          const owner = `/workspace-sse-aba-${suffix}`;
          const primary = `/workspace-sse-primary-${suffix}`;
          const oldId = `old-${suffix}`;
          const newId = `new-${suffix}`;
          const staleNode = {
            path: 'stale.txt', name: 'stale.txt', is_dir: false,
            size: 1, mtime: 1, depth: 0,
          };
          const originals = {
            EventSource: window.EventSource,
            fetch: window.fetch,
            capabilities: app._fileCapabilities,
            visible: app._fileTreeIsVisible,
            loadRoot: app.loadRoot,
          };
          const streams = [];
          class FakeEventSource extends EventTarget {
            constructor(url) {
              super();
              this.url = url;
              this.readyState = 1;
              this.closed = false;
              streams.push(this);
            }
            close() {
              this.closed = true;
              this.readyState = 2;
            }
          }
          let coldLoad = false;
          let loadCalls = 0;
          try {
            app._stopFileEvents(false);
            window.EventSource = FakeEventSource;
            app._fileCapabilities = async () => ({
              mintTicket: async () => 'fake-ticket',
            });
            app._fileTreeIsVisible = () => true;
            window.fetch = async (url, init = {}) => {
              const parsed = new URL(String(url), location.origin);
              if (parsed.pathname === '/api/chat/workspaces'
                  && (!init.method || init.method === 'GET')) {
                return new Response(JSON.stringify({workspaces: [
                  {path: primary, name: 'primary', primary: true, id: 'primary-id'},
                  {path: owner, name: 'owner', primary: false, id: newId},
                ]}), {status: 200, headers: {'Content-Type': 'application/json'}});
              }
              return originals.fetch(url, init);
            };
            app.loadRoot = async options => {
              loadCalls += 1;
              coldLoad = options?.runtimeSnapshot === false
                && app.visible.length === 0
                && Object.keys(app.childCache).length === 0
                && !app._workspaceTreeCursors.has(owner)
                && !app._workspaceRuntimeCaches.has(owner);
              return true;
            };
            app.sessionWorkspaces = [
              {path: primary, name: 'primary', primary: true, id: 'primary-id'},
              {path: owner, name: 'owner', primary: false, id: oldId},
            ];
            app.activeWorkspace = owner;
            app.visible = [staleNode];
            app.childCache = {':false': [staleNode]};
            app.expanded = new Set();
            app._workspaceTreeCursors.set(owner, 0);
            app._workspaceRuntimeCaches.set(owner, {visible: [staleNode]});
            await cache.putWorkspaceSnapshot(owner, {
              workspaceId: oldId, showHidden: false, cursor: 0,
              visible: [staleNode], childCache: {}, expanded: [],
            });
            const oldGeneration = app._workspaceGeneration(owner);
            await app._startFileEvents();
            // Layout watchers from the shared page can race an additional
            // connection into the fake stream list. Exercise the stream that
            // actually owns app state instead of assuming streams[0] won.
            const first = app._fileEvents;
            if (!first) throw new Error('owner file-event stream did not start');
            first.dispatchEvent(new MessageEvent('ready', {
              data: JSON.stringify({
                ready: true, cursor: 0, workspace_id: newId,
              }),
            }));
            for (let i = 0; i < 100 && (
              !first.closed || loadCalls < 1
              || app._workspaceRegistryId(owner) !== newId
              || app._fileEvents === first
            ); i += 1) {
              await new Promise(resolve => setTimeout(resolve, 10));
            }
            const stored = await cache.getWorkspaceSnapshot(owner);
            return {
              streams: streams.length,
              firstClosed: first.closed,
              loadCalls,
              coldLoad,
              registryId: app._workspaceRegistryId(owner),
              generationChanged: app._workspaceGeneration(owner) !== oldGeneration,
              activeGeneration: app._fileEventsGeneration
                === app._workspaceGeneration(owner),
              staleVisible: app.visible.some(node => node.path === 'stale.txt'),
              stored,
            };
          } finally {
            app._stopFileEvents(false);
            app._workspaceRuntimeCaches.delete(owner);
            app._workspaceTreeCursors.delete(owner);
            app._workspaceEpochs.delete(owner);
            await cache.deleteWorkspaceSnapshot(owner);
            window.EventSource = originals.EventSource;
            window.fetch = originals.fetch;
            app._fileCapabilities = originals.capabilities;
            app._fileTreeIsVisible = originals.visible;
            app.loadRoot = originals.loadRoot;
          }
        }"""
    )
    # Layout watchers may race one extra same-generation reconnect, but the
    # stale stream must be replaced and the active stream must own the new id.
    assert result["streams"] >= 2
    assert result["firstClosed"] is True
    assert result["loadCalls"] == 1
    assert result["coldLoad"] is True
    assert result["registryId"].startswith("new-")
    assert result["generationChanged"] is True
    assert result["activeGeneration"] is True
    assert result["staleVisible"] is False
    assert result["stored"] is None


def test_warm_workspace_surface_does_not_wait_for_slow_delta(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const target = `/warm-workspace-${Date.now()}-${Math.random()}`;
          const cached = {
            path: 'cached.txt', name: 'cached.txt', is_dir: false,
            size: 1, mtime: 1, depth: 0,
          };
          app.workspaceSurfaces[target] = {previewSurface: 'file'};
          app._workspaceRuntimeCaches.set(target, {
            visible: [cached], childCache: {}, previewCache: [],
            previewCacheBytes: 0,
            trash: {items: [], count: 0, loading: false},
          });
          app._workspaceTreeCursors.set(target, 7);

          const realFetch = window.fetch;
          const originals = {
            fetchTerminals: app.fetchTerminals,
            loadTrash: app.loadTrash,
            fetchContextInfo: app.fetchContextInfo,
            startFileEvents: app._startFileEvents,
            savePrefs: app.savePrefs,
            persist: app._scheduleWorkspaceTreePersist,
            loadRoot: app.loadRoot,
            scheduleRetry: app._scheduleWorkspaceSyncRetry,
          };
          const calls = [];
          window.fetch = async (url, init = {}) => {
            const parsed = new URL(String(url), location.origin);
            if (parsed.pathname === '/api/files/delta') {
              calls.push(`delta:${parsed.searchParams.get('cursor') || ''}`);
              await new Promise(resolve => setTimeout(resolve, 800));
              return new Response(JSON.stringify({
                cursor: 7, has_more: false, resync: false, changes: [],
              }), {status: 200, headers: {'Content-Type': 'application/json'}});
            }
            if (parsed.pathname === '/api/files/bootstrap') calls.push('bootstrap');
            return realFetch(url, init);
          };
          app.fetchTerminals = async () => true;
          app.loadTrash = async () => true;
          app.fetchContextInfo = async () => true;
          app._startFileEvents = () => {};
          app.savePrefs = () => {};
          app._scheduleWorkspaceTreePersist = () => {};
          try {
            const started = performance.now();
            const ok = await app._changeWorkspaceSurface(target);
            const elapsed = performance.now() - started;
            const immediatePaths = app.visible.map(item => item.path);
            await new Promise(resolve => setTimeout(resolve, 900));

            const failedTarget = `${target}-failed`;
            app.workspaceSurfaces[failedTarget] = {previewSurface: 'file'};
            app._workspaceRuntimeCaches.set(failedTarget, {
              visible: [cached], childCache: {}, previewCache: [],
              previewCacheBytes: 0,
              trash: {items: [], count: 0, loading: false},
            });
            let retryCount = 0;
            let dirtyWhenEventsStarted = null;
            app.loadRoot = async () => false;
            app._scheduleWorkspaceSyncRetry = () => { retryCount += 1; };
            app._startFileEvents = () => {
              dirtyWhenEventsStarted = app._fileTreeDirty;
            };
            const failedOk = await app._changeWorkspaceSurface(failedTarget);
            await new Promise(resolve => setTimeout(resolve, 20));
            return {
              ok, elapsed, immediatePaths, calls,
              failure: {failedOk, retryCount, dirtyWhenEventsStarted},
            };
          } finally {
            window.fetch = realFetch;
            app.fetchTerminals = originals.fetchTerminals;
            app.loadTrash = originals.loadTrash;
            app.fetchContextInfo = originals.fetchContextInfo;
            app._startFileEvents = originals.startFileEvents;
            app.savePrefs = originals.savePrefs;
            app._scheduleWorkspaceTreePersist = originals.persist;
            app.loadRoot = originals.loadRoot;
            app._scheduleWorkspaceSyncRetry = originals.scheduleRetry;
          }
        }"""
    )
    assert result["ok"] is True
    assert result["elapsed"] < 400
    assert result["immediatePaths"] == ["cached.txt"]
    assert result["calls"] == ["delta:7"]
    assert result["failure"] == {
        "failedOk": True,
        "retryCount": 1,
        "dirtyWhenEventsStarted": True,
    }


def test_workspace_switch_only_waits_for_cold_tree_not_auxiliary_refreshes(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    # _bootApp intentionally no longer awaits its initial file bootstrap.
    # Settle that independent owner chain before timing a later user switch so
    # first-load DOM work is not charged to this path.
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return !app.treeLoading && app._workspaceSyncChains.size === 0;
        }""",
        timeout=5000,
    )
    page.evaluate(
        """() => new Promise(resolve =>
          requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
    )
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const originals = {
            loadRoot: app.loadRoot,
            loadTrash: app.loadTrash,
            fetchContextInfo: app.fetchContextInfo,
            fetchTerminals: app.fetchTerminals,
            startFileEvents: app._startFileEvents,
            savePrefs: app.savePrefs,
            persist: app._scheduleWorkspaceTreePersist,
          };
          let pending = new Map();
          const deferred = label => new Promise(resolve => {
            pending.set(label, resolve);
          });
          const finishPending = () => {
            for (const resolve of pending.values()) resolve(true);
            pending.clear();
          };
          app._startFileEvents = () => {};
          app.savePrefs = () => {};
          app._scheduleWorkspaceTreePersist = () => {};
          try {
            const coldTarget = `/cold-aux-${Date.now()}-${Math.random()}`;
            app.workspaceSurfaces[coldTarget] = {previewSurface: 'file'};
            let coldTreeCalls = 0;
            app.loadRoot = async () => {
              coldTreeCalls += 1;
              await new Promise(resolve => setTimeout(resolve, 25));
              return true;
            };
            app.loadTrash = () => deferred('trash');
            app.fetchContextInfo = () => deferred('context');
            app.fetchTerminals = () => deferred('terminals');
            const coldStarted = performance.now();
            const coldOk = await app._changeWorkspaceSurface(coldTarget);
            const cold = {
              ok: coldOk,
              elapsed: performance.now() - coldStarted,
              treeCalls: coldTreeCalls,
              pending: Array.from(pending.keys()).sort(),
              dirty: app._fileTreeDirty,
            };
            finishPending();
            await Promise.resolve();

            const terminalTarget = `${coldTarget}-terminal`;
            app.workspaceSurfaces[terminalTarget] = {
              previewSurface: 'terminal',
              activeTerminalId: 'terminal-slow',
            };
            app._workspaceRuntimeCaches.set(terminalTarget, {
              visible: [], childCache: {}, previewCache: [],
              previewCacheBytes: 0,
              trash: {items: [], count: 0, loading: false},
            });
            let finishRuntimeTree;
            app.loadRoot = () => new Promise(resolve => {
              finishRuntimeTree = resolve;
            });
            app.loadTrash = async () => true;
            app.fetchContextInfo = async () => true;
            app.fetchTerminals = () => deferred('runtime-terminals');
            const runtimeStarted = performance.now();
            const runtimeOk = await app._changeWorkspaceSurface(terminalTarget);
            const runtime = {
              ok: runtimeOk,
              elapsed: performance.now() - runtimeStarted,
              pending: Array.from(pending.keys()).sort(),
              surface: app.previewSurface,
              terminalId: app.activeTerminalId,
            };
            finishRuntimeTree(true);
            finishPending();
            await new Promise(resolve => setTimeout(resolve, 20));
            return {cold, runtime};
          } finally {
            finishPending();
            app.loadRoot = originals.loadRoot;
            app.loadTrash = originals.loadTrash;
            app.fetchContextInfo = originals.fetchContextInfo;
            app.fetchTerminals = originals.fetchTerminals;
            app._startFileEvents = originals.startFileEvents;
            app.savePrefs = originals.savePrefs;
            app._scheduleWorkspaceTreePersist = originals.persist;
          }
        }"""
    )
    assert result["cold"]["ok"] is True
    assert result["cold"]["elapsed"] < 500, result
    assert result["cold"]["treeCalls"] == 1
    assert result["cold"]["pending"] == ["context", "terminals", "trash"]
    assert result["cold"]["dirty"] is False
    assert result["runtime"]["ok"] is True
    assert result["runtime"]["elapsed"] < 400
    assert result["runtime"]["pending"] == ["runtime-terminals"]
    assert result["runtime"]["surface"] == "terminal"
    assert result["runtime"]["terminalId"] == "terminal-slow"


def test_workspace_compact_bootstrap_never_falls_back_to_unbounded_get(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const owner = app.fileWorkspacePath();
          const realFetch = window.fetch;
          const originalPersist = app._scheduleWorkspaceTreePersist;
          const originalExpanded = app.expanded;
          const originalVisible = app.visible;
          const originalChildCache = app.childCache;
          const originalShowHidden = app.showHidden;
          const calls = [];
          const node = (path, isDir = false) => ({
            path, name: path.split('/').pop(), is_dir: isDir,
            size: 1, mtime: 1, mtime_ns: 1,
          });
          app.showHidden = true;
          app.expanded = new Set(['src', 'src/deep']);
          app.visible = [];
          app.childCache = {};
          app._workspaceTreeCursors.delete(owner);
          app._scheduleWorkspaceTreePersist = () => {};
          window.fetch = async (url, init = {}) => {
            const parsed = new URL(String(url), location.origin);
            if (parsed.pathname !== '/api/files/bootstrap') {
              return realFetch(url, init);
            }
            const method = String(init.method || 'GET').toUpperCase();
            calls.push({
              method,
              query: parsed.search,
              body: init.body ? JSON.parse(init.body) : null,
              workspace: decodeURIComponent(
                new Headers(init.headers).get('X-Muselab-Workspace') || ''),
            });
            if (method === 'POST') return new Response('', {status: 405});
            return new Response(JSON.stringify({
              cursor: 9,
              entries: [
                node('src', true), node('.hidden'),
                node('src/deep', true), node('src/deep/file.txt'),
              ],
            }), {status: 200, headers: {'Content-Type': 'application/json'}});
          };
          try {
            const ok = await app._syncWorkspaceTree(
              owner, app._treeLoadSeq, false);
            return {
              ok, calls, owner,
              visible: app.visible.map(row => row.path),
              cursor: app._workspaceTreeCursors.get(owner),
            };
          } finally {
            window.fetch = realFetch;
            app._scheduleWorkspaceTreePersist = originalPersist;
            app.expanded = originalExpanded;
            app.visible = originalVisible;
            app.childCache = originalChildCache;
            app.showHidden = originalShowHidden;
          }
        }"""
    )
    assert result["ok"] is False
    assert {call["workspace"] for call in result["calls"]} == {result["owner"]}
    calls = [
        {key: value for key, value in call.items() if key != "workspace"}
        for call in result["calls"]
    ]
    assert calls == [
        {
            "method": "POST",
            "query": "",
            "body": {
                "show_hidden": True,
                "parents": ["src", "src/deep"],
            },
        },
    ]
    assert result["visible"] == []
    assert result["cursor"] is None


def test_terminal_foreground_still_persists_cursor_matched_tree_snapshot(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          // Use an isolated owner so unrelated boot/session filesystem events
          // cannot keep replacing this owner's debounce handle.
          const owner = `/terminal-persist-${Date.now()}-${Math.random()}`;
          const originals = {
            debounce: app.WORKSPACE_TREE_PERSIST_DEBOUNCE_MS,
            surface: app.previewSurface,
            requestIdle: window.requestIdleCallback,
            cancelIdle: window.cancelIdleCallback,
          };
          let stored = null;
          let idleCalls = 0;
          const cache = await app._persistentCache();
          app.previewSurface = 'terminal';
          const captured = {
            showHidden: false,
            cursor: 17,
            visible: [{
              path: 'src', name: 'src', is_dir: true,
              size: 0, mtime: 1, depth: 0,
            }],
            childCache: {
              ':false': [{
                path: 'src', name: 'src', is_dir: true,
                size: 0, mtime: 1,
              }],
              'src:false': [{
                path: 'src/a.txt', name: 'a.txt', is_dir: false,
                size: 1, mtime: 1,
              }],
            },
            expanded: ['src'],
          };
          app.WORKSPACE_TREE_PERSIST_DEBOUNCE_MS = 0;
          window.requestIdleCallback = callback => {
            idleCalls += 1;
            return setTimeout(callback, 0);
          };
          window.cancelIdleCallback = handle => clearTimeout(handle);
          try {
            app._scheduleWorkspaceTreePersist(owner, captured);
            const deadline = performance.now() + 4000;
            while (!stored && performance.now() < deadline) {
              await new Promise(resolve => setTimeout(resolve, 50));
              stored = await cache.getWorkspaceSnapshot(owner);
            }
            return {stored, idleCalls};
          } finally {
            app.WORKSPACE_TREE_PERSIST_DEBOUNCE_MS = originals.debounce;
            app.previewSurface = originals.surface;
            window.requestIdleCallback = originals.requestIdle;
            window.cancelIdleCallback = originals.cancelIdle;
          }
        }"""
    )
    assert result["stored"] is not None, result
    assert result["idleCalls"] >= 1
    snapshot = result["stored"]
    assert snapshot["cursor"] == 17
    assert snapshot["expanded"] == ["src"]
    assert set(snapshot["childCache"]) == {":false", "src:false"}


def test_workspace_sync_orders_snapshot_events_and_batches_linear_delta(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const owner = `/ordered-tree-${Date.now()}-${Math.random()}`;
          const node = (path, extra = {}) => ({
            path, name: path.split('/').pop(), is_dir: false,
            size: 1, mtime: 1, mtime_ns: 1, ...extra,
          });
          const realFetch = window.fetch;
          const originals = {
            persist: app._scheduleWorkspaceTreePersist,
            mobile: app._isMobileLayout,
            apply: app._applyWorkspaceEventPayload,
            sync: app._syncWorkspaceTree,
            retry: app._scheduleWorkspaceSyncRetry,
            mobileDelay: app.WORKSPACE_EVENT_BATCH_MOBILE_MS,
          };
          app.activeWorkspace = owner;
          app.showHidden = false;
          app.visible = [node('old.txt', {depth: 0})];
          app.childCache = {':false': [node('old.txt')]};
          app.expanded = new Set();
          app._pendingExpanded = [];
          app._workspaceTreeCursors.delete(owner);
          app._scheduleWorkspaceTreePersist = () => {};

          let bootstrapStarted = false;
          window.fetch = async (url, init = {}) => {
            const parsed = new URL(String(url), location.origin);
            if (parsed.pathname === '/api/files/bootstrap') {
              bootstrapStarted = true;
              await new Promise(resolve => setTimeout(resolve, 100));
              return new Response(JSON.stringify({
                cursor: 1, entries: [node('snapshot.txt')],
              }), {status: 200, headers: {'Content-Type': 'application/json'}});
            }
            return realFetch(url, init);
          };
          try {
            const loading = app.loadRoot();
            while (!bootstrapStarted) {
              await new Promise(resolve => setTimeout(resolve, 1));
            }
            const event = app._enqueueWorkspaceSync(
              owner,
              () => app._applyWorkspaceEventPayload({
                cursor: 2,
                changes: [{...node('late.txt'), type: 'added', seq: 2}],
              }, owner),
            );
            await Promise.all([loading, event]);
            const ordered = {
              paths: app.visible.map(row => row.path),
              cursor: app._workspaceTreeCursors.get(owner),
            };

            const files = Array.from({length: 20000}, (_, i) =>
              node(`file-${String(i).padStart(5, '0')}.txt`, {depth: 0}));
            const directory = node('z-dir', {is_dir: true, depth: 0});
            const child = node('z-dir/child.txt', {depth: 1});
            app.visible = [...files, directory, child];
            app.childCache = {
              ':false': [...files.map(({depth, ...row}) => row),
                (({depth, ...row}) => row)(directory)],
              'z-dir:false': [(({depth, ...row}) => row)(child)],
            };
            app.expanded = new Set(['z-dir']);
            app._pendingExpanded = ['z-dir'];
            const changes = Array.from({length: 1000}, (_, i) => {
              const changed = node(
                `file-${String(19000 + i).padStart(5, '0')}.txt`,
                {mtime: 2},
              );
              delete changed.mtime_ns;  // compatibility with an older event wire
              return {...changed, type: 'modified'};
            });
            changes.push({type: 'deleted', path: 'z-dir'});
            changes.push({...node('aaa-dir', {is_dir: true}), type: 'added'});
            const started = performance.now();
            const applied = app._applyFileTreeDelta(changes);
            const updated = app.visible.find(row => row.path === 'file-19999.txt');
            const linear = {
              applied,
              elapsed: performance.now() - started,
              first: app.visible[0]?.path,
              count: app.visible.length,
              deleted: app.visible.some(row => row.path.startsWith('z-dir')),
              expanded: Array.from(app.expanded),
              pendingExpanded: app._pendingExpanded,
              updatedMtime: updated?.mtime,
              updatedMtimeNs: updated?.mtime_ns,
            };

            const collapsed = node('collapsed', {is_dir: true, depth: 0});
            app.visible = [collapsed];
            app.childCache = {
              ':false': [(({depth, ...row}) => row)(collapsed)],
            };
            app.expanded = new Set();
            app._applyFileTreeDelta([
              {...node('collapsed/new.txt'), type: 'added'},
            ]);
            const partialCache = Object.prototype.hasOwnProperty.call(
              app.childCache, 'collapsed:false');

            app.visible = [];
            app.childCache = {':false': []};
            app._workspaceTreeCursors.set(owner, 0);
            app._workspaceEventBatches = new Map();
            app._workspaceSyncChains = new Map();
            const batches = [];
            app._applyWorkspaceEventPayload = async payload => {
              batches.push(payload);
              return true;
            };
            app._isMobileLayout = () => true;
            app.WORKSPACE_EVENT_BATCH_MOBILE_MS = 60;
            app._queueWorkspaceEventPayload({
              cursor: 1,
              changes: [{...node('one.txt'), type: 'added', seq: 1}],
            }, owner);
            app._queueWorkspaceEventPayload({
              cursor: 2,
              changes: [{...node('two.txt'), type: 'added', seq: 2}],
            }, owner);
            await new Promise(resolve => setTimeout(resolve, 20));
            const beforeBatchDeadline = batches.length;
            await new Promise(resolve => setTimeout(resolve, 80));
            const batched = {
              beforeBatchDeadline,
              calls: batches.length,
              changes: batches[0]?.changes.length,
              cursor: batches[0]?.cursor,
            };

            app._applyWorkspaceEventPayload = originals.apply;
            app._syncWorkspaceTree = async () => false;
            let retries = 0;
            app._scheduleWorkspaceSyncRetry = () => { retries += 1; };
            app._workspaceTreeCursors.set(owner, 2);
            const recovered = await originals.apply.call(
              app, {resync: true, changes: []}, owner);
            const failure = {
              recovered,
              retries,
              dirty: app._fileTreeDirty,
              hasCursor: app._workspaceTreeCursors.has(owner),
            };
            return {ordered, linear, partialCache, batched, failure};
          } finally {
            window.fetch = realFetch;
            app._clearWorkspaceEventBatch(owner);
            app._clearWorkspaceSyncRetry(owner);
            app._scheduleWorkspaceTreePersist = originals.persist;
            app._isMobileLayout = originals.mobile;
            app._applyWorkspaceEventPayload = originals.apply;
            app._syncWorkspaceTree = originals.sync;
            app._scheduleWorkspaceSyncRetry = originals.retry;
            app.WORKSPACE_EVENT_BATCH_MOBILE_MS = originals.mobileDelay;
          }
        }"""
    )
    assert result["ordered"] == {
        "paths": ["late.txt", "snapshot.txt"],
        "cursor": 2,
    }
    assert result["linear"]["applied"] is True
    assert result["linear"]["elapsed"] < 3000
    assert result["linear"]["first"] == "aaa-dir"
    assert result["linear"]["count"] == 20001
    assert result["linear"]["deleted"] is False
    assert result["linear"]["expanded"] == []
    assert result["linear"]["pendingExpanded"] == []
    assert result["linear"]["updatedMtime"] == 2
    assert result["linear"]["updatedMtimeNs"] == 0
    assert result["partialCache"] is False
    assert result["batched"] == {
        "beforeBatchDeadline": 0,
        "calls": 1,
        "changes": 2,
        "cursor": 2,
    }
    assert result["failure"] == {
        "recovered": False,
        "retries": 1,
        "dirty": True,
        "hasCursor": False,
    }


def test_tree_refresh_failure_keeps_rows_and_search_ignores_stale_results(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const realFetch = window.fetch;
          const delayedJson = (body, delay, signal) => new Promise((resolve, reject) => {
            const timer = setTimeout(() => resolve(new Response(JSON.stringify(body), {
              status: 200, headers: {'Content-Type': 'application/json'},
            })), delay);
            signal?.addEventListener('abort', () => {
              clearTimeout(timer);
              reject(new DOMException('Aborted', 'AbortError'));
            }, {once: true});
          });
          window.fetch = (url, init = {}) => {
            const s = String(url);
            if (s.includes('/api/files/search?q=alpha')) {
              return delayedJson({entries: [{path: 'old.txt', name: 'old.txt'}]}, 100, init.signal);
            }
            if (s.includes('/api/files/grep?q=alpha')) {
              return delayedJson({hits: []}, 100, init.signal);
            }
            if (s.includes('/api/files/search?q=beta')) {
              return delayedJson({entries: [{path: 'new.txt', name: 'new.txt'}]}, 5, init.signal);
            }
            if (s.includes('/api/files/grep?q=beta')) {
              return delayedJson({hits: [{path: 'new.txt', name: 'new.txt', line: 1}]}, 5, init.signal);
            }
            if (s.includes('/api/files/bootstrap') || s.includes('/api/files/delta')
                || s.includes('/api/files/list?path=')) {
              return Promise.reject(new TypeError('tree offline'));
            }
            return realFetch(url, init);
          };
          try {
            app.searchQ = 'alpha';
            const first = app.doSearch();
            await new Promise(r => setTimeout(r, 5));
            app.searchQ = 'beta';
            const second = app.doSearch();
            await Promise.all([first, second]);
            const search = {
              name: app.searchHits[0]?.name,
              grep: app.grepHits[0]?.name,
              searching: app.searching,
            };
            app.visible = [{path: 'keep.txt', name: 'keep.txt', is_dir: false, depth: 0}];
            const ok = await app.reloadTree();
            return {
              search, ok, paths: app.visible.map(n => n.path),
              treeLoading: app.treeLoading,
            };
          } finally {
            window.fetch = realFetch;
          }
        }"""
    )
    assert result["search"] == {
        "name": "new.txt", "grep": "new.txt", "searching": False,
    }
    assert result["ok"] is False
    assert result["paths"] == ["keep.txt"]
    assert result["treeLoading"] is False


def test_tree_refresh_restores_all_expanded_branches(page: Page,
                                                      backend_url,
                                                      auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const realFetch = window.fetch;
          const entry = (path, isDir) => ({
            path, name: path.split('/').pop(), is_dir: isDir,
            size: 0, mtime: 1,
          });
          const listings = {
            '': [entry('alpha', true), entry('beta', true)],
            'alpha': [entry('alpha/deep', true), entry('alpha/a.txt', false)],
            'alpha/deep': [entry('alpha/deep/x.txt', false)],
            'beta': [entry('beta/b.txt', false)],
          };
          const calls = [];
          window.fetch = (url, init = {}) => {
            const parsed = new URL(String(url), location.origin);
            if (parsed.pathname === '/api/files/bootstrap'
                || parsed.pathname === '/api/files/delta') {
              return Promise.resolve(new Response('', {status: 404}));
            }
            if (parsed.pathname === '/api/files/list') {
              const path = parsed.searchParams.get('path') || '';
              calls.push(path);
              return Promise.resolve(new Response(JSON.stringify({
                entries: listings[path] || [], truncated: false,
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return realFetch(url, init);
          };
          app.visible = [
            {...entry('alpha', true), depth: 0},
            {...entry('alpha/deep', true), depth: 1},
            {...entry('alpha/deep/x.txt', false), depth: 2},
            {...entry('alpha/a.txt', false), depth: 1},
            {...entry('beta', true), depth: 0},
            {...entry('beta/b.txt', false), depth: 1},
          ];
          app.expanded = new Set(['alpha', 'alpha/deep', 'beta']);
          try {
            const ok = await app.reloadTree();
            return {
              ok,
              expanded: Array.from(app.expanded).sort(),
              paths: app.visible.map(n => n.path),
              calls: calls.sort(),
            };
          } finally {
            window.fetch = realFetch;
          }
        }"""
    )
    assert result == {
        "ok": True,
        "expanded": ["alpha", "alpha/deep", "beta"],
        "paths": [
            "alpha", "alpha/deep", "alpha/deep/x.txt", "alpha/a.txt",
            "beta", "beta/b.txt",
        ],
        "calls": ["", "alpha", "alpha/deep", "beta"],
    }


def test_large_file_tree_mounts_only_the_viewport_window(page: Page,
                                                         backend_url,
                                                         auth_token):
    """Ten thousand logical rows must not become ten thousand Alpine nodes."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const started = performance.now();
          app.visible = Array.from({length: 10000}, (_, i) => ({
            path: `large/file-${String(i).padStart(5, '0')}.txt`,
            name: `file-${String(i).padStart(5, '0')}.txt`,
            is_dir: false, size: i, mtime: 1, depth: 1,
          }));
          app.fileTreeViewport = {start: 0, end: 80};
          app._scheduleFileTreeViewportSync(true);
          await new Promise(resolve => requestAnimationFrame(
            () => requestAnimationFrame(resolve)));
          const list = app.$refs.fileList;
          const firstMounted = list.querySelectorAll(':scope > li[data-path]').length;
          list.scrollTop = list.scrollHeight;
          app._syncFileTreeViewport(list);
          await new Promise(resolve => requestAnimationFrame(
            () => requestAnimationFrame(resolve)));
          return {
            elapsed: performance.now() - started,
            logical: app.visible.length,
            mounted: list.querySelectorAll(':scope > li[data-path]').length,
            firstMounted,
            lastMounted: !!list.querySelector(
              'li[data-path="large/file-09999.txt"]'),
          };
        }"""
    )
    assert result["logical"] == 10000
    assert 1 <= result["firstMounted"] <= 120
    assert 1 <= result["mounted"] <= 120
    assert result["lastMounted"] is True
    assert result["elapsed"] < 5000


def test_hidden_toggle_does_not_replay_expanded_directories(page: Page,
                                                            backend_url,
                                                            auth_token):
    _login(page, backend_url, auth_token)
    expect(page.locator(".pane.files .files-hidden-toggle")).to_be_visible()
    expect(page.locator(".pane.files .files-tools")).to_have_count(0)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const realFetch = window.fetch;
          const calls = [];
          window.fetch = (url, init = {}) => {
            const parsed = new URL(String(url), location.origin);
            if (parsed.pathname === '/api/files/bootstrap') {
              const payload = init.body ? JSON.parse(String(init.body)) : {};
              calls.push({
                endpoint: 'bootstrap',
                method: String(init.method || 'GET').toUpperCase(),
                showHidden: !!payload.show_hidden,
                parents: payload.parents || [],
              });
              return Promise.resolve(new Response(JSON.stringify({
                cursor: 1,
                entries: [{
                  path: '.hidden-root', name: '.hidden-root',
                  is_dir: false, size: 1, mtime: 1,
                }],
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (parsed.pathname === '/api/files/list') {
              calls.push(parsed.searchParams.get('path') || '');
              return Promise.resolve(new Response(JSON.stringify({
                entries: [{
                  path: '.hidden-root', name: '.hidden-root',
                  is_dir: false, size: 1, mtime: 1,
                }],
                truncated: false,
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return realFetch(url, init);
          };
          app.showHidden = false;
          app.expanded = new Set(['alpha', 'alpha/deep', 'beta']);
          app._pendingExpanded = Array.from(app.expanded);
          app.visible = [
            {path: 'alpha', name: 'alpha', is_dir: true, depth: 0},
            {path: 'alpha/deep', name: 'deep', is_dir: true, depth: 1},
            {path: 'beta', name: 'beta', is_dir: true, depth: 0},
          ];
          try {
            const ok = await app.toggleHidden();
            return {
              ok, calls, showHidden: app.showHidden,
              expanded: Array.from(app.expanded),
              paths: app.visible.map(node => node.path),
              scrollTop: app.$refs.fileList.scrollTop,
            };
          } finally {
            window.fetch = realFetch;
          }
        }"""
    )
    assert result == {
        "ok": True,
        "calls": [{
            "endpoint": "bootstrap",
            "method": "POST",
            "showHidden": True,
            "parents": [],
        }],
        "showHidden": True,
        "expanded": [],
        "paths": [".hidden-root"],
        "scrollTop": 0,
    }


def test_directory_remap_and_delete_update_descendant_preview_state(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.editing = false;
          app.tabs = [
            {path: 'dir/a.txt', name: 'a.txt', preview: false},
            {path: 'dir/sub/b.txt', name: 'b.txt', preview: false},
            {path: 'other.txt', name: 'other.txt', preview: false},
          ];
          app.selected = 'dir/sub/b.txt';
          app.treeFocusPath = 'dir/a.txt';
          app.selectedPaths = new Set(['dir/a.txt', 'dir/sub/b.txt']);
          app.fileClipboard = {path: 'dir/a.txt', name: 'a.txt'};
          app._previewNeedsReload = 'dir/sub/b.txt';
          const moved = app._remapPreviewPaths('dir', 'renamed');
          const remapped = {
            moved,
            tabs: app.tabs.map(t => t.path),
            selected: app.selected,
            focus: app.treeFocusPath,
            picked: Array.from(app.selectedPaths).sort(),
            clipboard: app.fileClipboard.path,
            needsReload: app._previewNeedsReload,
          };
          const realOpen = app.openFile;
          app.openFile = async (node) => {
            app.selected = node.path;
            app.previewMode = 'text';
            return true;
          };
          try {
            await app._dropPreviewPathsUnder(['renamed']);
          } finally {
            app.openFile = realOpen;
          }
          return {
            remapped,
            remaining: app.tabs.map(t => t.path),
            selectedAfterDelete: app.selected,
            pickedAfterDelete: Array.from(app.selectedPaths),
            needsReloadAfterDelete: app._previewNeedsReload,
          };
        }"""
    )
    assert result["remapped"] == {
        "moved": True,
        "tabs": ["renamed/a.txt", "renamed/sub/b.txt", "other.txt"],
        "selected": "renamed/sub/b.txt",
        "focus": "renamed/a.txt",
        "picked": ["renamed/a.txt", "renamed/sub/b.txt"],
        "clipboard": "renamed/a.txt",
        "needsReload": "renamed/sub/b.txt",
    }
    assert result["remaining"] == ["other.txt"]
    assert result["selectedAfterDelete"] == "other.txt"
    assert result["pickedAfterDelete"] == []
    assert result["needsReloadAfterDelete"] == ""


def test_save_keeps_edits_typed_while_write_is_in_flight(page: Page,
                                                          backend_url,
                                                          auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const realFetch = window.fetch;
          const realMount = app.mountCM;
          app.mountCM = () => {};
          app.tabs = [{path: 'typing.txt', name: 'typing.txt', preview: false}];
          app.selected = 'typing.txt';
          app.previewMode = 'text';
          app.previewLang = 'plaintext';
          app.rawText = 'disk-before';
          app.editText = 'sent-version';
          app._cm = null;
          app.editing = true;
          let finish;
          let sentBody = null;
          window.fetch = (url, init = {}) => {
            if (String(url).includes('/api/files/write')) {
              sentBody = JSON.parse(init.body);
              return new Promise(resolve => { finish = () => resolve(new Response('{}', {status: 200})); });
            }
            return realFetch(url, init);
          };
          try {
            const saving = app.saveEdit();
            while (!finish) await new Promise(r => setTimeout(r, 0));
            app.editText = 'typed-after-click';
            finish();
            await saving;
            return {
              sentBody, editing: app.editing, rawText: app.rawText,
              editText: app.editText, dirty: app.cmStatus.dirty,
            };
          } finally {
            window.fetch = realFetch;
            app.mountCM = realMount;
            app.editing = false;
          }
        }"""
    )
    assert result["sentBody"] == {"path": "typing.txt", "content": "sent-version"}
    assert result["editing"] is True
    assert result["rawText"] == "sent-version"
    assert result["editText"] == "typed-after-click"
    assert result["dirty"] is True


def test_reopening_current_file_does_not_discard_editor_buffer(page: Page,
                                                               backend_url,
                                                               auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.tabs = [{path: 'draft.txt', name: 'draft.txt', preview: true}];
          app.selected = 'draft.txt';
          app.treeFocusPath = 'draft.txt';
          app.previewMode = 'text';
          app.rawText = 'saved';
          app.editText = 'unsaved draft';
          app.editing = true;
          app._cm = null;
          app.cmStatus = {...app.cmStatus, dirty: true};
          let confirms = 0;
          const realConfirm = window.confirm;
          window.confirm = () => { confirms += 1; return true; };
          try {
            const ok = await app.openFile(
              {path: 'draft.txt', name: 'draft.txt'}, {preview: false});
            return {
              ok, confirms, editing: app.editing, editText: app.editText,
              dirty: app.cmStatus.dirty,
              pinned: app.tabs.find(t => t.path === 'draft.txt')?.preview === false,
            };
          } finally {
            window.confirm = realConfirm;
            app.editing = false;
          }
        }"""
    )
    assert result == {
        "ok": True,
        "confirms": 0,
        "editing": True,
        "editText": "unsaved draft",
        "dirty": True,
        "pinned": True,
    }


def test_cancelled_dirty_switch_preserves_hidden_layout_and_terminal(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.selected = 'draft.txt';
          app.editing = true;
          app.previewOpen = false;
          app.desktopFullPane = 'chat';
          app.previewSurface = 'terminal';
          let teardowns = 0;
          const realConfirm = app._confirmLoseEdits;
          const realTeardown = app._teardownTerminalView;
          app._confirmLoseEdits = () => false;
          app._teardownTerminalView = () => { teardowns += 1; };
          try {
            const ok = await app.openFile(
              {path: 'other.txt', name: 'other.txt'}, {reveal: true});
            return {
              ok, teardowns, selected: app.selected,
              previewOpen: app.previewOpen,
              desktopFullPane: app.desktopFullPane,
              previewSurface: app.previewSurface,
            };
          } finally {
            app._confirmLoseEdits = realConfirm;
            app._teardownTerminalView = realTeardown;
            app.editing = false;
          }
        }"""
    )
    assert result == {
        "ok": False,
        "teardowns": 0,
        "selected": "draft.txt",
        "previewOpen": False,
        "desktopFullPane": "chat",
        "previewSurface": "terminal",
    }


def test_mobile_reopening_current_editor_reveals_preview_without_discarding(
        page: Page, backend_url, auth_token):
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.tabs = [{path: 'draft.txt', name: 'draft.txt', preview: false}];
          app.selected = 'draft.txt';
          app.previewMode = 'text';
          app.editText = 'unsaved mobile draft';
          app.editing = true;
          app.cmStatus = {...app.cmStatus, dirty: true};
          app.mobileTab = 'files';
          const ok = await app.openFile(
            {path: 'draft.txt', name: 'draft.txt'}, {reveal: true});
          const state = {
            ok, mobileTab: app.mobileTab, editing: app.editing,
            editText: app.editText, dirty: app.cmStatus.dirty,
          };
          app.editing = false;
          return state;
        }"""
    )
    assert result == {
        "ok": True,
        "mobileTab": "preview",
        "editing": True,
        "editText": "unsaved mobile draft",
        "dirty": True,
    }


def test_upload_completion_keeps_text_typed_after_overwrite_confirmation(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.tabs = [{path: 'upload.txt', name: 'upload.txt', preview: false}];
          app.selected = 'upload.txt';
          app.previewMode = 'text';
          app.rawText = 'disk-before';
          app.editText = 'confirmed-version';
          app.editing = true;
          app._cm = null;
          app.cmStatus = {...app.cmStatus, dirty: true};
          const realConfirm = window.confirm;
          const realOpen = app.openFile;
          window.confirm = () => true;
          try {
            const context = app._prepareUploadOverwrite('', [
              {name: 'upload.txt'},
            ]);
            app.editText = 'typed-during-upload';
            await app._syncUploadedFiles([{
              status: 'fulfilled',
              value: {path: 'upload.txt', replaced_trash_id: null},
            }], context);
            const preserved = {
              editing: app.editing, editText: app.editText,
              dirty: app.cmStatus.dirty,
              needsReload: app._previewNeedsReload,
            };
            let reload = null;
            app.openFile = async (node, opts) => {
              reload = {path: node.path, forceReload: !!opts.forceReload};
              return true;
            };
            await app.toggleEdit();
            return {
              preserved, editingAfterExit: app.editing, reload,
            };
          } finally {
            window.confirm = realConfirm;
            app.openFile = realOpen;
            app.editing = false;
          }
        }"""
    )
    assert result == {
        "preserved": {
            "editing": True,
            "editText": "typed-during-upload",
            "dirty": True,
            "needsReload": "upload.txt",
        },
        "editingAfterExit": False,
        "reload": {"path": "upload.txt", "forceReload": True},
    }
