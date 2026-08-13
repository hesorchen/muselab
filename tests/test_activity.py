import asyncio
import json
import stat
from pathlib import Path

import pytest

from backend.activity import ActivityService


def _service(tmp_path, monkeypatch):
    service = ActivityService(tmp_path)
    workspace = str(tmp_path / "ws")
    monkeypatch.setattr(
        service,
        "_metadata",
        lambda sid: (f"Session {sid}", workspace, "ws"),
    )
    return service


def test_deferred_activity_initialization_is_import_safe(tmp_path):
    storage = tmp_path / ".muselab"
    storage.mkdir(mode=0o777)
    storage.chmod(0o777)
    activity_path = storage / "activity.json"
    groups_path = storage / "activity-groups.json"
    transaction_path = storage / "activity-transaction.json"
    activity_path.write_text("[]", encoding="utf-8")
    groups_path.write_text("{}", encoding="utf-8")
    transaction_path.write_text(json.dumps({
        "version": 1,
        "events": [{
            "id": "event-1", "session_id": "s1", "state": "completed",
            "read": False, "started_at": 1, "finished_at": 2, "updated_at": 2,
        }],
        "group_state": {
            "version": 2, "groups": [], "assignments": {}, "order": [],
        },
    }), encoding="utf-8")
    for path in (activity_path, groups_path, transaction_path):
        path.chmod(0o666)

    def snapshot(path: Path) -> tuple[bytes, int, int]:
        info = path.stat()
        return path.read_bytes(), stat.S_IMODE(info.st_mode), info.st_mtime_ns

    before = {
        path: snapshot(path)
        for path in (activity_path, groups_path, transaction_path)
    }
    directory_before = stat.S_IMODE(storage.stat().st_mode)

    service = ActivityService(tmp_path, initialize_runtime_state=False)

    assert stat.S_IMODE(storage.stat().st_mode) == directory_before
    assert all(snapshot(path) == expected for path, expected in before.items())

    service.initialize_runtime_state()
    assert not transaction_path.exists()
    assert service.list()[0]["session_id"] == "s1"
    assert stat.S_IMODE(storage.stat().st_mode) == 0o700
    assert stat.S_IMODE(activity_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(groups_path.stat().st_mode) == 0o600


def test_deferred_activity_initialization_retries_failed_reconciliation_write(
    tmp_path,
    monkeypatch,
):
    storage = tmp_path / ".muselab"
    storage.mkdir()
    activity_path = storage / "activity.json"
    activity_path.write_text(json.dumps([{
        "id": "event-1",
        "session_id": "s1",
        "state": "running",
        "read": True,
        "started_at": 1,
        "updated_at": 1,
    }]), encoding="utf-8")

    service = ActivityService(tmp_path, initialize_runtime_state=False)
    real_save = service._save
    attempts = 0

    def flaky_save() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary activity write failure")
        real_save()

    monkeypatch.setattr(service, "_save", flaky_save)
    with pytest.raises(OSError, match="temporary activity write failure"):
        service.initialize_runtime_state()

    assert service._initialized is False
    row = service.list()[0]
    assert attempts == 2
    assert service._initialized is True
    assert row["state"] == "failed"
    assert json.loads(activity_path.read_text(encoding="utf-8"))[0]["state"] == "failed"


def test_activity_storage_repairs_and_preserves_private_permissions(
    tmp_path,
    monkeypatch,
):
    storage = tmp_path / ".muselab"
    storage.mkdir(mode=0o777)
    storage.chmod(0o777)
    activity_path = storage / "activity.json"
    groups_path = storage / "activity-groups.json"
    activity_path.write_text("[]", encoding="utf-8")
    groups_path.write_text(
        json.dumps({"version": 2, "groups": [], "assignments": {}, "order": []}),
        encoding="utf-8",
    )
    activity_path.chmod(0o666)
    groups_path.chmod(0o666)

    service = _service(tmp_path, monkeypatch)
    assert stat.S_IMODE(storage.stat().st_mode) == 0o700
    assert stat.S_IMODE(activity_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(groups_path.stat().st_mode) == 0o600

    item = service.start("s1", summary="private task prompt")
    group = service.create_group("Private", "blue")["group"]

    def fail_group_save() -> None:
        raise OSError("leave the private transaction journal for recovery")

    monkeypatch.setattr(service, "_save_group_state", fail_group_save)
    with pytest.raises(OSError, match="private transaction journal"):
        service.set_group(item["id"], group["id"])

    transaction_path = storage / "activity-transaction.json"
    assert transaction_path.exists()
    assert stat.S_IMODE(transaction_path.stat().st_mode) == 0o600


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
    assert plain["activity_source"] == "direct"
    assert "source_id" not in plain


def test_activity_source_tracks_direct_queue_and_background_delivery(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path, monkeypatch)

    direct = service.start("s1", summary="watched reply")
    assert direct["activity_source"] == "direct"
    service.finish("s1", "completed")

    queued = service.start(
        "s1", summary="durable follow-up", activity_source="queued")
    assert queued["activity_source"] == "queued"
    assert service.finish("s1", "completed")["activity_source"] == "queued"

    service.start("s1", summary="launch background work")
    settled = service.finish(
        "s1", "completed", activity_source="background")
    assert settled["activity_source"] == "background"


def test_stale_background_owner_cannot_finish_new_foreground_turn(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path, monkeypatch)
    service.start(
        "s1", summary="old background", activity_source="background",
        owner_id="turn-old")
    service.start(
        "s1", summary="new foreground", activity_source="direct",
        owner_id="turn-new")

    stale = service.finish(
        "s1", "completed", activity_source="background",
        owner_id="turn-old")
    assert stale["state"] == "running"
    assert stale["owner_id"] == "turn-new"
    assert stale["activity_source"] == "direct"

    current = service.finish(
        "s1", "completed", activity_source="direct",
        owner_id="turn-new")
    assert current["state"] == "completed"


def test_owner_finish_without_started_row_is_noop(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)

    assert service.finish(
        "missing-session", "completed", owner_id="missing-owner"
    ) == {}
    assert service.list() == []


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


def test_repeated_same_terminal_finish_preserves_ack_and_is_a_noop(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path, monkeypatch)
    started = service.start("s1", summary="produce result")
    service.finish("s1", "completed")
    assert service.ack(started["id"]) == 1

    before = service.list()[0]
    revision = service.revision
    persisted = service.path.read_bytes()

    repeated = service.finish(
        "s1", "completed", activity_source="background")

    assert repeated == before
    assert repeated["read"] is True
    assert repeated["activity_source"] == "direct"
    assert service.revision == revision
    assert service.path.read_bytes() == persisted
    assert service.summary()["groups"]["history"] == 1


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

    restarted = _service(tmp_path, monkeypatch)
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
    restarted = _service(tmp_path, monkeypatch)
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


def test_group_layout_order_includes_ungrouped_and_persists(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path, monkeypatch)
    research = service.create_group("Research", "violet")["group"]
    delivery = service.create_group("Delivery", "green")["group"]

    requested = ["__ungrouped__", delivery["id"], research["id"]]
    order = [delivery["id"], research["id"], "__ungrouped__"]
    update = service.reorder_groups(requested)
    assert update["group_order"] == order
    assert service.group_state()["group_order"] == order

    restarted = _service(tmp_path, monkeypatch)
    assert restarted.group_state()["group_order"] == order
    assert [row["id"] for row in restarted.list_groups()] == [
        delivery["id"], research["id"],
    ]


def test_activity_rows_reorder_within_and_across_groups_and_persist(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path, monkeypatch)
    items = [service.start(f"s{index}", summary=f"task {index}")
             for index in range(1, 5)]
    group_a = service.create_group("A", "blue")["group"]
    group_b = service.create_group("B", "green")["group"]

    service.set_group(items[0]["id"], group_a["id"], before_event_id="")
    service.set_group(items[1]["id"], group_a["id"], before_event_id="")
    service.set_group(
        items[2]["id"], group_a["id"], before_event_id=items[1]["id"],
    )
    service.set_group(items[3]["id"], group_b["id"], before_event_id="")

    def ordered(group_id):
        return [
            row["id"] for row in sorted(
                (row for row in service.list(500)
                 if str(row.get("group_id") or "") == group_id),
                key=lambda row: row["group_order"],
            )
        ]

    assert ordered(group_a["id"]) == [
        items[0]["id"], items[2]["id"], items[1]["id"],
    ]
    service.set_group(
        items[2]["id"], group_b["id"], before_event_id=items[3]["id"],
    )
    assert ordered(group_a["id"]) == [items[0]["id"], items[1]["id"]]
    assert ordered(group_b["id"]) == [items[2]["id"], items[3]["id"]]

    restarted = _service(tmp_path, monkeypatch)
    restored = {
        row["id"]: row for row in restarted.list(500)
        if str(row.get("group_id") or "") == group_b["id"]
    }
    assert sorted(restored, key=lambda event_id: restored[event_id]["group_order"]) == [
        items[2]["id"], items[3]["id"],
    ]


def test_group_placement_rolls_back_memory_and_files_when_second_save_fails(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path, monkeypatch)
    item = service.start("s1", summary="task")
    group = service.create_group("Ordered", "blue")["group"]
    real_save = service._save
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated activity write failure")
        real_save()

    monkeypatch.setattr(service, "_save", fail_once)
    with pytest.raises(OSError, match="simulated activity write failure"):
        service.set_group(item["id"], group["id"], before_event_id="")

    current = next(row for row in service.list() if row["id"] == item["id"])
    assert "group_id" not in current
    assert "group_order" not in current
    assert service._group_assignments == {}

    restarted = _service(tmp_path, monkeypatch)
    restored = next(row for row in restarted.list() if row["id"] == item["id"])
    assert "group_id" not in restored
    assert "group_order" not in restored
    assert restarted._group_assignments == {}


def test_group_transaction_journal_recovers_when_disk_rollback_also_fails(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path, monkeypatch)
    item = service.start("s1", summary="task")
    group = service.create_group("Ordered", "blue")["group"]

    real_save = service._save

    def always_fail():
        raise OSError("simulated persistent activity write failure")

    monkeypatch.setattr(service, "_save", always_fail)
    with pytest.raises(OSError, match="simulated persistent activity write failure"):
        service.set_group(item["id"], group["id"], before_event_id="")
    assert service.transaction_path.exists()
    monkeypatch.setattr(service, "_save", real_save)
    with pytest.raises(RuntimeError, match="unrecovered transaction"):
        service.set_group(item["id"], group["id"], before_event_id="")
    with pytest.raises(RuntimeError, match="unrecovered transaction"):
        service.start("s2", summary="must not be accepted")

    restarted = _service(tmp_path, monkeypatch)
    restored = next(row for row in restarted.list() if row["id"] == item["id"])
    assert "group_id" not in restored
    assert "group_order" not in restored
    assert restarted._group_assignments == {}
    assert not restarted.transaction_path.exists()


@pytest.mark.asyncio
async def test_group_placement_pushes_resync_for_sibling_order_changes(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path, monkeypatch)
    first = service.start("s1", summary="first")
    second = service.start("s2", summary="second")
    group = service.create_group("Ordered", "amber")["group"]
    service.set_group(first["id"], group["id"], before_event_id="")

    async with service.subscribe() as queue:
        update = await asyncio.to_thread(
            service.set_group,
            second["id"],
            group["id"],
            before_event_id=first["id"],
        )
        payload = await asyncio.wait_for(queue.get(), timeout=1)

    assert update is not None
    assert [row["id"] for row in update["items"]] == [
        second["id"], first["id"],
    ]
    assert payload["resync"] is True


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

    restarted = _service(tmp_path, monkeypatch)
    restored = next(row for row in restarted.list()
                    if row["session_id"] == "s1")
    assert restored["session_name"] == "Renamed conversation"
    assert service.rename_session("missing", "No row") is None


def test_restart_marks_running_as_failed(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    service.start("s1", summary="long task")
    restarted = _service(tmp_path, monkeypatch)
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
