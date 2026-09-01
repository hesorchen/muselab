"""Canonical Subagent transcript shaping for history and live SDK messages.

This module deliberately has no dependency on ``backend.chat``.  The chat
runtime can therefore use the same stable block contract for SDK history and
forwarded live messages without importing the monolithic route module.

Subagent attachment is strict: only the SDK-provided ``parent_tool_use_id``
may attach a thread to an Agent/Task card.  Missing or conflicting metadata is
kept as an orphaned history thread and live messages without that field are
ignored; callers must never guess a nearby card.
"""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

from claude_agent_sdk import (
    AssistantMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    get_subagent_messages,
    list_subagents,
)

from .chat_presentation import (
    MAX_INPUT_FIELD_LEN,
    SLIM_INPUT_FIELDS,
    TOOL_RESULT_PREVIEW_CAP,
    TOOL_RESULT_TEXT_CAP,
    parse_bash_result,
    render_tool_result,
    render_tool_use,
    slim_input_value,
)


SubagentBlock = dict[str, Any]
SubagentThread = dict[str, Any]
SubagentEvent = dict[str, Any]


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def is_subagent_message(message: Any) -> bool:
    """Whether a live SDK message belongs to a forwarded Subagent sidechain.

    Callers should use this predicate to decide whether to bypass their parent
    message handlers, even when :meth:`SubagentStreamMux.feed` returns no event
    for a malformed/unsupported frame.  This prevents an incomplete sidechain
    frame from falling through into the parent Assistant bubble.
    """
    return (
        isinstance(message, (StreamEvent, AssistantMessage, UserMessage))
        and _nonempty_string(getattr(message, "parent_tool_use_id", None))
        is not None
    )


def _encoded_id_part(value: str) -> str:
    return quote(value, safe="")


def subagent_block_id(
    *,
    parent_tool_use_id: str | None,
    message_uuid: str,
    source_block_index: int,
    kind: str,
    agent_id: str | None = None,
) -> str:
    """Return the shared live/history identity for one Subagent block.

    ``agent_id`` is intentionally not part of attached-thread identity because
    forwarded SDK messages do not expose it.  It is used only to give an
    orphaned history block a deterministic, non-attachable namespace.
    """
    parent = _nonempty_string(parent_tool_use_id)
    if parent is None:
        parent = f"orphan:{_nonempty_string(agent_id) or 'unknown'}"
    return "subagent:{}:{}:{}:{}".format(
        _encoded_id_part(parent),
        _encoded_id_part(str(message_uuid)),
        int(source_block_index),
        _encoded_id_part(str(kind)),
    )


def _block_type(block: Any) -> str:
    if isinstance(block, TextBlock):
        return "text"
    if isinstance(block, ThinkingBlock):
        return "thinking"
    if isinstance(block, ToolUseBlock):
        return "tool_use"
    if isinstance(block, ToolResultBlock):
        return "tool_result"
    if isinstance(block, Mapping):
        return str(block.get("type") or "")
    return ""


def _block_value(block: Any, key: str, default: Any = None) -> Any:
    if isinstance(block, Mapping):
        return block.get(key, default)
    return getattr(block, key, default)


def _tool_use_view(block: Any) -> Any:
    raw_input = _block_value(block, "input", {})
    if not isinstance(raw_input, dict):
        raw_input = {}
    return SimpleNamespace(
        id=str(_block_value(block, "id", "") or ""),
        name=str(_block_value(block, "name", "") or ""),
        input=raw_input,
    )


def _tool_result_view(block: Any) -> Any:
    return SimpleNamespace(
        tool_use_id=str(_block_value(block, "tool_use_id", "") or ""),
        content=_block_value(block, "content"),
        is_error=bool(_block_value(block, "is_error", False)),
    )


def _base_block(
    *,
    session_id: str,
    parent_tool_use_id: str | None,
    parent_agent_id: str | None,
    agent_id: str | None,
    message_uuid: str,
    source_block_index: int,
    role: str,
) -> SubagentBlock:
    return {
        "session_id": session_id,
        "parent_tool_use_id": parent_tool_use_id,
        "parent_agent_id": parent_agent_id,
        "agent_id": agent_id,
        "message_uuid": message_uuid,
        "source_block_index": source_block_index,
        "block_id": subagent_block_id(
            parent_tool_use_id=parent_tool_use_id,
            message_uuid=message_uuid,
            source_block_index=source_block_index,
            kind=role,
            agent_id=agent_id,
        ),
        "role": role,
    }


