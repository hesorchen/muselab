"""Chat session CRUD + persistence (no LLM calls).

Post-refactor (2026-05-17 PR): muselab no longer stores the transcript
locally — CLI's JSONL is source of truth. These tests cover muselab's
metadata + per-message annotation sidecar layer only. End-to-end transcript
flows require a live SDK and are not unit-testable here.
"""


def test_session_lifecycle(client, auth):
    r = client.post("/api/chat/sessions", headers=auth, json={"name": "t1"})
    assert r.status_code == 200
    sid = r.json()["id"]

    r = client.get("/api/chat/sessions", headers=auth)
    assert any(s["id"] == sid for s in r.json()["sessions"])

    r = client.get(f"/api/chat/sessions/{sid}", headers=auth)
    assert r.status_code == 200
    s = r.json()
    assert s["name"] == "t1"
    # New session, no SDK turn yet → CLI JSONL doesn't exist → empty messages
    assert s["messages"] == []

    r = client.patch(f"/api/chat/sessions/{sid}", headers=auth, json={"name": "t2"})
    assert r.status_code == 200
    r = client.get(f"/api/chat/sessions/{sid}", headers=auth)
    assert r.json()["name"] == "t2"

    r = client.delete(f"/api/chat/sessions/{sid}", headers=auth)
    assert r.status_code == 200
    r = client.get(f"/api/chat/sessions/{sid}", headers=auth)
    assert r.status_code == 404


def test_default_session_name_includes_timestamp(app_module):
    from backend import sessions as sess
    a = sess.create_session()
    b = sess.create_session()
    assert a["name"].startswith("新会话 ")
    assert b["name"].startswith("新会话 ")


def test_bump_session_auto_renames_from_first_user_text(app_module):
    """After a stream completes, bump_session is called with the user text;
    if the session is still auto-named, rename to that text snippet."""
    from backend import sessions as sess
    meta = sess.create_session()
    assert meta["auto_named"] is True
    sess.bump_session(meta["id"], message_count=2,
                       auto_rename_from="怎么解读这次体检报告")
    s = sess.get_session_meta(meta["id"])
    assert s["name"] == "怎么解读这次体检报告"
    assert s["auto_named"] is False


def test_manual_rename_disables_auto_rename(app_module):
    from backend import sessions as sess
    meta = sess.create_session()
    sess.rename_session(meta["id"], "我的体检笔记")
    sess.bump_session(meta["id"], message_count=2,
                       auto_rename_from="完全不同的内容")
    s = sess.get_session_meta(meta["id"])
    assert s["name"] == "我的体检笔记"


def test_bump_session_trims_long_titles(app_module):
    from backend import sessions as sess
    long_text = "这是一段非常非常非常非常长的提问占位文字测试" * 5
    meta = sess.create_session()
    sess.bump_session(meta["id"], message_count=1, auto_rename_from=long_text)
    s = sess.get_session_meta(meta["id"])
    assert len(s["name"]) <= 25
    assert s["name"].endswith("…")


def test_bump_session_strips_at_mentions(app_module):
    from backend import sessions as sess
    meta = sess.create_session()
    sess.bump_session(meta["id"], message_count=1,
                       auto_rename_from="@health/checkup.pdf 这里 LDL 偏高严重吗")
    s = sess.get_session_meta(meta["id"])
    assert "@" not in s["name"]
    assert "LDL" in s["name"]


def test_patch_model_allowed_on_empty_session(client, auth, app_module):
    from backend import sessions as sess
    meta = sess.create_session("t", model="claude-opus-4-7")
    r = client.patch(f"/api/chat/sessions/{meta['id']}",
                      json={"model": "deepseek-v4-flash"}, headers=auth)
    assert r.status_code == 200
    assert sess.get_session_meta(meta["id"])["model"] == "deepseek-v4-flash"


