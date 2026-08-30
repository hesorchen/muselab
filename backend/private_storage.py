"""Private-storage primitives for MuseLab-owned internal data."""

from __future__ import annotations

import os
import secrets
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
    """Atomically replace one private regular file with durable 0600 bytes.

    The destination is never truncated in place.  A same-directory private
    temporary file is written and fsynced first, then ``os.replace`` is the
    sole commit point.  Every failure before that point removes the temporary
    file and leaves an existing destination untouched.  There is deliberately
    no fallible operation after a successful replace: callers can therefore
    treat an exception as proof that this invocation did not publish a new
    final file.
    """
    path = Path(path)
    ensure_private_directory(path.parent)
    existing = private_path_kind(path)
    if existing not in {"missing", "file"}:
        raise UnsafePrivatePath("private storage destination is unsafe")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)

    fd = -1
    temp_path: Path | None = None
    try:
        # O_EXCL makes a pre-created symlink or special-file substitution fail
        # closed. Retrying only handles an astronomically unlikely random-name
        # collision; no existing entry is ever opened or removed here.
        for _ in range(16):
            candidate = path.parent / (
                f".{path.name}.{secrets.token_hex(8)}.tmp"
            )
            try:
                fd = os.open(candidate, flags, PRIVATE_FILE_MODE)
            except FileExistsError:
                continue
            temp_path = candidate
            break
        if temp_path is None:
            raise FileExistsError("could not allocate private temporary file")
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafePrivatePath("private temporary path is not a file")
        os.fchmod(fd, PRIVATE_FILE_MODE)

        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("private storage write made no progress")
            view = view[written:]
        os.fsync(fd)

        # Close before rename so every operation that can report a write or
        # flush failure happens before the visible commit point.
        closing_fd = fd
        fd = -1
        os.close(closing_fd)

        # Preserve the existing contract: a destination replaced by a symlink,
        # directory, FIFO, or device while we wrote the temp file is rejected.
        # Replacing a regular file remains supported.
        existing = private_path_kind(path)
        if existing not in {"missing", "file"}:
            raise UnsafePrivatePath("private storage destination is unsafe")
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    # This must remain the final fallible operation. If replace raises, the
    # temporary file is still ours to remove; if it succeeds, return directly
    # without an fsync/chmod/cleanup step that could report failure while
    # leaving the newly published destination behind.
    try:
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