def _normalize_content(
    *,
    session_id: str,
    parent_tool_use_id: str | None,
    parent_agent_id: str | None,
    agent_id: str | None,
    message_uuid: str,
    message_type: str,
    content: Any,
    tool_use_names: MutableMapping[str, str],
) -> list[SubagentBlock]:
    if not message_uuid:
        return []

    if isinstance(content, str):
        # The spawning prompt is already visible in the Agent card.  Only an
        # assistant string is a nested transcript block; user strings (which
        # also carry task-notification records) remain lifecycle metadata.
        if message_type != "assistant" or not content:
            return []
        return [{
            **_base_block(
                session_id=session_id,
                parent_tool_use_id=parent_tool_use_id,
                parent_agent_id=parent_agent_id,
                agent_id=agent_id,
                message_uuid=message_uuid,
                source_block_index=0,
                role="assistant",
            ),
            "text": content,
        }]

    if not isinstance(content, (list, tuple)):
        return []

    normalized: list[SubagentBlock] = []
    for index, raw_block in enumerate(content):
        block_type = _block_type(raw_block)
        if block_type == "text":
            if message_type != "assistant":
                continue
            text = str(_block_value(raw_block, "text", "") or "")
            if not text:
                continue
            normalized.append({
                **_base_block(
                    session_id=session_id,
                    parent_tool_use_id=parent_tool_use_id,
                    parent_agent_id=parent_agent_id,
                    agent_id=agent_id,
                    message_uuid=message_uuid,
                    source_block_index=index,
                    role="assistant",
                ),
                "text": text,
            })
        elif block_type == "thinking":
            if message_type != "assistant":
                continue
            text = str(_block_value(raw_block, "thinking", "") or "")
            if not text and _block_value(raw_block, "signature"):
                text = "[已加密推理 · 仅 streaming 期间可见明文]"
            if not text:
                continue
            normalized.append({
                **_base_block(
                    session_id=session_id,
                    parent_tool_use_id=parent_tool_use_id,
                    parent_agent_id=parent_agent_id,
                    agent_id=agent_id,
                    message_uuid=message_uuid,
                    source_block_index=index,
                    role="thinking",
                ),
                "text": text,
            })
        elif block_type == "tool_use":
            block = _tool_use_view(raw_block)
            if block.id:
                tool_use_names[block.id] = block.name
            rendered = render_tool_use(
                block,
                max_input_field_len=MAX_INPUT_FIELD_LEN,
                slim_input_fields=SLIM_INPUT_FIELDS,
                slim_value=lambda value: slim_input_value(
                    value, max_length=MAX_INPUT_FIELD_LEN),
            )
            normalized.append({
                **_base_block(
                    session_id=session_id,
                    parent_tool_use_id=parent_tool_use_id,
                    parent_agent_id=parent_agent_id,
                    agent_id=agent_id,
                    message_uuid=message_uuid,
                    source_block_index=index,
                    role="tool_use",
                ),
                **rendered,
            })
        elif block_type == "tool_result":
            block = _tool_result_view(raw_block)
            rendered = render_tool_result(
                block,
                tool_name=tool_use_names.get(block.tool_use_id, ""),
                preview_cap=TOOL_RESULT_PREVIEW_CAP,
                text_cap=TOOL_RESULT_TEXT_CAP,
                parse_bash=parse_bash_result,
            )
            normalized.append({
                **_base_block(
                    session_id=session_id,
                    parent_tool_use_id=parent_tool_use_id,
                    parent_agent_id=parent_agent_id,
                    agent_id=agent_id,
                    message_uuid=message_uuid,
                    source_block_index=index,
                    role="tool_result",
                ),
                **rendered,
            })
    return normalized


