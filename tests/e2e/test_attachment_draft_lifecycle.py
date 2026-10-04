"""Attachment preparation, cancellation and batches keep their original draft."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from .test_files_preview import _install_fake_mux_chat_transport
from .test_multi_tab import _activate_chat_tab, _login

APP = "document.querySelector('#app')._x_dataStack[0]"
FILE_INPUT = 'input[type="file"][x-ref="attachInput"]'


def _safe_send(page, backend_url, auth_token):
    turns = []
    _install_fake_mux_chat_transport(page, turns)
    _login(page, backend_url, auth_token)
    page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      app.availableModels = [{model:'e2e-model', label:'E2E', group:'e2e'}];
      app.model = 'e2e-model';
      app._confirmSessionBusy = async () => false;
      app._awaitRuntimeSettingPatches = async () => true;
    }""")
    return turns


def _pick(page, kind="doc", names=None):
    names = names or (["photo.png"] if kind == "image" else ["note.txt"])
    page.locator(FILE_INPUT).set_input_files([
        {"name": name, "mimeType": "image/png" if kind == "image" else "text/plain",
         "buffer": b"synthetic attachment fixture"}
        for name in names
    ])


@pytest.mark.parametrize("source", ["picked", "paste"])
def test_selected_batch_reserves_all_chips_and_keeps_starting_tab(
        page, backend_url, auth_token, source):
    _login(page, backend_url, auth_token)
    sid_a = page.evaluate(APP + ".currentId")
    page.locator(".chat-tab-new").click()
    sid_b = page.evaluate(APP + ".currentId")
    _activate_chat_tab(page, sid_a)
    page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      window.__uploadCalls = [];
      app._uploadAttachment = fd => {
        const name = fd.get('file').name;
        window.__uploadCalls.push(name);
        const response = () => new Response(JSON.stringify({id:name, kind:'text'}));
        if (window.__uploadCalls.length === 1) {
          return new Promise(resolve => { window.__releaseUpload = () => resolve(response()); });
        }
        return Promise.resolve(response());
      };
    }""")
    if source == "picked":
        _pick(page, names=["first.txt", "second.txt"])
    else:
        page.evaluate("""() => {
          const transfer = new DataTransfer();
          for (const name of ['first.txt', 'second.txt']) {
            transfer.items.add(new File(['synthetic fixture'], name, {type:'text/plain'}));
          }
          document.querySelector('.chat-input-textarea').dispatchEvent(
            new ClipboardEvent('paste', {clipboardData:transfer, bubbles:true, cancelable:true}));
        }""")
    page.wait_for_function("window.__uploadCalls.length === 1")
    reserved = page.evaluate(APP + ".pendingDocs.length")
    _activate_chat_tab(page, sid_b)
    page.locator(".chat-input-textarea").fill("other tab draft")
    page.evaluate("window.__releaseUpload()")
    page.wait_for_function("window.__uploadCalls.length === 2")
    page.wait_for_function("""() => Object.values(document.querySelector('#app')._x_dataStack[0]
      .tabState).every(st => st.draft.pendingDocs.every(doc => !doc.uploading))""")
    result = page.evaluate("""([a,b]) => {
      const app = document.querySelector('#app')._x_dataStack[0];
      return {a:app.tabState[a].draft.pendingDocs.map(doc => doc.id),
        b:app.tabState[b].draft.pendingDocs.map(doc => doc.id), input:app.input};
    }""", [sid_a, sid_b])
    assert result == {"a": ["first.txt", "second.txt"], "b": [], "input": "other tab draft"}
    assert reserved == 2


@pytest.mark.parametrize("phase", ["compression", "thumbnail"])
def test_send_waits_for_image_preparation_and_preserves_later_draft(
        page, backend_url, auth_token, phase):
    turns = _safe_send(page, backend_url, auth_token)
    page.evaluate("""phase => {
      const app = document.querySelector('#app')._x_dataStack[0];
      app._maybeCompressImage = async file => file;
      app._imageToThumbDataURL = async () => '';
      if (phase === 'compression') {
        app._maybeCompressImage = file => new Promise(resolve => {
          window.__releasePreparation = () => resolve(file);
        });
      } else {
        app._imageToThumbDataURL = () => new Promise(resolve => {
          window.__releasePreparation = () => resolve('');
        });
      }
      app._uploadAttachment = async () => new Response(JSON.stringify({id:'prepared-image', attach_ext:'png'}));
    }""", phase)
    _pick(page, "image")
    page.wait_for_function("!!window.__releasePreparation")
    page.locator(".chat-input-textarea").fill("send with photo")
    page.locator(".chat-input-textarea").press("Enter")
    page.wait_for_function("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      const st = app.tabState[app.currentId];
      return st.draft._sendWaitingForUpload || st.streaming;
    }""")
    before = page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      return {waiting:app._sendWaitingForUpload, chips:app.pendingImages.length};
    }""")
    early_turns = list(turns)
    # A later edit belongs to the next draft, even when the earlier send waits.
    page.locator(".chat-input-textarea").fill("later draft")
    page.evaluate("window.__releasePreparation()")
    page.wait_for_function(APP + ".tabState[" + APP + ".currentId].streaming")
    assert before == {"waiting": True, "chips": 1}
    assert early_turns == []
    assert len(turns) == 1
    assert turns[0]["image_ids"] == "prepared-image"
    assert turns[0]["prompt"] == "send with photo"
    expect(page.locator(".chat-input-textarea")).to_have_value("later draft")
    assert page.evaluate(APP + "._sendWaitingForUpload") is False


@pytest.mark.parametrize("kind", ["doc", "image"])
@pytest.mark.parametrize("with_text", [False, True])
def test_remove_uploading_chip_cancels_transfer_and_releases_send(
        page, backend_url, auth_token, kind, with_text):
    turns = _safe_send(page, backend_url, auth_token)
    page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      app._maybeCompressImage = async file => file;
      app._imageToThumbDataURL = async () => '';
      window.__uploadAborted = false;
      app._uploadAttachment = (_fd, {signal}) => new Promise((resolve, reject) => {
        window.__uploadStarted = true;
        signal.addEventListener('abort', () => {
          window.__uploadAborted = true;
          reject(new DOMException('synthetic cancellation', 'AbortError'));
        }, {once:true});
      });
    }""")
    _pick(page, kind)
    page.wait_for_function("window.__uploadStarted")
    if with_text:
        page.locator(".chat-input-textarea").fill("send without removed file")
    page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      window.__sendSettled = false;
      app.send().finally(() => { window.__sendSettled = true; });
    }""")
    page.wait_for_function(APP + "._sendWaitingForUpload")
    page.locator(".chat-input-textarea").fill("later draft after cancellation")
    page.locator(".img-chip-x" if kind == "image" else ".doc-chip-x").click()
    # Sample both outcomes together: old code keeps the removed upload and wait alive.
    page.wait_for_timeout(250)
    assert page.evaluate("window.__uploadAborted") is True
    page.wait_for_function("window.__sendSettled", timeout=3000)
    assert page.evaluate(APP + "._sendWaitingForUpload") is False
    assert not page.evaluate(APP + ".tabState[" + APP + ".currentId]._composerSubmitToken")
    if with_text:
        assert len(turns) == 1
        assert turns[0]["prompt"] == "send without removed file"
        assert turns[0]["image_ids"] == ""
    else:
        assert turns == []
    expect(page.locator(".chat-input-textarea")).to_have_value("later draft after cancellation")
    assert page.locator(".toast").filter(has_text="附件上传失败").count() == 0



def _hold_first_upload(page):
    page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      window.__uploadCalls = [];
      window.__uploadAborted = false;
      app._uploadAttachment = (fd, {signal}) => {
        const name = fd.get('file').name;
        window.__uploadCalls.push(name);
        const response = () => new Response(JSON.stringify({id:name, kind:'text'}));
        if (window.__uploadCalls.length !== 1) return Promise.resolve(response());
        return new Promise((resolve,reject) => {
          window.__releaseUpload = () => resolve(response());
          signal.addEventListener('abort', () => {
            window.__uploadAborted = true;
            reject(new DOMException('synthetic cancellation', 'AbortError'));
          }, {once:true});
        });
      };
    }""")


