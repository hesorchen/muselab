"""Durable to-do revisions must agree across reads, disk, and subscribers."""

import asyncio
import errno

import pytest


@pytest.mark.asyncio
async def test_failed_replace_preserves_state_and_can_retry(tmp_path, monkeypatch):
    from backend import todos as todos_module

    service = todos_module.TodosService(tmp_path)
    before = service.replace([{"id": "original", "text": "Keep this task"}], 0)
    before_bytes = service.path.read_bytes()
    replacement = [{"id": "replacement", "text": "Save this task"}]

    def fail_write(*args, **kwargs):
        raise OSError(errno.ENOSPC, "simulated full disk")

    async with service.subscribe() as updates:
        with monkeypatch.context() as patch:
            patch.setattr(todos_module, "atomic_write_text", fail_write)
            with pytest.raises(OSError, match="simulated full disk"):
                service.replace(replacement, before["revision"])

        await asyncio.sleep(0)
        assert updates.empty()
        assert service.get() == before
        assert service.revision == before["revision"]
        assert service.path.read_bytes() == before_bytes
        assert todos_module.TodosService(tmp_path).get() == before

        saved = service.replace(replacement, before["revision"])
        assert saved is not None
        assert saved["revision"] == before["revision"] + 1
        assert saved["items"][0]["id"] == "replacement"
        assert await asyncio.wait_for(updates.get(), 1) == saved
        assert todos_module.TodosService(tmp_path).get() == saved
        assert service.replace([], before["revision"]) is None
        assert service.get() == saved
