"""Tests for backend.scheduler — focused on the 2026-05-28 fresh/reuse
session_mode addition. _execute_task itself is not unit-tested (needs the
Claude SDK + a real model call); we cover the state-management surface
that fresh mode introduced and the back-compat path for old tasks."""
from __future__ import annotations


def _sched_mod(app_module):
    """Pull the scheduler module out of the reloaded backend.* tree.

    conftest's `app_module` fixture reloads everything under `backend.`
    against a tmp ROOT, so importing `backend.scheduler` here resolves
    against the test-isolated state file (~/tmp/.muselab/scheduler.json),
    not the dev's real one."""
    from backend import scheduler as sched
    # Each test gets a fresh in-memory state — the fixture creates a new
    # temp ROOT every time, so _STATE_FILE doesn't exist on disk yet, but
    # the module-global `_state` may carry over from a prior test in the
    # same process. Reset explicitly for isolation.
    sched._state = {"tasks": {}, "history": [], "unread_count": 0}
    return sched


def _daily_at(hour: int = 9, minute: int = 0) -> dict:
    return {"kind": "daily", "hour": hour, "minute": minute,
            "tz_offset_minutes": 480}


# ---- create_task ----

def test_create_task_fresh_does_not_preallocate_session(app_module):
    sched = _sched_mod(app_module)
    t = sched.create_task("daily-news", "summarize today",
                          _daily_at(9, 0), session_mode="fresh")
    assert t["session_mode"] == "fresh"
    # Fresh: session_id must be empty so the first run mints one.
    assert t["session_id"] == ""


def test_create_task_reuse_preallocates_session(app_module):
    sched = _sched_mod(app_module)
    t = sched.create_task("daily-log", "continue the log",
                          _daily_at(9, 0), session_mode="reuse")
    assert t["session_mode"] == "reuse"
    # Reuse: session_id is set to the bound session at creation time.
    assert t["session_id"]
    # And the session actually exists on disk.
    from backend import sessions as sess
    listing = sess.list_sessions()
    assert any(s["id"] == t["session_id"] for s in listing)


def test_create_task_rejects_invalid_session_mode(app_module):
    sched = _sched_mod(app_module)
    import pytest
    with pytest.raises(ValueError):
        sched.create_task("x", "p", _daily_at(), session_mode="bogus")


def test_create_task_default_is_fresh(app_module):
    """No session_mode kwarg → fresh, per 2026-05-28 design choice."""
    sched = _sched_mod(app_module)
    t = sched.create_task("t", "p", _daily_at())
    assert t["session_mode"] == "fresh"
    assert t["session_id"] == ""


# ---- _effective_session_mode ----

def test_effective_session_mode_falls_back_to_reuse_for_legacy(app_module):
    """Tasks that predate the field (no session_mode key) must fall back
    to 'reuse' so their bound-session behavior is preserved."""
    sched = _sched_mod(app_module)
    legacy_task = {
        "id": "old-task",
        "name": "legacy",
        "prompt": "p",
        "session_id": "sess-abc",
        "schedule": _daily_at(),
        "enabled": True,
    }
    # No "session_mode" key at all — this is the migration scenario.
    assert sched._effective_session_mode(legacy_task) == "reuse"


def test_effective_session_mode_respects_explicit_value(app_module):
    sched = _sched_mod(app_module)
    assert sched._effective_session_mode(
        {"session_mode": "fresh"}) == "fresh"
    assert sched._effective_session_mode(
        {"session_mode": "reuse"}) == "reuse"


# ---- delete_task ----

def test_delete_task_reuse_removes_bound_session(app_module):
    sched = _sched_mod(app_module)
    t = sched.create_task("rt", "p", _daily_at(), session_mode="reuse")
    sid = t["session_id"]
    from backend import sessions as sess
    assert any(s["id"] == sid for s in sess.list_sessions())
    assert sched.delete_task(t["id"]) is True
    # Bound session gone after delete.
    assert all(s["id"] != sid for s in sess.list_sessions())


