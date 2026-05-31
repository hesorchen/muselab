"""
permission_request — bridge SDK's can_use_tool callback to a UI prompt.

Two responsibilities now share one callback (SDK 0.2.82 routes both through
`can_use_tool`):

1. **Tool approval** (when permission_mode != bypassPermissions):
   surface a permission card, await Allow / Deny / Always.

2. **AskUserQuestion** (always, regardless of permission_mode): SDK's
   native multiple-choice tool. Detected by `tool_name == "AskUserQuestion"`.
   We push a question to the SSE channel reusing `ask_user_question`'s
   queue/Future registry (same UI rendering, same submit endpoint), then
   return `PermissionResultAllow(updated_input={questions, answers})` per
   the SDK contract.

"Always allow" works at the muselab session level (in-memory): subsequent calls
to the same (tool, key) pair bypass the prompt for the rest of this session.
"""
import asyncio
import json
import uuid
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from . import ask_user_question as auq  # share its _pending + _session_queues

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

# Bash binaries whose flags/subcommands radically change blast radius. For
# these we must NOT collapse the always-allow cache to the first word — e.g.
# approving `git status` would otherwise silently green-light `git push
# --force` / `git reset --hard`. The full command string is used as the cache
# key instead, so each distinct invocation re-prompts. (2026-05-29 audit:
# privilege-escalation via first-word caching.)
_DANGEROUS_BASH_BINS = frozenset({
    "git", "rm", "rmdir", "mv", "cp", "dd", "curl", "wget", "ssh", "scp",
    "rsync", "chmod", "chown", "sudo", "kill", "pkill", "killall",
    "bash", "sh", "zsh", "eval", "python", "python3", "node", "npm",
    "npx", "pip", "pip3", "uv", "docker", "systemctl", "mkfs",
})


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
        if not cmd:
            return ""
        bin0 = cmd.split()[0]
        # Strip a leading path so /usr/bin/git is matched as "git".
        bin_name = bin0.rsplit("/", 1)[-1]
        # Dangerous binaries: key by the FULL command so always-allow can't
        # escalate from a benign subcommand to a destructive one. Also key by
        # full command whenever the line contains shell metacharacters that
        # could chain a second command past the first word.
        if bin_name in _DANGEROUS_BASH_BINS or any(
                c in cmd for c in (";", "&&", "||", "|", "`", "$(", ">", "<")):
            return cmd
        # Safe binaries: first word — so "ls -la X" and "ls Y" share a grant.
        return bin0
    if tool_name in ("Read", "Edit", "Write", "NotebookEdit"):
        return str(tool_input.get("file_path") or "")
    if tool_name in ("Glob", "Grep"):
        return str(tool_input.get("pattern") or "")
    if tool_name in ("WebFetch", "WebSearch"):
        return str(tool_input.get("url") or tool_input.get("query") or "")
    return ""


async def _handle_ask_user_question(
        session_id: str, tool_input: dict[str, Any]
) -> PermissionResultAllow | PermissionResultDeny:
    """SDK calls can_use_tool with tool_name='AskUserQuestion' when the model
    invokes the trained-in multiple-choice tool. We forward the questions to
    muselab's existing ask UI (same SSE event + Future registry as the MCP
    fallback), then return PermissionResultAllow(updated_input=...) per SDK
    contract.

    IMPORTANT: must return PermissionResultAllow / PermissionResultDeny class
    instances, NOT plain dicts. Older code returned `{behavior, ...}` dicts
    that worked with a previous SDK shape; current SDK raises
    "Tool permission callback must return PermissionResult" if you hand it a
    dict. Discovered the hard way 2026-05-23 when my first attempt at this
    fix made every AskUserQuestion call crash on the SDK side.

    Questions go through `auq._normalize_questions` first so the frontend
    always sees the canonical `{question, header, multiSelect, options:
    [{label, description}]}` shape — same as the MCP fallback. Without
    normalization, a model that emits options as bare strings (`["yes",
    "no"]`) or with `text`/`name`/`value` keys would render a question
    card with no clickable buttons (silent "user can't pick" symptom).
    """
    raw_questions = tool_input.get("questions") or []
    if not raw_questions:
        return PermissionResultDeny(
            message="AskUserQuestion called with empty questions list.")
    questions = auq._normalize_questions(raw_questions)
    if not questions:
        return PermissionResultDeny(
            message="AskUserQuestion: no usable options after normalization.")

    q = auq._session_queues.get(session_id)
    if q is None:
        return PermissionResultDeny(
            message="No active UI session; cannot prompt for question.")

    question_id = uuid.uuid4().hex[:12]
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    auq._pending[(session_id, question_id)] = fut

    await q.put({
        "event": "ask_user_question",
        "data": json.dumps({"id": question_id, "questions": questions},
                            ensure_ascii=False),
    })
    # FIX ⑨: the MCP-alias path (ask_user_question.py) already pushes a
    # "Muse 需要你拍板" notification, but the SDK's built-in AskUserQuestion
    # routes through HERE and previously pushed nothing — so a headless queued
    # turn that hit the built-in tool left the user with no signal. Presence-
    # gated + fire-and-forget inside.
    auq._maybe_push_needs_input(session_id)

    try:
        answers = await asyncio.wait_for(fut, timeout=auq.ANSWER_TIMEOUT_S)
    except asyncio.TimeoutError:
        return PermissionResultDeny(
            message="User did not respond within 10 minutes.")
    except asyncio.CancelledError:
        return PermissionResultDeny(
            message="User session ended before answering.")
    finally:
        auq._pending.pop((session_id, question_id), None)

    # answers shape: {question_text: chosen_label_or_list}
    # SDK requires both `questions` and `answers` in updated_input.
    return PermissionResultAllow(
        updated_input={"questions": questions, "answers": answers})


