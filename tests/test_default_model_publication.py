"""Concurrent default-model saves must publish the latest durable value."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from dotenv import dotenv_values


def test_default_model_publication_stays_ordered_with_settings_commit(
    client, auth, monkeypatch, tmp_path,
):
    from backend import api_settings, chat, settings

    path = tmp_path / "default-model.env"
    monkeypatch.setattr(api_settings, "ENV_PATH", path)
    for name in ("MUSELAB_MODEL", "MUSELAB_DEFAULT_MODEL"):
        monkeypatch.setenv(name, "synthetic-initial-model")
    monkeypatch.setattr(settings, "MODEL", "synthetic-initial-model")
    monkeypatch.setattr(chat, "MODEL", "synthetic-initial-model")
    first_model = "claude-haiku-4-5-20251001"
    second_model = "claude-opus-4-7"
    first_released = threading.Event()
    second_done = threading.Event()

    class PausedRelease:
        def __init__(self):
            self.lock = threading.RLock()
            self.depth = threading.local()
            self.first_thread = None

        def __enter__(self):
            self.lock.acquire()
            if self.first_thread is None:
                self.first_thread = threading.get_ident()
            self.depth.value = getattr(self.depth, "value", 0) + 1
            return self

        def __exit__(self, *_exc):
            self.depth.value -= 1
            self.lock.release()
            if not self.depth.value and threading.get_ident() == self.first_thread:
                # Simulate preemption after the first writer releases its
                # transaction lock, while another request saves a new model.
                first_released.set()
                assert second_done.wait(timeout=5)

    monkeypatch.setattr(api_settings, "_ENV_WRITE_LOCK", PausedRelease())

    def save_second():
        try:
            return client.put(
                "/api/settings", headers=auth, json={"default_model": second_model},
            )
        finally:
            second_done.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            client.put, "/api/settings", headers=auth,
            json={"default_model": first_model},
        )
        assert first_released.wait(timeout=5)
        second = executor.submit(save_second)
        assert first.result(timeout=10).status_code == 200
        assert second.result(timeout=10).status_code == 200

    saved = dotenv_values(path)
    for name in ("MUSELAB_MODEL", "MUSELAB_DEFAULT_MODEL"):
        assert saved[name] == second_model
        assert os.environ[name] == second_model
    assert settings.MODEL == second_model
    assert chat.MODEL == second_model
