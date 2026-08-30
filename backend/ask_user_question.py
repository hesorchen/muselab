"""Browser-side state bridge for the SDK-native AskUserQuestion tool.

permission_request.py receives the native tool call through can_use_tool, or a
PreToolUse hook in bypassPermissions mode. It publishes the normalized question
to this module's per-session queue, awaits the Future resolved by the browser's
answer endpoint, and injects that answer back into the native tool input.
"""
import asyncio
from typing import Any

# Per-session pending registry: (session_id, question_id) -> Future of answers dict.
_pending: dict[tuple[str, str], asyncio.Future] = {}

# Per-session SSE event queue: streaming endpoint subscribes; tool handler publishes.
_session_queues: dict[str, asyncio.Queue] = {}

# How long to wait for a user answer before timing out the tool call.
# Aligned with chat.py's BG_TIMEOUT_S (30-min turn ceiling): a headless
# queued turn (Option B) may hit a question with nobody watching. Per the
# product decision it HANGS here until the user comes back to answer, capped
# at the same 30 min the whole turn is capped at — so the question can't
# outlive its turn. (Was 600s/10min when every turn had a live browser;
# headless execution needs the longer human-response window.)
ANSWER_TIMEOUT_S = 1800


def _maybe_push_needs_input(session_id: str) -> None:
    """Push 'Muse 需要你拍板' when a turn hits ask_user_question and the user
    isn't at any screen (headless queued turn, Option B). Presence-gated +
    best-effort + fire-and-forget — never blocks the awaiting handler. If the
    user IS active they'll see the question in-app, so no push.

    Imports are lazy + local to dodge any import cycle (chat → this module)
    and to keep this file importable in tests without the push stack."""
    async def _go():
        try:
            from . import presence as _presence
            if _presence.recently_active():
                return
            from . import push as _push
            from . import sessions as _sess
            sname = ""
            try:
                # list_sessions() on cache-miss reads + walks all SDK JSONL
                # synchronously — off-load so this needs-input push task can't
                # stall the event loop mid-turn. (perf: YELLOW —
                # ask_user_question list_sessions)
                _sessions = await asyncio.to_thread(_sess.list_sessions)
                for s in _sessions:
                    if s.get("id") == session_id:
                        sname = s.get("name", "")
                        break
            except Exception:
                pass
            await asyncio.to_thread(
                _push.send_to_all,
                title=sname or "muselab",
                body="Muse 需要你拍板",
                url=f"/?session={session_id}",
                tag=f"needs-input-{session_id}",
            )
        except Exception as e:
            import sys
            sys.stderr.write(f"[ask] needs-input push failed: {e}\n")
    try:
        asyncio.get_running_loop().create_task(_go())
    except RuntimeError:
        pass  # no running loop — nothing to do


def _normalize_questions(raw: list) -> list[dict]:
    """Coerce model output into the exact shape the frontend expects.

    Models are inconsistent: some send bare strings as options, some use
    `text`/`name`/`value` instead of `label`, some omit `multiSelect`, etc.
    The SDK schema (`{questions: list}`) is intentionally loose so the
    model is free to phrase questions naturally — we pay the price here
    by hand-normalizing rather than failing the tool call on a strict
    pydantic check (which would just retry with another loose shape).

    Output guarantees per question:
      - `question`: non-empty str
      - `header`: str (may be empty)
      - `multiSelect`: bool
      - `options`: list of {label: str, description: str}, length >= 1
    Questions with no usable options are dropped silently — better than
    rendering a dead question.
    """
    out: list[dict] = []
    for q in raw:
        if not isinstance(q, dict):
            # Bare-string question with no options is not something the UI
            # can render — skip rather than fake options.
            continue
        # The actual question text: try common synonyms before giving up.
        q_text = (q.get("question")
                   or q.get("text")
                   or q.get("prompt")
                   or q.get("title")
                   or "")
        q_text = str(q_text).strip()
        if not q_text:
            continue
        header = str(q.get("header") or "").strip()
        # multiSelect — accept both camelCase (per SDK docs) and snake_case
        # (models sometimes "correct" to Python style).
        multi = bool(q.get("multiSelect") or q.get("multi_select") or False)

        options_raw = q.get("options") or q.get("choices") or []
        options: list[dict] = []
        for opt in options_raw:
            preview = ""
            if isinstance(opt, str):
                label = opt.strip()
                desc = ""
            elif isinstance(opt, dict):
                label = str(opt.get("label")
                              or opt.get("text")
                              or opt.get("name")
                              or opt.get("value")
                              or "").strip()
                desc = str(opt.get("description")
                            or opt.get("desc")
                            or opt.get("detail")
                            or "").strip()
                # `preview` carries rich content (markdown / mockup / code
                # diff) the model wants to show when this option is focused.
                # SDK exposes it on the AskUserQuestion schema; the MCP
                # fallback path here just needs to forward it untouched so
                # the FE can render it as a side panel under the buttons.
                preview = str(opt.get("preview") or "").strip()
            else:
                continue
            if not label:
                continue
            option_entry: dict = {"label": label, "description": desc}
            if preview:
                option_entry["preview"] = preview
            options.append(option_entry)
        if not options:
            continue
        out.append({
            "question": q_text,
            "header": header,
            "multiSelect": multi,
            "options": options,
        })
    return out


def register_session_queue(session_id: str) -> asyncio.Queue:
    """Streaming endpoint calls this at start; returns the queue to merge into SSE."""
    q: asyncio.Queue = asyncio.Queue()
    _session_queues[session_id] = q
    return q


def unregister_session_queue(session_id: str) -> None:
    """Streaming endpoint calls this when the stream ends. Drops queue + cancels
    any still-pending question Futures for this session (so the tool handler
    raises and the model gets an error result instead of leaking memory)."""
    _session_queues.pop(session_id, None)
    for key in list(_pending.keys()):
        if key[0] == session_id:
            fut = _pending.pop(key, None)
            if fut is not None and not fut.done():
                fut.cancel()


def submit_answer(session_id: str, question_id: str, answers: dict[str, Any]) -> bool:
    """Called by POST /api/chat/answer/{sid}/{qid}. Returns False if no such
    pending question (already answered, timed out, or never existed)."""
    fut = _pending.get((session_id, question_id))
    if fut is None or fut.done():
        return False
    # Move the current turn back to running before resolving the Future. Once
    # resolved, the model can immediately finish or ask another question, so a
    # later resume write could overwrite a newer state.
    try:
        from .activity import activity
        activity.resume(session_id)
    except Exception:
        pass
    fut.set_result(answers)
    return True
