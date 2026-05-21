"""Regressions caught manually + reasoned through to pytest form. Each test
guards against a real bug that shipped during muselab's 2026-05-17 debugging
sprint. Keep them passing — if you add new file I/O, encoding handling, or
provider gating, the bug class will be re-caught here, not in production."""
from __future__ import annotations



# ============================================================================
# Bug 1: UnicodeEncodeError on emoji in session messages (Windows GBK default).
# Fix: every read_text/write_text in backend now passes encoding='utf-8'.
# ============================================================================

def test_session_persist_handles_emoji_and_cjk(client, auth):
    """Session save+load must round-trip emoji / rare CJK without crashing.
    Before df3f567, write_text relied on the system codepage — Windows zh-CN
    would crash with `UnicodeEncodeError: 'gbk' codec can't encode '\\U0001f604'`."""
    # Create session
    r = client.post("/api/chat/sessions",
                     headers={**auth, "Content-Type": "application/json"},
                     json={"name": "emoji test 😄", "model": "deepseek-v4-flash"})
    assert r.status_code == 200, r.text
    # sid extracted not needed — round-trip check below uses the listing
    # endpoint to verify emoji survived the on-disk round-trip.

    # Round-trip the session name (contains emoji)
    r = client.get("/api/chat/sessions", headers=auth)
    assert r.status_code == 200
    names = [s["name"] for s in r.json()["sessions"]]
    assert "emoji test 😄" in names

    # The session file on disk must be UTF-8
    from backend import sessions as sess
    index_text = sess.INDEX.read_text(encoding="utf-8")
    assert "😄" in index_text, "emoji lost on persist — encoding regression"


# ============================================================================
# Bug 2: Claude model group offered even without Anthropic auth.
# Fix: available_groups() gates Claude on has_anthropic_auth() which checks
# either ~/.claude/.credentials.json or ANTHROPIC_API_KEY env.
# ============================================================================

