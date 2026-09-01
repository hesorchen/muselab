"""Pure canonical-message to UI presentation shaping for chat history."""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from .task_summaries import normalize_task_summary


_CLI_SLASH_TAGS_RE = re.compile(
    r"<(command-name|command-message|command-args|"
    r"local-command-stdout|local-command-stderr)>.*?</\1>",
    re.DOTALL,
)
_TASK_NOTIFICATION_RE = re.compile(
    r"<task-notification>(.*?)</task-notification>", re.DOTALL)
_BG_LAUNCH_RE = re.compile(
    r"Command running in background with ID:\s*([A-Za-z0-9._-]+)\."
    r"\s*Output is being written to:\s*(\S+?\.output)\b")
_BASH_TAG_RE = re.compile(
    r"<(stdout|stderr|exit_code|interrupted|description)>"
    r"(.*?)</\1>",
    re.DOTALL,
)

MAX_INPUT_FIELD_LEN = 100_000
SLIM_INPUT_FIELDS = frozenset({
    "file_path", "notebook_path", "path",
    "command", "pattern", "url", "query",
    "name", "skill", "subagent_type", "description", "todos",
    "old_string", "new_string", "edits", "content",
    "offset", "limit",
    "timeout", "run_in_background",
    "replace_all",
    "subject", "activeForm",
    "taskId", "task_id", "status",
    "addBlocks", "addBlockedBy",
})
TOOL_RESULT_PREVIEW_CAP = 500
TOOL_RESULT_TEXT_CAP = 50_000
HISTORY_INLINE_BODY_CAP = 8_000
HISTORY_BODY_PREVIEW_CAP = 2_000


def strip_cli_slash_wrapper(text: str) -> str:
    if not text:
        return text
    return _CLI_SLASH_TAGS_RE.sub("", text).strip()


def parse_task_notifications(text: str) -> list[dict]:
    if not text or not text.lstrip().startswith("<task-notification>"):
        return []
    recs: list[dict] = []
    for match in _TASK_NOTIFICATION_RE.finditer(text):
        body = match.group(1)

        def field(tag: str) -> str:
            found = re.search(rf"<{tag}>(.*?)</{tag}>", body, re.DOTALL)
            return found.group(1).strip() if found else ""

        recs.append({
            "tool_use_id": field("tool-use-id"),
            "task_id": field("task-id"),
            "status": field("status"),
            "summary": field("summary"),
            "output_file": field("output-file"),
        })
    return recs


def parse_bg_launch(text: str) -> dict | None:
    if not text:
        return None
    match = _BG_LAUNCH_RE.search(text)
    if not match:
        return None
    return {"task_id": match.group(1), "output_file": match.group(2)}


def usermsg_task_notification_text(
    msg: Any,
    *,
    user_message_type: type,
    text_block_type: type,
) -> str:
    if not isinstance(msg, user_message_type):
        return ""
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, text_block_type):
                parts.append(getattr(block, "text", "") or "")
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        text = "".join(parts)
    else:
        return ""
    return text if "<task-notification>" in text else ""


def slim_input_value(value: Any, *, max_length: int) -> Any:
    if isinstance(value, str) and len(value) > max_length:
        return (value[:max_length]
                + f"\n…[truncated, {len(value) - max_length} chars more]")
    if isinstance(value, (list, dict)):
        try:
            dumped = json.dumps(value, ensure_ascii=False)
            if len(dumped) > max_length:
                return f"[truncated structured field, {len(dumped)} chars total]"
        except (TypeError, ValueError):
            pass
    return value


def summarize_tool_input(name: str | None, inp: dict) -> str:
    if not name:
        return ""
    if name in ("Read", "Edit", "Write"):
        return inp.get("file_path", "")
    if name == "Bash":
        return (inp.get("command") or "")[:200]
    if name in ("Glob", "Grep"):
        return ((inp.get("pattern") or "")
                + (f"  in {inp.get('path', '')}" if inp.get("path") else ""))
    if name == "WebFetch":
        return inp.get("url", "")
    if name == "WebSearch":
        return inp.get("query", "")
    if name == "TodoWrite":
        return f"{len(inp.get('todos') or [])} todos"
    if name in ("Task", "Agent"):
        sub = inp.get("subagent_type") or "agent"
        desc = inp.get("description") or ""
        return f"[{sub}] {desc}"[:240]
    if name == "ExitPlanMode":
        return (inp.get("plan") or "")[:240]
    if name == "Skill":
        return inp.get("name") or inp.get("skill") or ""
    return ""


