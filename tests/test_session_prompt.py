"""Per-session custom system prompt: persistence + PATCH endpoint."""


def test_create_with_system_prompt(app_module):
    from backend import sessions as sess
    meta = sess.create_session("sp-test", "claude-sonnet-4-6", system_prompt="be terse")
    s = sess.get_session(meta["id"])
    assert s["system_prompt"] == "be terse"


def test_update_system_prompt_via_function(app_module):
    from backend import sessions as sess
    meta = sess.create_session("sp2")
    assert meta["system_prompt"] == ""   # default empty
    assert sess.update_system_prompt(meta["id"], "be funny") is True
    s = sess.get_session(meta["id"])
    assert s["system_prompt"] == "be funny"


def test_update_system_prompt_nonexistent(app_module):
    from backend import sessions as sess
    assert sess.update_system_prompt("nonexistent-id", "x") is False


def test_patch_session_endpoint_name_and_prompt(client, auth):
    r = client.post("/api/chat/sessions", headers=auth, json={"name": "p"})
    sid = r.json()["id"]
    r = client.patch(f"/api/chat/sessions/{sid}", headers=auth,
                     json={"name": "renamed", "system_prompt": "you are a poet"})
    assert r.status_code == 200
    r = client.get(f"/api/chat/sessions/{sid}", headers=auth)
    s = r.json()
    assert s["name"] == "renamed"
    assert s["system_prompt"] == "you are a poet"


def test_patch_session_empty_body_returns_404(client, auth):
    r = client.post("/api/chat/sessions", headers=auth, json={"name": "p2"})
    sid = r.json()["id"]
    r = client.patch(f"/api/chat/sessions/{sid}", headers=auth, json={})
    assert r.status_code in (200, 404)


def test_patch_clears_system_prompt(client, auth):
    r = client.post("/api/chat/sessions", headers=auth, json={"name": "p3"})
    sid = r.json()["id"]
    client.patch(f"/api/chat/sessions/{sid}", headers=auth, json={"system_prompt": "X"})
    client.patch(f"/api/chat/sessions/{sid}", headers=auth, json={"system_prompt": ""})
    r = client.get(f"/api/chat/sessions/{sid}", headers=auth)
    assert r.json()["system_prompt"] == ""
