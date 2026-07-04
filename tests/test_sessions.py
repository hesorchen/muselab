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


def test_sessions_list_conditional_get(client, auth):
    """GET /sessions is a conditional resource: a matching If-None-Match
    yields 304 (lets the picker skip transfer + Alpine re-render), and any
    user-visible change (new session) invalidates the ETag → fresh 200."""
    # First fetch hands back a weak ETag.
    r1 = client.get("/api/chat/sessions", headers=auth)
    assert r1.status_code == 200
    etag = r1.headers.get("etag")
    assert etag and etag.startswith('W/"')

    # Echoing it back with nothing changed → 304, no body.
    r2 = client.get("/api/chat/sessions",
                    headers={**auth, "If-None-Match": etag})
    assert r2.status_code == 304
    assert not r2.content

    # Mutating the list (new session) must flip the tag → 200 with new ETag.
    client.post("/api/chat/sessions", headers=auth, json={"name": "etag-bust"})
    r3 = client.get("/api/chat/sessions",
                    headers={**auth, "If-None-Match": etag})
    assert r3.status_code == 200
    assert r3.headers.get("etag") and r3.headers["etag"] != etag


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
    # 2026-06-06: corrected to 200K. The bundled Claude Code CLI reports a
    # 200K effective window for Claude models (verified via get_context_usage:
    # maxTokens=200000); the earlier 1M assumption made the context meter read
    # ~5x too low. The hardcoded table is only the never-measured FALLBACK —
    # accounts that genuinely have the 1M beta window auto-upgrade once a turn
    # runs (the SDK maxTokens is then persisted per-session and overrides this).
    # Test value follows MODEL_CONTEXT_LIMITS — bump together if it changes.
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


def test_providers_marks_codex_effort_capability(client, auth, monkeypatch):
    monkeypatch.setenv("CODEX_GATEWAY_API_KEY", "local-secret")
    r = client.get("/api/chat/providers", headers=auth)
    assert r.status_code == 200
    models = r.json()["models"]
    codex = next(m for m in models if m["model"] == "codex:gpt-5.5")
    assert codex["supports_effort"] is True
    assert codex["supports_thinking"] is False


def test_codex_legacy_raw_model_alias_resolves(app_module, monkeypatch):
    """Existing sessions/prefs may store the vendor id without `codex:`.

    The backend must canonicalize it before routing, while the frontend maps the
    same alias for capability gates so the mobile gear can show Effort.
    """
    monkeypatch.setenv("CODEX_GATEWAY_API_KEY", "local-secret")
    import backend.chat as chat
    assert chat._resolve_default_model("gpt-5.5", allow_fallback=False) == "codex:gpt-5.5"
    assert chat._heal_unreachable_locked_model("no-such-sid", "gpt-5.5", "") == "codex:gpt-5.5"