def test_delete_task_fresh_keeps_all_sessions(app_module):
    """fresh-mode tasks may have minted N independent run sessions.
    Deleting the task must NOT cascade-delete them (per user spec
    2026-05-28: 'past runs may be valuable history snapshots')."""
    sched = _sched_mod(app_module)
    t = sched.create_task("ft", "p", _daily_at(), session_mode="fresh")
    # Simulate a couple of fresh runs having minted their own sessions.
    from backend import sessions as sess
    s1 = sess.create_session(name="[定时] ft · 05-28 09:00", model="")
    s2 = sess.create_session(name="[定时] ft · 05-29 09:00", model="")
    t["session_id"] = s2["id"]  # latest run
    sched._save_state()
    assert sched.delete_task(t["id"]) is True
    # Both fresh-mode sessions still on disk.
    surviving = {s["id"] for s in sess.list_sessions()}
    assert s1["id"] in surviving
    assert s2["id"] in surviving


def test_delete_task_legacy_no_mode_removes_session(app_module):
    """Legacy task with no session_mode field acts like reuse — bound
    session DOES get deleted. Guards against the migration silently
    flipping these to fresh and leaving orphans."""
    sched = _sched_mod(app_module)
    from backend import sessions as sess
    s = sess.create_session(name="[定时] legacy", model="")
    sched._state["tasks"]["legacy"] = {
        "id": "legacy",
        "name": "legacy",
        "prompt": "p",
        "session_id": s["id"],
        "schedule": _daily_at(),
        "enabled": True,
        # NB: no session_mode field — exercises the fallback path.
    }
    assert sched.delete_task("legacy") is True
    assert all(x["id"] != s["id"] for x in sess.list_sessions())


# ---- list_task_history ----

def test_list_task_history_filters_by_tid(app_module):
    sched = _sched_mod(app_module)
    sched._state["history"] = [
        {"task_id": "A", "ts": 100, "ok": True, "session_id": "s1"},
        {"task_id": "B", "ts": 110, "ok": True, "session_id": "s2"},
        {"task_id": "A", "ts": 120, "ok": False, "session_id": "s3",
         "error": "boom"},
        {"task_id": "C", "ts": 130, "ok": True, "session_id": "s4"},
    ]
    out = sched.list_task_history("A")
    assert len(out) == 2
    # Newest first.
    assert out[0]["ts"] == 120
    assert out[1]["ts"] == 100


def test_list_task_history_respects_limit(app_module):
    sched = _sched_mod(app_module)
    sched._state["history"] = [
        {"task_id": "X", "ts": i, "ok": True, "session_id": f"s{i}"}
        for i in range(50)
    ]
    out = sched.list_task_history("X", limit=5)
    assert len(out) == 5
    # Newest 5 by ts.
    assert [e["ts"] for e in out] == [49, 48, 47, 46, 45]


def test_list_task_history_empty_for_unknown_task(app_module):
    sched = _sched_mod(app_module)
    sched._state["history"] = [
        {"task_id": "A", "ts": 100, "ok": True, "session_id": "s1"},
    ]
    assert sched.list_task_history("NEVER-EXISTED") == []


# ---- update_task: mode transitions ----

def test_update_task_fresh_to_reuse_seeds_session(app_module):
    """Switching fresh → reuse on a task with no prior runs should mint
    a bound session so the next run has somewhere to land."""
    sched = _sched_mod(app_module)
    t = sched.create_task("u", "p", _daily_at(), session_mode="fresh")
    assert t["session_id"] == ""
    updated = sched.update_task(t["id"], session_mode="reuse")
    assert updated["session_mode"] == "reuse"
    assert updated["session_id"]   # was seeded
    from backend import sessions as sess
    assert any(s["id"] == updated["session_id"] for s in sess.list_sessions())