def build_callback_for_session(session_id: str,
                                bypass_state: dict | None = None):
    """Return an async callable matching the SDK's can_use_tool signature.

    Wired UNCONDITIONALLY now (2026-05-23) — not just when permission_mode
    requires per-tool prompts. Reason: the SDK routes the built-in
    `AskUserQuestion` tool calls through `can_use_tool`, so without a
    callback the model's questions silently vanish on bypassPermissions
    (the default). The MCP fallback `mcp__muselab__ask_user_question` is
    great when the model remembers to use that name, but models often
    forget and call the shorter built-in instead.

    `bypass_state` is a MUTABLE dict `{"bypass": bool}` owned by the caller
    (chat.get_client). The callback reads `bypass_state["bypass"]` at call
    time — NOT baked in as a constant — so that switching permission mode on
    a cached/pooled client (via set_permission_mode) takes effect without
    rebuilding the closure. When `bypass` is True (permission_mode ==
    bypassPermissions) every non-AskUserQuestion tool is auto-allowed
    without showing a permission card; when False, full per-tool prompting.
    AskUserQuestion is always forwarded to the UI regardless — it's
    interactive by design. (2026-05-29: previously this captured a constant
    `bypass`, so switching from bypassPermissions to default left the cached
    closure unconditionally allowing every tool — permission cards silently
    failed.)"""
    if bypass_state is None:
        bypass_state = {"bypass": False}

    async def can_use_tool(
            tool_name: str, tool_input: dict[str, Any], context: Any
    ) -> PermissionResultAllow | PermissionResultDeny:
        # AskUserQuestion is interactive — always route to the muselab UI
        # regardless of permission_mode. Without this, models that call
        # the SDK's built-in `AskUserQuestion` (rather than the MCP
        # alias) get no UI prompt on bypassPermissions and the user
        # sees no options to pick. See _handle_ask_user_question above
        # for the SDK-contract details.
        if tool_name == "AskUserQuestion":
            return await _handle_ask_user_question(session_id, tool_input)

        # Bypass: auto-allow everything else without prompting. This
        # keeps the "no permission cards" UX promise of bypassPermissions
        # while still letting AskUserQuestion above route through the UI.
        # Read the flag LIVE (not a captured constant) so a mode switch on
        # the pooled client takes effect immediately.
        if bypass_state.get("bypass"):
            return PermissionResultAllow(updated_input=tool_input)

        # Always-allow cache check. Empty set is falsy, so don't use `or`.
        key = _input_key(tool_name, tool_input)
        cache = _always_allow.setdefault(session_id, set())
        if (tool_name, key) in cache:
            return PermissionResultAllow(updated_input=tool_input)

        q = _session_queues.get(session_id)
        if q is None:
            # No UI subscribed — fail closed (deny) so the model gets a clear
            # signal instead of hanging.
            return PermissionResultDeny(
                message="No active UI session; cannot prompt for permission.")

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
        # FIX ⑨: a tool-permission prompt is just as blocking as a question.
        # Push the same presence-gated "needs你拍板" notification so a headless
        # queued turn that stops on a permission card reaches the user even
        # when no screen is open.
        auq._maybe_push_needs_input(session_id)

        try:
            result = await asyncio.wait_for(fut, timeout=DECISION_TIMEOUT_S)
        except asyncio.TimeoutError:
            return PermissionResultDeny(
                message="User did not respond within 10 minutes.")
        except asyncio.CancelledError:
            return PermissionResultDeny(
                message="User session ended before answering.")
        finally:
            _pending.pop((session_id, request_id), None)

        decision = result["decision"]
        if decision == "always":
            _always_allow.setdefault(session_id, set()).add((tool_name, key))
            return PermissionResultAllow(updated_input=tool_input)
        if decision == "allow":
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(
            message=result.get("message") or "User denied the request.")

    return can_use_tool
