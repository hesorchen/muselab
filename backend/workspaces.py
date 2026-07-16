"""Registered working directories and the authenticated server folder picker.

The original muselab treated ``MUSELAB_ROOT`` as the only workspace.  The
browser can now register a small set of additional roots, and sessions/files
carry the selected root explicitly.  Registration is the only operation that
may look outside an existing workspace; normal file APIs remain confined to a
registered root.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from .auth import require_token
from .settings import ROOT, atomic_write_text


_FORBIDDEN = frozenset({
    Path("/"), Path("/home"), Path("/root"), Path("/etc"), Path("/usr"),
    Path("/var"), Path("/boot"),
})
_BROWSER_DENIED_ROOTS = (
    Path("/boot"), Path("/dev"), Path("/etc"), Path("/proc"), Path("/root"),
    Path("/run"), Path("/sys"), Path("/usr"), Path("/var"),
)
_PROJECT_MARKERS = (
    (".git", "Git"),
    ("AGENTS.md", "Codex"),
    ("CLAUDE.md", "Claude"),
    ("pyproject.toml", "Python"),
    ("package.json", "Node.js"),
    ("Cargo.toml", "Rust"),
    ("go.mod", "Go"),
    (".vscode", "VS Code"),
)
_BROWSER_LIMIT = 300


@dataclass(frozen=True)
class WorkspaceEntry:
    path: str
    name: str
    primary: bool = False


class WorkspaceRegistry:
    def __init__(self, primary: Path):
        self.primary = Path(primary).expanduser().resolve()
        self._path = self.primary / ".muselab" / "workspaces.json"
        self._lock = threading.RLock()
        self._workspaces = self._load()

    def list(self) -> list[WorkspaceEntry]:
        with self._lock:
            return [
                WorkspaceEntry(path=path, name=name,
                               primary=path == str(self.primary))
                for path, name in self._workspaces.items()
            ]

    def paths(self) -> tuple[Path, ...]:
        with self._lock:
            return tuple(Path(path) for path in self._workspaces)

    def contains(self, value: str | Path | None) -> bool:
        if value is None or not str(value).strip():
            return False
        try:
            clean = str(Path(value).expanduser().resolve())
        except (OSError, RuntimeError):
            return False
        with self._lock:
            return clean in self._workspaces

    def resolve(self, value: str | Path | None = None) -> Path:
        if value is None or not str(value).strip():
            return self.primary
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raw = self.primary / raw
        try:
            clean = raw.resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError("invalid workspace path") from exc
        with self._lock:
            if str(clean) not in self._workspaces:
                raise ValueError("workspace is not registered")
        return clean

    def register(self, value: str | Path, name: str | None = None) -> WorkspaceEntry:
        path = self._validated(value)
        clean_name = (name or path.name or str(path)).strip()
        if not clean_name:
            raise ValueError("workspace name cannot be empty")
        with self._lock:
            updated = dict(self._workspaces)
            updated[str(path)] = clean_name
            self._save(updated)
            self._workspaces = updated
        return WorkspaceEntry(str(path), clean_name, path == self.primary)

    def remove(self, value: str | Path) -> None:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.primary / path
        path = path.resolve()
        if path == self.primary:
            raise ValueError("primary workspace cannot be removed")
        with self._lock:
            if str(path) not in self._workspaces:
                raise ValueError("workspace is not registered")
            updated = dict(self._workspaces)
            del updated[str(path)]
            self._save(updated)
            self._workspaces = updated

    def browse(self, value: str | Path | None = None) -> dict[str, Any]:
        path = self._browser_path(value)
        with self._lock:
            registered = set(self._workspaces)
        try:
            candidates = sorted(path.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise ValueError("directory cannot be read") from exc

        directories: list[dict[str, Any]] = []
        for candidate in candidates:
            if candidate.name.startswith("."):
                continue
            try:
                if not candidate.is_dir():
                    continue
                clean = candidate.resolve()
            except (OSError, RuntimeError):
                continue
            if (not self._browser_allowed(clean)
                    or not os.access(clean, os.R_OK | os.X_OK)):
                continue
            directories.append({
                "path": str(clean),
                "name": candidate.name,
                "registered": str(clean) in registered,
                "selectable": self._selectable(clean),
                "project": self._project_hint(clean),
            })
        directories.sort(key=lambda item: (
            not bool(item["project"]), str(item["name"]).casefold()))
        truncated = len(directories) > _BROWSER_LIMIT
        directories = directories[:_BROWSER_LIMIT]
        parent = path.parent
        parent_value = (
            str(parent)
            if parent != path and self._browser_allowed(parent)
            else ""
        )
        return {
            "path": str(path),
            "name": path.name or str(path),
            "parent": parent_value,
            "registered": str(path) in registered,
            "selectable": self._selectable(path),
            "directories": directories,
            "truncated": truncated,
        }

    def _validated(self, value: str | Path) -> Path:
        raw = str(value).strip()
        if not raw:
            raise ValueError("workspace path cannot be empty")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.primary / path
        path = path.resolve()
        if path in _FORBIDDEN:
            raise ValueError("workspace path is too broad or sensitive")
        if not path.exists() or not path.is_dir():
            raise ValueError("workspace path must be an existing directory")
        if not os.access(path, os.R_OK | os.X_OK):
            raise ValueError("workspace directory cannot be read")
        return path

    def _browser_roots(self) -> tuple[Path, ...]:
        home = Path.home().resolve()
        roots: list[Path] = []
        for workspace in self.paths():
            if workspace == home or workspace.is_relative_to(home):
                root = home
            else:
                parent = workspace.parent
                sensitive = any(
                    parent == denied or parent.is_relative_to(denied)
                    for denied in _BROWSER_DENIED_ROOTS
                )
                root = workspace if parent in _FORBIDDEN or sensitive else parent
            if root not in roots:
                roots.append(root)
        return tuple(roots)

    def _browser_allowed(self, path: Path) -> bool:
        try:
            clean = path.resolve()
        except (OSError, RuntimeError):
            return False
        matches = [
            root for root in self._browser_roots()
            if clean == root or clean.is_relative_to(root)
        ]
        if not matches:
            return False
        sensitive = next((
            denied for denied in _BROWSER_DENIED_ROOTS
            if clean == denied or clean.is_relative_to(denied)
        ), None)
        if sensitive is None:
            return True
        home = Path.home().resolve()
        return any(
            root == home or (root != sensitive and root.is_relative_to(sensitive))
            for root in matches
        )

    def _browser_path(self, value: str | Path | None) -> Path:
        raw = str(value or "").strip()
        try:
            path = Path(raw).expanduser().resolve() if raw else self.primary
        except (OSError, RuntimeError) as exc:
            raise ValueError("invalid directory path") from exc
        if not self._browser_allowed(path):
            raise ValueError("directory is outside selectable workspace roots")
        if not path.exists() or not path.is_dir():
            raise ValueError("directory must exist")
        if not os.access(path, os.R_OK | os.X_OK):
            raise ValueError("directory cannot be read")
        return path

    def _selectable(self, path: Path) -> bool:
        try:
            self._validated(path)
        except ValueError:
            return False
        return True

    @staticmethod
    def _project_hint(path: Path) -> str:
        labels: list[str] = []
        for marker, label in _PROJECT_MARKERS:
            try:
                present = (path / marker).exists()
            except OSError:
                present = False
            if present:
                labels.append(label)
            if len(labels) >= 2:
                break
        return " · ".join(labels)

    def _load(self) -> dict[str, str]:
        values = {str(self.primary): self.primary.name or str(self.primary)}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return values
        entries = payload.get("workspaces") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return values
        for item in entries:
            if not isinstance(item, dict):
                continue
            try:
                path = self._validated(str(item.get("path") or ""))
            except ValueError:
                continue
            values[str(path)] = str(item.get("name") or path.name or path).strip()
        return values

    def _save(self, values: dict[str, str]) -> None:
        atomic_write_text(
            self._path,
            json.dumps({
                "workspaces": [
                    {"path": path, "name": name}
                    for path, name in values.items()
                ]
            }, ensure_ascii=False, indent=2),
        )


registry = WorkspaceRegistry(ROOT)


def resolve_workspace_root(
    workspace: str = Query(""),
    x_muselab_workspace: str = Header("", alias="X-Muselab-Workspace"),
) -> Path:
    try:
        requested = workspace or (
            unquote(x_muselab_workspace) if x_muselab_workspace else ""
        )
        return registry.resolve(requested or None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class WorkspaceRequest(BaseModel):
    path: str
    name: str = ""


router = APIRouter(prefix="/api/chat/workspaces", tags=["workspaces"])


@router.get("", dependencies=[Depends(require_token)])
def list_workspaces() -> dict[str, Any]:
    return {"workspaces": [asdict(entry) for entry in registry.list()]}


@router.get("/browse", dependencies=[Depends(require_token)])
def browse_workspaces(path: str = Query("")) -> dict[str, Any]:
    try:
        return registry.browse(path or None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("", dependencies=[Depends(require_token)])
def register_workspace(body: WorkspaceRequest) -> dict[str, Any]:
    try:
        entry = registry.register(body.path, body.name or None)
        # Session discovery is cached.  A newly registered directory may
        # already contain Claude JSONL history, so invalidate before the next
        # list pull instead of making the user wait for the stale TTL.
        from . import sessions
        sessions.invalidate_sessions_cache()
        return asdict(entry)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("", dependencies=[Depends(require_token)])
def remove_workspace(path: str = Query(...)) -> dict[str, bool]:
    try:
        registry.remove(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    from . import sessions
    sessions.invalidate_sessions_cache()
    return {"ok": True}
