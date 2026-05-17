"""Chat session CRUD + persistence (no actual LLM calls)."""


def test_session_lifecycle(client, auth):
    # create
    r = client.post("/api/chat/sessions", headers=auth, json={"name": "t1"})
    assert r.status_code == 200
    sid = r.json()["id"]

    # list contains it
    r = client.get("/api/chat/sessions", headers=auth)
    assert any(s["id"] == sid for s in r.json()["sessions"])

    # get it back
    r = client.get(f"/api/chat/sessions/{sid}", headers=auth)
    assert r.status_code == 200
    s = r.json()
    assert s["name"] == "t1"
    assert s["messages"] == []

    # rename
    r = client.patch(f"/api/chat/sessions/{sid}", headers=auth, json={"name": "t2"})
    assert r.status_code == 200
    r = client.get(f"/api/chat/sessions/{sid}", headers=auth)
    assert r.json()["name"] == "t2"

    # delete
    r = client.delete(f"/api/chat/sessions/{sid}", headers=auth)
    assert r.status_code == 200
    r = client.get(f"/api/chat/sessions/{sid}", headers=auth)
    assert r.status_code == 404


def test_append_messages_persists(client, auth, app_module):
    """Use the sess module directly since chat streaming needs a live LLM."""
    from backend import sessions as sess  # type: ignore[import]
    meta = sess.create_session("persist-test")
    sess.append_messages(meta["id"], [
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "hi back"},
    ])
    s = sess.get_session(meta["id"])
    assert len(s["messages"]) == 2
    assert s["messages"][0]["text"] == "hello"


def test_default_session_name_includes_timestamp(app_module):
    """Two new sessions back-to-back should NOT both render as plain "新会话" —
    the dropdown is unusable when every entry is identical."""
    from backend import sessions as sess
    a = sess.create_session()
    b = sess.create_session()
    # both default-named but include MM-DD HH:mm so they're distinguishable in UI
    assert a["name"].startswith("新会话 ")
    assert b["name"].startswith("新会话 ")


def test_first_user_message_auto_renames_session(app_module):
    """Auto-named sessions become titled after the first user message — this is
    what makes the session list readable instead of 'all 新会话'."""
    from backend import sessions as sess
    meta = sess.create_session()
    assert meta["auto_named"] is True
    sess.append_messages(meta["id"], [
        {"role": "user", "text": "怎么解读这次体检报告"},
    ])
    s = sess.get_session(meta["id"])
    assert s["name"] == "怎么解读这次体检报告"
    assert s["auto_named"] is False


def test_manual_rename_disables_auto_rename(app_module):
    """If the user renamed the session, the first message shouldn't blow that away."""
    from backend import sessions as sess
    meta = sess.create_session()
    sess.rename_session(meta["id"], "我的体检笔记")
    sess.append_messages(meta["id"], [
        {"role": "user", "text": "完全不同的内容"},
    ])
    s = sess.get_session(meta["id"])
    assert s["name"] == "我的体检笔记"


def test_auto_rename_trims_long_messages(app_module):
    from backend import sessions as sess
    long_text = "这是一段非常非常非常非常长的提问占位文字测试" * 5
    meta = sess.create_session()
    sess.append_messages(meta["id"], [{"role": "user", "text": long_text}])
    s = sess.get_session(meta["id"])
    # 标题长度受限（默认 24 字符 + 省略号）
    assert len(s["name"]) <= 25
    assert s["name"].endswith("…")


def test_auto_rename_strips_at_mentions(app_module):
    """@ mentions are paths, not the actual question — drop them from the title."""
    from backend import sessions as sess
    meta = sess.create_session()
    sess.append_messages(meta["id"], [
        {"role": "user", "text": "@health/checkup.pdf 这里 LDL 偏高严重吗"},
    ])
    s = sess.get_session(meta["id"])
    assert "@" not in s["name"]
    assert "LDL" in s["name"]


def test_patch_model_allowed_on_empty_session(client, auth, app_module):
    """Virgin session can still change model — user hasn't committed to it yet."""
    from backend import sessions as sess
    meta = sess.create_session("t", model="claude-opus-4-7")
    r = client.patch(f"/api/chat/sessions/{meta['id']}",
                      json={"model": "deepseek-v4-flash"}, headers=auth)
    assert r.status_code == 200
    assert sess.get_session(meta["id"])["model"] == "deepseek-v4-flash"


def test_patch_model_rejected_after_first_message(client, auth, app_module):
    """One session = one model (VS Code alignment): once any message exists,
    PATCH model returns 409. UI nudges user to create a fresh session instead."""
    from backend import sessions as sess
    meta = sess.create_session("t", model="claude-opus-4-7")
    sess.append_messages(meta["id"], [{"role": "user", "text": "hi"}])
    r = client.patch(f"/api/chat/sessions/{meta['id']}",
                      json={"model": "deepseek-v4-flash"}, headers=auth)
    assert r.status_code == 409
    assert sess.get_session(meta["id"])["model"] == "claude-opus-4-7"


