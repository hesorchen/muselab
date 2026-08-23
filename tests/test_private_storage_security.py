"""Private attachment and trash storage security contracts."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _legacy_manifest(trash_id: str, original_path: str) -> dict:
    return {
        "trash_id": trash_id,
        "original_path": original_path,
        "original_name": Path(original_path).name,
        "deleted_at": 1.0,
        "kind": "file",
        "size": 6,
    }


def test_attachment_creation_is_private_under_permissive_umask(
    app_module,
    temp_root,
) -> None:
    del app_module
    from backend import chat

    sid = "session_secure_123"
    old_umask = os.umask(0)
    try:
        saved = chat._persist_attachment(
            sid,
            "attachment123",
            "report.txt",
            b"secret",
        )
    finally:
        os.umask(old_umask)

    assert saved is not None
    base = temp_root / ".muselab-attach"
    attachment = Path(saved[0])
    assert attachment.read_bytes() == b"secret"
    assert _mode(base) == 0o700
    assert _mode(base / sid) == 0o700
    assert _mode(attachment) == 0o600


def test_attachment_repair_skips_symlinks_and_repairs_real_paths(
    app_module,
    temp_root,
    tmp_path,
) -> None:
    del app_module
    from backend import chat

    base = temp_root / ".muselab-attach"
    session_dir = base / "session_repair_123"
    session_dir.mkdir(parents=True)
    attachment = session_dir / "attachment123-report.txt"
    attachment.write_bytes(b"private")
    base.chmod(0o755)
    session_dir.chmod(0o755)
    attachment.chmod(0o644)

    outside = tmp_path / "outside-attachment.txt"
    outside.write_bytes(b"outside")
    outside.chmod(0o644)
    link = session_dir / "linked.txt"
    link.symlink_to(outside)

    assert chat.ensure_private_attachment_storage() == 3
    assert _mode(base) == 0o700
    assert _mode(session_dir) == 0o700
    assert _mode(attachment) == 0o600
    assert link.is_symlink()
    assert outside.read_bytes() == b"outside"
    assert _mode(outside) == 0o644

    attachment.chmod(0o644)
    assert chat._validate_attachment_ref(session_dir.name, attachment.name) == attachment
    assert _mode(attachment) == 0o600


def test_attachment_storage_rejects_symlink_substitution(
    app_module,
    temp_root,
    tmp_path,
) -> None:
    del app_module
    from backend import chat

    base = temp_root / ".muselab-attach"
    outside = tmp_path / "outside-attachments"
    outside.mkdir()
    outside.chmod(0o755)
    base.symlink_to(outside, target_is_directory=True)

    assert chat._persist_attachment(
        "session_secure_123",
        "attachment123",
        "report.txt",
        b"secret",
    ) is None
    assert list(outside.iterdir()) == []
    assert _mode(outside) == 0o755

    base.unlink()
    base.mkdir(mode=0o700)
    session_link = base / "session_secure_123"
    session_link.symlink_to(outside, target_is_directory=True)
    assert chat._persist_attachment(
        "session_secure_123",
        "attachment123",
        "report.txt",
        b"secret",
    ) is None
    with pytest.raises(HTTPException) as exc_info:
        chat._validate_attachment_ref(
            "session_secure_123",
            "attachment123-report.txt",
        )
    assert exc_info.value.status_code == 400
    assert list(outside.iterdir()) == []
    assert _mode(outside) == 0o755


def test_legacy_attachment_migration_never_follows_symlink(
    app_module,
    temp_root,
    tmp_path,
) -> None:
    del app_module
    from backend import chat, sessions

    outside = tmp_path / "legacy-outside"
    child = outside / "session_legacy_123"
    child.mkdir(parents=True)
    old_base = sessions.SESS_DIR / "attachments"
    old_base.symlink_to(outside, target_is_directory=True)

    chat._migrate_legacy_attachments()

    assert child.parent == outside
    assert child.is_dir()
    assert old_base.is_symlink()
    assert not (temp_root / ".muselab-attach").exists()


def test_trash_file_and_manifest_are_private_then_restore_original_mode(
    app_module,
    temp_root,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "private-data.txt"
    source.write_bytes(b"secret")
    source.chmod(0o640)

    manifest = files._move_to_trash(source, temp_root)
    trash_id = manifest["trash_id"]
    dustbin = temp_root / ".muselab-dustbin"
    payload = dustbin / trash_id
    manifest_path = dustbin / f"{trash_id}.json"

    assert not source.exists()
    assert payload.read_bytes() == b"secret"
    assert _mode(dustbin) == 0o700
    assert _mode(payload) == 0o600
    assert _mode(manifest_path) == 0o600
    assert _mode(dustbin / files._TRASH_LOCK_NAME) == 0o600
    assert manifest["original_mode"] == 0o640

    result = files.trash_restore(
        files.TrashIdReq(trash_id=trash_id),
        root=temp_root,
    )
    assert result == {"ok": True, "restored_path": "private-data.txt"}
    assert source.read_bytes() == b"secret"
    assert _mode(source) == 0o640
    assert not manifest_path.exists()


def test_trash_directory_preserves_contents_and_does_not_follow_symlinks(
    app_module,
    temp_root,
    tmp_path,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "private-dir"
    source.mkdir()
    source.chmod(0o751)
    child = source / "child.txt"
    child.write_bytes(b"abc")
    child.chmod(0o640)
    outside = tmp_path / "outside-payload.txt"
    outside.write_bytes(b"x" * 200)
    outside.chmod(0o644)
    link = source / "outside-link"
    link.symlink_to(outside)

    manifest = files._move_to_trash(source, temp_root)
    trash_id = manifest["trash_id"]
    dustbin = temp_root / ".muselab-dustbin"
    payload = dustbin / trash_id

    assert manifest["size"] == 3
    assert _mode(payload) == 0o700
    assert _mode(payload / "child.txt") == 0o640
    assert (payload / "outside-link").is_symlink()
    assert outside.read_bytes() == b"x" * 200
    assert _mode(outside) == 0o644

    files.trash_restore(files.TrashIdReq(trash_id=trash_id), root=temp_root)
    assert _mode(source) == 0o751
    assert _mode(source / "child.txt") == 0o640
    assert (source / "outside-link").is_symlink()
    assert outside.read_bytes() == b"x" * 200
    assert _mode(outside) == 0o644


def test_existing_trash_is_repaired_without_losing_restore_mode(
    app_module,
    temp_root,
) -> None:
    del app_module
    from backend import files

    dustbin = temp_root / ".muselab-dustbin"
    dustbin.mkdir()
    dustbin.chmod(0o755)
    trash_id = "1234567890_deadbeef"
    payload = dustbin / trash_id
    payload.write_bytes(b"legacy")
    payload.chmod(0o644)
    manifest_path = dustbin / f"{trash_id}.json"
    manifest_path.write_text(
        json.dumps(_legacy_manifest(trash_id, "legacy.txt")),
        encoding="utf-8",
    )
    manifest_path.chmod(0o644)

    assert files.ensure_private_trash_storage(temp_root) == 3
    repaired_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert repaired_manifest["original_mode"] == 0o644
    assert _mode(dustbin) == 0o700
    assert _mode(payload) == 0o600
    assert _mode(manifest_path) == 0o600

    payload.chmod(0o644)
    manifest_path.chmod(0o644)
    assert files._read_manifest(trash_id, temp_root) == repaired_manifest
    assert _mode(payload) == _mode(manifest_path) == 0o600

    files.trash_restore(files.TrashIdReq(trash_id=trash_id), root=temp_root)
    restored = temp_root / "legacy.txt"
    assert restored.read_bytes() == b"legacy"
    assert _mode(restored) == 0o644


def test_invalid_legacy_restore_mode_is_recaptured_before_hardening(
    app_module,
    temp_root,
) -> None:
    del app_module
    from backend import files

    dustbin = temp_root / ".muselab-dustbin"
    dustbin.mkdir()
    trash_id = "1234567890_cafebabe"
    payload = dustbin / trash_id
    payload.write_bytes(b"legacy")
    payload.chmod(0o640)
    manifest = {
        **_legacy_manifest(trash_id, "legacy-invalid.txt"),
        "original_mode": -1,
    }
    manifest_path = dustbin / f"{trash_id}.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    files.ensure_private_trash_storage(temp_root)

    repaired = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert repaired["original_mode"] == 0o640
    assert _mode(payload) == 0o600


def test_trash_storage_never_follows_root_manifest_or_payload_symlinks(
    app_module,
    temp_root,
    tmp_path,
) -> None:
    del app_module
    from backend import files
    from backend.private_storage import UnsafePrivatePath

    dustbin = temp_root / ".muselab-dustbin"
    outside_dir = tmp_path / "outside-trash"
    outside_dir.mkdir()
    outside_dir.chmod(0o755)
    marker = outside_dir / "marker.txt"
    marker.write_bytes(b"outside")
    marker.chmod(0o644)
    dustbin.symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(UnsafePrivatePath):
        files.ensure_private_trash_storage(temp_root)
    assert marker.read_bytes() == b"outside"
    assert _mode(outside_dir) == 0o755
    assert _mode(marker) == 0o644

    dustbin.unlink()
    dustbin.mkdir()
    dustbin.chmod(0o755)

    outside_lock = tmp_path / "outside-transaction.lock"
    outside_lock.write_bytes(b"outside lock")
    outside_lock.chmod(0o644)
    lock_link = dustbin / files._TRASH_LOCK_NAME
    lock_link.symlink_to(outside_lock)
    with pytest.raises(OSError):
        files.ensure_private_trash_storage(temp_root)
    assert outside_lock.read_bytes() == b"outside lock"
    assert _mode(outside_lock) == 0o644
    assert lock_link.is_symlink()
    lock_link.unlink()

    manifest_link_id = "1234567890_aaaaaaaa"
    outside_manifest = tmp_path / "outside-manifest.json"
    outside_manifest.write_text(
        json.dumps(_legacy_manifest(manifest_link_id, "linked.txt")),
        encoding="utf-8",
    )
    outside_manifest.chmod(0o644)
    (dustbin / f"{manifest_link_id}.json").symlink_to(outside_manifest)

    payload_link_id = "1234567890_bbbbbbbb"
    outside_payload = tmp_path / "outside-payload.txt"
    outside_payload.write_bytes(b"external payload")
    outside_payload.chmod(0o644)
    (dustbin / payload_link_id).symlink_to(outside_payload)
    payload_manifest = _legacy_manifest(payload_link_id, "payload.txt")
    payload_manifest_path = dustbin / f"{payload_link_id}.json"
    payload_manifest_path.write_text(
        json.dumps(payload_manifest),
        encoding="utf-8",
    )
    payload_manifest_path.chmod(0o644)

    files.ensure_private_trash_storage(temp_root)

    assert _mode(dustbin) == 0o700
    assert files._read_manifest(manifest_link_id, temp_root) is None
    assert _mode(outside_manifest) == 0o644
    assert _mode(outside_payload) == 0o644
    assert outside_payload.read_bytes() == b"external payload"
    assert _mode(payload_manifest_path) == 0o600
    assert files._list_trash(temp_root) == []
    with pytest.raises(HTTPException) as exc_info:
        files.trash_restore(
            files.TrashIdReq(trash_id=payload_link_id),
            root=temp_root,
        )
    assert exc_info.value.status_code == 404
    assert outside_payload.read_bytes() == b"external payload"
    assert _mode(outside_payload) == 0o644


@pytest.mark.parametrize("fault", ["fchmod", "write", "fsync", "replace"])
def test_private_writer_faults_never_publish_partial_final(
    app_module,
    temp_root,
    monkeypatch,
    fault,
) -> None:
    del app_module
    from backend import private_storage

    parent = temp_root / "atomic-writer"
    parent.mkdir(mode=0o700)
    existing = parent / "existing.bin"
    missing = parent / "missing.bin"
    existing.write_bytes(b"old-value")
    existing.chmod(0o600)

    original_fchmod = private_storage.os.fchmod
    original_write = private_storage.os.write
    original_fsync = private_storage.os.fsync
    original_replace = private_storage.os.replace

    if fault == "fchmod":
        def fail_fchmod(fd, mode):
            original_fchmod(fd, mode)
            raise OSError("post-open fault")

        monkeypatch.setattr(private_storage.os, "fchmod", fail_fchmod)
    elif fault == "write":
        def fail_write(fd, data):
            original_write(fd, bytes(data[:1]))
            raise OSError("post-write fault")

        monkeypatch.setattr(private_storage.os, "write", fail_write)
    elif fault == "fsync":
        def fail_fsync(fd):
            original_fsync(fd)
            raise OSError("post-fsync fault")

        monkeypatch.setattr(private_storage.os, "fsync", fail_fsync)
    else:
        def fail_replace(src, dst):
            raise OSError("pre-commit replace fault")

        monkeypatch.setattr(private_storage.os, "replace", fail_replace)

    for target in (existing, missing):
        with pytest.raises(OSError):
            private_storage.write_private_bytes(target, b"new-value")

    assert existing.read_bytes() == b"old-value"
    assert not missing.exists()
    assert not [
        child for child in parent.iterdir()
        if child.name.startswith(".") and child.name.endswith(".tmp")
    ]
    assert original_replace is not None


def test_trash_manifest_prepare_failure_never_moves_source(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "prepare-failure.txt"
    source.write_bytes(b"source stays")

    def fail_prepare(_path, _data):
        raise OSError("simulated manifest prepare failure")

    monkeypatch.setattr(files, "_create_trash_manifest", fail_prepare)
    with pytest.raises(OSError, match="prepare failure"):
        files._move_to_trash(source, temp_root)

    assert source.read_bytes() == b"source stays"
    dustbin = temp_root / ".muselab-dustbin"
    assert list(dustbin.glob("*.json")) == []
    assert [
        entry.name
        for entry in dustbin.iterdir()
        if entry.name != files._TRASH_LOCK_NAME
    ] == []


def test_trash_id_collision_never_removes_existing_manifest(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "collision-source.txt"
    source.write_bytes(b"source")
    dustbin = temp_root / ".muselab-dustbin"
    dustbin.mkdir(mode=0o700)
    colliding_id = "1234567890_deadbeef"
    existing = dustbin / f"{colliding_id}.json"
    existing.write_bytes(b"existing transaction")
    existing.chmod(0o600)
    monkeypatch.setattr(files, "_gen_trash_id", lambda: colliding_id)

    with pytest.raises(OSError, match="unique trash transaction"):
        files._move_to_trash(source, temp_root)

    assert source.read_bytes() == b"source"
    assert existing.read_bytes() == b"existing transaction"
    assert _mode(existing) == 0o600


def test_trash_rename_failure_cleans_prepare_and_keeps_source(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "rename-failure.txt"
    source.write_bytes(b"not moved")

    def fail_rename(_source, _destination, **_kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(files, "_rename_noreplace", fail_rename)
    with pytest.raises(OSError, match="rename failure"):
        files._move_to_trash(source, temp_root)

    assert source.read_bytes() == b"not moved"
    assert list((temp_root / ".muselab-dustbin").glob("*.json")) == []


def test_delete_crash_after_rename_is_recovered_from_prepare_manifest(
    app_module,
    temp_root,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "delete-crash.txt"
    source.write_bytes(b"recoverable")
    script = "\n".join([
        "import os",
        "import sys",
        "from pathlib import Path",
        "from backend import files",
        "root = Path(sys.argv[1])",
        "real_write = files._write_trash_manifest",
        "def crash_on_commit(path, data):",
        "    if data.get(files._TRASH_STATE_KEY) == files._TRASHED:",
        "        os._exit(73)",
        "    return real_write(path, data)",
        "files._write_trash_manifest = crash_on_commit",
        "files._move_to_trash(root / 'delete-crash.txt', root)",
    ])

    result = subprocess.run(
        [sys.executable, "-c", script, str(temp_root)],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
    )

    assert result.returncode == 73
    assert not source.exists()
    dustbin = temp_root / ".muselab-dustbin"
    manifest_path = next(dustbin.glob("*.json"))
    prepared = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert prepared[files._TRASH_STATE_KEY] == files._TRASH_DELETE_PREPARED
    payload = dustbin / prepared["trash_id"]
    assert payload.read_bytes() == b"recoverable"

    listed = files._list_trash(temp_root)
    assert [item["trash_id"] for item in listed] == [prepared["trash_id"]]
    committed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert committed[files._TRASH_STATE_KEY] == files._TRASHED


def test_restore_state_write_failure_does_not_move_payload(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "restore-prepare-failure.txt"
    source.write_bytes(b"still trashed")
    manifest = files._move_to_trash(source, temp_root)
    dustbin = temp_root / ".muselab-dustbin"
    payload = dustbin / manifest["trash_id"]
    manifest_path = dustbin / f"{manifest['trash_id']}.json"
    real_write = files._write_trash_manifest

    def fail_restore_prepare(path, data):
        if data.get(files._TRASH_STATE_KEY) == files._TRASH_RESTORE_PREPARED:
            raise OSError("simulated restore state failure")
        return real_write(path, data)

    monkeypatch.setattr(files, "_write_trash_manifest", fail_restore_prepare)
    with pytest.raises(OSError, match="restore state failure"):
        files.trash_restore(
            files.TrashIdReq(trash_id=manifest["trash_id"]),
            root=temp_root,
        )

    assert not source.exists()
    assert payload.read_bytes() == b"still trashed"
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk[files._TRASH_STATE_KEY] == files._TRASHED


def test_restore_crash_after_rename_is_idempotently_recovered(
    app_module,
    temp_root,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "restore-crash.txt"
    source.write_bytes(b"restored despite crash")
    source.chmod(0o640)
    manifest = files._move_to_trash(source, temp_root)
    trash_id = manifest["trash_id"]
    script = "\n".join([
        "import os",
        "import sys",
        "from pathlib import Path",
        "from backend import files",
        "root = Path(sys.argv[1])",
        "trash_id = sys.argv[2]",
        "real_write = files._write_trash_manifest",
        "def crash_before_terminal_state(path, data):",
        "    if data.get(files._TRASH_STATE_KEY) == files._TRASH_RESTORED:",
        "        os._exit(74)",
        "    return real_write(path, data)",
        "files._write_trash_manifest = crash_before_terminal_state",
        "files.trash_restore(files.TrashIdReq(trash_id=trash_id), root=root)",
    ])

    result = subprocess.run(
        [sys.executable, "-c", script, str(temp_root), trash_id],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
    )

    assert result.returncode == 74
    dustbin = temp_root / ".muselab-dustbin"
    manifest_path = dustbin / f"{trash_id}.json"
    interrupted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert interrupted[files._TRASH_STATE_KEY] == files._TRASH_RESTORE_PREPARED
    assert not (dustbin / trash_id).exists()
    assert source.read_bytes() == b"restored despite crash"

    retried = files.trash_restore(
        files.TrashIdReq(trash_id=trash_id),
        root=temp_root,
    )
    assert retried == {"ok": True, "restored_path": "restore-crash.txt"}
    assert _mode(source) == 0o640
    assert not manifest_path.exists()


def test_restore_terminal_state_write_failure_stays_idempotent(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "terminal-state-failure.txt"
    source.write_bytes(b"physically restored")
    manifest = files._move_to_trash(source, temp_root)
    trash_id = manifest["trash_id"]
    dustbin = temp_root / ".muselab-dustbin"
    manifest_path = dustbin / f"{trash_id}.json"
    real_write = files._write_trash_manifest

    def fail_terminal_state(path, data):
        if data.get(files._TRASH_STATE_KEY) == files._TRASH_RESTORED:
            raise OSError("simulated terminal state failure")
        return real_write(path, data)

    monkeypatch.setattr(files, "_write_trash_manifest", fail_terminal_state)
    first = files.trash_restore(
        files.TrashIdReq(trash_id=trash_id),
        root=temp_root,
    )
    assert first == {
        "ok": True,
        "restored_path": "terminal-state-failure.txt",
    }
    assert source.read_bytes() == b"physically restored"
    assert not (dustbin / trash_id).exists()
    assert not manifest_path.exists()
    receipt_path = dustbin / f"{trash_id}{files._TRASH_RECEIPT_SUFFIX}"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt[files._TRASH_STATE_KEY] == files._TRASH_RESTORED
    assert _mode(receipt_path) == 0o600

    replacement = temp_root / "terminal-replacement.tmp"
    replacement.write_bytes(b"edited after success")
    os.replace(replacement, source)

    second = files.trash_restore(
        files.TrashIdReq(trash_id=trash_id),
        root=temp_root,
    )
    assert second == first
    assert source.read_bytes() == b"edited after success"


def test_restore_manifest_unlink_failure_retries_as_success(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "unlink-failure.txt"
    source.write_bytes(b"restored once")
    manifest = files._move_to_trash(source, temp_root)
    trash_id = manifest["trash_id"]
    dustbin = temp_root / ".muselab-dustbin"
    manifest_path = dustbin / f"{trash_id}.json"
    real_unlink = files._unlink_trash_manifest
    calls = 0

    def fail_once(path, *, missing_ok=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated manifest unlink failure")
        return real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(files, "_unlink_trash_manifest", fail_once)
    first = files.trash_restore(
        files.TrashIdReq(trash_id=trash_id),
        root=temp_root,
    )
    assert first == {"ok": True, "restored_path": "unlink-failure.txt"}
    terminal = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert terminal[files._TRASH_STATE_KEY] == files._TRASH_RESTORED
    assert source.read_bytes() == b"restored once"
    assert not (dustbin / trash_id).exists()

    replacement = temp_root / "replacement.tmp"
    replacement.write_bytes(b"edited after restore")
    os.replace(replacement, source)

    second = files.trash_restore(
        files.TrashIdReq(trash_id=trash_id),
        root=temp_root,
    )
    assert second == first
    assert source.read_bytes() == b"edited after restore"
    assert calls == 2
    assert not manifest_path.exists()


def test_restore_concurrent_occupant_never_clobbers_either_file(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "occupied.txt"
    source.write_bytes(b"trash payload")
    manifest = files._move_to_trash(source, temp_root)
    trash_id = manifest["trash_id"]
    dustbin = temp_root / ".muselab-dustbin"
    payload = dustbin / trash_id
    manifest_path = dustbin / f"{trash_id}.json"
    real_rename = files._rename_noreplace

    def occupy_then_rename(rename_source, destination, **kwargs):
        destination.write_bytes(b"concurrent writer")
        return real_rename(rename_source, destination, **kwargs)

    monkeypatch.setattr(files, "_rename_noreplace", occupy_then_rename)
    with pytest.raises(HTTPException) as exc_info:
        files.trash_restore(
            files.TrashIdReq(trash_id=trash_id),
            root=temp_root,
        )

    assert exc_info.value.status_code == 409
    assert source.read_bytes() == b"concurrent writer"
    assert payload.read_bytes() == b"trash payload"
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk[files._TRASH_STATE_KEY] == files._TRASHED


def test_delete_rename_reported_error_with_moved_inode_finishes_commit(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "outcome-in-doubt.txt"
    source.write_bytes(b"moved before error")
    dustbin = temp_root / ".muselab-dustbin"
    real_fsync_open_directory = files._fsync_open_directory
    fsynced: list[Path] = []

    def move_then_report_error(rename_source, destination, **kwargs):
        del kwargs
        fsynced.clear()
        os.rename(rename_source, destination)
        raise OSError("simulated uncertain rename result")

    def record_parent_barrier(directory_fd, path):
        fsynced.append(Path(path))
        return real_fsync_open_directory(directory_fd, path)

    monkeypatch.setattr(files, "_rename_noreplace", move_then_report_error)
    monkeypatch.setattr(
        files, "_fsync_open_directory", record_parent_barrier)
    manifest = files._move_to_trash(source, temp_root)

    manifest_path = dustbin / f"{manifest['trash_id']}.json"
    assert not source.exists()
    assert (dustbin / manifest["trash_id"]).read_bytes() == b"moved before error"
    assert fsynced[:2] == [temp_root, dustbin]
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk[files._TRASH_STATE_KEY] == files._TRASHED


def test_delete_fsync_failure_replays_both_parent_barriers(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "delete-fsync-failure.txt"
    source.write_bytes(b"durable after retry")
    real_fsync_open_directory = files._fsync_open_directory
    failed_second_parent = False

    def fail_second_parent_once(directory_fd, path):
        nonlocal failed_second_parent
        if (Path(path) == temp_root / ".muselab-dustbin"
                and any(
                    files._valid_trash_id(name)
                    for name in os.listdir(path)
                )
                and not failed_second_parent):
            failed_second_parent = True
            raise OSError("simulated destination parent fsync failure")
        return real_fsync_open_directory(directory_fd, path)

    monkeypatch.setattr(
        files, "_fsync_open_directory", fail_second_parent_once)
    with pytest.raises(OSError, match="destination parent fsync failure"):
        files._move_to_trash(source, temp_root)

    dustbin = temp_root / ".muselab-dustbin"
    manifest_path = next(dustbin.glob("*.json"))
    prepared = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert prepared[files._TRASH_STATE_KEY] == files._TRASH_DELETE_PREPARED
    assert (dustbin / prepared["trash_id"]).read_bytes() == b"durable after retry"

    listed = files._list_trash(temp_root)
    assert [item["trash_id"] for item in listed] == [prepared["trash_id"]]
    committed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert committed[files._TRASH_STATE_KEY] == files._TRASHED


def test_restore_fsync_failure_replays_barriers_before_terminal_state(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "restore-fsync-failure.txt"
    source.write_bytes(b"restore survives retry")
    manifest = files._move_to_trash(source, temp_root)
    trash_id = manifest["trash_id"]
    dustbin = temp_root / ".muselab-dustbin"
    manifest_path = dustbin / f"{trash_id}.json"
    real_fsync_open_directory = files._fsync_open_directory
    barrier_available = False

    def fail_until_barrier_recovers(directory_fd, path):
        if (Path(path) == dustbin
                and not (dustbin / trash_id).exists()
                and not barrier_available):
            raise OSError("simulated source parent fsync failure")
        return real_fsync_open_directory(directory_fd, path)

    monkeypatch.setattr(
        files, "_fsync_open_directory", fail_until_barrier_recovers)
    with pytest.raises(OSError, match="source parent fsync failure"):
        files.trash_restore(
            files.TrashIdReq(trash_id=trash_id),
            root=temp_root,
        )

    interrupted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert interrupted[files._TRASH_STATE_KEY] == files._TRASH_RESTORE_PREPARED
    assert source.read_bytes() == b"restore survives retry"
    assert not (dustbin / trash_id).exists()

    with pytest.raises(HTTPException) as retry_error:
        files.trash_restore(
            files.TrashIdReq(trash_id=trash_id),
            root=temp_root,
        )
    assert retry_error.value.status_code == 500
    assert manifest_path.exists()

    barrier_available = True
    retried = files.trash_restore(
        files.TrashIdReq(trash_id=trash_id),
        root=temp_root,
    )
    assert retried == {
        "ok": True,
        "restored_path": "restore-fsync-failure.txt",
    }
    assert not manifest_path.exists()


def test_restore_unlink_then_fsync_failure_uses_durable_receipt(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "receipt-after-unlink.txt"
    source.write_bytes(b"receipt-backed")
    manifest = files._move_to_trash(source, temp_root)
    trash_id = manifest["trash_id"]
    dustbin = temp_root / ".muselab-dustbin"
    manifest_path = dustbin / f"{trash_id}.json"

    def unlink_then_fail(path, *, missing_ok=False):
        del missing_ok
        path.unlink()
        raise OSError("simulated directory fsync failure after unlink")

    monkeypatch.setattr(files, "_unlink_trash_manifest", unlink_then_fail)
    first = files.trash_restore(
        files.TrashIdReq(trash_id=trash_id),
        root=temp_root,
    )
    assert first == {
        "ok": True,
        "restored_path": "receipt-after-unlink.txt",
    }
    assert not manifest_path.exists()
    receipt = files._restore_receipt_path(dustbin, trash_id)
    assert receipt.is_file()
    assert _mode(receipt) == 0o600

    second = files.trash_restore(
        files.TrashIdReq(trash_id=trash_id),
        root=temp_root,
    )
    assert second == first
    assert source.read_bytes() == b"receipt-backed"

def test_restore_visible_terminal_state_without_parent_barrier_stays_retryable(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "terminal-parent-fsync.txt"
    source.write_bytes(b"visible but not yet durable")
    manifest = files._move_to_trash(source, temp_root)
    trash_id = manifest["trash_id"]
    dustbin = temp_root / ".muselab-dustbin"
    manifest_path = dustbin / f"{trash_id}.json"
    receipt_path = files._restore_receipt_path(dustbin, trash_id)
    real_fsync_open_directory = files._fsync_open_directory
    real_rename = files._rename_noreplace
    fail_post_rename_barrier = True
    terminal_barrier_armed = False

    def rename_then_arm(*args, **kwargs):
        nonlocal terminal_barrier_armed
        result = real_rename(*args, **kwargs)
        terminal_barrier_armed = True
        return result

    def fail_after_payload_move(directory_fd, path):
        if (fail_post_rename_barrier
                and terminal_barrier_armed
                and Path(path) == dustbin):
            raise OSError("simulated terminal parent fsync failure")
        return real_fsync_open_directory(directory_fd, path)

    monkeypatch.setattr(files, "_rename_noreplace", rename_then_arm)
    monkeypatch.setattr(
        files, "_fsync_open_directory", fail_after_payload_move)

    with pytest.raises(HTTPException) as first_error:
        files.trash_restore(
            files.TrashIdReq(trash_id=trash_id),
            root=temp_root,
        )

    assert first_error.value.status_code == 500
    visible = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert visible[files._TRASH_STATE_KEY] == files._TRASH_RESTORED
    assert source.read_bytes() == b"visible but not yet durable"
    assert not receipt_path.exists()

    with pytest.raises(HTTPException) as retry_error:
        files.trash_restore(
            files.TrashIdReq(trash_id=trash_id),
            root=temp_root,
        )

    assert retry_error.value.status_code == 500
    assert manifest_path.exists()

    fail_post_rename_barrier = False
    restored = files.trash_restore(
        files.TrashIdReq(trash_id=trash_id),
        root=temp_root,
    )
    assert restored == {
        "ok": True,
        "restored_path": "terminal-parent-fsync.txt",
    }
    assert not manifest_path.exists()
    assert receipt_path.is_file()


def test_restore_nested_missing_parents_are_all_fsynced(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "missing" / "one" / "two" / "nested.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"nested")
    manifest = files._move_to_trash(source, temp_root)
    shutil.rmtree(temp_root / "missing")
    real_fsync_open_directory = files._fsync_open_directory
    fsynced: list[Path] = []

    def record_fsync(directory_fd, path):
        fsynced.append(Path(path))
        return real_fsync_open_directory(directory_fd, path)

    monkeypatch.setattr(files, "_fsync_open_directory", record_fsync)
    files.trash_restore(
        files.TrashIdReq(trash_id=manifest["trash_id"]),
        root=temp_root,
    )

    expected = {
        temp_root,
        temp_root / "missing",
        temp_root / "missing" / "one",
        temp_root / "missing" / "one" / "two",
    }
    assert expected.issubset(set(fsynced))
    assert source.read_bytes() == b"nested"

    fsynced.clear()
    files._mkdir_durable(source.parent)
    assert fsynced == []


@pytest.mark.parametrize("original_mode", [0o000, 0o200])
def test_restore_unreadable_file_mode_recovers_after_terminal_write_failure(
    app_module,
    temp_root,
    monkeypatch,
    original_mode,
) -> None:
    del app_module
    from backend import files

    source = temp_root / f"mode-{original_mode:o}.txt"
    source.write_bytes(b"mode protected")
    source.chmod(original_mode)
    manifest = files._move_to_trash(source, temp_root)
    trash_id = manifest["trash_id"]
    real_write = files._write_trash_manifest
    terminal_failures = 0

    def fail_terminal_once(path, data):
        nonlocal terminal_failures
        if (data.get(files._TRASH_STATE_KEY) == files._TRASH_RESTORED
                and terminal_failures == 0):
            terminal_failures += 1
            raise OSError("simulated terminal write failure")
        return real_write(path, data)

    monkeypatch.setattr(files, "_write_trash_manifest", fail_terminal_once)
    first = files.trash_restore(
        files.TrashIdReq(trash_id=trash_id),
        root=temp_root,
    )
    assert first["ok"] is True
    assert _mode(source) == original_mode

    second = files.trash_restore(
        files.TrashIdReq(trash_id=trash_id),
        root=temp_root,
    )
    assert second == first
    assert _mode(source) == original_mode
    source.chmod(0o600)
    assert source.read_bytes() == b"mode protected"


def test_restore_mode_zero_directory_recovers_after_terminal_write_failure(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "mode-zero-directory"
    source.mkdir()
    (source / "child.txt").write_bytes(b"child")
    source.chmod(0o000)
    manifest = files._move_to_trash(source, temp_root)
    trash_id = manifest["trash_id"]
    real_write = files._write_trash_manifest
    terminal_failures = 0

    def fail_terminal_once(path, data):
        nonlocal terminal_failures
        if (data.get(files._TRASH_STATE_KEY) == files._TRASH_RESTORED
                and terminal_failures == 0):
            terminal_failures += 1
            raise OSError("simulated terminal write failure")
        return real_write(path, data)

    monkeypatch.setattr(files, "_write_trash_manifest", fail_terminal_once)
    files.trash_restore(
        files.TrashIdReq(trash_id=trash_id),
        root=temp_root,
    )
    assert _mode(source) == 0o000
    files.trash_restore(
        files.TrashIdReq(trash_id=trash_id),
        root=temp_root,
    )
    assert _mode(source) == 0o000
    source.chmod(0o700)
    assert (source / "child.txt").read_bytes() == b"child"


def test_trash_id_reservation_skips_existing_restore_receipt(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    dustbin = files._ensure_trash_dir(temp_root)
    collision_id = "1700000000_deadbeef"
    fresh_id = "1700000001_cafebabe"
    old_receipt = {
        "schema_version": files._TRASH_SCHEMA_VERSION,
        files._TRASH_STATE_KEY: files._TRASH_RESTORED,
        "transaction_nonce": "old-transaction",
        "trash_id": collision_id,
        "original_path": "old-location.txt",
        "deleted_at": 1.0,
        "payload_identity": {"device": 1, "inode": 2, "kind": "file"},
    }
    receipt_path = files._restore_receipt_path(dustbin, collision_id)
    files._create_trash_manifest(receipt_path, old_receipt)
    ids = iter((collision_id, fresh_id))
    monkeypatch.setattr(files, "_gen_trash_id", lambda: next(ids))

    source = temp_root / "new-transaction.txt"
    source.write_bytes(b"new transaction")
    manifest = files._move_to_trash(source, temp_root)

    assert manifest["trash_id"] == fresh_id
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == old_receipt
    assert (dustbin / fresh_id).read_bytes() == b"new transaction"


def test_restore_finalization_rejects_receipt_from_different_transaction(
    app_module,
    temp_root,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "receipt-identity.txt"
    source.write_bytes(b"current")
    manifest = files._move_to_trash(source, temp_root)
    trash_id = manifest["trash_id"]
    dustbin = temp_root / ".muselab-dustbin"
    manifest_path = dustbin / f"{trash_id}.json"
    receipt_path = files._restore_receipt_path(dustbin, trash_id)
    old_receipt = {
        **manifest,
        files._TRASH_STATE_KEY: files._TRASH_RESTORED,
        "transaction_nonce": "different-transaction",
        "original_path": "wrong-location.txt",
    }
    files._create_trash_manifest(receipt_path, old_receipt)

    with pytest.raises(files.UnsafePrivatePath, match="receipt is invalid"):
        files._finalize_restore_manifest(
            manifest_path,
            {**manifest, files._TRASH_STATE_KEY: files._TRASH_RESTORED},
        )

    assert manifest_path.exists()
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == old_receipt


def test_restore_parent_symlink_swap_cannot_escape_workspace(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    ancestor = temp_root / "anchored-ancestor"
    parent = ancestor / "parent"
    parent.mkdir(parents=True)
    source = parent / "payload.txt"
    source.write_bytes(b"must stay in trash")
    manifest = files._move_to_trash(source, temp_root)
    trash_id = manifest["trash_id"]
    dustbin = temp_root / ".muselab-dustbin"
    payload = dustbin / trash_id
    detached_ancestor = temp_root.parent / "detached-ancestor"
    real_rename = files._rename_noreplace
    swapped = False

    def swap_parent_then_rename(rename_source, destination, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            destination.parent.parent.rename(detached_ancestor)
            destination.parent.parent.symlink_to(detached_ancestor)
        return real_rename(rename_source, destination, **kwargs)

    monkeypatch.setattr(
        files, "_rename_noreplace", swap_parent_then_rename)
    with pytest.raises((OSError, files.UnsafePrivatePath, HTTPException)):
        files.trash_restore(
            files.TrashIdReq(trash_id=trash_id),
            root=temp_root,
        )

    assert payload.read_bytes() == b"must stay in trash"
    assert not (detached_ancestor / "parent" / source.name).exists()
    ancestor.unlink()
    detached_ancestor.rename(ancestor)


@pytest.mark.parametrize(
    ("payload_kind", "original_mode"),
    (("file", 0o4750), ("directory", 0o3750)),
)
def test_restore_preserves_special_permission_bits(
    app_module,
    temp_root,
    payload_kind,
    original_mode,
) -> None:
    del app_module
    from backend import files

    source = temp_root / f"special-mode-{payload_kind}"
    if payload_kind == "directory":
        source.mkdir()
        (source / "child.txt").write_bytes(b"child")
    else:
        source.write_bytes(b"file")
    source.chmod(original_mode)
    assert _mode(source) == original_mode

    manifest = files._move_to_trash(source, temp_root)
    files.trash_restore(
        files.TrashIdReq(trash_id=manifest["trash_id"]),
        root=temp_root,
    )

    assert _mode(source) == original_mode
    source.chmod(0o700 if payload_kind == "directory" else 0o600)


def test_delete_crash_after_temporary_chmod_rolls_back_original_mode(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "prepared-mode-directory"
    source.mkdir()
    source.chmod(0o3750)
    real_rename = files._rename_noreplace

    def crash_before_rename(_source, _destination, **_kwargs):
        raise RuntimeError("simulated process crash before rename")

    monkeypatch.setattr(files, "_rename_noreplace", crash_before_rename)
    with pytest.raises(RuntimeError, match="process crash"):
        files._move_to_trash(source, temp_root)
    assert _mode(source) == 0o700

    monkeypatch.setattr(files, "_rename_noreplace", real_rename)
    files.ensure_private_trash_storage(temp_root, create=True)

    assert _mode(source) == 0o3750
    assert list((temp_root / ".muselab-dustbin").glob("*.json")) == []
    source.chmod(0o700)

def test_permanent_delete_rejects_workspace_root(
    app_module,
    temp_root,
) -> None:
    del app_module
    from backend import files

    marker = temp_root / "root-marker.txt"
    marker.write_bytes(b"keep root")
    with pytest.raises(HTTPException) as exc_info:
        files.delete(
            files.DeleteReq(path="."),
            permanent=True,
            root=temp_root,
        )

    assert exc_info.value.status_code == 400
    assert marker.read_bytes() == b"keep root"
    assert temp_root.is_dir()


def test_permanent_delete_rejects_registered_workspace_root(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    container = temp_root / "registered-container"
    registered = container / "registered-root"
    registered.mkdir(parents=True)
    marker = registered / "marker.txt"
    marker.write_bytes(b"registered")
    monkeypatch.setattr(
        files.workspace_registry,
        "paths",
        lambda: (temp_root, registered),
    )

    with pytest.raises(HTTPException) as exc_info:
        files.delete(
            files.DeleteReq(path=container.name),
            permanent=True,
            root=temp_root,
        )

    assert exc_info.value.status_code == 400
    assert marker.read_bytes() == b"registered"


def test_permanent_delete_unlinks_final_symlink_without_following_target(
    app_module,
    temp_root,
) -> None:
    del app_module
    from backend import files

    outside = temp_root.parent / "outside-permanent-target"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_bytes(b"outside")
    link = temp_root / "outside-link"
    link.symlink_to(outside, target_is_directory=True)

    result = files.delete(
        files.DeleteReq(path=link.name),
        permanent=True,
        root=temp_root,
    )

    assert result == {"ok": True, "permanent": True}
    assert not link.exists()
    assert not link.is_symlink()
    assert marker.read_bytes() == b"outside"
    shutil.rmtree(outside)


def test_delete_parent_same_inode_symlink_swap_rolls_back_via_held_dirfd(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    ancestor = temp_root / "anchored-delete"
    parent = ancestor / "parent"
    parent.mkdir(parents=True)
    source = parent / "payload.txt"
    source.write_bytes(b"must not disappear")
    detached = temp_root.parent / "detached-delete-ancestor"
    real_rename = files._rename_noreplace
    swapped = False

    def swap_parent_then_rename(rename_source, destination, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            ancestor.rename(detached)
            ancestor.symlink_to(detached, target_is_directory=True)
        return real_rename(rename_source, destination, **kwargs)

    monkeypatch.setattr(
        files,
        "_rename_noreplace",
        swap_parent_then_rename,
    )
    with pytest.raises(
        files.UnsafePrivatePath,
        match="no longer reachable",
    ):
        files._move_to_trash(source, temp_root)

    assert ancestor.is_symlink()
    assert (detached / "parent" / source.name).read_bytes() == (
        b"must not disappear"
    )
    dustbin = temp_root / ".muselab-dustbin"
    assert list(dustbin.glob("*.json")) == []
    assert not any(
        files._valid_trash_id(item.name)
        for item in dustbin.iterdir()
    )
    ancestor.unlink()
    detached.rename(ancestor)


def test_restore_wins_against_concurrent_purge(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "restore-wins.txt"
    source.write_bytes(b"restore wins")
    manifest = files._move_to_trash(source, temp_root)
    trash_id = manifest["trash_id"]
    persist_started = threading.Event()
    allow_persist = threading.Event()
    purge_finished = threading.Event()
    real_persist = files._persist_restore_completion
    outcomes: dict[str, object] = {}
    errors: list[BaseException] = []

    def block_restore_completion(manifest_path, data):
        persist_started.set()
        if not allow_persist.wait(timeout=5):
            raise TimeoutError("restore completion release timed out")
        return real_persist(manifest_path, data)

    def run_restore():
        try:
            outcomes["restore"] = files.trash_restore(
                files.TrashIdReq(trash_id=trash_id),
                root=temp_root,
            )
        except BaseException as exc:
            errors.append(exc)

    def run_purge():
        try:
            outcomes["purge"] = files._purge_one(trash_id, temp_root)
        except BaseException as exc:
            errors.append(exc)
        finally:
            purge_finished.set()

    monkeypatch.setattr(
        files,
        "_persist_restore_completion",
        block_restore_completion,
    )
    restore_thread = threading.Thread(target=run_restore, daemon=True)
    restore_thread.start()
    assert persist_started.wait(timeout=5)

    purge_thread = threading.Thread(target=run_purge, daemon=True)
    purge_thread.start()
    assert not purge_finished.wait(timeout=0.2)
    allow_persist.set()
    restore_thread.join(timeout=5)
    purge_thread.join(timeout=5)

    assert not restore_thread.is_alive()
    assert not purge_thread.is_alive()
    assert errors == []
    assert outcomes["restore"] == {
        "ok": True,
        "restored_path": source.name,
    }
    assert outcomes["purge"] == ("restored", False)
    assert source.read_bytes() == b"restore wins"


def test_purge_wins_and_recursive_cleanup_runs_outside_transaction_lock(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "purge-wins.txt"
    source.write_bytes(b"purge wins")
    manifest = files._move_to_trash(source, temp_root)
    trash_id = manifest["trash_id"]
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    restore_finished = threading.Event()
    real_remove = files._remove_tombstone_at
    outcomes: dict[str, object] = {}
    errors: list[BaseException] = []

    def block_tombstone_cleanup(trash_fd, name):
        if name.startswith(files._PURGE_TOMBSTONE_PREFIX):
            cleanup_started.set()
            if not allow_cleanup.wait(timeout=5):
                raise TimeoutError("purge cleanup release timed out")
        return real_remove(trash_fd, name)

    def run_purge():
        try:
            outcomes["purge"] = files._purge_one(trash_id, temp_root)
        except BaseException as exc:
            errors.append(exc)

    def run_restore():
        try:
            files.trash_restore(
                files.TrashIdReq(trash_id=trash_id),
                root=temp_root,
            )
        except HTTPException as exc:
            outcomes["restore_status"] = exc.status_code
        except BaseException as exc:
            errors.append(exc)
        finally:
            restore_finished.set()

    monkeypatch.setattr(
        files,
        "_remove_tombstone_at",
        block_tombstone_cleanup,
    )
    purge_thread = threading.Thread(target=run_purge, daemon=True)
    purge_thread.start()
    assert cleanup_started.wait(timeout=5)

    restore_thread = threading.Thread(target=run_restore, daemon=True)
    restore_thread.start()
    assert restore_finished.wait(timeout=1)
    assert outcomes["restore_status"] == 404

    allow_cleanup.set()
    purge_thread.join(timeout=5)
    restore_thread.join(timeout=5)
    assert not purge_thread.is_alive()
    assert not restore_thread.is_alive()
    assert errors == []
    assert outcomes["purge"] == ("purged", False)

    monkeypatch.setattr(files, "_remove_tombstone_at", real_remove)
    assert files._purge_one(trash_id, temp_root) == (
        "already_purged",
        False,
    )


def test_trash_auxiliary_gc_is_ttl_bounded_and_keeps_fresh_entries(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    files.ensure_private_trash_storage(temp_root, create=True)
    dustbin = temp_root / ".muselab-dustbin"
    stale_files = [
        dustbin / "stale.restored-receipt",
        dustbin / "stale.purged-receipt",
        dustbin / ".manifest.txn.stale",
    ]
    for path in stale_files:
        path.write_bytes(b"stale")
        path.chmod(0o600)
        os.utime(path, (0, 0))

    stale_tombstones = [
        dustbin / ".purging-stale",
        dustbin / ".permanent-stale",
    ]
    for path in stale_tombstones:
        path.mkdir(mode=0o700)
        (path / "payload").write_bytes(b"stale")
        os.utime(path, (0, 0))

    fresh = dustbin / "fresh.restored-receipt"
    fresh.write_bytes(b"fresh")
    fresh.chmod(0o600)
    monkeypatch.setattr(files, "_TRASH_AUX_TTL_SECONDS", 1)

    removed = files._gc_trash_auxiliary(temp_root)

    assert removed == len(stale_files) + len(stale_tombstones)
    assert all(not path.exists() for path in stale_files)
    assert all(not path.exists() for path in stale_tombstones)
    assert fresh.read_bytes() == b"fresh"


def test_trash_list_reports_storage_io_as_degraded(
    app_module,
    temp_root,
    monkeypatch,
) -> None:
    del app_module
    from backend import files

    source = temp_root / "degraded-list.txt"
    source.write_bytes(b"must not become an empty list")
    manifest = files._move_to_trash(source, temp_root)
    manifest_name = f"{manifest['trash_id']}.json"
    real_open = files.os.open

    def fail_manifest_open(path, *args, **kwargs):
        if (
            path == manifest_name
            and kwargs.get("dir_fd") is not None
        ):
            raise OSError("simulated manifest I/O failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(files.os, "open", fail_manifest_open)
    with pytest.raises(HTTPException) as exc_info:
        files.trash_list(root=temp_root)

    assert exc_info.value.status_code == 503
    assert (
        exc_info.value.headers["X-MuseLab-Error-Code"]
        == "trash_degraded"
    )


def test_transactions_for_distinct_roots_do_not_share_a_process_lock(
    app_module,
    temp_root,
) -> None:
    del app_module
    from backend import files

    other_root = temp_root.parent / "other-workspace-root"
    other_root.mkdir()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    errors: list[BaseException] = []

    def hold_first_root():
        try:
            with files._trash_transaction(temp_root):
                first_entered.set()
                if not release_first.wait(timeout=5):
                    raise TimeoutError("first root release timed out")
        except BaseException as exc:
            errors.append(exc)

    def enter_second_root():
        try:
            with files._trash_transaction(other_root):
                pass
        except BaseException as exc:
            errors.append(exc)
        finally:
            second_finished.set()

    first_thread = threading.Thread(target=hold_first_root, daemon=True)
    first_thread.start()
    assert first_entered.wait(timeout=5)

    second_thread = threading.Thread(target=enter_second_root, daemon=True)
    second_thread.start()
    assert second_finished.wait(timeout=1)

    release_first.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