def normalize_subagent_message(
    message: Any,
    *,
    agent_id: str,
    tool_use_names: MutableMapping[str, str] | None = None,
) -> list[SubagentBlock]:
    """Normalize one SDK ``SessionMessage`` into canonical nested blocks."""
    raw_message = getattr(message, "message", None)
    if isinstance(raw_message, Mapping):
        content = raw_message.get("content")
    else:
        content = getattr(raw_message, "content", None)
    names = tool_use_names if tool_use_names is not None else {}
    return _normalize_content(
        session_id=str(getattr(message, "session_id", "") or ""),
        parent_tool_use_id=_nonempty_string(
            getattr(message, "parent_tool_use_id", None)),
        parent_agent_id=_nonempty_string(
            getattr(message, "parent_agent_id", None)),
        agent_id=_nonempty_string(agent_id),
        message_uuid=str(getattr(message, "uuid", "") or ""),
        message_type=str(getattr(message, "type", "") or ""),
        content=content,
        tool_use_names=names,
    )


def _thread_metadata(
    session_id: str,
    messages: list[Any],
) -> tuple[str | None, str | None, str | None]:
    if not messages:
        return None, None, "empty_transcript"
    if any(str(getattr(message, "session_id", "") or "") != session_id
           for message in messages):
        return None, None, "session_mismatch"

    parents = [
        _nonempty_string(getattr(message, "parent_tool_use_id", None))
        for message in messages
    ]
    parent_values = {value for value in parents if value is not None}
    if any(value is None for value in parents):
        return None, None, "missing_parent_metadata"
    if len(parent_values) != 1:
        return None, None, "conflicting_parent_metadata"

    parent_agents = [
        _nonempty_string(getattr(message, "parent_agent_id", None))
        for message in messages
    ]
    parent_agent_values = {
        value for value in parent_agents if value is not None
    }
    # ``None`` is valid for a root Subagent.  A mixture of a concrete parent
    # agent and no parent, or multiple concrete parents, is not safe topology.
    if (len(parent_agent_values) > 1
            or (parent_agent_values and any(value is None
                                             for value in parent_agents))):
        return None, None, "conflicting_parent_agent_metadata"
    parent_agent_id = next(iter(parent_agent_values), None)
    return next(iter(parent_values)), parent_agent_id, None


def load_subagent_threads(
    session_id: str,
    directory: str | None = None,
) -> list[SubagentThread]:
    """Load every SDK Subagent transcript as deterministic canonical threads.

    The caller owns any provider-specific ``CLAUDE_CONFIG_DIR`` context.  This
    function intentionally uses only the SDK's top-level list/get APIs and does
    not inspect transcript or metadata files itself.
    """
    threads: list[SubagentThread] = []
    for agent_id in sorted(set(list_subagents(session_id, directory=directory))):
        messages = list(get_subagent_messages(
            session_id,
            agent_id,
            directory=directory,
        ))
        parent_tool_use_id, parent_agent_id, orphan_reason = _thread_metadata(
            session_id, messages)
        orphaned = orphan_reason is not None
        tool_use_names: dict[str, str] = {}
        blocks: list[SubagentBlock] = []
        for message in messages:
            message_blocks = normalize_subagent_message(
                message,
                agent_id=agent_id,
                tool_use_names=tool_use_names,
            )
            if orphaned:
                # Keep diagnostic history visible to callers, but remove every
                # attachable identifier and rebuild keys in an orphan namespace.
                for block in message_blocks:
                    block["parent_tool_use_id"] = None
                    block["parent_agent_id"] = None
                    block["block_id"] = subagent_block_id(
                        parent_tool_use_id=None,
                        message_uuid=block["message_uuid"],
                        source_block_index=block["source_block_index"],
                        kind=block["role"],
                        agent_id=agent_id,
                    )
            blocks.extend(message_blocks)
        thread: SubagentThread = {
            "session_id": session_id,
            "agent_id": agent_id,
            "parent_tool_use_id": parent_tool_use_id,
            "parent_agent_id": parent_agent_id,
            "orphaned": orphaned,
            "message_count": len(messages),
            "blocks": blocks,
        }
        if orphan_reason is not None:
            thread["orphan_reason"] = orphan_reason
        threads.append(thread)
    return threads


def _utf16_length(value: str) -> int:
    """Return JavaScript ``String.length`` units for an SSE text offset."""
    return len(value.encode("utf-16-le")) // 2


