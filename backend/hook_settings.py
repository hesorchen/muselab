"""Safe CRUD for Claude Code hooks stored in standard settings files.

The Claude CLI remains the source of truth.  This module edits only the
``hooks`` and ``disableAllHooks`` keys in the three settings scopes that the
SDK loads through ``setting_sources``:

* ``~/.claude/settings.json`` (user)
* ``<workspace>/.claude/settings.json`` (project)
* ``<workspace>/.claude/settings.local.json`` (local)

All mutations are revision-checked read/modify/write transactions.  Unrelated
settings and forward-compatible hook fields are retained, while HTTP hook
headers are masked on reads and restored when a UI echoes the mask back.
"""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import secrets
import stat
import threading
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .auth import require_token
from .workspaces import resolve_workspace_root


HookScope = Literal["user", "project", "local"]

USER_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
_MAX_SETTINGS_BYTES = 4 * 1024 * 1024
_MISSING_REVISION = hashlib.sha256(b"muselab:missing-hook-settings").hexdigest()
_REVISION_PATTERN = r"^[0-9a-f]{64}$"

router = APIRouter(prefix="/hooks", tags=["settings"])

_path_locks_guard = threading.Lock()
_path_locks: dict[str, threading.RLock] = {}
_runtime_invalidator: Callable[[HookScope, Path], None] | None = None

# Programmatic SDK hooks are part of MuseLab's runtime bridge, not any Claude
# settings file.  Surface them so the GUI explains why they cannot be edited
# alongside standard settings-based hooks.
_BUILTIN_HOOKS = [
    {
        "event": "UserPromptSubmit",
        "matcher": "",
        "label": "Memory recall / runtime context",
        "conditional": True,
    },
    {
        "event": "PreToolUse",
        "matcher": "AskUserQuestion",
        "label": "Browser question bridge",
        "conditional": False,
    },
    {
        "event": "PreToolUse",
        "matcher": "Skill",
        "label": "Codex gateway Skill guard",
        "conditional": True,
    },
    {
        "event": "PostToolUse / PostToolUseFailure",
        "matcher": "EnterPlanMode / ExitPlanMode",
        "label": "Permission mode synchronization",
        "conditional": False,
    },
]


def configure_runtime_invalidator(
    callback: Callable[[HookScope, Path], None] | None,
) -> None:
    """Install the chat-runtime invalidation bridge during app startup."""
    global _runtime_invalidator
    _runtime_invalidator = callback


class HookSettingsError(RuntimeError):
    """Base class for a settings file that cannot be safely edited."""


class UnsafeHookSettingsPath(HookSettingsError):
    """The settings directory or file is a symlink/special file."""


class InvalidHookSettings(HookSettingsError):
    """The file or requested hook structure is not valid JSON settings."""


class HookRevisionConflict(HookSettingsError):
    """The file changed after the caller loaded it."""

    def __init__(self, current_revision: str):
        super().__init__("hook settings changed; reload and retry")
        self.current_revision = current_revision


class HookTargetNotFound(HookSettingsError):
    """The requested matcher group or handler no longer exists."""


class HookMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str = Field(
        min_length=64,
        max_length=64,
        pattern=_REVISION_PATTERN,
    )


class HookCreateRequest(HookMutationRequest):
    event: str = Field(min_length=1, max_length=128)
    matcher: str | None = Field(default=None, max_length=2048)
    handler: dict[str, Any]
    group_index: int | None = Field(default=None, ge=0)


class HookUpdateRequest(HookMutationRequest):
    event: str = Field(min_length=1, max_length=128)
    group_index: int = Field(ge=0)
    handler_index: int = Field(ge=0)
    matcher: str | None = Field(default=None, max_length=2048)
    handler: dict[str, Any] = Field(default_factory=dict)
    remove_fields: list[str] = Field(default_factory=list, max_length=64)


class HookDeleteRequest(HookMutationRequest):
    event: str = Field(min_length=1, max_length=128)
    group_index: int = Field(ge=0)
    handler_index: int = Field(ge=0)


class DisableAllHooksRequest(HookMutationRequest):
    disabled: bool


def _lock_for(path: Path) -> threading.RLock:
    key = os.path.abspath(os.fspath(path))
    with _path_locks_guard:
        return _path_locks.setdefault(key, threading.RLock())


def _settings_path(scope: HookScope, workspace_root: Path) -> Path:
    if scope == "user":
        return Path(USER_SETTINGS_PATH)
    if scope == "project":
        return Path(workspace_root) / ".claude" / "settings.json"
    return Path(workspace_root) / ".claude" / "settings.local.json"


def _path_label(scope: HookScope) -> str:
    return {
        "user": "~/.claude/settings.json",
        "project": ".claude/settings.json",
        "local": ".claude/settings.local.json",
    }[scope]


