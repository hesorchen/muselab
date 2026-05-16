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