def test_seed_persists_summary_as_first_user_message(client, auth, app_module):
    """/compact + /seed: model summarizes old session, summary becomes the
    seed of the fresh fork as a user message so subsequent turns see it."""
    from backend import sessions as sess
    new = sess.create_session("compacted", model="claude-opus-4-7")
    r = client.post(f"/api/chat/sessions/{new['id']}/seed",
                     json={"summary": "user is 28, lives in Beijing, asking about FIRE"},
                     headers=auth)
    assert r.status_code == 200
    s = sess.get_session(new["id"])
    assert len(s["messages"]) == 1
    assert s["messages"][0]["role"] == "user"
    assert "user is 28" in s["messages"][0]["text"]


def test_seed_rejects_non_empty_session(client, auth, app_module):
    from backend import sessions as sess
    s = sess.create_session("not-empty")
    sess.append_messages(s["id"], [{"role": "user", "text": "existing"}])
    r = client.post(f"/api/chat/sessions/{s['id']}/seed",
                     json={"summary": "x"}, headers=auth)
    assert r.status_code == 409


def test_compact_creates_empty_target_session(client, auth, app_module):
    """/compact creates a fresh session inheriting model + system prompt; the
    actual summarization happens on the next streamed turn (front-end fills the
    prompt)."""
    from backend import sessions as sess
    src = sess.create_session("orig", model="deepseek-v4-flash",
                                system_prompt="custom")
    sess.append_messages(src["id"], [{"role": "user", "text": "hi"}])
    r = client.post(f"/api/chat/sessions/{src['id']}/compact",
                     headers=auth)
    assert r.status_code == 200
    new = r.json()
    assert new["model"] == "deepseek-v4-flash"
    assert new["system_prompt"] == "custom"
    assert new["name"].endswith("(compact)")
    # New session starts empty — frontend will fire the summarize prompt
    assert sess.get_session(new["id"])["messages"] == []


def test_session_usage_endpoint_returns_meter_data(client, auth, app_module):
    r = client.get("/api/chat/usage/never-existed?model=claude-opus-4-7",
                    headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["context_limit"] == 200_000     # Claude window
    assert d["context_used_pct"] == 0
    assert d["input_tokens"] == 0


def test_usage_endpoint_includes_cache_hit_pct(client, auth):
    r = client.get("/api/chat/usage", headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert "cache_hit_pct" in d
    assert "budget_usd" in d


def test_per_message_model_field_survives_roundtrip(app_module):
    """Regression: switching models mid-session would silently drop the
    provenance — old A-model bubbles disappeared their badge after reload, then
    only the new B-model bubble showed a badge. Symptom: 'all bubbles became B'.
    Backend now persists `model` per assistant message so reload preserves
    which model produced which reply."""
    from backend import sessions as sess
    meta = sess.create_session()
    sess.append_messages(meta["id"], [
        {"role": "user", "text": "first"},
        {"role": "assistant", "text": "reply via A",
         "cost": "$0.0001", "model": "claude-opus-4-7"},
        {"role": "user", "text": "second"},
        {"role": "assistant", "text": "reply via B",
         "cost": "$0.0000", "model": "deepseek-v4-flash"},
    ])
    s = sess.get_session(meta["id"])
    asst = [m for m in s["messages"] if m["role"] == "assistant"]
    assert asst[0]["model"] == "claude-opus-4-7"
    assert asst[1]["model"] == "deepseek-v4-flash"


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
    # Claude is always present
    assert any(m["model"].startswith("claude-") for m in models)
    # DeepSeek hidden because key not set in test env
    assert not any(m["model"].startswith("deepseek-") for m in models)


def test_providers_includes_deepseek_after_key_set(client, auth, monkeypatch):
    """Setting an API key should make the provider show up immediately."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-runtime")
    r = client.get("/api/chat/providers", headers=auth)
    models = r.json()["models"]
    assert any(m["model"].startswith("deepseek-") for m in models)
    # 现在每个 DeepSeek 条目都有 short label
    ds_models = [m for m in models if m["model"].startswith("deepseek-")]
    # labels are now full model ids
    assert any(m["label"] == "deepseek-v4-pro" for m in ds_models)
    assert any(m["label"] == "deepseek-v4-flash" for m in ds_models)


def test_reset_session_endpoint(client, auth):
    """POST /reset with session_id is idempotent (no clients to reset)."""
    r = client.post("/api/chat/sessions", headers=auth, json={"name": "to-reset"})
    sid = r.json()["id"]
    # token comes through query
    r = client.post(f"/api/chat/reset?session_id={sid}",
                    headers={"X-Auth-Token": "ignored",
                             "Cookie": ""},
                    params={"token": "test-token-1234567890abcdef-secure-min-32"})
    # 注意：reset 用 require_token_query 鉴权
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True


def test_delete_session_clears_disk(client, auth, app_module):
    from backend import sessions as sess
    meta = sess.create_session("ephemeral")
    sid = meta["id"]
    assert sess.get_session(sid) is not None
    assert sess.delete_session(sid) is True
    assert sess.get_session(sid) is None
    # 再删一次 = False
    assert sess.delete_session(sid) is False
