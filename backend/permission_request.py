"""
permission_request — bridge SDK's can_use_tool callback to a UI prompt.

The callback surfaces SDK tool approvals (when permission_mode is not
bypassPermissions), awaits Allow / Deny / Always, and returns the SDK-native
PermissionResult shape.

It also provides the PreToolUse adapter that bridges SDK-native
AskUserQuestion into MuseLab's browser UI in every permission mode. A hook is
required because can_use_tool can be bypassed by allow rules and live mode
transitions.

"Always allow" works at the muselab session level (in-memory): subsequent calls
to the same (tool, key) pair bypass the prompt for the rest of this session.
"""
import asyncio
import json
import uuid
from typing import Any, get_args

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import PermissionMode, PermissionUpdate

from . import ask_user_question as auq  # share its _pending + _session_queues

# (session_id, request_id) -> Future of {"decision": "allow"|"deny"|"always",
#                                          "message": str|None}
_pending: dict[tuple[str, str], asyncio.Future] = {}

# ExitPlanMode requests are stricter than ordinary permission prompts.  The
# value maps each UI request to the exact SDK-provided modes the user may pick;
# submit_decision() rejects anything else before waking the SDK callback.
_pending_plan_modes: dict[
    tuple[str, str], dict[str, PermissionUpdate]
] = {}
# Safe compatibility target for a cached pre-Plan-Mode frontend that submits
# generic Allow without a `mode`. It is usable only when the same mode is also
# present in that request's sanitized SDK suggestions.
_pending_plan_return_modes: dict[tuple[str, str], str] = {}

# An ExitPlanMode allow response changes the live CLI's permission mode before
# MuseLab can durably update its own session metadata.  Keep that change
# pending, keyed by the SDK's stable tool_use_id, until a matching PostToolUse
# hook confirms the tool completed successfully.
_plan_transitions: dict[tuple[str, str], PermissionUpdate] = {}

# session_id -> queue (re-uses ask_user_question's _session_queues at runtime
# via the shared registry below).
_session_queues: dict[str, asyncio.Queue] = {}

# Per-session "always allow" cache: {sid: set[(tool_name, key)]}
# key derives from tool input — e.g. for Bash: the command; for Edit: file_path.
_always_allow: dict[str, set[tuple[str, str]]] = {}

DECISION_TIMEOUT_S = 600
_VALID_PERMISSION_MODES = frozenset(get_args(PermissionMode))

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


def _queue_resolution(
    session_id: str,
    request_id: str,
    *,
    kind: str,
    decision: str,
    mode: str | None = None,
    reason: str | None = None,
) -> bool:
    """Queue an authoritative permission-card outcome for every subscriber."""
    q = _session_queues.get(session_id)
    if q is None:
        return False
    payload: dict[str, Any] = {
        "id": request_id,
        "kind": kind,
        "decision": decision,
        "mode": mode,
    }
    if reason:
        payload["reason"] = reason
    q.put_nowait({
        "event": "permission_request_resolved",
        "data": json.dumps(payload, ensure_ascii=False),
    })
    return True


