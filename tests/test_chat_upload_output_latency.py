"""Attachment cleanup and task-output previews keep bounded I/O costs."""
import asyncio
import io
import threading
from pathlib import Path

from starlette.datastructures import UploadFile


def test_chat_attachment_cleanup_runs_off_loop(app_module, monkeypatch):
    from backend import chat
    calls = []
    monkeypatch.setattr(chat, '_gc_images', lambda: calls.append(threading.get_ident()))

    async def scenario():
        result = await chat.upload_image(UploadFile(io.BytesIO(b'synthetic'), filename='sample.txt'))
        assert result['id']
        assert calls and threading.get_ident() not in calls
    asyncio.run(scenario())


def test_task_output_preview_bounds_read_before_allocating(app_module, monkeypatch):
    from backend import chat
    target = Path('/tmp/claude-1000/fixture/fixture-session/tasks/fixture.output')
    original_open, original_is_file = Path.open, Path.is_file
    read_sizes = []

    class Output(io.StringIO):
        def read(self, size=-1):
            read_sizes.append(size)
            assert 0 < size <= 200001
            return super().read(size)

    monkeypatch.setattr(Path, 'is_file', lambda path: True if path == target else original_is_file(path))
    monkeypatch.setattr(Path, 'open', lambda path, *args, **kwargs:
                        Output('x' * 300000) if path == target else original_open(path, *args, **kwargs))
    result = chat.get_task_output(session_id='fixture-session', path=str(target))
    assert read_sizes == [200001]
    assert result.body.startswith(b'x' * 200000)
    assert b'truncated' in result.body
