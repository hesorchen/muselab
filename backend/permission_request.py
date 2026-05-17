"""
permission_request — bridge SDK's can_use_tool callback to a UI prompt.

When permission_mode is not "bypassPermissions", the SDK calls `can_use_tool`
before invoking certain tools. We push a question to the session's side channel,
await the user's button click, and translate the answer into the SDK's expected
PermissionResultAllow / PermissionResultDeny shape.

"Always allow" works at the muselab session level (in-memory): subsequent calls
to the same (tool, key) pair bypass the prompt for the rest of this session.
"""
import asyncio
import json
import uuid
from typing import Any

# (session_id, request_id) -> Future of {"decision": "allow"|"deny"|"always",
#                                          "message": str|None}
_pending: dict[tuple[str, str], asyncio.Future] = {}

# session_id -> queue (re-uses ask_user_question's _session_queues at runtime
# via the shared registry below).
_session_queues: dict[str, asyncio.Queue] = {}

# Per-session "always allow" cache: {sid: set[(tool_name, key)]}
# key derives from tool input — e.g. for Bash: the command; for Edit: file_path.
_always_allow: dict[str, set[tuple[str, str]]] = {}

DECISION_TIMEOUT_S = 600


def register_session_queue(session_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _session_queues[session_id] = q
    _always_allow.setdefault(session_id, set())
    return q


def unregister_session_queue(session_id: str) -> None:
    _session_queues.pop(session_id, None)
    _always_allow.pop(session_id, None)
    for key in list(_pending.keys()):
        if key[0] == session_id:
            fut = _pending.pop(key, None)
            if fut is not None and not fut.done():
                fut.cancel()


def submit_decision(session_id: str, request_id: str, decision: str,
                     message: str | None = None) -> bool:
    """Frontend POSTs here. decision in {allow, deny, always}."""
    if decision not in ("allow", "deny", "always"):
        return False
    fut = _pending.get((session_id, request_id))
    if fut is None or fut.done():
        return False
    fut.set_result({"decision": decision, "message": message})
    return True


def _input_key(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Pick a stable identifying field per tool for the always-allow cache."""
    if tool_name == "Bash":
        cmd = (tool_input.get("command") or "").strip()
        # First word/binary — broaden the cache so "ls -la X" and "ls Y" share.
        return cmd.split()[0] if cmd else ""
    if tool_name in ("Read", "Edit", "Write", "NotebookEdit"):
        return str(tool_input.get("file_path") or "")
    if tool_name in ("Glob", "Grep"):
        return str(tool_input.get("pattern") or "")
    if tool_name in ("WebFetch", "WebSearch"):
        return str(tool_input.get("url") or tool_input.get("query") or "")
    return ""


def build_callback_for_session(session_id: str):
    """Return an async callable matching the SDK's can_use_tool signature."""

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any],
                            context: Any) -> dict[str, Any]:
        # Always-allow cache check. Empty set is falsy, so don't use `or`.
        key = _input_key(tool_name, tool_input)
        cache = _always_allow.setdefault(session_id, set())
        if (tool_name, key) in cache:
            return {"behavior": "allow", "updatedInput": tool_input}

        q = _session_queues.get(session_id)
        if q is None:
            # No UI subscribed — fail closed (deny) so the model gets a clear
            # signal instead of hanging.
            return {
                "behavior": "deny",
                "message": "No active UI session; cannot prompt for permission.",
            }

        request_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        _pending[(session_id, request_id)] = fut

        # Render the input compactly for the UI.
        if tool_name == "Bash":
            summary = (tool_input.get("command") or "")[:400]
        elif tool_name in ("Read", "Edit", "Write"):
            summary = str(tool_input.get("file_path") or "")
        else:
            try:
                summary = json.dumps(tool_input, ensure_ascii=False)[:400]
            except Exception:
                summary = str(tool_input)[:400]

        await q.put({
            "event": "permission_request",
            "data": json.dumps({
                "id": request_id,
                "tool": tool_name,
                "summary": summary,
                "input": tool_input,
            }, ensure_ascii=False),
        })

        try:
            result = await asyncio.wait_for(fut, timeout=DECISION_TIMEOUT_S)
        except asyncio.TimeoutError:
            return {
                "behavior": "deny",
                "message": "User did not respond within 10 minutes.",
            }
        except asyncio.CancelledError:
            return {
                "behavior": "deny",
                "message": "User session ended before answering.",
            }
        finally:
            _pending.pop((session_id, request_id), None)

        decision = result["decision"]
        if decision == "always":
            _always_allow.setdefault(session_id, set()).add((tool_name, key))
            return {"behavior": "allow", "updatedInput": tool_input}
        if decision == "allow":
            return {"behavior": "allow", "updatedInput": tool_input}
        return {
            "behavior": "deny",
            "message": result.get("message") or "User denied the request.",
        }

    return can_use_tool