def parse_bash_result(text: str) -> dict | None:
    if not text or "<" not in text:
        return None
    matches = list(_BASH_TAG_RE.finditer(text))
    if not matches:
        return None
    parts: dict[str, Any] = {}
    for match in matches:
        tag, body = match.group(1), match.group(2)
        if tag == "exit_code":
            try:
                parts["exit_code"] = int(body.strip())
            except ValueError:
                pass
        elif tag == "interrupted":
            parts["interrupted"] = body.strip().lower() in ("true", "1", "yes")
        else:
            parts[tag] = body
    return parts or None


def render_tool_use(
    block: Any,
    *,
    max_input_field_len: int,
    slim_input_fields: frozenset[str],
    slim_value: Callable[[Any], Any],
) -> dict:
    inp = block.input or {}
    name = block.name
    if name in ("Read", "Edit", "Write"):
        summary = inp.get("file_path", "")
    elif name == "Bash":
        summary = (inp.get("command") or "")[:max_input_field_len]
    elif name in ("Glob", "Grep"):
        summary = ((inp.get("pattern") or "")
                   + (f"  in {inp.get('path', '')}" if inp.get("path") else ""))
    elif name == "WebFetch":
        summary = inp.get("url", "")
    elif name == "WebSearch":
        summary = inp.get("query", "")
    elif name == "TodoWrite":
        summary = f"{len(inp.get('todos') or [])} todos"
    elif name in ("Task", "Agent"):
        sub = inp.get("subagent_type") or "agent"
        desc = inp.get("description") or ""
        summary = f"[{sub}] {desc}"[:240]
    elif name == "ExitPlanMode":
        summary = (inp.get("plan") or "")[:240]
    elif name == "Skill":
        summary = inp.get("name") or inp.get("skill") or ""
    else:
        summary = json.dumps(inp, ensure_ascii=False)[:200]

    out: dict = {
        "name": name,
        "summary": summary,
        "id": block.id,
        "input": {k: slim_value(v) for k, v in inp.items()
                  if k in slim_input_fields},
    }
    if name == "TodoWrite":
        out["todos"] = inp.get("todos") or []
    elif name in ("Task", "Agent"):
        out["task"] = {
            "subagent_type": inp.get("subagent_type"),
            "description": inp.get("description"),
            "prompt": inp.get("prompt"),
        }
    elif name == "ExitPlanMode":
        out["plan"] = inp.get("plan") or ""
    return out


def tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text", str(part)))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return ""


def render_tool_result(
    block: Any,
    *,
    tool_name: str,
    preview_cap: int,
    text_cap: int,
    parse_bash: Callable[[str], dict | None],
) -> dict:
    text = tool_result_text(block.content)
    out: dict = {
        "id": getattr(block, "tool_use_id", None),
        "preview": text[:preview_cap],
        "truncated": len(text) > preview_cap,
        "text": text[:text_cap],
        "text_truncated": len(text) > text_cap,
        "is_error": bool(getattr(block, "is_error", False)),
    }
    if tool_name:
        out["tool_name"] = tool_name
    if tool_name == "Bash":
        bash = parse_bash(text)
        if bash:
            out["bash"] = bash
    return out