def test_update_task_reuse_to_fresh_keeps_session(app_module):
    """reuse → fresh: the old bound session stays as the 'most recent
    run' pointer; not deleted (it has the user's prior conversation)."""
    sched = _sched_mod(app_module)
    t = sched.create_task("u", "p", _daily_at(), session_mode="reuse")
    old_sid = t["session_id"]
    updated = sched.update_task(t["id"], session_mode="fresh")
    assert updated["session_mode"] == "fresh"
    # session_id retained as the "latest run" anchor.
    assert updated["session_id"] == old_sid
    from backend import sessions as sess
    assert any(s["id"] == old_sid for s in sess.list_sessions())


def test_update_task_rejects_invalid_mode(app_module):
    sched = _sched_mod(app_module)
    t = sched.create_task("u", "p", _daily_at())
    import pytest
    with pytest.raises(ValueError):
        sched.update_task(t["id"], session_mode="bogus")


# ---- API surface ----

def test_api_create_task_with_session_mode(client, auth, app_module):
    """End-to-end: POST /api/scheduler/tasks honors session_mode."""
    _sched_mod(app_module)  # reset module state
    r = client.post("/api/scheduler/tasks", headers=auth, json={
        "name": "api-fresh",
        "prompt": "do a thing",
        "schedule": {"kind": "daily", "hour": 9, "minute": 0,
                     "tz_offset_minutes": 480},
        "model": "",
        "session_mode": "fresh",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_mode"] == "fresh"
    assert body["session_id"] == ""


def test_api_create_task_rejects_bogus_mode(client, auth, app_module):
    """Pydantic pattern validator returns 422 for non-{fresh,reuse}."""
    _sched_mod(app_module)
    r = client.post("/api/scheduler/tasks", headers=auth, json={
        "name": "api-bad",
        "prompt": "do a thing",
        "schedule": {"kind": "daily", "hour": 9, "minute": 0,
                     "tz_offset_minutes": 480},
        "session_mode": "weird",
    })
    assert r.status_code == 422


def test_api_create_task_default_is_fresh(client, auth, app_module):
    """Omitting session_mode → server applies the Pydantic default
    'fresh'. Mirrors the in-memory create_task default."""
    _sched_mod(app_module)
    r = client.post("/api/scheduler/tasks", headers=auth, json={
        "name": "api-default",
        "prompt": "p",
        "schedule": {"kind": "daily", "hour": 9, "minute": 0,
                     "tz_offset_minutes": 480},
    })
    assert r.status_code == 200, r.text
    assert r.json()["session_mode"] == "fresh"


def test_api_task_history_endpoint(client, auth, app_module):
    sched = _sched_mod(app_module)
    r = client.post("/api/scheduler/tasks", headers=auth, json={
        "name": "withhist",
        "prompt": "p",
        "schedule": {"kind": "daily", "hour": 9, "minute": 0,
                     "tz_offset_minutes": 480},
        "session_mode": "fresh",
    })
    tid = r.json()["id"]
    # Inject synthetic history directly (bypassing _execute_task).
    sched._state["history"] = [
        {"task_id": tid, "ts": 100, "ok": True, "session_id": "s1",
         "reply_preview": "first"},
        {"task_id": tid, "ts": 200, "ok": True, "session_id": "s2",
         "reply_preview": "second"},
        {"task_id": "OTHER", "ts": 150, "ok": True, "session_id": "x",
         "reply_preview": "unrelated"},
    ]
    r = client.get(f"/api/scheduler/tasks/{tid}/history", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert len(body["history"]) == 2
    # Newest first; "OTHER" task excluded.
    assert body["history"][0]["reply_preview"] == "second"
    assert body["history"][1]["reply_preview"] == "first"


def test_api_task_history_404_for_unknown_task(client, auth, app_module):
    _sched_mod(app_module)
    r = client.get("/api/scheduler/tasks/never-existed/history",
                   headers=auth)
    assert r.status_code == 404