def register_session_queue(session_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _session_queues[session_id] = q
    _always_allow.setdefault(session_id, set())
    return q


def unregister_session_queue(session_id: str) -> bool:
    """Tear down one turn queue; report an ambiguous live mode transition.

    A True return means can_use_tool already returned updatedPermissions but no
    matching PostToolUse/Failure hook consumed the transition before the stream
    ended. The caller must discard the pooled runtime at the turn boundary.
    """
    _session_queues.pop(session_id, None)
    for key in list(_pending.keys()):
        if key[0] == session_id:
            fut = _pending.pop(key, None)
            _pending_plan_modes.pop(key, None)
            _pending_plan_return_modes.pop(key, None)
            if fut is not None and not fut.done():
                fut.cancel()
    transition_keys = [
        key for key in _plan_transitions if key[0] == session_id
    ]
    for key in transition_keys:
        _plan_transitions.pop(key, None)
    return bool(transition_keys)


def clear_session_permissions(session_id: str) -> None:
    """Forget session-scoped grants when the session itself is deleted.

    Queue registration is turn-scoped, while an "always" decision is
    session-scoped.  Keeping these lifetimes separate makes the UI promise
    ("always allow for this session") true across multiple turns.
    """
    _always_allow.pop(session_id, None)
    for key in [key for key in _plan_transitions if key[0] == session_id]:
        _plan_transitions.pop(key, None)


def submit_decision(session_id: str, request_id: str, decision: str,
                     message: str | None = None,
                     mode: str | None = None) -> bool:
    """Frontend POSTs here. decision in {allow, deny, always}."""
    if decision not in ("allow", "deny", "always"):
        return False
    key = (session_id, request_id)
    fut = _pending.get(key)
    if fut is None or fut.done():
        return False
    plan_modes = _pending_plan_modes.get(key)
    if plan_modes is not None:
        # A plan approval is a one-shot transition, never an "always allow"
        # grant.  The selected mode must be one of the exact sanitized SDK
        # suggestions attached to this request.
        if decision not in ("allow", "deny"):
            return False
        if decision == "allow":
            if mode is None:
                fallback = _pending_plan_return_modes.get(key)
                mode = fallback if fallback in plan_modes else None
            if mode not in plan_modes:
                return False
    # Resume before waking the model; after set_result it may finish or produce
    # another permission prompt before this call stack gets control again.
    try:
        from .activity import activity
        activity.resume(session_id)
    except Exception:
        pass
    result = {"decision": decision, "message": message}
    if plan_modes is not None:
        result["mode"] = mode
    # The permission card is part of a fan-out TurnBroadcast: another tab may
    # have replayed the same pending request. Publish the accepted decision
    # before waking the SDK callback so every subscriber converges and the
    # event is ordered before any later ExitPlanMode success/failure hook.
    _queue_resolution(
        session_id,
        request_id,
        kind="exit_plan" if plan_modes is not None else "tool",
        decision=decision,
        mode=mode if plan_modes is not None else None,
    )
    fut.set_result(result)
    return True


def consume_plan_transition(
    session_id: str, tool_use_id: str
) -> PermissionUpdate | None:
    """Pop the permission change confirmed by a matching PostToolUse hook."""
    return _plan_transitions.pop((session_id, tool_use_id), None)


def discard_plan_transition(
    session_id: str, tool_use_id: str
) -> PermissionUpdate | None:
    """Forget an uncommitted plan change after failure, cancellation, or EOF."""
    return _plan_transitions.pop((session_id, tool_use_id), None)


async def emit_session_event(session_id: str, event: str, data: Any) -> bool:
    """Emit one JSON-encoded side-channel event to the active session stream."""
    q = _session_queues.get(session_id)
    if q is None:
        return False
    await q.put({
        "event": event,
        "data": json.dumps(data, ensure_ascii=False),
    })
    # Queue.put() on an unbounded queue need not yield. Give the active side
    # pump one scheduling turn so a following SDK Result/done cannot overtake
    # this mode-commit event.
    await asyncio.sleep(0)
    return True


def _plan_return_mode(
    session_id: str,
    runtime_plan_return_permission: str | None = None,
) -> str:
    """Return the durable post-plan mode, defaulting legacy sessions safely."""
    if runtime_plan_return_permission is not None:
        mode = str(runtime_plan_return_permission or "default")
        if mode == "plan" or mode not in _VALID_PERMISSION_MODES:
            return "default"
        return mode
    try:
        from . import sessions as sess
        session = sess.get_session(session_id) or {}
        mode = str(session.get("plan_return_permission") or "default")
    except Exception:
        mode = "default"
    if mode == "plan" or mode not in _VALID_PERMISSION_MODES:
        return "default"
    return mode


def _plan_mode_suggestions(
    session_id: str,
    context: Any,
    runtime_plan_return_permission: str | None = None,
) -> tuple[list[PermissionUpdate], str]:
    """Sanitize ExitPlanMode suggestions without widening the SDK's choices."""
    return_mode = _plan_return_mode(
        session_id, runtime_plan_return_permission)
    raw = getattr(context, "suggestions", None) or []
    if not raw:
        return (
            [PermissionUpdate(
                type="setMode",
                mode=return_mode,
                destination="session",
            )],
            return_mode,
        )

    suggestions: list[PermissionUpdate] = []
    seen_modes: set[str] = set()
    for item in raw:
        if not isinstance(item, PermissionUpdate):
            continue
        mode = item.mode
        if (
            item.type != "setMode"
            or item.destination != "session"
            or mode == "plan"
            or mode not in _VALID_PERMISSION_MODES
            or mode in seen_modes
        ):
            continue
        seen_modes.add(mode)
        # Copy only the three fields this flow understands.  Do not echo
        # unrelated rule/directory payloads back into the CLI.
        suggestions.append(PermissionUpdate(
            type="setMode",
            mode=mode,
            destination="session",
        ))
    return suggestions, return_mode


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


def _native_answer_payload(answers: dict[str, Any]) -> dict[str, str]:
    """Convert browser answer values to AskUserQuestion's native string map."""
    out: dict[str, str] = {}
    for question, answer in answers.items():
        if isinstance(answer, list):
            out[str(question)] = ", ".join(str(item) for item in answer)
        else:
            out[str(question)] = str(answer)
    return out


async def _handle_ask_user_question(
        session_id: str, tool_input: dict[str, Any]
) -> PermissionResultAllow | PermissionResultDeny:
    """Collect a native AskUserQuestion answer through MuseLab's browser UI."""
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
    auq._maybe_push_needs_input(session_id)

    try:
        answers = await asyncio.wait_for(fut, timeout=auq.ANSWER_TIMEOUT_S)
    except asyncio.TimeoutError:
        return PermissionResultDeny(
            message="User did not respond within 30 minutes.")
    except asyncio.CancelledError:
        return PermissionResultDeny(
            message="User session ended before answering.")
    finally:
        auq._pending.pop((session_id, question_id), None)

    return PermissionResultAllow(updated_input={
        "questions": questions,
        "answers": _native_answer_payload(answers),
    })


def build_ask_user_question_hook_for_session(session_id: str):
    """Route native AskUserQuestion through the browser in bypass mode."""
    async def hook(input_data, _tool_use_id, _context):
        data = input_data if isinstance(input_data, dict) else {}
        tool_input = data.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        result = await _handle_ask_user_question(session_id, tool_input)
        if result.behavior == "allow":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": result.updated_input or tool_input,
                }
            }
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": result.message,
            }
        }

    return hook


