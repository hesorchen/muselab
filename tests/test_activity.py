import json
from pathlib import Path

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
