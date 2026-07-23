"""Session metadata excludes muselab-owned system prompts."""

import json


def test_new_session_has_no_system_prompt(app_module):
    from backend import sessions as sess
    meta = sess.create_session("native-session", "claude-sonnet-4-6")
    s = sess.get_session(meta["id"])
    assert "system_prompt" not in meta
    assert "system_prompt" not in s


def test_legacy_system_prompt_is_inert_and_hidden(app_module, monkeypatch):
    from backend import sessions as sess
    from backend.settings import atomic_write_text
    meta = sess.create_session("legacy")
    rows = json.loads(sess.INDEX.read_text(encoding="utf-8"))
    rows[0]["system_prompt"] = "old custom instructions"
    atomic_write_text(sess.INDEX, json.dumps(rows))
    sess._META_CACHE.clear()
    sess.invalidate_sessions_cache()
    monkeypatch.setattr(sess, "sdk_get_session_info", lambda *_a, **_kw: None)
    assert "system_prompt" not in sess.get_session(meta["id"])
    assert "system_prompt" not in sess.list_sessions()[0]


def test_patch_ignores_removed_system_prompt_field(client, auth):
    r = client.post("/api/chat/sessions", headers=auth, json={"name": "p"})
    sid = r.json()["id"]
    r = client.patch(
        f"/api/chat/sessions/{sid}",
        headers=auth,
        json={"system_prompt": "you are a poet"},
    )
    assert r.status_code == 404
    r = client.get(f"/api/chat/sessions/{sid}", headers=auth)
    assert "system_prompt" not in r.json()


def test_valid_patch_does_not_restore_system_prompt(client, auth):
    r = client.post("/api/chat/sessions", headers=auth, json={"name": "p"})
    sid = r.json()["id"]
    r = client.patch(
        f"/api/chat/sessions/{sid}",
        headers=auth,
        json={"name": "renamed", "system_prompt": "ignored"},
    )
    assert r.status_code == 200
    r = client.get(f"/api/chat/sessions/{sid}", headers=auth)
    assert r.json()["name"] == "renamed"
    assert "system_prompt" not in r.json()
