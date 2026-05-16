"""Session metadata + message history persisted as JSON files."""
import json
import time
import uuid
from pathlib import Path
from typing import Any

from .settings import ROOT

# sessions live alongside the project, not in ROOT (so they don't pollute archives)
SESS_DIR = Path(__file__).resolve().parent.parent / "sessions"
SESS_DIR.mkdir(exist_ok=True)
INDEX = SESS_DIR / "index.json"


def _load_index() -> list[dict]:
    if not INDEX.exists():
        return []
    try:
        return json.loads(INDEX.read_text())
    except Exception:
        return []


def _save_index(items: list[dict]) -> None:
    INDEX.write_text(json.dumps(items, ensure_ascii=False, indent=2))


def list_sessions() -> list[dict]:
    return sorted(_load_index(), key=lambda s: s.get("updated_at", 0), reverse=True)


def create_session(name: str = "", model: str = "", system_prompt: str = "") -> dict:
    sid = str(uuid.uuid4())
    now = time.time()
    meta = {
        "id": sid,
        "name": name or "新会话",
        "model": model,
        "system_prompt": system_prompt,
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
    }
    idx = _load_index()
    idx.append(meta)
    _save_index(idx)
    (SESS_DIR / f"{sid}.json").write_text(json.dumps({"messages": []}, ensure_ascii=False))
    return meta


def get_session(sid: str) -> dict | None:
    idx = _load_index()
    meta = next((s for s in idx if s["id"] == sid), None)
    if meta is None:
        return None
    p = SESS_DIR / f"{sid}.json"
    messages = []
    if p.exists():
        try:
            messages = json.loads(p.read_text()).get("messages", [])
        except Exception:
            pass
    return {**meta, "messages": messages}


def delete_session(sid: str) -> bool:
    idx = _load_index()
    new = [s for s in idx if s["id"] != sid]
    if len(new) == len(idx):
        return False
    _save_index(new)
    p = SESS_DIR / f"{sid}.json"
    if p.exists():
        p.unlink()
    return True


def rename_session(sid: str, name: str) -> bool:
    idx = _load_index()
    for s in idx:
        if s["id"] == sid:
            s["name"] = name
            s["updated_at"] = time.time()
            _save_index(idx)
            return True
    return False


def append_messages(sid: str, new_messages: list[dict]) -> None:
    p = SESS_DIR / f"{sid}.json"
    data: dict[str, Any] = {"messages": []}
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except Exception:
            pass
    data["messages"].extend(new_messages)
    p.write_text(json.dumps(data, ensure_ascii=False))
    # bump index
    idx = _load_index()
    for s in idx:
        if s["id"] == sid:
            s["updated_at"] = time.time()
            s["message_count"] = len(data["messages"])
            break
    _save_index(idx)


def update_model(sid: str, model: str) -> None:
    idx = _load_index()
    for s in idx:
        if s["id"] == sid:
            s["model"] = model
            _save_index(idx)
            return


def update_system_prompt(sid: str, system_prompt: str) -> bool:
    idx = _load_index()
    for s in idx:
        if s["id"] == sid:
            s["system_prompt"] = system_prompt
            s["updated_at"] = time.time()
            _save_index(idx)
            return True
    return False