async def _handle_exit_plan_mode(
    session_id: str,
    tool_input: dict[str, Any],
    context: Any,
    runtime_plan_return_permission: str | None = None,
) -> PermissionResultAllow | PermissionResultDeny:
    """Present plan approval and stage the selected runtime mode for commit."""
    q = _session_queues.get(session_id)
    if q is None:
        return PermissionResultDeny(
            message="No active UI session; cannot approve the plan.")

    tool_use_id = str(getattr(context, "tool_use_id", None) or "")
    if not tool_use_id:
        # A mode switch without a correlation ID cannot be safely committed or
        # rolled back when PostToolUse/PostToolUseFailure arrives.
        return PermissionResultDeny(
            message="ExitPlanMode request is missing tool_use_id.")

    suggestions, return_mode = _plan_mode_suggestions(
        session_id, context, runtime_plan_return_permission)
    if not suggestions:
        return PermissionResultDeny(
            message="ExitPlanMode supplied no safe session mode transition.")

    request_id = uuid.uuid4().hex[:12]
    key = (session_id, request_id)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    modes = {
        suggestion.mode: suggestion
        for suggestion in suggestions
        if suggestion.mode is not None
    }
    _pending[key] = fut
    _pending_plan_modes[key] = modes
    _pending_plan_return_modes[key] = return_mode

    payload = {
        "id": request_id,
        "kind": "exit_plan",
        "tool": "ExitPlanMode",
        "tool_use_id": tool_use_id,
        "suggestions": [suggestion.to_dict() for suggestion in suggestions],
        "return_mode": return_mode,
        "title": getattr(context, "title", None),
        "display_name": getattr(context, "display_name", None),
        "description": getattr(context, "description", None),
        "input": tool_input,
    }

    try:
        await q.put({
            "event": "permission_request",
            "data": json.dumps(payload, ensure_ascii=False),
        })
        auq._maybe_push_needs_input(session_id)
        result = await asyncio.wait_for(fut, timeout=DECISION_TIMEOUT_S)
    except asyncio.TimeoutError:
        _queue_resolution(
            session_id,
            request_id,
            kind="exit_plan",
            decision="expired",
            reason="timeout",
        )
        return PermissionResultDeny(
            message="User did not respond within 10 minutes.")
    except asyncio.CancelledError:
        _queue_resolution(
            session_id,
            request_id,
            kind="exit_plan",
            decision="expired",
            reason="cancelled",
        )
        return PermissionResultDeny(
            message="User session ended before answering.")
    finally:
        _pending.pop(key, None)
        _pending_plan_modes.pop(key, None)
        _pending_plan_return_modes.pop(key, None)

    if result["decision"] != "allow":
        return PermissionResultDeny(
            message=result.get("message") or "User rejected the plan.")

    selected = modes.get(result.get("mode"))
    if selected is None:
        # submit_decision() enforces this before resolving the Future; retain a
        # defensive check in case another in-process caller resolves it.
        return PermissionResultDeny(
            message="Selected plan return mode is no longer available.")

    _plan_transitions[(session_id, tool_use_id)] = selected
    return PermissionResultAllow(
        updated_input=tool_input,
        updated_permissions=[selected],
    )


def build_callback_for_session(
    session_id: str,
    *,
    plan_return_permission: str | None = None,
):
    """Return an async callable matching the SDK's can_use_tool signature.

    The callback is installed for every ordinary workspace runtime, including
    bypass. The SDK still does not invoke it for calls already approved by
    bypass, acceptEdits, allow rules, or whole-tool Skill grants; keeping it
    attached lets a native EnterPlanMode transition use the same stdio control
    bridge for ExitPlanMode. It remains a prompt resolver, not a universal tool
    gate. Use a PreToolUse hook when an operation must observe every tool call."""

    async def can_use_tool(
            tool_name: str, tool_input: dict[str, Any], context: Any
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name == "ExitPlanMode":
            return await _handle_exit_plan_mode(
                session_id,
                tool_input,
                context,
                runtime_plan_return_permission=plan_return_permission,
            )

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
                "kind": "tool",
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
            _queue_resolution(
                session_id,
                request_id,
                kind="tool",
                decision="expired",
                reason="timeout",
            )
            return PermissionResultDeny(
                message="User did not respond within 10 minutes.")
        except asyncio.CancelledError:
            _queue_resolution(
                session_id,
                request_id,
                kind="tool",
                decision="expired",
                reason="cancelled",
            )
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
