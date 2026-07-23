"""Shared filesystem watcher and SSE endpoint regressions."""

import asyncio
from pathlib import Path

import pytest
from watchfiles import Change


def test_file_events_require_query_token(client):
    response = client.get("/api/files/events")
    assert response.status_code == 401


def test_normalise_changes_keeps_paths_relative_and_deduplicated(app_module, temp_root):
    from backend.file_events import _normalise_changes

    changes = {
        (Change.added, str(temp_root / "notes" / "new.md")),
        (Change.added, str(temp_root / "notes" / "new.md")),
        (Change.deleted, str(temp_root / "old.txt")),
        (Change.modified, str(temp_root.parent / "outside.txt")),
    }
    assert _normalise_changes(temp_root, changes) == [
        {"type": "added", "path": "notes/new.md"},
        {"type": "deleted", "path": "old.txt"},
    ]


@pytest.mark.asyncio
async def test_manager_shares_one_native_watcher_per_workspace(
    app_module,
    temp_root: Path,
    monkeypatch,
):
    import backend.file_events as file_events

    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_awatch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        yield {(Change.added, str(temp_root / "fresh.txt"))}
        await asyncio.Future()

    monkeypatch.setattr(file_events, "awatch", fake_awatch)
    manager = file_events.FileWatchManager()
    async with manager.subscribe(temp_root) as first:
        await asyncio.wait_for(started.wait(), timeout=1)
        async with manager.subscribe(temp_root) as second:
            assert calls == 1
            release.set()
            first_payload, second_payload = await asyncio.gather(
                asyncio.wait_for(first.get(), timeout=1),
                asyncio.wait_for(second.get(), timeout=1),
            )
            expected = {
                "changes": [{"type": "added", "path": "fresh.txt"}],
            }
            assert first_payload == expected
            assert second_payload == expected
    assert manager._states == {}


def test_slow_subscriber_is_collapsed_to_resync(app_module, temp_root):
    from backend.file_events import FileWatchManager, _WatchState

    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    state = _WatchState(root=temp_root, subscribers={queue})
    FileWatchManager._broadcast(
        state,
        {"changes": [{"type": "added", "path": "first"}]},
    )
    FileWatchManager._broadcast(
        state,
        {"changes": [{"type": "added", "path": "second"}]},
    )
    assert queue.get_nowait() == {"resync": True, "changes": []}