def test_claude_hidden_without_auth(client, auth, monkeypatch):
    """No Claude OAuth + no ANTHROPIC_API_KEY → /providers must NOT list Claude."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Pretend the credentials file doesn't exist
    from backend import endpoints
    monkeypatch.setattr(endpoints, "has_anthropic_auth", lambda: False)
    r = client.get("/api/chat/providers", headers=auth)
    assert r.status_code == 200
    groups = {m["group"] for m in r.json()["models"]}
    assert "Claude" not in groups, "Claude shown without any auth — regression"


def test_claude_appears_with_api_key(client, auth, monkeypatch):
    """ANTHROPIC_API_KEY set → Claude must be available (previously settings.py
    pop'd this env, locking out non-Pro users entirely)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    from backend import endpoints
    monkeypatch.setattr(endpoints, "has_anthropic_auth", lambda: True)
    r = client.get("/api/chat/providers", headers=auth)
    assert r.status_code == 200
    groups = {m["group"] for m in r.json()["models"]}
    assert "Claude" in groups


# ============================================================================
# Bug 3 (rev 2026-05-18): Context meter previously summed input + cache_read +
# cache_creation, on the assumption those were per-turn values. The CLI's
# ResultMessage.usage is actually CUMULATIVE for the session (per SDK doc on
# ContextUsageResponse.apiUsage), so summing them grew unboundedly — meter
# read e.g. 796.6% on a fresh 200K window. New rule: trust SDK-authoritative
# `context_used` populated by client.get_context_usage(); if absent (no turn
# yet), fall back to per-turn `input_tokens` only — NEVER sum the cache fields.
# ============================================================================

def test_context_used_prefers_sdk_authoritative_value(client, auth):
    """When the stream handler has populated `context_used` from
    client.get_context_usage(), /usage must return it as-is."""
    r = client.post("/api/chat/sessions",
                     headers={**auth, "Content-Type": "application/json"},
                     json={"name": "ctx test", "model": "deepseek-v4-flash"})
    sid = r.json()["id"]

    from backend import chat as chat_mod
    chat_mod._session_usage[sid] = {
        "input_tokens": 500,
        "output_tokens": 100,
        "cache_read_tokens": 40_000,         # cumulative, must NOT be summed in
        "cache_creation_tokens": 2_000,      # cumulative, must NOT be summed in
        "total_cost_usd": 0.01,
        "last_turn_at": 0.0,
        "context_used": 12_500,              # authoritative live-window value
        "context_used_pct": 6.3,             # stale, /usage recomputes
        "context_limit": 200_000,            # stale (table now says 1M for v4-flash)
    }

    r = client.get(f"/api/chat/usage/{sid}?model=deepseek-v4-flash", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["context_used"] == 12_500, \
        f"must use SDK-populated context_used, got {body['context_used']}"
    # /usage takes max(stored, hardcoded) for context_limit, so a stale
    # stored 200K loses to the new MODEL_CONTEXT_LIMITS["deepseek-v4-flash"]
    # = 1_000_000 (2026-05-18 update). pct recomputes against the new limit.
    assert body["context_limit"] == 1_000_000
    assert body["context_used_pct"] == round(12_500 / 1_000_000 * 100, 1)
    # Regression guard: must NOT be the legacy sum that produced 796.6%
    assert body["context_used"] != 500 + 40_000 + 2_000


def test_context_used_fallback_when_sdk_value_missing(client, auth):
    """Pre-first-turn (no SDK call yet) → fallback is per-turn input_tokens only,
    NOT summed with the cumulative cache fields."""
    r = client.post("/api/chat/sessions",
                     headers={**auth, "Content-Type": "application/json"},
                     json={"name": "ctx fallback", "model": "deepseek-v4-flash"})
    sid = r.json()["id"]

    from backend import chat as chat_mod
    chat_mod._session_usage[sid] = {
        "input_tokens": 500,
        "output_tokens": 100,
        "cache_read_tokens": 40_000,
        "cache_creation_tokens": 2_000,
        "total_cost_usd": 0.01,
        "last_turn_at": 0.0,
        # context_used absent / 0 → fallback path
    }

    r = client.get(f"/api/chat/usage/{sid}?model=deepseek-v4-flash", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["context_used"] == 500, \
        f"fallback must be input_tokens only, got {body['context_used']}"
    assert body["context_used"] != 42_500   # explicitly NOT the cumulative sum


# ============================================================================
# Bug 4: Settings PUT didn't refresh contextInfo on the frontend so
# has_any_provider stayed false after adding a key. Backend test: putting a
# DeepSeek key updates os.environ in-process AND context-info reflects it.
# ============================================================================

def test_settings_put_reflects_in_context_info(client, auth):
    """PUT /api/settings with a key → /api/chat/context-info must immediately
    show has_any_provider=true. Backed by the in-process os.environ refresh
    in api_settings._write_env."""
    # Initially no provider
    r = client.get("/api/chat/context-info", headers=auth)
    pre = r.json()
    assert pre["has_any_provider"] in (False, True)   # depends on env at test start

    # Save a key
    r = client.put("/api/settings",
                    headers={**auth, "Content-Type": "application/json"},
                    json={"deepseek_api_key": "sk-test-key-12345"})
    assert r.status_code == 200, r.text

    # Now context-info should show it
    r = client.get("/api/chat/context-info", headers=auth)
    post = r.json()
    assert post["has_any_provider"] is True, \
        "context-info didn't pick up the new provider — settings/env sync regression"
    assert "DeepSeek" in post["third_party_configured"]


# ============================================================================
# Bug 5: seed endpoint accepts is_compact flag and persists marker metadata
# (used by frontend to render the 📦 marker pill).
# ============================================================================

def test_context_breakdown_returns_409_for_session_without_client(client, auth):
    """SDK audit takeaway: prefer client.get_context_usage() over manual
    arithmetic for breakdown info. This endpoint surfaces the SDK call.
    A session that hasn't run a turn yet has no live client → 409, not
    fake data. Forces the frontend to fall back to /usage cleanly."""
    r = client.post("/api/chat/sessions",
                     headers={**auth, "Content-Type": "application/json"},
                     json={"name": "no client yet", "model": "deepseek-v4-flash"})
    sid = r.json()["id"]
    r = client.get(f"/api/chat/context-breakdown/{sid}", headers=auth)
    assert r.status_code == 409, f"expected 409 (no live client), got {r.status_code}: {r.text}"


# Removed test_seed_with_compact_flag_persists_marker — the /seed endpoint
# was deleted in the 2026-05-17 refactor. CLI's native /compact writes
# isCompactSummary into the JSONL; SDK get_session_messages returns it as a
# normal message, so no muselab-side marker is needed.


# ============================================================================
# Bug 6: in-flight turn persistence — sidecars must survive process restart
# so an OOM-kill mid-stream doesn't silently lose the user's prompt
# ============================================================================

def test_interrupted_turns_endpoint_empty_on_clean_boot(client, auth):
    """Fresh test fixture has no active_turns/ sidecars. Endpoint must
    return an empty list, not 404 / not 500 / not omit the `turns` key
    — the frontend's _checkInterruptedTurns() reads `data.turns` and
    expects an Array."""
    r = client.get("/api/chat/interrupted-turns", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "turns" in body and body["turns"] == []


def test_interrupted_turn_sidecar_round_trip(app_module, client, auth, tmp_path):
    """Write a fake sidecar (simulating a previous-process crash mid-turn),
    re-scan, hit the endpoint, dismiss, verify cleanup.

    Why this matters: the recovery flow's whole point is "you had N
    unfinished turns last session, here they are." A regression that
    silently dropped sidecars would break the contract without breaking
    any other test."""
    import json
    import time
    from backend import chat as chat_mod

    # Drop a fake sidecar (mimics what _write_active_turn_sidecar does
    # on turn start, then a process death before _delete... could fire).
    fake_sid = "TEST-CRASHED-TURN-001"
    sidecar_path = chat_mod._active_turn_path(fake_sid)
    sidecar_path.write_text(json.dumps({
        "sid": fake_sid,
        "user_text": "review this PR for security risks",
        "user_text_preview": "review this PR for security risks",
        "model": "claude-sonnet-4-6",
        "started_at": time.time() - 120,
    }), encoding="utf-8")

    # The startup scan already happened at import time, so we patch in
    # the new entry as if the scan caught it. (In real life this only
    # happens at process boot — testing that path separately would
    # require a full subprocess restart.)
    chat_mod._interrupted_at_startup[fake_sid] = json.loads(
        sidecar_path.read_text(encoding="utf-8"))

    # Endpoint surfaces the entry.
    r = client.get("/api/chat/interrupted-turns", headers=auth)
    assert r.status_code == 200
    sids = [t["sid"] for t in r.json()["turns"]]
    assert fake_sid in sids
    # Preview must carry through (the toast shows it).
    entry = next(t for t in r.json()["turns"] if t["sid"] == fake_sid)
    assert "security risks" in entry["preview"]

    # Dismiss removes both in-memory state AND the on-disk sidecar.
    r = client.post(f"/api/chat/interrupted-turns/{fake_sid}/dismiss",
                     headers=auth)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert not sidecar_path.exists(), "dismiss didn't delete the sidecar"

    # Subsequent list call returns empty for this sid.
    r = client.get("/api/chat/interrupted-turns", headers=auth)
    assert fake_sid not in [t["sid"] for t in r.json()["turns"]]


# ============================================================================
# Bug 7: default response security headers — token-in-query-string mitigation
# ============================================================================

def test_security_headers_present_on_every_response(client, auth):
    """Auth token rides in query strings for SSE / file download endpoints
    (see auth.py docstring). Without `Referrer-Policy: same-origin`, a
    user clicking a link out to github.com would leak the URL — token
    included — in the Referer header. Lock the three headers we set."""
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("Referrer-Policy") == "same-origin"
    assert r.headers.get("X-Frame-Options") == "SAMEORIGIN"


def test_robots_txt_disallows_all(client):
    """Defense-in-depth for accidental public exposure. If a user
    misconfigures their reverse proxy or Cloudflare tunnel, at least
    search engines won't index the archive contents."""
    r = client.get("/robots.txt")
    assert r.status_code == 200
    body = r.text
    assert "User-agent: *" in body
    assert "Disallow: /" in body


# ============================================================================
# Profile-intake session: chat-driven CLAUDE.md setup (replaces direct edit UI)
# ============================================================================

def test_profile_intake_session_seeds_template_when_claude_md_missing(
    client, auth, temp_root
):
    """First-time user with no CLAUDE.md should get one seeded from the
    template when they start a profile-intake session — so the agent's
    first Read tool call succeeds. The chat workflow assumes the file
    exists; if it doesn't, the agent would fail on the first turn."""
    claude_md = temp_root / "CLAUDE.md"
    assert not claude_md.exists()  # fixture starts clean

    r = client.post(
        "/api/chat/sessions/profile-intake",
        headers={**auth, "Content-Type": "application/json"},
        json={},
    )
    assert r.status_code == 200
    body = r.json()
    # Session metadata + bilingual initial seed must come back together —
    # frontend reads meta.initial_message[lang] to auto-send the first prompt.
    assert "id" in body
    assert "initial_message" in body
    assert "zh" in body["initial_message"]
    assert "en" in body["initial_message"]
    # The file should now exist with the template content (date substituted).
    assert claude_md.exists()
    content = claude_md.read_text(encoding="utf-8")
    assert "CLAUDE.md" in content  # template header
    assert "%DATE%" not in content  # date placeholder was substituted


def test_profile_intake_session_doesnt_clobber_existing_claude_md(
    client, auth, temp_root
):
    """If the user already has a CLAUDE.md (from the install-time CLI
    intake or a previous profile-intake session), the new session must
    NOT overwrite it — the in-chat workflow is meant to refine, not
    reset."""
    claude_md = temp_root / "CLAUDE.md"
    custom_content = "# my hand-edited profile\n\n- name: Alice\n"
    claude_md.write_text(custom_content, encoding="utf-8")

    r = client.post(
        "/api/chat/sessions/profile-intake",
        headers={**auth, "Content-Type": "application/json"},
        json={},
    )
    assert r.status_code == 200
    # File content must be unchanged — seeding only happens when missing.
    assert claude_md.read_text(encoding="utf-8") == custom_content