def test_heal_unreachable_locked_model_switches_to_configured(app_module, monkeypatch):
    """A session pinned to claude (no Anthropic auth) before any provider was
    configured self-heals to the configured DeepSeek once the user sets the
    key — the "only configured DeepSeek but still got claude auth error" bug.
    Fresh sid has no on-disk JSONL, so the history guard allows the switch."""
    import backend.chat as chat
    from backend import endpoints
    # Simulate the user's machine: no Claude OAuth / API key. (The dev box
    # running this suite may have ~/.claude/.credentials.json, which would
    # otherwise make claude "reachable" and mask the bug.)
    monkeypatch.setattr(endpoints, "has_anthropic_auth", lambda: False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-runtime")
    healed = chat._heal_unreachable_locked_model("no-such-sid", "claude-sonnet-4-6", "")
    assert healed == "deepseek-v4-pro"


def test_heal_keeps_locked_when_nothing_configured(app_module, monkeypatch):
    """No provider configured at all → can't do better, keep the lock (the UI
    surfaces the no-provider onboarding card instead of swapping)."""
    import backend.chat as chat
    from backend import endpoints
    monkeypatch.setattr(endpoints, "has_anthropic_auth", lambda: False)
    healed = chat._heal_unreachable_locked_model("no-such-sid", "claude-sonnet-4-6", "")
    assert healed == "claude-sonnet-4-6"


def test_heal_keeps_reachable_locked_model(app_module, monkeypatch):
    """A locked model whose provider IS configured stays untouched."""
    import backend.chat as chat
    from backend import endpoints
    monkeypatch.setattr(endpoints, "has_anthropic_auth", lambda: False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-runtime")
    healed = chat._heal_unreachable_locked_model("no-such-sid", "deepseek-v4-pro", "")
    assert healed == "deepseek-v4-pro"


def test_heal_keeps_locked_when_session_has_history(app_module, monkeypatch):
    """A session with real on-disk history is NOT switched even if its locked
    model became unreachable — avoids cross-vendor thinking-signature
    corruption (the one-session-one-model rule's whole point)."""
    import backend.chat as chat
    from backend import endpoints
    monkeypatch.setattr(endpoints, "has_anthropic_auth", lambda: False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-runtime")
    monkeypatch.setattr(chat, "_find_session_jsonl", lambda sid: "/fake/path.jsonl")
    healed = chat._heal_unreachable_locked_model("sid-with-history", "claude-sonnet-4-6", "")
    assert healed == "claude-sonnet-4-6"


def test_resolve_default_model_empty_when_no_provider_and_no_fallback(app_module, monkeypatch):
    """allow_fallback=False with nothing configured returns "" so session
    creation leaves the model unlocked — the root fix for the poisoned
    first-session bug. The legacy default (allow_fallback=True) still returns
    the MODEL constant for callers that need a non-empty id."""
    import backend.chat as chat
    from backend import endpoints
    monkeypatch.setattr(endpoints, "has_anthropic_auth", lambda: False)
    assert chat._resolve_default_model("", allow_fallback=False) == ""
    assert chat._resolve_default_model("", allow_fallback=True) == chat.MODEL


def test_resolve_default_model_picks_configured_even_without_fallback(app_module, monkeypatch):
    """allow_fallback only governs the no-provider case. When a provider IS
    configured, allow_fallback=False still resolves to the available model."""
    import backend.chat as chat
    from backend import endpoints
    monkeypatch.setattr(endpoints, "has_anthropic_auth", lambda: False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-runtime")
    assert chat._resolve_default_model("", allow_fallback=False) == "deepseek-v4-pro"


def test_create_session_leaves_model_empty_when_no_provider(app_module, monkeypatch):
    """End-to-end: a session created before any provider is configured gets an
    empty model (not the unreachable claude lock)."""
    import backend.chat as chat
    from backend import endpoints
    monkeypatch.setattr(endpoints, "has_anthropic_auth", lambda: False)
    meta = chat.create_session_api(chat.CreateReq(name="fresh", model=""))
    assert (meta.get("model") or "") == ""


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


def test_export_session_markdown_empty(client, auth, app_module):
    """Export endpoint must produce a valid markdown body even for a brand-new
    session with no turns yet. Verifies metadata header (name + created +
    msg count) lands and Content-Disposition includes both ASCII filename
    fallback and RFC 5987 UTF-8 filename* for CJK / spaces."""
    r = client.post("/api/chat/sessions", headers=auth,
                     json={"name": "export-empty"})
    sid = r.json()["id"]
    r = client.get(
        f"/api/chat/sessions/{sid}/export",
        params={"token": "test-token-1234567890abcdef-secure-min-32"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    cd = r.headers["content-disposition"]
    assert "attachment" in cd
    # ASCII fallback filename present
    assert "filename=" in cd
    # RFC 5987 UTF-8 variant present (for CJK / spaces)
    assert "filename*=UTF-8" in cd
    body = r.text
    # Title from session name
    assert "# export-empty" in body
    # Empty-session marker — no message turns yet
    assert "*Messages: 0*" in body


def test_export_session_markdown_404_for_unknown(client, auth):
    r = client.get(
        "/api/chat/sessions/no-such-session/export",
        params={"token": "test-token-1234567890abcdef-secure-min-32"},
    )
    assert r.status_code == 404


def test_export_session_markdown_rejects_bad_token(client, auth, app_module):
    r = client.post("/api/chat/sessions", headers=auth,
                     json={"name": "export-auth"})
    sid = r.json()["id"]
    r = client.get(
        f"/api/chat/sessions/{sid}/export",
        params={"token": "not-the-real-token-but-long-enough-to-pass-the-len-check"},
    )
    assert r.status_code == 401
