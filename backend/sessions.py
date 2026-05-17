"""Session metadata + message history persisted as JSON files."""
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .settings import ROOT


def _default_session_name() -> str:
    """新会话 MM-DD HH:mm — enough to distinguish multiple new sessions per day."""
    return "新会话 " + datetime.now().strftime("%m-%d %H:%M")


def _title_from_message(text: str, limit: int = 24) -> str:
    """First-line snippet of the user's first message, trimmed for the dropdown."""
    if not text:
        return ""
    # Drop @-mention paths so the title shows what the user actually asked
    cleaned = re.sub(r"@\S+\s*", "", text).strip()
    first_line = cleaned.splitlines()[0] if cleaned else ""
    first_line = first_line.strip()
    if len(first_line) > limit:
        first_line = first_line[: limit - 1].rstrip() + "…"
    return first_line

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
        "name": name or _default_session_name(),
        "model": model,
        "system_prompt": system_prompt,
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
        # marker: True until the user manually renames (or first message auto-renames)
        # — used by append_messages to decide whether to auto-rename
        "auto_named": True,
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
            s["auto_named"] = False   # user-set: stop auto-renaming on next message
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
            # Auto-rename from the first user message — only if still auto-named.
            # (Legacy sessions don't have the flag; treat name == "新会话*" as auto.)
            is_auto = s.get("auto_named", s.get("name", "").startswith("新会话"))
            if is_auto:
                first_user = next(
                    (m for m in data["messages"] if m.get("role") == "user"
                                                   and (m.get("text") or "").strip()),
                    None,
                )
                if first_user:
                    title = _title_from_message(first_user.get("text", ""))
                    if title:
                        s["name"] = title
                        s["auto_named"] = False
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