def defer_large_ui_bodies(
    messages: list[dict],
    *,
    inline_body_cap: int,
    body_preview_cap: int,
    tool_result_preview_cap: int,
) -> None:
    for message in messages:
        role = str(message.get("role") or "")
        if (role not in {"assistant", "thinking", "tool_result"}
                and not message.get("_is_compact_summary")):
            continue
        text = message.get("text")
        block_id = str(message.get("block_id") or "")
        if (not isinstance(text, str) or len(text) <= inline_body_cap
                or not block_id):
            continue
        preview_cap = (tool_result_preview_cap
                       if role == "tool_result" else body_preview_cap)
        preview = text[:preview_cap]
        message["text"] = preview
        message["preview"] = preview
        message["body_state"] = "unloaded"
        message["body_available"] = True
        message["body_length"] = len(text)
        message["body_ref"] = block_id
        message.pop("bash", None)
        message["text_truncated"] = False


def sdk_messages_to_ui(
    sm_list: list,
    annotations: dict[str, dict],
    compact_uuids: set[str] | None,
    *,
    defer_large_bodies: bool,
    is_cli_interrupt_message: Callable[[str], bool],
    slim_input_fields: frozenset[str],
    slim_value: Callable[[Any], Any],
    summarize_input: Callable[[str | None, dict], str],
    parse_bash: Callable[[str], dict | None],
    tool_result_preview_cap: int,
    defer_bodies: Callable[[list[dict]], None],
) -> list[dict]:
    compact_uuids = compact_uuids or set()
    out: list[dict] = []
    tool_use_names: dict[str, str] = {}
    for sm in sm_list:
        ann = annotations.get(sm.uuid, {})
        is_compact = sm.uuid in compact_uuids
        msg = sm.message or {}
        content = msg.get("content")
        steering = msg.get("_muselab_steering")
        if isinstance(steering, dict):
            text = content if isinstance(content, str) else ""
            if not text:
                continue
            entry = {
                "role": "user",
                "text": text,
                "displayText": str(
                    ann.get("steering_display_text", text) or ""),
                "selectionQuotes": (
                    ann.get("steering_selection_quotes")
                    if isinstance(ann.get("steering_selection_quotes"), list)
                    else []
                ),
                "uuid": sm.uuid,
                "_turnRoot": False,
                "_steeringAdjustment": True,
            }
            queue_item_id = str(
                ann.get("steering_queue_item_id") or "")
            if queue_item_id:
                entry["_queueItemId"] = queue_item_id
            turn_id = str(ann.get("steering_turn_id") or "")
            if turn_id:
                entry["_turnId"] = turn_id
            for key, value in ann.items():
                if not key.startswith("steering_"):
                    entry[key] = value
            out.append(entry)
            continue
        if isinstance(content, str):
            if is_cli_interrupt_message(content):
                continue
            notifications = parse_task_notifications(content)
            if notifications:
                for notification in notifications:
                    tool_use_id = notification.get("tool_use_id")
                    if not tool_use_id:
                        continue
                    raw_status = notification.get("status") or ""
                    state = (raw_status if raw_status in
                             ("completed", "failed", "stopped") else "done")
                    for previous in reversed(out):
                        if (previous.get("role") == "tool_use"
                                and previous.get("id") == tool_use_id):
                            previous["task_status"] = {
                                "task_id": notification.get("task_id") or "",
                                "state": state,
                                **normalize_task_summary(
                                    notification.get("summary"),
                                    summary_length=notification.get("summary_length"),
                                    summary_truncated=notification.get("summary_truncated"),
                                ),
                                "output_file": notification.get("output_file") or "",
                            }
                            break
                continue
            text = strip_cli_slash_wrapper(content)
            if not text:
                continue
            entry = {"role": sm.type, "text": text, "uuid": sm.uuid}
            if is_compact:
                entry["_is_compact_summary"] = True
            entry.update(ann)
            out.append(entry)
            continue
        if not isinstance(content, list):
            continue

        text_buf = ""
        image_refs = []

        def flush_text() -> None:
            nonlocal text_buf, image_refs
            cleaned = strip_cli_slash_wrapper(text_buf)
            if is_cli_interrupt_message(cleaned):
                cleaned = ""
            if not cleaned and not image_refs:
                text_buf = ""
                image_refs = []
                return
            entry = {"role": sm.type, "text": cleaned, "uuid": sm.uuid}
            if is_compact:
                entry["_is_compact_summary"] = True
            if image_refs:
                entry.setdefault("images", image_refs)
            entry.update(ann)
            out.append(entry)
            text_buf = ""
            image_refs = []

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text_buf += block.get("text", "")
            elif block_type == "thinking":
                flush_text()
                thinking_text = block.get("thinking", "") or ""
                if not thinking_text.strip() and block.get("signature"):
                    thinking_text = "[已加密推理 · 仅 streaming 期间可见明文]"
                out.append({"role": "thinking", "text": thinking_text,
                            "uuid": sm.uuid})
            elif block_type == "tool_use":
                flush_text()
                tool_name = block.get("name") or ""
                tool_use_id = block.get("id") or ""
                if tool_use_id:
                    tool_use_names[tool_use_id] = tool_name
                raw_input = block.get("input") or {}
                tool_use = {
                    "role": "tool_use",
                    "uuid": sm.uuid,
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": {k: slim_value(v) for k, v in raw_input.items()
                              if k in slim_input_fields},
                    "summary": summarize_input(tool_name, raw_input),
                }
                if tool_name == "TodoWrite":
                    tool_use["todos"] = raw_input.get("todos") or []
                elif tool_name in ("Task", "Agent"):
                    tool_use["task"] = {
                        "subagent_type": raw_input.get("subagent_type"),
                        "description": raw_input.get("description"),
                        "prompt": raw_input.get("prompt"),
                    }
                elif tool_name == "ExitPlanMode":
                    tool_use["plan"] = raw_input.get("plan") or ""
                out.append(tool_use)
            elif block_type == "tool_result":
                flush_text()
                result_text = tool_result_text(block.get("content"))
                tool_use_id = block.get("tool_use_id") or ""
                tool_name = tool_use_names.get(tool_use_id, "")
                entry = {
                    "role": "tool_result", "uuid": sm.uuid,
                    "id": tool_use_id,
                    "preview": result_text[:tool_result_preview_cap],
                    "truncated": len(result_text) > tool_result_preview_cap,
                    "text": result_text,
                    "text_truncated": False,
                    "is_error": bool(block.get("is_error", False)),
                }
                if tool_name:
                    entry["tool_name"] = tool_name
                if tool_name == "Bash":
                    bash = parse_bash(result_text)
                    if bash:
                        entry["bash"] = bash
                out.append(entry)
            elif block_type == "image":
                source = block.get("source") or {}
                image_refs.append({"mime": source.get("media_type") or ""})
        flush_text()

    mts_by_uuid: dict[str, int] = {}
    for sm in sm_list:
        value = getattr(sm, "mts", None)
        if value:
            mts_by_uuid[sm.uuid] = value
    key_ordinals: dict[str, int] = {}
    for entry in out:
        message_uuid = entry.get("uuid")
        if not message_uuid:
            continue
        ordinal = key_ordinals.get(message_uuid, 0)
        key_ordinals[message_uuid] = ordinal + 1
        block_id = f"{message_uuid}:{ordinal}:{entry.get('role') or 'unknown'}"
        entry["block_id"] = block_id
        entry["_key"] = block_id
        if message_uuid in mts_by_uuid:
            entry["mts"] = mts_by_uuid[message_uuid]
        ann = annotations.get(message_uuid, {})
        for source, target in (
            ("ts", "ts"),
            ("elapsed_s", "elapsed"),
            ("model", "model"),
            ("turn_status", "turn_status"),
            ("turn_id", "turn_id"),
            ("memory_recall", "memoryRecall"),
        ):
            value = ann.get(source)
            if value is not None and (source not in {
                "model", "turn_status", "turn_id", "memory_recall",
            }
                                      or value):
                entry.setdefault(target, value)
        ann_images = ann.get("images")
        if ann_images and entry.get("role") == "user":
            entry["images"] = ann_images
        ann_docs = ann.get("docs")
        if ann_docs and entry.get("role") == "user":
            entry["docs"] = ann_docs
    if defer_large_bodies:
        defer_bodies(out)
    return out