class SubagentStreamMux:
    """Normalize forwarded live SDK sidechain messages without cross-wiring.

    ``feed`` returns publish-ready records with a string event name and a dict
    payload.  The caller remains responsible for JSON serialization and the
    surrounding turn broadcast.
    """

    def __init__(self, session_id: str):
        self.session_id = str(session_id)
        self._streamed: dict[str, str] = {}
        self._tool_use_names: dict[str, dict[str, str]] = {}
        self._final_blocks: dict[str, SubagentBlock] = {}

    def _identity(self, message: Any) -> tuple[str, str] | None:
        parent_tool_use_id = _nonempty_string(
            getattr(message, "parent_tool_use_id", None))
        if parent_tool_use_id is None:
            return None
        message_uuid = _nonempty_string(getattr(message, "uuid", None))
        if message_uuid is None:
            return None
        message_session_id = _nonempty_string(
            getattr(message, "session_id", None))
        if (message_session_id is not None
                and message_session_id != self.session_id):
            return None
        return parent_tool_use_id, message_uuid

    @staticmethod
    def _event(name: str, data: dict[str, Any]) -> SubagentEvent:
        return {"event": name, "data": data}

    def _feed_stream_event(
        self,
        message: StreamEvent,
        parent_tool_use_id: str,
        message_uuid: str,
    ) -> list[SubagentEvent]:
        event = message.event if isinstance(message.event, Mapping) else {}
        if event.get("type") != "content_block_delta":
            return []
        try:
            source_block_index = int(event["index"])
        except (KeyError, TypeError, ValueError):
            return []
        if source_block_index < 0:
            return []
        delta = event.get("delta")
        if not isinstance(delta, Mapping):
            return []
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            role = "assistant"
            chunk = str(delta.get("text") or "")
        elif delta_type == "thinking_delta":
            role = "thinking"
            chunk = str(delta.get("thinking") or "")
        else:
            return []
        if not chunk:
            return []
        block_id = subagent_block_id(
            parent_tool_use_id=parent_tool_use_id,
            message_uuid=message_uuid,
            source_block_index=source_block_index,
            kind=role,
        )
        if block_id in self._final_blocks:
            return []
        current = self._streamed.get(block_id, "")
        self._streamed[block_id] = current + chunk
        return [self._event("subagent_delta", {
            "session_id": self.session_id,
            "parent_tool_use_id": parent_tool_use_id,
            "parent_agent_id": None,
            "agent_id": None,
            "message_uuid": message_uuid,
            "source_block_index": source_block_index,
            "block_id": block_id,
            "kind": role,
            "offset": _utf16_length(current),
            "delta": chunk,
            "replace": False,
        })]

    def _feed_complete(
        self,
        message: AssistantMessage | UserMessage,
        parent_tool_use_id: str,
        message_uuid: str,
    ) -> list[SubagentEvent]:
        message_type = "assistant" if isinstance(message, AssistantMessage) else "user"
        names = self._tool_use_names.setdefault(parent_tool_use_id, {})
        blocks = _normalize_content(
            session_id=self.session_id,
            parent_tool_use_id=parent_tool_use_id,
            parent_agent_id=None,
            agent_id=None,
            message_uuid=message_uuid,
            message_type=message_type,
            content=getattr(message, "content", None),
            tool_use_names=names,
        )
        events: list[SubagentEvent] = []
        for block in blocks:
            block_id = str(block["block_id"])
            self._streamed.pop(block_id, None)
            if self._final_blocks.get(block_id) == block:
                continue
            self._final_blocks[block_id] = block
            events.append(self._event("subagent_block", {
                "session_id": self.session_id,
                "parent_tool_use_id": parent_tool_use_id,
                "parent_agent_id": None,
                "agent_id": None,
                "message_uuid": message_uuid,
                "source_block_index": block["source_block_index"],
                "block_id": block_id,
                "kind": block["role"],
                "replace": True,
                "block": block,
            }))
        return events

    def feed(self, message: Any) -> list[SubagentEvent]:
        """Return canonical Subagent events, or ``[]`` for unbound messages."""
        identity = self._identity(message)
        if identity is None:
            return []
        parent_tool_use_id, message_uuid = identity
        if isinstance(message, StreamEvent):
            return self._feed_stream_event(
                message, parent_tool_use_id, message_uuid)
        if isinstance(message, (AssistantMessage, UserMessage)):
            return self._feed_complete(
                message, parent_tool_use_id, message_uuid)
        return []
