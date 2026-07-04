"""Regressions caught manually + reasoned through to pytest form. Each test
guards against a real bug that shipped during muselab's 2026-05-17 debugging
sprint. Keep them passing — if you add new file I/O, encoding handling, or
provider gating, the bug class will be re-caught here, not in production."""
from __future__ import annotations



# ============================================================================
# Bug 1: UnicodeEncodeError on emoji in session messages when the system
# default encoding wasn't UTF-8 (historically observed on non-UTF-8 locales).
# Fix: every read_text/write_text in backend now passes encoding='utf-8'.
# ============================================================================

def test_session_persist_handles_emoji_and_cjk(client, auth):
    """Session save+load must round-trip emoji / rare CJK without crashing.
    Before df3f567, write_text relied on the system codepage — non-UTF-8
    locales would crash with `UnicodeEncodeError: ... can't encode '\\U0001f604'`."""
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


def test_codex_gateway_ctx_limit_fallback_is_safe_200k(client, auth):
    """Codex Gateway model cards may advertise 400K, but local sidecars and
    account tiers can fail earlier, so the meter uses the conservative 200K
    fallback until the gateway reports a smaller runtime window."""
    r = client.get("/api/chat/usage/no-such-codex-sid?model=codex:gpt-5.5",
                   headers=auth)
    assert r.status_code == 200
    assert r.json()["context_limit"] == 200_000


# ============================================================================
# Bug 3b (2026-06-06): context ring read ~5x too low. The Claude entries in
# MODEL_CONTEXT_LIMITS claimed a 1M window, but the bundled CLI's
# get_context_usage reports 200K — so /usage divided by 1M and showed e.g.
# 8.8% when the breakdown popup (SDK truth) showed 44%. Two-part fix:
#   1. Claude table fallback corrected to 200K.
#   2. The real per-account window (SDK maxTokens) is persisted per-session so
#      it survives restart and overrides the table (incl. genuine-1M accounts).
# ============================================================================

def test_claude_ctx_limit_fallback_is_200k_not_1m(client, auth):
    """Never-measured Claude session → /usage denominator is the corrected
    200K fallback, not the old 1M that made the meter read ~5x low."""
    r = client.get("/api/chat/usage/no-such-claude-sid?model=claude-opus-4-8",
                    headers=auth)
    assert r.status_code == 200
    assert r.json()["context_limit"] == 200_000


def test_persisted_sdk_window_overrides_table(client, auth):
    """A per-session SDK-measured window (persisted via set_session_ctx_window)
    must win over the hardcoded table — this is how genuine-1M accounts get the
    right denominator after their first turn, and how the meter stays correct
    across a muselab restart with no live client."""
    r = client.post("/api/chat/sessions",
                     headers={**auth, "Content-Type": "application/json"},
                     json={"name": "ctx persist", "model": "claude-opus-4-8"})
    sid = r.json()["id"]

    from backend import chat as chat_mod
    from backend import sessions as sess_mod
    # Simulate a turn having measured a real 1M window on this account.
    sess_mod.set_session_ctx_window(sid, 1_000_000)
    chat_mod._session_usage[sid] = {
        "input_tokens": 2, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "total_cost_usd": 0.0, "last_turn_at": 0.0,
        "context_used": 250_000, "context_used_pct": 0.0,
        "context_limit": 200_000,   # stale table-shaped value, must lose
    }
    r = client.get(f"/api/chat/usage/{sid}?model=claude-opus-4-8", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["context_limit"] == 1_000_000, \
        f"persisted SDK window must win, got {body['context_limit']}"
    assert body["context_used_pct"] == round(250_000 / 1_000_000 * 100, 1)
    # Persistence must survive a cold cache (the restart case): drop the
    # in-memory snapshot and the limit still resolves from the sidecar.
    chat_mod._session_usage.pop(sid, None)
    r = client.get(f"/api/chat/usage/{sid}?model=claude-opus-4-8", headers=auth)
    assert r.json()["context_limit"] == 1_000_000


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


# ============================================================================
# Bug 9: mcp_status crashed with `too many values to unpack` because the
# `_clients` cache key gained a third dimension (effort) but this endpoint
# still unpacked into 2 vars. Caused every call to the MCP status pane to
# 500 once any client was alive.
# Fix: index `key[0]`, `key[1]` instead of unpacking.
# ============================================================================

def test_mcp_status_handles_three_tuple_cache_key(client, auth, monkeypatch):
    """Stub `_clients` with a 3-tuple key and a fake client, then hit the
    endpoint — must succeed (return per-session list), not 500."""
    from backend import chat as _chat

    class _FakeClient:
        async def get_mcp_status(self):
            return {"connected": ["muselab"]}

    fake_key = ("fake-sid-abc", "claude-sonnet-4-6", "")
    saved = dict(_chat._clients)
    _chat._clients[fake_key] = _FakeClient()
    try:
        r = client.get("/api/settings/mcp/status", headers=auth)
    finally:
        _chat._clients.clear()
        _chat._clients.update(saved)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("clients"), list)
    assert any(c.get("session_id") == "fake-sid-abc" for c in body["clients"])