def test_per_message_annotation_roundtrip(app_module):
    """Replaces test_per_message_model_field_survives_roundtrip. Per-message
    metadata (cost, model badge, images) is now stored as annotations keyed by
    SDK message UUID — chat.py merges these onto SDK-returned transcripts."""
    from backend import sessions as sess
    meta = sess.create_session()
    sid = meta["id"]
    # Simulate two assistant replies from different models
    sess.set_message_annotation(sid, "uuid-asst-1",
                                  cost="$0.0001", model="claude-opus-4-7")
    sess.set_message_annotation(sid, "uuid-asst-2",
                                  cost="$0.0000", model="deepseek-v4-flash")
    anns = sess.get_message_annotations(sid)
    assert anns["uuid-asst-1"]["model"] == "claude-opus-4-7"
    assert anns["uuid-asst-2"]["model"] == "deepseek-v4-flash"
    assert anns["uuid-asst-1"]["cost"] == "$0.0001"


def test_annotation_partial_update_preserves_other_fields(app_module):
    """set_message_annotation merges fields rather than replacing the dict —
    useful when stream writes cost+model first, then a later sync adds images."""
    from backend import sessions as sess
    meta = sess.create_session()
    sid = meta["id"]
    sess.set_message_annotation(sid, "uuid-x", cost="$0.01", model="m1")
    sess.set_message_annotation(sid, "uuid-x", images=[{"mime": "image/png"}])
    anns = sess.get_message_annotations(sid)
    assert anns["uuid-x"]["cost"] == "$0.01"
    assert anns["uuid-x"]["model"] == "m1"
    assert anns["uuid-x"]["images"] == [{"mime": "image/png"}]


def test_session_usage_endpoint_returns_meter_data(client, auth, app_module):
    r = client.get("/api/chat/usage/never-existed?model=claude-opus-4-7",
                    headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["context_limit"] == 200_000
    assert d["context_used_pct"] == 0
    assert d["input_tokens"] == 0


def test_usage_endpoint_includes_cache_hit_pct(client, auth):
    r = client.get("/api/chat/usage", headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert "cache_hit_pct" in d
    assert "budget_usd" in d


def test_usage_endpoint(client, auth):
    r = client.get("/api/chat/usage", headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert "total_cost_usd" in d
    assert "total_messages" in d


def test_providers_endpoint(client, auth):
    r = client.get("/api/chat/providers", headers=auth)
    assert r.status_code == 200
    models = r.json()["models"]
    # Conftest deletes ANTHROPIC_API_KEY; without claude OAuth either,
    # Claude group must be hidden (regression fix from ba00629).
    from backend import endpoints
    if not endpoints.has_anthropic_auth():
        assert not any(m["model"].startswith("claude-") for m in models)
    # DeepSeek hidden because key not set in test env
    assert not any(m["model"].startswith("deepseek-") for m in models)


def test_providers_includes_deepseek_after_key_set(client, auth, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-runtime")
    r = client.get("/api/chat/providers", headers=auth)
    models = r.json()["models"]
    assert any(m["model"].startswith("deepseek-") for m in models)
    ds_models = [m for m in models if m["model"].startswith("deepseek-")]
    assert any(m["label"] == "V4 Pro" and m["model"] == "deepseek-v4-pro"
                for m in ds_models)
    assert any(m["label"] == "V4 Flash" and m["model"] == "deepseek-v4-flash"
                for m in ds_models)


def test_reset_session_endpoint(client, auth):
    r = client.post("/api/chat/sessions", headers=auth, json={"name": "to-reset"})
    sid = r.json()["id"]
    r = client.post(f"/api/chat/reset?session_id={sid}",
                    headers={"X-Auth-Token": "ignored", "Cookie": ""},
                    params={"token": "test-token-1234567890abcdef-secure-min-32"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_delete_session_clears_sidecar(client, auth, app_module):
    from backend import sessions as sess
    meta = sess.create_session("ephemeral")
    sid = meta["id"]
    assert sess.get_session_meta(sid) is not None
    assert sess.delete_session(sid) is True
    assert sess.get_session_meta(sid) is None
    assert sess.delete_session(sid) is False