def _directory_mode(scope: HookScope) -> int:
    return 0o700 if scope == "user" else 0o755


def _new_file_mode(scope: HookScope) -> int:
    return 0o644 if scope == "project" else 0o600


def _open_settings_directory(
    path: Path,
    scope: HookScope,
    *,
    create: bool,
) -> int | None:
    """Open the direct settings parent without following a symlink."""
    parent = path.parent
    if create:
        try:
            parent.mkdir(mode=_directory_mode(scope), exist_ok=True)
        except FileNotFoundError as exc:
            raise UnsafeHookSettingsPath(
                "settings parent directory does not exist"
            ) from exc
        except OSError as exc:
            raise UnsafeHookSettingsPath(
                "settings directory cannot be created safely"
            ) from exc

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(parent, flags)
    except FileNotFoundError:
        if not create:
            return None
        raise UnsafeHookSettingsPath("settings directory is missing")
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UnsafeHookSettingsPath(
                "settings directory must not be a symlink"
            ) from exc
        raise UnsafeHookSettingsPath(
            "settings directory cannot be opened safely"
        ) from exc


def _read_raw(
    path: Path,
    scope: HookScope,
) -> tuple[bytes | None, int | None]:
    """Read one regular file via its directory fd, never through a symlink."""
    directory_fd = _open_settings_directory(path, scope, create=False)
    if directory_fd is None:
        return None, None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            file_fd = os.open(path.name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EISDIR, errno.ENXIO}:
                raise UnsafeHookSettingsPath(
                    "settings file must be a regular file, not a symlink"
                ) from exc
            raise UnsafeHookSettingsPath(
                "settings file cannot be opened safely"
            ) from exc

        with os.fdopen(file_fd, "rb") as handle:
            file_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise UnsafeHookSettingsPath(
                    "settings file must be a regular file"
                )
            raw = handle.read(_MAX_SETTINGS_BYTES + 1)
            if len(raw) > _MAX_SETTINGS_BYTES:
                raise InvalidHookSettings("settings file is too large")
            return raw, stat.S_IMODE(file_stat.st_mode)
    finally:
        os.close(directory_fd)


def _revision(raw: bytes | None) -> str:
    if raw is None:
        return _MISSING_REVISION
    return hashlib.sha256(raw).hexdigest()


def _parse_settings(raw: bytes | None) -> dict[str, Any]:
    if raw is None:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidHookSettings(
            "settings file contains invalid JSON; repair it before editing hooks"
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidHookSettings("settings file must contain a JSON object")
    _validate_hooks_container(payload)
    return payload


def _validate_hooks_container(settings: dict[str, Any]) -> None:
    disabled = settings.get("disableAllHooks")
    if disabled is not None and not isinstance(disabled, bool):
        raise InvalidHookSettings("disableAllHooks must be a boolean")

    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise InvalidHookSettings("hooks must be a JSON object")
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            raise InvalidHookSettings("each hook event must contain a list")
        for group in groups:
            if not isinstance(group, dict):
                raise InvalidHookSettings("each matcher group must be an object")
            matcher = group.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                raise InvalidHookSettings("hook matcher must be a string")
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                raise InvalidHookSettings(
                    "each matcher group must contain a hooks list"
                )
            if not all(isinstance(handler, dict) for handler in handlers):
                raise InvalidHookSettings("each hook handler must be an object")


def _validate_handler(handler: dict[str, Any]) -> None:
    handler_type = handler.get("type")
    if not isinstance(handler_type, str) or not handler_type.strip():
        raise InvalidHookSettings("hook handler type is required")
    headers = handler.get("headers")
    if headers is not None:
        if not isinstance(headers, dict):
            raise InvalidHookSettings("hook headers must be an object")
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in headers.items()
        ):
            raise InvalidHookSettings("hook header names and values must be strings")


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * 8
    return f"{value[:4]}{'•' * 8}{value[-4:]}"