def test_send_includes_whole_batch_and_keeps_later_attachment(
        page, backend_url, auth_token):
    turns = _safe_send(page, backend_url, auth_token)
    _hold_first_upload(page)
    _pick(page, names=["first.txt", "second.txt"])
    page.wait_for_function("window.__uploadCalls.length === 1")
    page.locator(".chat-input-textarea").fill("batch prompt")
    page.evaluate("() => { document.querySelector('#app')._x_dataStack[0].send(); }")
    page.wait_for_function(APP + "._sendWaitingForUpload")
    _pick(page, names=["later.txt"])
    page.wait_for_function(APP + ".pendingDocs.some(doc => doc.id === 'later.txt')")
    page.evaluate("window.__releaseUpload()")
    page.wait_for_function(APP + ".tabState[" + APP + ".currentId].streaming")
    assert len(turns) == 1
    assert turns[0]["image_ids"] == "first.txt,second.txt"
    assert turns[0]["prompt"] == "batch prompt"
    assert page.evaluate(APP + ".pendingDocs.map(doc => doc.id)") == ["later.txt"]
    expect(page.locator(".chat-input-textarea")).to_have_value("")
    assert page.evaluate(APP + ".tabState[" + APP + ".currentId].draft._uploadControllers.size") == 0


def test_removing_queued_batch_item_skips_its_transfer_without_canceling_first(
        page, backend_url, auth_token):
    turns = _safe_send(page, backend_url, auth_token)
    _hold_first_upload(page)
    _pick(page, names=["first.txt", "second.txt"])
    expect(page.locator(".doc-chip")).to_have_count(2)
    page.locator(".chat-input-textarea").fill("only first remains")
    page.evaluate("() => { document.querySelector('#app')._x_dataStack[0].send(); }")
    page.wait_for_function(APP + "._sendWaitingForUpload")
    page.locator(".doc-chip-x").nth(1).click()
    assert page.evaluate("window.__uploadAborted") is False
    page.evaluate("window.__releaseUpload()")
    page.wait_for_function(APP + ".tabState[" + APP + ".currentId].streaming")
    assert turns[0]["image_ids"] == "first.txt"
    assert page.evaluate("window.__uploadCalls") == ["first.txt"]
    assert page.evaluate(APP + ".tabState[" + APP + ".currentId].draft._uploadControllers.size") == 0


