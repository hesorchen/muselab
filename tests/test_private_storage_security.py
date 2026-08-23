"""Private attachment and trash storage security contracts."""

from __future__ import annotations

import json
import os
import stat
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
