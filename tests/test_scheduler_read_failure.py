"""Read failures must fence scheduler CRUD until a successful durable reload."""
from __future__ import annotations

import asyncio
import errno
import json
from unittest.mock import AsyncMock

import pytest


def _schedule():
    return {"kind": "daily", "hour": 9, "minute": 0, "tz_offset_minutes": 0}


@pytest.mark.parametrize("failure", ["stat_io", "inaccessible_exists_false", "json", "structure"])
def test_unavailable_scheduler_preserves_tasks_through_crud_and_reload(
    app_module, client, auth, monkeypatch, failure,
):
    from backend import scheduler as sched

    # Nothing in this test may execute a task or call a model.
    execute = AsyncMock(side_effect=AssertionError("unexpected scheduled execution"))
    monkeypatch.setattr(sched, "_execute_task", execute)
    original_tasks = [
        sched.create_task("synthetic-a", "synthetic", _schedule()),
        sched.create_task("synthetic-b", "synthetic", _schedule()),
    ]
    original_ids = {task["id"] for task in original_tasks}
    state_file = sched._STATE_FILE
    assert state_file is not None
    original_bytes = state_file.read_bytes()
    armed = True
    stat_failures = []

    if failure in {"stat_io", "inaccessible_exists_false"}:
        # Only this exact state Path has an unreadable stat, without OS chmod.
        # All other paths and actual file reads/writes retain their real behavior.
        class UnreadableStatePath(type(state_file)):
            def stat(self, *args, **kwargs):
                if armed and self == state_file:
                    stat_failures.append(True)
                    error = errno.EACCES if failure == "inaccessible_exists_false" else errno.EIO
                    raise OSError(error, "synthetic scheduler stat failure")
                return super().stat(*args, **kwargs)

            def exists(self, *args, **kwargs):
                if armed and self == state_file and failure == "inaccessible_exists_false":
                    # Model Python 3.14's documented OSError -> False contract.
                    try:
                        self.stat(*args, **kwargs)
                    except OSError:
                        return False
                return super().exists(*args, **kwargs)

            def read_text(self, *args, **kwargs):
                if armed and self == state_file and failure == "inaccessible_exists_false":
                    raise PermissionError(errno.EACCES, "synthetic scheduler read failure")
                return super().read_text(*args, **kwargs)

        monkeypatch.setattr(sched, "_STATE_FILE", UnreadableStatePath(state_file))
    elif failure == "json":
        state_file.write_bytes(original_bytes + b"{")
    else:
        invalid = json.loads(original_bytes)
        invalid["history"] = {}
        state_file.write_text(json.dumps(invalid), encoding="utf-8")
    protected_bytes = state_file.read_bytes()
    # Match a cold process: durable tasks have not been recovered into memory.
    sched._state = {
        "tasks": {}, "history": [], "unread_count": 0, "cleanup_pending": {},
    }
    try:
        if failure == "inaccessible_exists_false":
            assert sched._STATE_FILE.exists() is False
            with pytest.raises(PermissionError):
                sched._STATE_FILE.read_text(encoding="utf-8")
        with pytest.raises(sched.SchedulerPersistenceError, match="original file preserved"):
            if failure == "inaccessible_exists_false":
                # The baseline considers this absent; do not let startup spawn
                # a real tick after its unsafe empty-cache save.
                with sched._STATE_LOCK:
                    sched._load_state()
            else:
                asyncio.run(sched.start_scheduler())
    finally:
        armed = False

    if failure in {"stat_io", "inaccessible_exists_false"}:
        assert stat_failures
    assert sched.persistence_status()["available"] is False
    assert sched._scheduler_task is None
    assert state_file.read_bytes() == protected_bytes
    assert all(task_id.encode() in protected_bytes for task_id in original_ids)

    requests = [
        ("POST", "/api/scheduler/tasks", {
            "name": "must-not-overwrite", "prompt": "synthetic",
            "schedule": _schedule(), "session_mode": "fresh",
        }),
        ("PATCH", f"/api/scheduler/tasks/{original_tasks[0]['id']}", {
            "name": "must-not-overwrite",
        }),
        ("DELETE", f"/api/scheduler/tasks/{original_tasks[0]['id']}", None),
    ]
    for method, url, body in requests:
        kwargs = {"headers": auth}
        if body is not None:
            kwargs["json"] = body
        response = client.request(method, url, **kwargs)
        assert response.status_code == 503
        assert response.json()["code"] == "scheduler_persistence_unavailable"
        assert response.json()["degraded"] is True
        assert state_file.read_bytes() == protected_bytes
        assert sched._state["tasks"] == {}

    # Repair the simulated invalid bytes, then read the real two-task file.
    # For stat I/O failure this write is unnecessary: the file never changed.
    if failure in {"json", "structure"}:
        state_file.write_bytes(original_bytes)
    with sched._STATE_LOCK:
        sched._load_state()
    assert sched.persistence_status()["available"] is True
    assert set(sched._state["tasks"]) == original_ids
    response = client.post("/api/scheduler/tasks", headers=auth, json={
        "name": "synthetic-after-reload", "prompt": "synthetic",
        "schedule": _schedule(), "session_mode": "fresh",
    })
    assert response.status_code == 200
    persisted = json.loads(state_file.read_bytes())
    assert set(persisted["tasks"]) == original_ids | {response.json()["id"]}
    for task in original_tasks:
        assert persisted["tasks"][task["id"]] == task
    assert sched._scheduler_task is None
    execute.assert_not_called()


def test_absent_scheduler_file_remains_writable(app_module, client, auth):
    from backend import scheduler as sched

    state_file = sched._STATE_FILE
    assert state_file is not None and not state_file.exists()
    with sched._STATE_LOCK:
        sched._load_state()
    assert sched.persistence_status()["available"] is True
    assert sched.list_tasks() == []
    response = client.post("/api/scheduler/tasks", headers=auth, json={
        "name": "synthetic-first-task", "prompt": "synthetic",
        "schedule": _schedule(), "session_mode": "fresh",
    })
    assert response.status_code == 200
    assert set(json.loads(state_file.read_bytes())["tasks"]) == {response.json()["id"]}
    assert sched._scheduler_task is None
