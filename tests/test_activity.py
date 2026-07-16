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
    assert summary["workspaces"][0]["unread"] == 1
    assert service.ack(item["id"]) == 1
    assert service.summary()["unread"] == 0


def test_restart_marks_running_as_failed(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    service.start("s1", summary="long task")
    restarted = ActivityService(tmp_path)
    row = restarted.list()[0]
    assert row["state"] == "failed"
    assert row["needs_attention"] is True
    assert json.loads((Path(tmp_path) / ".muselab" / "activity.json").read_text())
