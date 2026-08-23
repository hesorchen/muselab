"""Private-storage primitives for MuseLab-owned internal data."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Literal


PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
PrivatePathKind = Literal["missing", "symlink", "directory", "file", "other"]


class UnsafePrivatePath(RuntimeError):
    """A MuseLab-owned storage path was replaced by an unsafe file type."""


def _private_path_status(path: Path) -> tuple[PrivatePathKind, int | None]:
    try:
        mode = Path(path).lstat().st_mode
    except FileNotFoundError:
        return "missing", None
    if stat.S_ISLNK(mode):
        kind: PrivatePathKind = "symlink"
    elif stat.S_ISDIR(mode):
        kind = "directory"
    elif stat.S_ISREG(mode):
        kind = "file"
    else:
        kind = "other"
    return kind, mode


def private_path_kind(path: Path) -> PrivatePathKind:
    """Classify without ever following a symlink."""
    return _private_path_status(path)[0]


def ensure_private_directory(path: Path, *, create: bool = True) -> bool:
    """Create/repair one private directory, rejecting symlink substitution."""
    path = Path(path)
    kind, mode = _private_path_status(path)
    if kind == "missing" and create:
        path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
        kind, mode = _private_path_status(path)
    if kind == "missing":
        return False
    if kind != "directory":
        raise UnsafePrivatePath("private storage directory is unsafe")
    if mode is not None and stat.S_IMODE(mode) != PRIVATE_DIR_MODE:
        os.chmod(path, PRIVATE_DIR_MODE, follow_symlinks=False)
    return True


def ensure_private_regular_file(path: Path) -> bool:
    """Repair a private regular file without following a symlink."""
    path = Path(path)
    kind, mode = _private_path_status(path)
    if kind == "missing":
        return False
    if kind != "file":
        raise UnsafePrivatePath("private storage file is unsafe")
    if mode is not None and stat.S_IMODE(mode) != PRIVATE_FILE_MODE:
        os.chmod(path, PRIVATE_FILE_MODE, follow_symlinks=False)
    return True


def repair_private_path(path: Path) -> PrivatePathKind:
    """Tighten one existing path; symlinks and special files are untouched."""
    path = Path(path)
    kind, mode = _private_path_status(path)
    current_mode = stat.S_IMODE(mode) if mode is not None else None
    if kind == "directory" and current_mode != PRIVATE_DIR_MODE:
        os.chmod(path, PRIVATE_DIR_MODE, follow_symlinks=False)
    elif kind == "file" and current_mode != PRIVATE_FILE_MODE:
        os.chmod(path, PRIVATE_FILE_MODE, follow_symlinks=False)
    return kind


def write_private_bytes(path: Path, data: bytes) -> None:
    """Write a regular file as 0600 while refusing a symlink at open time."""
    path = Path(path)
    ensure_private_directory(path.parent)
    existing = private_path_kind(path)
    if existing not in {"missing", "file"}:
        raise UnsafePrivatePath("private storage destination is unsafe")

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafePrivatePath("private storage destination is not a file")
        os.fchmod(fd, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
