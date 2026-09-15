"""Isolated real HTTP/SSE fixture; never imported by production."""
import json
import os
import threading
from pathlib import Path
from datetime import datetime, timezone

import uvicorn
from fastapi import Depends
from claude_agent_sdk import SystemMessage

from backend import chat
from backend.auth import require_token
from backend.main import app


_parents = {}
_history_gate = threading.Event()
_history_gate.set()
_index_history = chat._ensure_transcript_index


def _gated_index(sid):
    # Keep actual disk indexing, but hold history repair until the browser has
    # proved live delivery. Otherwise a fast reload masks a broken mux.
    _history_gate.wait(timeout=10)
    return _index_history(sid)


chat._ensure_transcript_index = _gated_index


def _record(sid, role, content, record_id):
    path = Path(os.environ["MUSELAB_ROOT"]) / "sdk-transcripts" / f"{sid}.jsonl"
    path.parent.mkdir(exist_ok=True)
    row = {
        "type": role, "uuid": record_id, "parentUuid": _parents.get(sid),
        "sessionId": sid, "timestamp": datetime.now(timezone.utc).isoformat(),
        "cwd": os.environ["MUSELAB_ROOT"], "isSidechain": False,
        "message": {"role": role, "content": content},
    }
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(row) + "\n")
    _parents[sid] = record_id
    chat._JSONL_PATH_CACHE[sid] = path


@app.post("/fixture/begin", dependencies=[Depends(require_token)])
async def begin(payload: dict):
    sid = payload["sid"]
    broadcast = chat.TurnBroadcast(session_id=sid, model="deepseek-v4-pro")
    broadcast.user_text = "Inspect the fixture with an Agent"
    _record(sid, "user", broadcast.user_text, "fixture-user")
    _record(sid, "assistant", [{"type": "text", "text": "LIVE_PARENT_START"}], "fixture-parent-start")
    chat._active_turns[sid] = broadcast
    chat._announce_mux_turn(broadcast)
    broadcast.publish({"event": "text", "data": json.dumps({"text": "LIVE_PARENT_START"})})
    return {"turn_id": broadcast.turn_id}


@app.post("/fixture/native-compact", dependencies=[Depends(require_token)])
async def native_compact(payload: dict):
    broadcast = chat._active_turns[payload["sid"]]
    phase = payload["phase"]
    message = (
        SystemMessage(subtype="compact_boundary", data={})
        if phase == "boundary" else
        SystemMessage(subtype="status", data={
            "status": "compacting" if phase == "start" else None,
        })
    )
    event = broadcast.native_compaction_progress(message)
    if event is not None:
        broadcast.publish(event)
    return {"started_at_ms": broadcast.native_compact_started_at_ms}


@app.post("/fixture/burst", dependencies=[Depends(require_token)])
async def burst(payload: dict):
    broadcast = chat._active_turns[payload["sid"]]
    sid = payload["sid"]
    for i in range(payload.get("count", 1)):
        _record(sid, "assistant", [{
            "type": "tool_use", "id": f"agent-{i}", "name": "Agent",
            "input": {"description": "Inspect fixture", "run_in_background": True},
        }], f"fixture-tool-{i}")
        _record(sid, "user", [{
            "type": "tool_result", "tool_use_id": f"agent-{i}", "content": "Fixture done",
        }], f"fixture-result-{i}")
        broadcast.publish({"event": "tool_use", "data": json.dumps({
            "id": f"agent-{i}", "name": "Agent", "summary": "Inspect fixture",
            "input": {"description": "Inspect fixture", "run_in_background": True},
        })})
        broadcast.publish({"event": "task_started", "data": json.dumps({
            "task_id": f"task-{i}", "tool_use_id": f"agent-{i}", "description": "Inspect fixture",
        })})
        broadcast.publish({"event": "subagent_delta", "data": json.dumps({
            "parent_tool_use_id": f"agent-{i}", "block_id": f"child-block-{i}",
            "kind": "assistant", "offset": 0, "delta": f"LIVE_CHILD_{i}",
        })})
        broadcast.publish({"event": "task_notification", "data": json.dumps({
            "task_id": f"task-{i}", "tool_use_id": f"agent-{i}", "status": "completed",
            "background_tasks_pending": 0, "summary": "Fixture done",
        })})
    _record(sid, "assistant", [{"type": "text", "text": "LIVE_PARENT_AFTER_AGENTS"}], "fixture-parent-final")
    broadcast.publish({"event": "text", "data": json.dumps({"text": "LIVE_PARENT_AFTER_AGENTS"})})
    return {"events": broadcast._event_seq}


@app.post("/fixture/continuation", dependencies=[Depends(require_token)])
async def continuation(payload: dict):
    sid = payload["sid"]
    previous = chat._active_turns[sid]
    _history_gate.clear()
    previous.publish({"event": "done", "data": json.dumps({
        "assistant_uuid": "fixture-parent-final", "background_tasks_pending": 0,
    })})
    previous.finish()
    for i in range(3):
        broadcast = chat.TurnBroadcast(session_id=sid, model="deepseek-v4-pro")
        broadcast.is_continuation = True
        broadcast.parent_turn_id = previous.turn_id
        chat._active_turns[sid] = broadcast
        chat._announce_mux_turn(broadcast)
        broadcast.publish({"event": "text", "data": json.dumps({
            "text": f"LIVE_CONTINUATION_{i}",
        })})
        _record(sid, "assistant", [{"type": "text", "text": f"LIVE_CONTINUATION_{i}"}], f"fixture-continuation-{i}")
        broadcast.publish({"event": "done", "data": json.dumps({
            "assistant_uuid": f"fixture-continuation-{i}", "continuation": True,
            "background_tasks_pending": 0,
        })})
        broadcast.finish()
        previous = broadcast
    chat._active_turns.pop(sid)
    chat._remember_recent_turn(sid, previous)
    return {"turn_id": previous.turn_id}


@app.post("/fixture/finish", dependencies=[Depends(require_token)])
async def finish(payload: dict):
    sid = payload["sid"]
    broadcast = chat._active_turns.pop(sid)
    broadcast.publish({"event": "done", "data": json.dumps({
        "assistant_uuid": "fixture-parent-start", **payload["done"],
    })})
    broadcast.finish()
    chat._remember_recent_turn(sid, broadcast)
    return {"finished": True}


@app.post("/fixture/release-history", dependencies=[Depends(require_token)])
async def release_history():
    _history_gate.set()
    return {"released": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["MUSELAB_PORT"]),
                log_level="warning", timeout_graceful_shutdown=2)
