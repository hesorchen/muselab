"""
ask_user_question — muselab's analogue of Claude Code's AskUserQuestion.

Flow:
  1. Model calls `mcp__muselab__ask_user_question` with a list of questions
  2. Our in-process MCP handler:
     a. Generates a question_id, creates an asyncio.Future
     b. Pushes the question to the session's SSE event queue (forwarded to UI)
     c. await future (with 10-min timeout)
  3. User clicks a button in the UI
  4. Frontend POSTs to /api/chat/answer/{session_id}/{question_id}
  5. submit_answer() resolves the Future
  6. Handler returns the user's choice as a tool result
  7. Model continues, sees the answer in tool_result block

The handler captures session_id via closure — each ClaudeSDKClient instance
gets its own handler bound to its session. This avoids ContextVar propagation
issues across SDK-managed tasks.
"""
import asyncio
import json
import uuid
from typing import Any
from claude_agent_sdk import tool, create_sdk_mcp_server

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
    fut.set_result(answers)
    return True


def build_server_for_session(session_id: str):
    """Build an SDK MCP server whose `ask_user_question` tool is bound to this
    session via closure. One server per ClaudeSDKClient instance — cheap; just
    a few Python objects."""

    @tool(
        "ask_user_question",
        (
            "Ask the user one or more structured questions with predefined options. "
            "Use this when you need quick disambiguation or a decision from the user "
            "(2-4 mutually exclusive choices) instead of long free-text reply. The UI "
            "renders each option as a clickable button. Returns the user's chosen "
            "label(s) as text. "
            "Input format: {questions: [{question, header, multiSelect, options: "
            "[{label, description}, ...]}, ...]} where header is a <12-char chip tag, "
            "and each option has a 1-5 word label + short description."
        ),
        # Schema is DELIBERATELY loose ({"questions": list}) rather than a
        # strict JSON Schema. Models frequently emit malformed shapes here —
        # `options: ["yes","no"]` (bare strings), `[{text: ...}]` (wrong key),
        # or a missing `multiSelect`. A strict schema would make the SDK
        # harness REJECT those tool calls outright, leaving the user staring
        # at the question text with no buttons and no error. Instead we accept
        # anything list-shaped and repair it in `_normalize_questions` below,
        # so a slightly-wrong tool call still renders. Tightening only the
        # top-level (questions must be a non-empty list) would be safe, but
        # the SDK's `{key: type}` mini-schema can't express "non-empty" — the
        # handler already returns a clean is_error for empty/unusable input,
        # which covers it. See 2026-05-21 frontend feedback + audit E/250.
        {"questions": list},
    )
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        raw_questions = args.get("questions") or []
        if not raw_questions:
            return {
                "content": [{"type": "text", "text": "Error: no questions provided"}],
                "is_error": True,
            }
        # Normalize every question into the exact shape the frontend's Alpine
        # template expects:
        #   {question: str, header: str, multiSelect: bool,
        #    options: [{label: str, description: str}, ...]}
        # Without this, models that hand us `options: ["yes", "no"]` (bare
        # strings), or `options: [{text: "yes"}]` (wrong key), or skip
        # `multiSelect` entirely, render as a question with NO clickable
        # buttons — the user sees the question text and dead air. See
        # 2026-05-21 frontend feedback.
        questions = _normalize_questions(raw_questions)
        if not questions:
            return {
                "content": [{"type": "text",
                              "text": "Error: questions payload had no usable options"}],
                "is_error": True,
            }

        question_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        _pending[(session_id, question_id)] = fut

        # Push to SSE so the UI can render.
        q = _session_queues.get(session_id)
        if q is None:
            # Stream not subscribed — shouldn't happen in normal flow, but be safe.
            _pending.pop((session_id, question_id), None)
            return {
                "content": [{"type": "text", "text": "Error: no active UI session"}],
                "is_error": True,
            }

        await q.put({
            "event": "ask_user_question",
            "data": json.dumps({"id": question_id, "questions": questions},
                                ensure_ascii=False),
        })
        # Headless-turn nudge: if nobody's at a screen, push so the user knows
        # Muse is blocked on their decision. The question already sits in the
        # session's broadcast buffer, so opening the session later replays it
        # and they can answer — as long as we're still within the timeout
        # window below. (Browser present → no push; they see it in-app.)
        _maybe_push_needs_input(session_id)

        try:
            answers = await asyncio.wait_for(fut, timeout=ANSWER_TIMEOUT_S)
        except asyncio.TimeoutError:
            return {
                "content": [{"type": "text",
                              "text": "User did not respond within 30 minutes."}],
                "is_error": True,
            }
        except asyncio.CancelledError:
            return {
                "content": [{"type": "text",
                              "text": "User session ended before answering."}],
                "is_error": True,
            }
        finally:
            _pending.pop((session_id, question_id), None)

        # `answers` shape: {question_text: chosen_label_or_list, ...}
        # Format as readable text the model can act on.
        lines = []
        for q_text, ans in answers.items():
            if isinstance(ans, list):
                ans_text = " + ".join(ans) if ans else "(none)"
            else:
                ans_text = str(ans)
            lines.append(f"Q: {q_text}\nA: {ans_text}")
        return {"content": [{"type": "text", "text": "\n\n".join(lines)}]}

    return create_sdk_mcp_server(
        name="muselab",
        version="1.0.0",
        tools=[handler],
    )