def _mask_headers(value: Any) -> Any:
    """Deep-copy hook JSON while hiding every field named ``headers``."""
    if isinstance(value, list):
        return [_mask_headers(item) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    masked: dict[str, Any] = {}
    for key, item in value.items():
        if key == "headers" and isinstance(item, dict):
            masked[key] = {
                name: _mask_secret(secret) if isinstance(secret, str) else "•" * 8
                for name, secret in item.items()
            }
        else:
            masked[key] = _mask_headers(item)
    return masked


def _restore_masked_headers(candidate: Any, existing: Any) -> Any:
    """Replace UI mask echoes with the corresponding stored header secret."""
    if isinstance(candidate, list):
        old_items = existing if isinstance(existing, list) else []
        return [
            _restore_masked_headers(
                item,
                old_items[index] if index < len(old_items) else None,
            )
            for index, item in enumerate(candidate)
        ]
    if not isinstance(candidate, dict):
        return copy.deepcopy(candidate)

    old_mapping = existing if isinstance(existing, dict) else {}
    restored: dict[str, Any] = {}
    for key, item in candidate.items():
        old_item = old_mapping.get(key)
        if key == "headers" and isinstance(item, dict):
            old_headers = old_item if isinstance(old_item, dict) else {}
            headers: dict[str, Any] = {}
            for name, value in item.items():
                if isinstance(value, str) and "•" in value:
                    old_value = old_headers.get(name)
                    if not isinstance(old_value, str):
                        raise InvalidHookSettings(
                            "masked hook header has no stored value"
                        )
                    headers[name] = old_value
                else:
                    headers[name] = copy.deepcopy(value)
            restored[key] = headers
        else:
            restored[key] = _restore_masked_headers(item, old_item)
    return restored


def _serialize(settings: dict[str, Any]) -> bytes:
    return (
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def _atomic_write(
    path: Path,
    scope: HookScope,
    payload: bytes,
    *,
    existing_mode: int | None,
) -> None:
    directory_fd = _open_settings_directory(path, scope, create=True)
    if directory_fd is None:  # pragma: no cover - create=True guarantees fd
        raise UnsafeHookSettingsPath("settings directory is missing")

    temp_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    mode = (
        _new_file_mode(scope)
        if existing_mode is None or scope in {"user", "local"}
        else existing_mode
    )
    temp_created = False
    try:
        # Recheck the destination at commit time.  os.replace would not follow
        # a symlink, but rejecting it gives callers the promised fail-closed
        # behavior instead of silently replacing an unexpected entry.
        try:
            destination_stat = os.stat(
                path.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            destination_stat = None
        if destination_stat is not None and not stat.S_ISREG(
            destination_stat.st_mode
        ):
            raise UnsafeHookSettingsPath(
                "settings file must be a regular file, not a symlink"
            )

        file_fd = os.open(
            temp_name,
            flags,
            mode,
            dir_fd=directory_fd,
        )
        temp_created = True
        with os.fdopen(file_fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temp_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temp_created = False
        os.fsync(directory_fd)
    finally:
        if temp_created:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except OSError:
                pass
        os.close(directory_fd)


def _load_scope(scope: HookScope, workspace_root: Path) -> tuple[
    Path, dict[str, Any], str, bool, int | None
]:
    path = _settings_path(scope, workspace_root)
    raw, mode = _read_raw(path, scope)
    return path, _parse_settings(raw), _revision(raw), raw is not None, mode


def _scope_payload(
    scope: HookScope,
    workspace_root: Path,
) -> dict[str, Any]:
    path, settings, revision, exists, _ = _load_scope(scope, workspace_root)
    return {
        "scope": scope,
        "path": _path_label(scope),
        "resolved_path": str(path),
        "exists": exists,
        "revision": revision,
        "disableAllHooks": bool(settings.get("disableAllHooks", False)),
        "hooks": _mask_headers(settings.get("hooks", {})),
    }


def _target_handler(
    settings: dict[str, Any],
    event: str,
    group_index: int,
    handler_index: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    hooks = settings.get("hooks")
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    if not isinstance(groups, list) or group_index >= len(groups):
        raise HookTargetNotFound("hook matcher group not found")
    group = groups[group_index]
    handlers = group.get("hooks") if isinstance(group, dict) else None
    if not isinstance(handlers, list) or handler_index >= len(handlers):
        raise HookTargetNotFound("hook handler not found")
    handler = handlers[handler_index]
    if not isinstance(handler, dict):  # validated on load; defensive only
        raise InvalidHookSettings("hook handler must be an object")
    return group, handlers, handler


def _mutate_scope(
    scope: HookScope,
    workspace_root: Path,
    expected_revision: str,
    mutate,
) -> dict[str, Any]:
    path = _settings_path(scope, workspace_root)
    with _lock_for(path):
        _, settings, current_revision, _, existing_mode = _load_scope(
            scope, workspace_root
        )
        if current_revision != expected_revision:
            raise HookRevisionConflict(current_revision)
        mutate(settings)
        _validate_hooks_container(settings)
        _atomic_write(
            path,
            scope,
            _serialize(settings),
            existing_mode=existing_mode,
        )
        payload = _scope_payload(scope, workspace_root)
    callback = _runtime_invalidator
    if callback is not None:
        # The settings write is already durable. Runtime invalidation is a
        # cache concern and must never turn a successful write into an error.
        try:
            callback(scope, workspace_root)
        except Exception:
            pass
    return payload


def _raise_http(exc: HookSettingsError) -> None:
    if isinstance(exc, HookRevisionConflict):
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "current_revision": exc.current_revision,
            },
        ) from exc
    if isinstance(exc, HookTargetNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, InvalidHookSettings):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("", dependencies=[Depends(require_token)])
def list_hook_settings(
    workspace_root: Path = Depends(resolve_workspace_root),
) -> dict[str, Any]:
    try:
        scopes = [
            _scope_payload(scope, workspace_root)
            for scope in ("user", "project", "local")
        ]
    except HookSettingsError as exc:
        _raise_http(exc)
    return {
        "workspace": str(workspace_root),
        "scopes": scopes,
        "builtin_hooks": copy.deepcopy(_BUILTIN_HOOKS),
    }


@router.get("/{scope}", dependencies=[Depends(require_token)])
def get_hook_settings_scope(
    scope: HookScope,
    workspace_root: Path = Depends(resolve_workspace_root),
) -> dict[str, Any]:
    try:
        return _scope_payload(scope, workspace_root)
    except HookSettingsError as exc:
        _raise_http(exc)


@router.post("/{scope}/handlers", dependencies=[Depends(require_token)])
def create_hook_handler(
    scope: HookScope,
    request: HookCreateRequest,
    workspace_root: Path = Depends(resolve_workspace_root),
) -> dict[str, Any]:
    def mutate(settings: dict[str, Any]) -> None:
        handler = _restore_masked_headers(request.handler, None)
        _validate_handler(handler)
        hooks = settings.setdefault("hooks", {})
        if not isinstance(hooks, dict):  # guarded by parser, keeps type narrow
            raise InvalidHookSettings("hooks must be a JSON object")
        groups = hooks.setdefault(request.event, [])
        if not isinstance(groups, list):
            raise InvalidHookSettings("hook event must contain a list")
        if request.group_index is None:
            group: dict[str, Any] = {"hooks": [handler]}
            if request.matcher:
                group["matcher"] = request.matcher
            groups.append(group)
            return
        if request.group_index >= len(groups):
            raise HookTargetNotFound("hook matcher group not found")
        target = groups[request.group_index]
        handlers = target.get("hooks") if isinstance(target, dict) else None
        if not isinstance(handlers, list):
            raise InvalidHookSettings("matcher group must contain a hooks list")
        handlers.append(handler)

    try:
        result = _mutate_scope(
            scope, workspace_root, request.revision, mutate
        )
    except HookSettingsError as exc:
        _raise_http(exc)
    return {"ok": True, **result}


@router.put("/{scope}/handlers", dependencies=[Depends(require_token)])
def update_hook_handler(
    scope: HookScope,
    request: HookUpdateRequest,
    workspace_root: Path = Depends(resolve_workspace_root),
) -> dict[str, Any]:
    def mutate(settings: dict[str, Any]) -> None:
        group, handlers, existing = _target_handler(
            settings,
            request.event,
            request.group_index,
            request.handler_index,
        )
        updated = copy.deepcopy(existing)
        patch = _restore_masked_headers(request.handler, existing)
        updated.update(patch)
        for field in request.remove_fields:
            updated.pop(field, None)
        _validate_handler(updated)
        handlers[request.handler_index] = updated
        if request.matcher is not None:
            if request.matcher:
                group["matcher"] = request.matcher
            else:
                group.pop("matcher", None)

    try:
        result = _mutate_scope(
            scope, workspace_root, request.revision, mutate
        )
    except HookSettingsError as exc:
        _raise_http(exc)
    return {"ok": True, **result}


@router.delete("/{scope}/handlers", dependencies=[Depends(require_token)])
def delete_hook_handler(
    scope: HookScope,
    request: HookDeleteRequest,
    workspace_root: Path = Depends(resolve_workspace_root),
) -> dict[str, Any]:
    def mutate(settings: dict[str, Any]) -> None:
        _, handlers, _ = _target_handler(
            settings,
            request.event,
            request.group_index,
            request.handler_index,
        )
        del handlers[request.handler_index]
        hooks = settings["hooks"]
        groups = hooks[request.event]
        if not handlers:
            del groups[request.group_index]
        if not groups:
            del hooks[request.event]
        if not hooks:
            settings.pop("hooks", None)

    try:
        result = _mutate_scope(
            scope, workspace_root, request.revision, mutate
        )
    except HookSettingsError as exc:
        _raise_http(exc)
    return {"ok": True, **result}


@router.patch("/{scope}/disable-all", dependencies=[Depends(require_token)])
def set_disable_all_hooks(
    scope: HookScope,
    request: DisableAllHooksRequest,
    workspace_root: Path = Depends(resolve_workspace_root),
) -> dict[str, Any]:
    def mutate(settings: dict[str, Any]) -> None:
        settings["disableAllHooks"] = request.disabled

    try:
        result = _mutate_scope(
            scope, workspace_root, request.revision, mutate
        )
    except HookSettingsError as exc:
        _raise_http(exc)
    return {"ok": True, **result}