def test_closing_batch_owner_cancels_upload_and_does_not_recreate_draft(
        page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    sid_a = page.evaluate(APP + ".currentId")
    page.locator(".chat-tab-new").click()
    sid_b = page.evaluate(APP + ".currentId")
    _activate_chat_tab(page, sid_a)
    _hold_first_upload(page)
    _pick(page, names=["first.txt", "second.txt"])
    page.wait_for_function("window.__uploadCalls.length === 1")
    _activate_chat_tab(page, sid_b)
    page.locator(f'.chat-tab[data-tid="{sid_a}"] .chat-tab-close').click()
    page.wait_for_function("window.__uploadAborted")
    assert page.evaluate(APP + ".currentId") == sid_b
    assert page.evaluate("window.__uploadCalls") == ["first.txt"]
    assert page.evaluate(APP + ".pendingDocs.length") == 0
    assert not page.evaluate("sid => !!document.querySelector('#app')._x_dataStack[0].tabState[sid]", sid_a)


@pytest.mark.parametrize("phase", ["compression", "thumbnail"])
def test_cancel_during_image_preparation_never_starts_transfer(
        page, backend_url, auth_token, phase):
    _login(page, backend_url, auth_token)
    page.evaluate("""phase => {
      const app = document.querySelector('#app')._x_dataStack[0];
      app._maybeCompressImage = async file => file;
      app._imageToThumbDataURL = async () => '';
      if (phase === 'compression') {
        app._maybeCompressImage = file => new Promise(resolve => {
          window.__releasePreparation = () => resolve(file);
        });
      } else {
        app._imageToThumbDataURL = () => new Promise(resolve => {
          window.__releasePreparation = () => resolve('');
        });
      }
      window.__uploadCalls = 0;
      app._uploadAttachment = async () => {
        window.__uploadCalls++;
        return new Response(JSON.stringify({id:'unexpected-upload'}));
      };
    }""", phase)
    _pick(page, "image")
    page.wait_for_function("!!window.__releasePreparation")
    expect(page.locator(".img-chip")).to_have_count(1)
    page.locator(".img-chip-x").click()
    page.evaluate("window.__releasePreparation()")
    page.wait_for_function(APP + ".tabState[" + APP + ".currentId].draft._uploadControllers.size === 0")
    assert page.evaluate("window.__uploadCalls") == 0
    expect(page.locator(".img-chip")).to_have_count(0)



@pytest.mark.parametrize("rejected", ["size", "type"])
def test_rejected_middle_item_keeps_valid_image_preparation_serial(
        page, backend_url, auth_token, rejected):
    _login(page, backend_url, auth_token)
    page.evaluate("""rejected => {
      const app = document.querySelector('#app')._x_dataStack[0];
      window.__preparedNames = [];
      window.__uploadedNames = [];
      const classify = app._classifyFile.bind(app);
      app._classifyFile = file => file.name === 'rejected.png' && rejected === 'type'
        ? 'unknown' : classify(file);
      app._maybeCompressImage = file => {
        window.__preparedNames.push(file.name);
        if (file.name === 'first.png') {
          return new Promise(resolve => { window.__releasePreparation = () => resolve(file); });
        }
        return Promise.resolve(file);
      };
      app._imageToThumbDataURL = async () => '';
      app._uploadAttachment = async fd => {
        const name = fd.get('file').name;
        window.__uploadedNames.push(name);
        return new Response(JSON.stringify({id:name, attach_ext:'png'}));
      };
      const transfer = new DataTransfer();
      transfer.items.add(new File(['synthetic fixture'], 'first.png', {type:'image/png'}));
      transfer.items.add(new File([
        rejected === 'size' ? new Uint8Array(10*1024*1024+1) : 'synthetic fixture'
      ], 'rejected.png', {type:'image/png'}));
      transfer.items.add(new File(['synthetic fixture'], 'last.png', {type:'image/png'}));
      const input = document.querySelector('input[x-ref="attachInput"]');
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', {bubbles:true}));
    }""", rejected)
    page.wait_for_function("!!window.__releasePreparation")
    page.wait_for_timeout(100)
    before = page.evaluate("({prepared:window.__preparedNames.slice(), uploaded:window.__uploadedNames.slice()})")
    page.evaluate("window.__releasePreparation()")
    page.wait_for_function(APP + ".tabState[" + APP + ".currentId].draft._uploadControllers.size === 0")
    assert before == {"prepared": ["first.png"], "uploaded": []}
    assert page.evaluate("window.__uploadedNames") == ["first.png", "last.png"]
    assert page.evaluate(APP + ".pendingImages.map(image => image.id)") == ["first.png", "last.png"]