# ============================================================================
# Bug 10: PUT /api/settings updated MUSELAB_DEFAULT_MODEL but chat.py only
# reads `settings.MODEL` (← MUSELAB_MODEL). Saving "default model" looked
# successful but new sessions still used the .env value.
# Fix: also write MUSELAB_MODEL + push back into settings.MODEL / chat.MODEL.
# ============================================================================

def test_default_model_change_takes_effect_immediately(client, auth, monkeypatch):
    """Changing default_model via Settings must update what new sessions get."""
    from backend import settings as _settings, chat as _chat

    original_model = _settings.MODEL
    original_chat_model = _chat.MODEL
    # Avoid mutating the user's real .env during the test — point both at a
    # temp path. _write_env writes both `MUSELAB_DEFAULT_MODEL` AND
    # `MUSELAB_MODEL`, and pushes the latter back into the module globals.
    import tempfile
    import pathlib
    tmp_env = pathlib.Path(tempfile.mkstemp(suffix=".env")[1])
    from backend import api_settings as _api_settings
    monkeypatch.setattr(_api_settings, "ENV_PATH", tmp_env)

    try:
        r = client.put(
            "/api/settings",
            headers={**auth, "Content-Type": "application/json"},
            json={"default_model": "claude-haiku-4-5-20251001"},
        )
        assert r.status_code == 200, r.text
        # Both modules must see the new value — without the explicit
        # push-back, chat.py would keep serving the import-time `MODEL`.
        assert _settings.MODEL == "claude-haiku-4-5-20251001"
        assert _chat.MODEL == "claude-haiku-4-5-20251001"
    finally:
        _settings.MODEL = original_model
        _chat.MODEL = original_chat_model
        tmp_env.unlink(missing_ok=True)


# ============================================================================
# Bug 11: scheduler used naive `datetime.fromtimestamp()` (server-local) for
# the schedule hh:mm. On a Docker container with default UTC clock, "daily
# 09:00" fired at 09:00 UTC = 17:00 Beijing — silently shifted vs what the
# user picked.
# Fix: ScheduleIn accepts `tz_offset_minutes` (browser supplies east-positive
# value); _compute_next_run interprets hh:mm in that TZ.
# ============================================================================

