"""Regression tests for the shared atomic text writer."""

import stat

import pytest


def test_atomic_write_text_sets_mode_before_replace(
    tmp_path, monkeypatch, app_module,
):
    from backend import settings

    path = tmp_path / "state.json"
    events = []
    real_fchmod = settings.os.fchmod
    real_replace = settings.os.replace
    real_chmod = settings.os.chmod

    def record_fchmod(fd, mode):
        events.append(("fchmod", mode))
        return real_fchmod(fd, mode)

    def record_replace(source, destination):
        events.append(("replace", None))
        return real_replace(source, destination)

    def record_chmod(target, mode):
        events.append(("chmod", mode))
        return real_chmod(target, mode)

    monkeypatch.setattr(settings.os, "fchmod", record_fchmod)
    monkeypatch.setattr(settings.os, "replace", record_replace)
    monkeypatch.setattr(settings.os, "chmod", record_chmod)

    settings.atomic_write_text(path, "new", mode=0o600)

    assert events == [("fchmod", 0o600), ("replace", None)]
    assert path.read_text() == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_atomic_write_text_mode_failure_preserves_destination(
    tmp_path, monkeypatch, app_module,
):
    from backend import settings

    path = tmp_path / "state.json"
    path.write_text("old")

    def fail_fchmod(_fd, _mode):
        raise OSError("mode rejected")

    monkeypatch.setattr(settings.os, "fchmod", fail_fchmod)

    with pytest.raises(OSError, match="mode rejected"):
        settings.atomic_write_text(path, "new", mode=0o600)

    assert path.read_text() == "old"
    assert list(tmp_path.glob("state.json.tmp.*")) == []
