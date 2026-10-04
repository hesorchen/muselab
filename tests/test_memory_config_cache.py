"""Config reads follow restored files even when their timestamps are retained."""

import os
import stat
import time

import pytest


@pytest.fixture()
def config_module(tmp_path, monkeypatch):
    from backend import memory_config

    monkeypatch.setenv("MUSELAB_MEMORY_DIR", str(tmp_path))
    monkeypatch.setattr(memory_config, "_cached", None)
    return memory_config


@pytest.mark.parametrize("replace_inode", [False, True])
def test_timestamp_preserving_restore_invalidates_config(config_module, replace_inode):
    config = config_module
    original = config.MemoryConfig(owner_id="owner-a")
    config.save_config(original)
    assert config.load_config().owner_id == "owner-a"
    path = config.config_path()
    before = path.stat()
    # Separate independent restores on filesystems with coarse ctime ticks.
    time.sleep(0.01)
    target = path.with_name("restored.json") if replace_inode else path
    target.write_text(
        path.read_text(encoding="utf-8").replace("owner-a", "owner-b"),
        encoding="utf-8",
    )
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    if replace_inode:
        target.replace(path)
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert config.load_config().owner_id == "owner-b"


def test_config_permissions_are_set_before_publication(config_module, monkeypatch):
    config = config_module
    replacement = config.MemoryConfig(owner_id="owner-b")

    def fail_chmod(*_args, **_kwargs):
        raise OSError("simulated metadata failure")

    # Publication must not be followed by a fallible chmod: the temporary
    # inode is already private. Otherwise callers see an error after the
    # new config has replaced the previous config on disk.
    monkeypatch.setattr(config.os, "chmod", fail_chmod)
    assert config.save_config(replacement) == replacement
    assert config.load_config() == replacement
    assert stat.S_IMODE(config.config_path().stat().st_mode) == 0o600


def test_permission_failure_closes_temp_file_and_preserves_previous_config(
    config_module, monkeypatch,
):
    config = config_module
    original = config.MemoryConfig(owner_id="owner-a")
    config.save_config(original)
    before = config.config_path().read_bytes()
    descriptor = []

    def fail_fchmod(fd, _mode):
        descriptor.append(fd)
        raise OSError("simulated permission failure")

    monkeypatch.setattr(config.os, "fchmod", fail_fchmod)
    try:
        with pytest.raises(OSError, match="simulated permission failure"):
            config.save_config(config.MemoryConfig(owner_id="owner-b"))
        assert config.config_path().read_bytes() == before
        assert config.load_config() == original
        assert not list(config.config_path().parent.glob(".config.*"))
        with pytest.raises(OSError):
            os.fstat(descriptor[0])
    finally:
        try:
            os.close(descriptor[0])
        except OSError:
            pass
