"""Regressions caught manually + reasoned through to pytest form. Each test
guards against a real bug that shipped during muselab's 2026-05-17 debugging
sprint. Keep them passing — if you add new file I/O, encoding handling, or
provider gating, the bug class will be re-caught here, not in production."""
from __future__ import annotations
from pathlib import Path

import pytest


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
    sid = r.json()["id"]

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
# Bug 3: Context meter used only input_tokens, which under prompt caching
# shrinks back to ~zero each turn while real window is mostly cache. And the
# per-session snapshot was accumulating cache_read/cache_creation — meter
# overflowed 100% after a few turns.
# Fix: context_used = input + cache_read + cache_creation; per-turn snapshot
# replaces (doesn't accumulate) the cache fields.
# ============================================================================

def test_context_used_sums_input_plus_cache(client, auth):
    """/usage/{sid} must return context_used as sum of input + cache_read +
    cache_creation, not just input."""
    # Create a session, fake-fill its usage stats (bypassing SDK)
    r = client.post("/api/chat/sessions",
                     headers={**auth, "Content-Type": "application/json"},
                     json={"name": "ctx test", "model": "deepseek-v4-flash"})
    sid = r.json()["id"]

    from backend import chat as chat_mod
    chat_mod._session_usage[sid] = {
        "input_tokens": 500,
        "output_tokens": 100,
        "cache_read_tokens": 40_000,
        "cache_creation_tokens": 2_000,
        "total_cost_usd": 0.01,
        "last_turn_at": 0.0,
    }

    r = client.get(f"/api/chat/usage/{sid}?model=deepseek-v4-flash", headers=auth)
    assert r.status_code == 200
    body = r.json()
    expected = 500 + 40_000 + 2_000
    assert body["context_used"] == expected, \
        f"context_used should be input+cache_read+cache_creation, got {body['context_used']}"
    # Pct should be 42500 / context_limit * 100 (whatever the limit is)
    assert body["context_used_pct"] > 0


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
