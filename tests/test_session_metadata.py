"""Session metadata excludes muselab-owned system prompts."""

import json
from types import SimpleNamespace


def _sdk_info(sid: str):
    return SimpleNamespace(
        session_id=sid,
        custom_title=None,
        first_prompt="hello",
        created_at=1_000,
        last_modified=2_000,
        tag=None,
    )


def test_new_session_has_no_system_prompt(app_module):
    from backend import sessions as sess
    meta = sess.create_session("native-session", "claude-sonnet-4-6")
    s = sess.get_session(meta["id"])
    assert "system_prompt" not in meta
    assert "system_prompt" not in s


def test_session_state_files_are_private(app_module):
    from backend import sessions as sess

    legacy_dir = sess.SESS_DIR / "legacy"
    legacy_dir.mkdir(mode=0o755)
    legacy_path = legacy_dir / "old.sidecar.json"
    legacy_path.write_text("{}", encoding="utf-8")
    legacy_path.chmod(0o644)

    sess.ensure_private_session_storage()
    assert sess.SESS_DIR.stat().st_mode & 0o777 == 0o700
    assert legacy_dir.stat().st_mode & 0o777 == 0o700
    assert legacy_path.stat().st_mode & 0o777 == 0o600
    sess.set_message_annotation(
        "privacy-sidecar-test", "assistant-privacy", model="fixture")
    path = sess.SESS_DIR / "privacy-sidecar-test.sidecar.json"
    try:
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        path.unlink(missing_ok=True)


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


def test_sdk_merge_preserves_plan_return_permission(app_module, monkeypatch):
    from backend import sessions as sess

    meta = sess.create_session(
        "sdk-plan",
        permission="plan",
        plan_return_permission="bypassPermissions",
    )
    monkeypatch.setattr(
        sess,
        "sdk_get_session_info",
        lambda *_a, **_kw: _sdk_info(meta["id"]),
    )
    sess.invalidate_sessions_cache()

    merged = sess.get_session_meta(meta["id"])
    assert merged["permission"] == "plan"
    assert merged["plan_return_permission"] == "bypassPermissions"


def test_legacy_plan_metadata_fails_closed_without_rewriting(
    app_module,
    monkeypatch,
):
    from backend import sessions as sess

    meta = sess.create_session("legacy-plan", permission="plan")
    rows = json.loads(sess.INDEX.read_text(encoding="utf-8"))
    rows[0].pop("plan_return_permission")
    sess.INDEX.write_text(json.dumps(rows), encoding="utf-8")
    before = sess.INDEX.read_text(encoding="utf-8")
    monkeypatch.setattr(sess, "sdk_get_session_info", lambda *_a, **_kw: None)
    sess.invalidate_sessions_cache()

    loaded = sess.get_session_meta(meta["id"])
    assert loaded["permission"] == "plan"
    assert loaded["plan_return_permission"] == "default"
    assert sess.INDEX.read_text(encoding="utf-8") == before

    listed = next(item for item in sess.list_sessions() if item["id"] == meta["id"])
    assert listed["plan_return_permission"] == "default"
    assert sess.INDEX.read_text(encoding="utf-8") == before


def test_invalid_and_stale_plan_return_metadata_is_normalized_on_read(
    app_module,
    monkeypatch,
):
    from backend import sessions as sess

    plan = sess.create_session("bad-plan", permission="plan")
    regular = sess.create_session("regular", permission="default")
    rows = json.loads(sess.INDEX.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in rows}
    by_id[plan["id"]]["plan_return_permission"] = "plan"
    by_id[regular["id"]]["plan_return_permission"] = "bypassPermissions"
    sess.INDEX.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(sess, "sdk_get_session_info", lambda *_a, **_kw: None)
    sess.invalidate_sessions_cache()

    assert sess.get_session_meta(plan["id"])["plan_return_permission"] == "default"
    assert sess.get_session_meta(regular["id"])["plan_return_permission"] == ""