def test_compute_next_run_respects_tz_offset_minutes():
    """Daily 09:00 in +480 (Beijing) and 09:00 in 0 (UTC) must produce
    timestamps 8 hours apart for the same calendar day."""
    from backend.scheduler import _compute_next_run
    import datetime as _dt
    # ref_ts = 2026-05-22 00:00:00 UTC (right after midnight, so daily 09:00
    # today hasn't fired yet in either TZ).
    ref = _dt.datetime(2026, 5, 22, 0, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
    beijing = _compute_next_run(
        {"kind": "daily", "hour": 9, "minute": 0, "tz_offset_minutes": 480}, ref)
    utc = _compute_next_run(
        {"kind": "daily", "hour": 9, "minute": 0, "tz_offset_minutes": 0}, ref)
    assert beijing is not None and utc is not None
    # Beijing 09:00 = UTC 01:00 — that's 8 hours BEFORE UTC 09:00.
    assert utc - beijing == 8 * 3600, (
        f"expected 8h delta, got {(utc - beijing) / 3600:.2f}h "
        f"(beijing={beijing}, utc={utc})"
    )


# ============================================================================
# Bug 12: write_file silently used non-atomic write_text → mid-write crash
# could leave the user's file truncated. And had no size cap → could ingest
# arbitrarily large bodies.
# Fix: atomic_write_text + MAX_WRITE_BYTES enforcement.
# ============================================================================

def test_write_file_uses_atomic_write(client, auth, temp_root):
    """Writing a file via /api/files/write must leave no .tmp.* siblings
    behind on success (signature of atomic_write_text vs direct write_text)."""
    r = client.put(
        "/api/files/write",
        headers={**auth, "Content-Type": "application/json"},
        json={"path": "atomic_test.md", "content": "# hello atomic\n"},
    )
    assert r.status_code == 200, r.text
    target = temp_root / "atomic_test.md"
    assert target.read_text(encoding="utf-8") == "# hello atomic\n"
    # No leftover .tmp.<pid> file from atomic_write_text's tmpfile+rename.
    tmps = list(temp_root.glob("atomic_test.md.tmp.*"))
    assert tmps == [], f"atomic_write_text left tmpfile behind: {tmps}"


def test_write_file_rejects_oversize_payload(client, auth, temp_root):
    """Body > MAX_WRITE_BYTES must 413 instead of writing the file."""
    from backend.files import MAX_WRITE_BYTES
    too_big = "x" * (MAX_WRITE_BYTES + 1)
    r = client.put(
        "/api/files/write",
        headers={**auth, "Content-Type": "application/json"},
        json={"path": "big.txt", "content": too_big},
    )
    assert r.status_code == 413, r.text
    assert not (temp_root / "big.txt").exists()


# ============================================================================
# Bug 13: backfill_turn_counts re-walked every JSONL on every startup. Should
# run once, then write a sentinel.
# ============================================================================

def test_backfill_writes_sentinel_after_run(monkeypatch, tmp_path):
    """First call writes sentinel; second call is a no-op (early-returns)."""
    import asyncio
    from backend import main as _main
    from backend import sessions as _sess

    saved_sess_dir = _sess.SESS_DIR
    monkeypatch.setattr(_sess, "SESS_DIR", tmp_path)
    sentinel = tmp_path / ".backfill_done"
    try:
        assert not sentinel.exists()
        asyncio.run(_main._backfill_turn_counts())
        assert sentinel.exists(), "sentinel not written after first run"
        # Second call should early-return without touching anything.
        mtime_before = sentinel.stat().st_mtime
        asyncio.run(_main._backfill_turn_counts())
        assert sentinel.stat().st_mtime == mtime_before, (
            "backfill ran twice — sentinel gate ineffective"
        )
    finally:
        monkeypatch.setattr(_sess, "SESS_DIR", saved_sess_dir)


# ============================================================================
# tool_use payload completeness — Edit / Write / MultiEdit transparently
# carry old_string / new_string / edits / content so the frontend can render
# a diff strip. Before this, _SLIM_INPUT_FIELDS dropped those silently and
# the FE only had file_path to show.
# ============================================================================

def test_render_tool_use_passes_edit_diff_fields():
    from backend.chat import _render_tool_use

    class _Block:
        id = "tu_test_001"
        name = "Edit"
        input = {
            "file_path": "/tmp/foo.py",
            "old_string": "x = 1\ny = 2",
            "new_string": "x = 10\ny = 20",
        }

    out = _render_tool_use(_Block())
    assert out["name"] == "Edit"
    assert out["input"]["old_string"] == "x = 1\ny = 2"
    assert out["input"]["new_string"] == "x = 10\ny = 20"
    assert out["input"]["file_path"] == "/tmp/foo.py"


def test_render_tool_use_caps_oversized_string_input():
    from backend.chat import _render_tool_use, _MAX_INPUT_FIELD_LEN

    big = "A" * (_MAX_INPUT_FIELD_LEN + 2000)

    class _Block:
        id = "tu_test_002"
        name = "Write"
        input = {"file_path": "/tmp/big.txt", "content": big}

    out = _render_tool_use(_Block())
    c = out["input"]["content"]
    assert c.startswith("A" * _MAX_INPUT_FIELD_LEN)
    assert "[truncated" in c, f"expected truncation marker, got tail: {c[-80:]!r}"


# ============================================================================
# tool_result Bash parsing — when the result body carries CLI's pseudo-XML
# wrapped output, _render_tool_result populates a `bash` field with
# stdout / stderr / exit_code so the FE can color-code each part.
# ============================================================================

def test_render_tool_result_extracts_bash_structure():
    from backend.chat import _render_tool_result

    body = (
        "<stdout>hello world\nline two</stdout>"
        "<stderr>warning: something</stderr>"
        "<exit_code>0</exit_code>"
    )

    class _Block:
        tool_use_id = "tu_b1"
        content = body
        is_error = False

    out = _render_tool_result(_Block(), tool_name="Bash")
    assert out["tool_name"] == "Bash"
    assert out["text"] == body
    assert out["bash"]["stdout"] == "hello world\nline two"
    assert out["bash"]["stderr"] == "warning: something"
    assert out["bash"]["exit_code"] == 0


def test_render_tool_result_no_bash_field_when_not_bash():
    from backend.chat import _render_tool_result

    class _Block:
        tool_use_id = "tu_r1"
        content = "file body line 1\nfile body line 2"
        is_error = False

    out = _render_tool_result(_Block(), tool_name="Read")
    assert out["tool_name"] == "Read"
    assert "bash" not in out
    # Full body is forwarded (not the legacy 500-cap preview).
    assert out["text"] == "file body line 1\nfile body line 2"


# ============================================================================
# error event classification — auth / quota / network / cross_vendor / session
# get distinct kind + cta so the FE can render a typed action button.
# ============================================================================

def test_classify_stream_error_buckets():
    from backend.chat import _classify_stream_error
    cases = [
        ("Invalid API key", "auth", "open_settings", False),
        ("ANTHROPIC_API_KEY not configured", "auth", "open_settings", False),
        ("HTTP 401 Unauthorized", "auth", "open_settings", False),
        ("429 Too Many Requests", "quota", "switch_model", True),
        ("quota exceeded", "quota", "switch_model", True),
        ("Connection refused", "network", "retry", True),
        ("Request timed out", "network", "retry", True),
        ("thinking signature missing", "cross_vendor", "compact_or_fork", True),
        ("Session ID already in use", "session", "retry", True),
        ("something weird", "unknown", "retry", True),
    ]
    for msg, expected_kind, expected_cta, expected_retry in cases:
        got = _classify_stream_error(msg)
        assert got["kind"] == expected_kind, f"{msg!r}: kind={got['kind']!r}"
        assert got["cta"] == expected_cta, f"{msg!r}: cta={got['cta']!r}"
        assert got["retryable"] == expected_retry, f"{msg!r}: retryable={got['retryable']!r}"


# ============================================================================
# ask_user_question preview field — pass-through to FE so the model can attach
# rich content (markdown, mockup, code snippet) to each option.
# ============================================================================

def test_normalize_questions_passes_preview_through():
    from backend.ask_user_question import _normalize_questions
    raw = [{
        "question": "Pick a layout",
        "options": [
            {"label": "Sidebar", "preview": "```\n[nav][content]\n```"},
            {"label": "Topbar"},   # no preview
        ],
    }]
    out = _normalize_questions(raw)
    assert len(out) == 1
    opts = out[0]["options"]
    assert opts[0]["label"] == "Sidebar"
    assert opts[0]["preview"] == "```\n[nav][content]\n```"
    # Options without `preview` must not get a fake one (FE checks
    # `opt.preview` truthiness to decide whether to show the footnote).
    assert "preview" not in opts[1]


# ============================================================================
# cost_dashboard vendor + label fields — "GLM/MiniMax 看不懂" UX fix.
# Before, by_model rows were bare model ids ("glm-4.7") with `cost: 0` for
# every third-party vendor (vendor doesn't report USD), giving the false
# impression that GLM/MiniMax were free. Now each row carries:
#   - label   : friendly name (endpoints.label_for)
#   - vendor  : group ("Claude" / "DeepSeek" / "GLM" / "MiniMax" / "Unknown")
#   - cost_reported: false for vendors that don't emit USD so FE can mark "$ —"
# Plus a top-level by_vendor rollup for at-a-glance totals.
# ============================================================================

def test_vendor_label_for_known_models(monkeypatch, tmp_path):
    from backend import endpoints as ep
    monkeypatch.setattr(ep, "OVERRIDES_PATH", tmp_path / "provider_overrides.json")
    ep._OVERRIDES_CACHE = None
    ep._CATALOG_CACHE = None
    ep._SORTED_CATALOG_CACHE = None
    from backend.chat import _vendor_label_for
    assert _vendor_label_for("claude-sonnet-4-6")   == "Claude"
    assert _vendor_label_for("claude-haiku-4-5")    == "Claude"
    assert _vendor_label_for("deepseek-v4-pro")     == "DeepSeek"
    assert _vendor_label_for("glm-4.7")             == "智谱 GLM"
    assert _vendor_label_for("minimax-m2.7")        == "MiniMax"
    # Re-added / new in 2026-05-22 wave — kimi/qwen/mimo now route to
    # their own provider, not the historical "Unknown" fallback.
    assert _vendor_label_for("kimi-k2.6")           == "Kimi"
    assert _vendor_label_for("qwen3-max")           == "Qwen"
    assert _vendor_label_for("qwen-plus")           == "Qwen"
    assert _vendor_label_for("mimo-v2.5-pro")       == "Xiaomi MiMo"
    # Unknown / vendor wrapper id — note "kimi-mystery" used to land here
    # back when Kimi was uncatalogued; after 2026-05-22 it routes to Kimi
    # (longest-prefix match treats it as a Kimi variant), so the unknown
    # case is now genuinely-foreign vendors like the gpt-5 line.
    assert _vendor_label_for("gpt-5")               == "Unknown"
    assert _vendor_label_for("")                    == "Unknown"


def test_cost_reported_only_for_claude():
    from backend.chat import _cost_reported_for
    assert _cost_reported_for("claude-sonnet-4-6") is True
    assert _cost_reported_for("deepseek-v4-pro")   is False
    assert _cost_reported_for("glm-4.7")           is False
    assert _cost_reported_for("minimax-m2.7")      is False
    assert _cost_reported_for("kimi-k2.6")         is False
    assert _cost_reported_for("qwen3-max")         is False
    assert _cost_reported_for("mimo-v2.5-pro")     is False
    assert _cost_reported_for("")                  is False


def test_cost_dashboard_includes_vendor_rollup_and_label(client, auth):
    """The endpoint must always return by_vendor + label / vendor /
    cost_reported on by_model rows so the FE doesn't have to fall back
    on its own mapping when the backend already knows the vendor."""
    r = client.get("/api/chat/cost-dashboard?days=7", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "by_vendor" in body
    assert isinstance(body["by_vendor"], list)
    # Per-model rows must carry the new keys whenever there's data; the
    # default-fixture archive may produce an empty by_model, so we only
    # check shape when populated.
    for row in body.get("by_model", []):
        assert "label" in row
        assert "vendor" in row
        assert "cost_reported" in row
        assert isinstance(row["cost_reported"], bool)
    for row in body["by_vendor"]:
        assert "vendor" in row
        assert "cost_reported" in row
        assert isinstance(row["cost_reported"], bool)