def outline_preview(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return "(empty)"
    lines = raw.split("\n")
    one_line = next(
        (line for line in lines
         if line.strip() and not line.strip().startswith(">")),
        lines[0] if lines else raw,
    )
    cleaned = re.sub(r"^#+\s*", "", one_line).strip()
    return cleaned[:77] + "…" if len(cleaned) > 80 else cleaned


def describe_transcript_record(
    entry: dict,
    *,
    raw_message_factory: Callable[..., Any],
    raw_entry_factory: Callable[[dict], Any] | None = None,
    render_messages: Callable[[list, dict, set[str]], list[dict]],
    is_real_user_prompt: Callable[[Any], bool],
) -> dict:
    msg = (
        raw_entry_factory(entry)
        if raw_entry_factory is not None
        else raw_message_factory(
            str(entry.get("uuid") or ""),
            str(entry.get("type") or ""),
            entry.get("message") or {},
        )
    )
    if msg is None:
        return {
            "bubble_count": 0,
            "user_preview": "",
            "real_user_prompt": False,
            "has_inline_images": False,
            "tool_uses": [],
            "task_notifications": [],
            "presentation_record": False,
            "presentation_uuid": "",
        }
    compact = {msg.uuid} if entry.get("isCompactSummary") else set()
    bubbles = render_messages([msg], {}, compact)
    tool_uses: list[dict] = []
    content = (msg.message or {}).get("content")
    is_steering = isinstance(
        (msg.message or {}).get("_muselab_steering"), dict)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_uses.append({
                    "id": block.get("id") or "",
                    "name": block.get("name") or "",
                })
    notifications = (parse_task_notifications(content)
                     if isinstance(content, str) else [])
    user_bubble = next(
        (bubble for bubble in bubbles if bubble.get("role") == "user"), None)
    user_text = (user_bubble or {}).get("text", "")
    has_inline_images = (
        isinstance(content, list)
        and any(isinstance(block, dict) and block.get("type") == "image"
                for block in content)
    )
    return {
        "bubble_count": len(bubbles),
        "user_preview": outline_preview(user_text) if user_bubble is not None else "",
        "real_user_prompt": (
            not is_steering
            and is_real_user_prompt(msg)
            and not notifications
        ),
        "has_inline_images": has_inline_images,
        "tool_uses": tool_uses,
        "task_notifications": notifications,
        "presentation_record": is_steering,
        "presentation_uuid": msg.uuid if is_steering else "",
    }


def complete_turn_footer_metadata(
    messages: list[dict],
    session_model: str,
    *,
    has_later: bool,
    active_turn: Any,
    now: Callable[[], float],
) -> None:
    if not messages:
        return
    active_uuid = ""
    if active_turn is not None and not active_turn.done:
        active_uuid = str(active_turn.last_assistant_uuid or "")

    index = 0
    while index < len(messages):
        if messages[index].get("role") == "user":
            index += 1
            continue
        group: list[dict] = []
        while index < len(messages):
            item = messages[index]
            if item.get("role") == "user":
                # Native steering is an inline human adjustment inside the
                # same logical turn. It splits assistant text/tool segments for
                # rendering, but must not close the preceding segment or grow
                # a second completed/running footer.
                if item.get("_steeringAdjustment") is True:
                    index += 1
                    continue
                break
            group.append(item)
            index += 1
        if not group:
            continue
        tail = group[-1]
        origin_uuid = str(tail.get("_turn_origin_uuid") or "")
        scope = ([item for item in group
                  if str(item.get("_turn_origin_uuid") or "") == origin_uuid]
                 if origin_uuid else group)
        if not scope:
            scope = [tail]

        def last_value(field: str) -> Any:
            for item in reversed(scope):
                if field not in item:
                    continue
                value = item.get(field)
                if value is not None and value != "":
                    return value
            return None

        status = str(last_value("turn_status") or "")
        active_scope = bool(active_uuid and any(
            str(item.get("uuid") or "") == active_uuid for item in scope))
        closed_by_user = index < len(messages)
        complete_window_tail = index == len(messages) and not has_later
        if not status:
            if active_scope:
                status = "running"
            elif closed_by_user or complete_window_tail:
                status = "completed"
        if not status:
            continue

        terminal_ms = last_value("ts")
        if terminal_ms is None:
            terminal_ms = last_value("mts")
        try:
            terminal_ms = int(terminal_ms) if terminal_ms is not None else None
        except (TypeError, ValueError, OverflowError):
            terminal_ms = None
        started_ms = last_value("turn_started_at")
        try:
            started_ms = int(started_ms) if started_ms is not None else None
        except (TypeError, ValueError, OverflowError):
            started_ms = None
        elapsed = last_value("elapsed")
        try:
            elapsed = float(elapsed) if elapsed is not None else None
        except (TypeError, ValueError, OverflowError):
            elapsed = None
        if elapsed is None and started_ms is not None:
            end_ms = int(now() * 1000) if status == "running" else terminal_ms
            if end_ms is not None:
                elapsed = round(max(0.0, (end_ms - started_ms) / 1000), 1)

        footer_model = str(last_value("model") or "")
        if not footer_model and active_scope and active_turn is not None:
            footer_model = str(active_turn.model or "")
        if not footer_model:
            footer_model = str(session_model or "")

        tail["turn_status"] = status
        if status != "running" and terminal_ms is not None:
            tail["ts"] = terminal_ms
        if started_ms is not None:
            tail["turn_started_at"] = started_ms
        if elapsed is not None:
            tail["elapsed"] = elapsed
        if footer_model:
            tail["model"] = footer_model
        memory_recall = last_value("memoryRecall")
        if memory_recall:
            tail["memoryRecall"] = memory_recall
        terminal_reason = last_value("terminal_reason")
        if terminal_reason:
            tail["terminal_reason"] = terminal_reason
        turn_origin = last_value("turn_origin")
        if isinstance(turn_origin, dict):
            tail["turn_origin"] = turn_origin
        turn_id = str(last_value("turn_id") or "")
        if turn_id:
            tail["turn_id"] = turn_id
        model_usage = last_value("model_usage")
        if isinstance(model_usage, dict):
            tail["model_usage"] = model_usage


def broadcast_to_ui_messages(broadcast: Any) -> list[dict]:
    out: list[dict] = []
    if broadcast.user_text or broadcast.user_images or broadcast.user_docs:
        out.append({
            "role": "user",
            "text": broadcast.user_text,
            "images": broadcast.user_images,
            "docs": broadcast.user_docs,
            "_turnId": broadcast.turn_id,
            "_turnRoot": True,
        })
    current_text: dict | None = None
    current_thinking: dict | None = None
    steering_users: set[str] = set()

    def close_segment() -> None:
        nonlocal current_text, current_thinking
        current_text = None
        current_thinking = None

    def apply_task_status(tool_use_id: str, task_id: str, **patch: Any) -> None:
        for message in reversed(out):
            if message.get("role") != "tool_use":
                continue
            status = message.get("task_status") or {}
            if ((tool_use_id and message.get("id") == tool_use_id)
                    or (task_id and status.get("task_id") == task_id)):
                message["task_status"] = {**status, "task_id": task_id, **patch}
                return

    for event in broadcast.replay_events():
        kind = event.get("event") or ""
        try:
            data = json.loads(event.get("data") or "{}")
        except Exception:
            continue
        if kind == "text":
            current_thinking = None
            chunk = data.get("text", "")
            if current_text is None:
                current_text = {"role": "assistant", "text": chunk,
                                "model": broadcast.model,
                                "turn_id": broadcast.turn_id}
                out.append(current_text)
            else:
                current_text["text"] += chunk
        elif kind == "thinking":
            current_text = None
            chunk = data.get("text", "")
            if current_thinking is None:
                current_thinking = {"role": "thinking", "text": chunk}
                out.append(current_thinking)
            else:
                current_thinking["text"] += chunk
        elif kind == "tool_use":
            close_segment()
            out.append({
                "role": "tool_use",
                "name": data.get("name"),
                "id": data.get("id"),
                "summary": data.get("summary"),
                "input": data.get("input") or {},
                "task_status": None,
                "_approvalSuperseded": False,
                **({"todos": data["todos"]} if "todos" in data else {}),
                **({"task": data["task"]} if "task" in data else {}),
                **({"plan": data["plan"]} if "plan" in data else {}),
            })
        elif kind == "tool_result":
            close_segment()
            out.append({
                "role": "tool_result",
                "id": data.get("id"),
                "tool_name": data.get("tool_name") or "",
                "preview": data.get("preview"),
                "text": data.get("text") or "",
                "truncated": data.get("truncated"),
                "text_truncated": data.get("text_truncated"),
                "is_error": data.get("is_error"),
                "bash": data.get("bash"),
            })
        elif kind == "queue_steering":
            state = str(data.get("state") or "")
            message = data.get("message")
            command_uuid = str(data.get("command_uuid") or "")
            identity = command_uuid or str(data.get("item_id") or "")
            if (state in {"started", "completed"}
                    and isinstance(message, dict)
                    and identity not in steering_users):
                close_segment()
                steering_users.add(identity)
                out.append({
                    "role": "user",
                    "text": str(message.get("text") or ""),
                    "displayText": str(message.get("display_text") or ""),
                    "selectionQuotes": (
                        message.get("selection_quotes")
                        if isinstance(message.get("selection_quotes"), list)
                        else []
                    ),
                    "images": [],
                    "docs": [],
                    "uuid": command_uuid,
                    "_turnId": str(data.get("turn_id") or broadcast.turn_id),
                    "_turnRoot": False,
                    "_steeringAdjustment": True,
                    "_queueItemId": str(data.get("item_id") or ""),
                })
        elif kind == "task_started":
            apply_task_status(
                str(data.get("tool_use_id") or ""),
                str(data.get("task_id") or ""),
                state="running",
                description=data.get("description") or "",
            )
        elif kind == "task_progress":
            apply_task_status(
                str(data.get("tool_use_id") or ""),
                str(data.get("task_id") or ""),
                state="running",
                usage=data.get("usage") or {},
                last_tool_name=data.get("last_tool_name") or "",
            )
        elif kind == "task_notification":
            raw_state = str(data.get("status") or "")
            state = (raw_state if raw_state in {"completed", "failed", "stopped"}
                     else "done")
            apply_task_status(
                str(data.get("tool_use_id") or ""),
                str(data.get("task_id") or ""),
                state=state,
                **normalize_task_summary(
                    data.get("summary"),
                    summary_length=data.get("summary_length"),
                    summary_truncated=data.get("summary_truncated"),
                ),
                output_file=data.get("output_file") or "",
            )
        elif kind == "ask_user_question":
            close_segment()
            questions = data.get("questions") or []
            out.append({
                "role": "ask_user_question",
                "id": data.get("id"),
                "questions": questions,
                "pendingAnswers": {
                    str(question.get("question") or ""): (
                        [] if question.get("multiSelect") else None)
                    for question in questions if isinstance(question, dict)
                },
                "submitted": True,
                "askOtherOpen": False,
                "askOtherText": "",
            })
        elif kind == "permission_request":
            close_segment()
            out.append({
                "role": "permission_request",
                "id": data.get("id"),
                "tool": data.get("tool"),
                "summary": data.get("summary"),
                "kind": data.get("kind") or "tool",
                "suggestions": data.get("suggestions") or [],
                "return_mode": data.get("return_mode") or "",
                "title": data.get("title") or "",
                "display_name": data.get("display_name") or "",
                "description": data.get("description") or "",
                "input": data.get("input") or {},
                "tool_use_id": data.get("tool_use_id") or data.get("toolUseId") or "",
                "plan": data.get("plan") or "",
                "resolved": True,
                "decision": "expired",
                "mode": None,
                "submitting": False,
                "awaitingTransition": False,
                "_decisionAcknowledged": True,
                "failure_message": "",
            })
    if out:
        out[-1].setdefault("turn_id", broadcast.turn_id)
    return out
