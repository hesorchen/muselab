import asyncio
import json
from pathlib import Path

import pytest

from backend.activity import ActivityService


def _service(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backend.activity.sessions.get_session",
        lambda sid: {"id": sid, "name": f"Session {sid}", "cwd": str(tmp_path / "ws")},
    )
    return ActivityService(tmp_path)


def test_one_row_per_session_and_latest_prompt(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    first = service.start("s1", summary="first task")
    service.finish("s1", "completed")
    second = service.start("s1", summary="second task")
    service.finish("s1", "completed")
    rows = service.list()
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"] == second["id"]
    assert rows[0]["task_summary"] == "second task"
    assert rows[0]["turn_count"] == 2


def test_start_records_source_kind_and_clears_it_for_plain_turn(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path, monkeypatch)
    scheduled = service.start(
        "s1",
        summary="daily report",
        kind="scheduled",
        source_id="task-1",
    )
    assert scheduled["kind"] == "scheduled"
    assert scheduled["source_id"] == "task-1"

    service.finish("s1", "completed")
    plain = service.start("s1", summary="follow-up")
    assert plain["kind"] == "turn"
    assert "source_id" not in plain


def test_summary_and_ack(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    service.start("s1", summary="inspect repository")
    item = service.finish("s1", "failed")
    summary = service.summary()
    assert summary["unread"] == 1
    assert summary["attention"] == 1
    assert summary["groups"] == {
        "review": 0, "running": 0, "failed": 1, "history": 0,
    }
    assert summary["group_unread"]["failed"] == 1
    assert summary["workspaces"][0]["unread"] == 1
    assert service.ack(item["id"]) == 1
    acknowledged = service.summary()
    assert acknowledged["unread"] == 0
    assert acknowledged["attention"] == 0
    assert acknowledged["groups"]["failed"] == 1
    assert acknowledged["group_unread"]["failed"] == 0


def test_completed_moves_from_review_to_history_after_ack(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    service.start("s1", summary="produce result")
    item = service.finish("s1", "completed")
    summary = service.summary()
    assert summary["groups"]["review"] == 1
    assert summary["groups"]["history"] == 0

    assert service.ack(item["id"]) == 1
    summary = service.summary()
    assert summary["groups"]["review"] == 0
    assert summary["groups"]["history"] == 1


def test_waiting_for_user_is_actionable_but_not_an_unread_result(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    service.start("s1", summary="ask a question")
    service.set_state("s1", "waiting_approval", detail="Waiting for user input")
    summary = service.summary()
    assert summary["running"] == 1
    assert summary["unread"] == 0
    assert summary["attention"] == 1
    assert summary["group_unread"]["running"] == 1

    assert service.resume("s1") is True
    summary = service.summary()
    assert summary["running"] == 1
    assert summary["attention"] == 0
    assert summary["group_unread"]["running"] == 0


def test_resume_never_revives_a_terminal_row(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    service.start("s1", summary="short task")
    service.finish("s1", "completed")

    assert service.resume("s1") is False
    row = service.list()[0]
    assert row["state"] == "completed"
    assert row["turn_count"] == 1
    assert row["finished_at"] is not None


def test_snapshot_keeps_rows_and_summary_on_one_ledger_view(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    service.start("s1", summary="produce result")
    service.finish("s1", "completed")

    snapshot = service.snapshot(500)
    assert len(snapshot["events"]) == 1
    assert snapshot["events"][0]["state"] == "completed"
    assert snapshot["summary"]["groups"]["review"] == 1


def test_list_sorts_by_latest_transition_not_storage_position(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    ticks = iter([1, 2, 3, 4, 5, 6])
    monkeypatch.setattr("backend.activity.time.time", lambda: next(ticks))
    service.start("s1", summary="old session")
    service.finish("s1", "completed")
    service.start("s2", summary="new session")
    service.finish("s2", "completed")
    service.start("s1", summary="old session, new turn")
    service.finish("s1", "completed")

    rows = service.list()
    assert [row["session_id"] for row in rows] == ["s1", "s2"]
    assert rows[0]["updated_at"] == 6


def test_pin_persists_without_rewriting_activity_time(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    ticks = iter([1, 2, 3, 4, 5])
    monkeypatch.setattr("backend.activity.time.time", lambda: next(ticks))
    old = service.start("s1", summary="older task")
    service.finish("s1", "completed")
    service.start("s2", summary="newer task")
    service.finish("s2", "completed")

    revision = service.revision
    update = service.set_pin(old["id"], True)
    assert update is not None
    pinned = update["item"]
    assert pinned["pinned"] is True
    assert pinned["updated_at"] == 2
    assert update["revision"] == service.revision == revision + 1
    assert update["generation"] == service.generation
    # The backend remains a chronological ledger. Pin priority belongs only
    # to the task center's timeline presentation.
    assert [row["session_id"] for row in service.list()] == ["s2", "s1"]

    restarted = ActivityService(tmp_path)
    restored = next(row for row in restarted.list() if row["session_id"] == "s1")
    assert restored["pinned"] is True
    resumed = restarted.start("s1", summary="same task, next turn")
    assert resumed["pinned"] is True
    unpinned = restarted.set_pin(old["id"], False)
    assert unpinned is not None
    assert unpinned["item"]["pinned"] is False
    assert restarted.set_pin("missing", True) is None


def test_custom_groups_persist_reorder_and_assignment_without_rewriting_task(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path, monkeypatch)
    ticks = iter([1, 2, 3, 4])
    monkeypatch.setattr("backend.activity.time.time", lambda: next(ticks))
    first = service.start("s1", summary="older task")
    service.finish("s1", "completed")
    service.start("s2", summary="newer task")
    service.finish("s2", "completed")
    before = dict(next(row for row in service.list()
                       if row["session_id"] == "s1"))

    research = service.create_group("Research", "violet")["group"]
    delivery = service.create_group("Delivery", "green")["group"]
    assigned = service.set_group(first["id"], research["id"])
    assert assigned is not None
    assert assigned["item"]["group_id"] == research["id"]
    after = next(row for row in service.list() if row["session_id"] == "s1")
    for field in (
        "updated_at", "started_at", "finished_at", "state", "read",
        "task_summary", "turn_count",
    ):
        assert after[field] == before[field]
    assert [row["session_id"] for row in service.list()] == ["s2", "s1"]

    ordered = service.reorder_groups([delivery["id"], research["id"]])
    assert [row["id"] for row in ordered["custom_groups"]] == [
        delivery["id"], research["id"],
    ]
    restarted = ActivityService(tmp_path)
    assert [row["id"] for row in restarted.list_groups()] == [
        delivery["id"], research["id"],
    ]
    restored = next(row for row in restarted.list()
                    if row["session_id"] == "s1")
    assert restored["group_id"] == research["id"]

    cleared = restarted.delete_group(research["id"])
    assert cleared is not None
    assert cleared["cleared_sessions"] == 1
    assert "group_id" not in next(
        row for row in restarted.list() if row["session_id"] == "s1"
    )
    state = json.loads(restarted.groups_path.read_text(encoding="utf-8"))
    assert state["assignments"] == {}


@pytest.mark.asyncio
async def test_group_delete_pushes_resync_for_all_affected_rows(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path, monkeypatch)
    item = service.start("s1", summary="grouped task")
    group = service.create_group("Temporary", "amber")["group"]
    service.set_group(item["id"], group["id"])

    async with service.subscribe() as queue:
        deleted = await asyncio.to_thread(service.delete_group, group["id"])
        payload = await asyncio.wait_for(queue.get(), timeout=1)

    assert deleted is not None
    assert payload["resync"] is True
    assert payload["custom_groups"] == []


@pytest.mark.asyncio
async def test_rename_persists_and_pushes_without_reordering_task(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path, monkeypatch)
    ticks = iter([1, 2, 3, 4])
    monkeypatch.setattr("backend.activity.time.time", lambda: next(ticks))
    first = service.start("s1", summary="older task")
    service.finish("s1", "completed")
    service.start("s2", summary="newer task")
    service.finish("s2", "completed")
    before = dict(next(row for row in service.list()
                       if row["session_id"] == "s1"))

    async with service.subscribe() as queue:
        update = await asyncio.to_thread(
            service.rename_session, "s1", "Renamed conversation",
        )
        payload = await asyncio.wait_for(queue.get(), timeout=1)

    assert update is not None
    assert update["item"]["id"] == first["id"]
    assert update["item"]["session_name"] == "Renamed conversation"
    assert payload["item"]["session_name"] == "Renamed conversation"
    assert payload["revision"] == update["revision"] == service.revision
    after = next(row for row in service.list() if row["session_id"] == "s1")
    for field in (
        "updated_at", "started_at", "finished_at", "state", "read",
        "task_summary", "turn_count",
    ):
        assert after[field] == before[field]
    assert [row["session_id"] for row in service.list()] == ["s2", "s1"]

    restarted = ActivityService(tmp_path)
    restored = next(row for row in restarted.list()
                    if row["session_id"] == "s1")
    assert restored["session_name"] == "Renamed conversation"
    assert service.rename_session("missing", "No row") is None


def test_restart_marks_running_as_failed(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    service.start("s1", summary="long task")
    restarted = ActivityService(tmp_path)
    row = restarted.list()[0]
    assert row["state"] == "failed"
    assert row["needs_attention"] is False
    assert row["read"] is False
    assert row["updated_at"] == row["finished_at"]
    assert json.loads((Path(tmp_path) / ".muselab" / "activity.json").read_text())


@pytest.mark.asyncio
async def test_subscriber_receives_task_transition_without_polling(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path, monkeypatch)
    async with service.subscribe() as queue:
        await asyncio.to_thread(service.start, "s1", summary="live task")
        payload = await asyncio.wait_for(queue.get(), timeout=1)

    assert payload["revision"] == 1
    assert payload["item"]["session_id"] == "s1"
    assert payload["item"]["state"] == "running"
    assert payload["summary"]["running"] == 1
    assert payload["summary"]["revision"] == 1
    assert payload["summary"]["generation"] == payload["generation"]


@pytest.mark.asyncio
async def test_slow_subscriber_gets_resync_marker(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    async with service.subscribe() as queue:
        await asyncio.to_thread(service.start, "s1", summary="first")
        await asyncio.sleep(0)
        await asyncio.to_thread(service.start, "s2", summary="second")
        await asyncio.sleep(0)
        payload = await asyncio.wait_for(queue.get(), timeout=1)

    assert payload["revision"] == 2
    assert payload["resync"] is True
    assert payload["summary"]["running"] == 2


def test_activity_event_ticket_requires_authentication(client, auth):
    denied = client.post("/api/activity/events-ticket")
    assert denied.status_code == 401

    response = client.post("/api/activity/events-ticket", headers=auth)
    assert response.status_code == 200
    assert response.json()["ticket"].startswith("activity.")


def test_activity_pin_endpoint_is_authenticated_and_returns_live_envelope(
    client,
    auth,
    tmp_path,
    monkeypatch,
):
    from backend import activity_api

    service = _service(tmp_path, monkeypatch)
    started = service.start("s1", summary="pin from task center")
    service.finish("s1", "completed")
    monkeypatch.setattr(activity_api, "activity", service)

    denied = client.patch(
        f"/api/activity/{started['id']}",
        json={"pinned": True},
    )
    assert denied.status_code == 401

    missing = client.patch(
        "/api/activity/missing",
        headers=auth,
        json={"pinned": True},
    )
    assert missing.status_code == 404

    response = client.patch(
        f"/api/activity/{started['id']}",
        headers=auth,
        json={"pinned": True},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["item"]["pinned"] is True
    assert payload["revision"] == service.revision
    assert payload["generation"] == service.generation


def test_activity_group_endpoints_manage_and_assign_custom_groups(
    client,
    auth,
    tmp_path,
    monkeypatch,
):
    from backend import activity_api

    service = _service(tmp_path, monkeypatch)
    started = service.start("s1", summary="group from task center")
    service.finish("s1", "completed")
    monkeypatch.setattr(activity_api, "activity", service)

    denied = client.post(
        "/api/activity/groups",
        json={"name": "Research", "color": "violet"},
    )
    assert denied.status_code == 401

    created = client.post(
        "/api/activity/groups",
        headers=auth,
        json={"name": "Research", "color": "violet"},
    )
    assert created.status_code == 200
    group = created.json()["group"]
    assert group["name"] == "Research"

    duplicate = client.post(
        "/api/activity/groups",
        headers=auth,
        json={"name": "research", "color": "blue"},
    )
    assert duplicate.status_code == 400

    assigned = client.put(
        f"/api/activity/{started['id']}/group",
        headers=auth,
        json={"group_id": group["id"]},
    )
    assert assigned.status_code == 200
    assert assigned.json()["item"]["group_id"] == group["id"]

    renamed = client.patch(
        f"/api/activity/groups/{group['id']}",
        headers=auth,
        json={"name": "Deep research", "color": "cyan"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["group"]["color"] == "cyan"

    listed = client.get("/api/activity/groups", headers=auth)
    assert listed.status_code == 200
    assert listed.json()["custom_groups"][0]["name"] == "Deep research"

    deleted = client.delete(
        f"/api/activity/groups/{group['id']}",
        headers=auth,
    )
    assert deleted.status_code == 200
    assert "group_id" not in service.list()[0]
