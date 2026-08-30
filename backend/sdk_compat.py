"""Narrow compatibility shims around the pinned Claude Agent SDK.

The bundled CLI already understands UUID-stamped asynchronous user messages
and emits ``command_lifecycle`` frames for them, but the Python SDK currently
drops those frames as unknown.  It also discards the useful receipt returned
by ``interrupt`` and does not expose ``cancel_async_message``.  The base
:class:`MuseLabSDKClient` fills only those small protocol gaps while the SDK is
pinned to a version whose wire contract we have tested.

The Claude Agent SDK treats ``signature`` as a required field on every
assistant ``thinking`` block.  Some third-party endpoints emit the thinking
text but omit that field entirely, which makes the SDK raise
``MessageParseError`` before callers can receive the remaining assistant and
result messages.

MuseLab only uses :class:`UnsignedThinkingCompatibleClient` for third-party
providers.  It supplies an empty in-memory signature for parsing, without
mutating the raw SDK frame or pretending that the vendor produced a valid
cryptographic signature.  ``backend.jsonl_cleanup`` removes the unverifiable
block from the persisted transcript after the turn completes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, cast

from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk._errors import CLIConnectionError, MessageParseError
from claude_agent_sdk.types import Message


logger = logging.getLogger(__name__)


CommandLifecycleState = Literal[
    "queued",
    "started",
    "completed",
    "cancelled",
    "discarded",
    "refused",
]
_COMMAND_LIFECYCLE_STATES = frozenset(
    {"queued", "started", "completed", "cancelled", "discarded", "refused"}
)


@dataclass(frozen=True, slots=True)
class CommandLifecycleMessage:
    """Delivery state for one UUID-stamped CLI command.

    ``command_uuid`` is the UUID MuseLab supplied on the input frame.  ``uuid``
    is the distinct UUID of this lifecycle event itself.
    """

    command_uuid: str
    state: CommandLifecycleState
    session_id: str
    uuid: str


@dataclass(frozen=True, slots=True)
class InterruptReceipt:
    """The queue snapshot returned synchronously by a CLI interrupt.

    ``None`` means that the running CLI did not advertise/return that receipt
    field; an empty tuple means it returned the field with no matching UUIDs.
    Keeping that distinction prevents an older CLI from looking like a
    confirmed empty queue.
    """

    still_queued: tuple[str, ...] | None
    cancelled: tuple[str, ...] | None


def parse_command_lifecycle(
    data: dict[str, Any],
) -> CommandLifecycleMessage | None:
    """Parse a CLI ``command_lifecycle`` frame, or return ``None`` otherwise."""
    if data.get("type") != "command_lifecycle":
        return None

    command_uuid = data.get("command_uuid")
    state = data.get("state")
    session_id = data.get("session_id")
    event_uuid = data.get("uuid")
    if not isinstance(command_uuid, str) or not command_uuid:
        raise MessageParseError(
            "Invalid command_lifecycle command_uuid", data)
    if state not in _COMMAND_LIFECYCLE_STATES:
        raise MessageParseError(
            f"Invalid command_lifecycle state: {state!r}", data)
    if not isinstance(session_id, str):
        raise MessageParseError(
            "Invalid command_lifecycle session_id", data)
    if not isinstance(event_uuid, str) or not event_uuid:
        raise MessageParseError(
            "Invalid command_lifecycle uuid", data)
    return CommandLifecycleMessage(
        command_uuid=command_uuid,
        state=cast(CommandLifecycleState, state),
        session_id=session_id,
        uuid=event_uuid,
    )


def _string_tuple_field(
    response: dict[str, Any], field: str,
) -> tuple[str, ...] | None:
    value = response.get(field)
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) for item in value)
    ):
        raise MessageParseError(
            f"Invalid interrupt receipt field: {field}", response)
    return tuple(value)


def normalize_missing_thinking_signatures(
    data: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Return a parser-safe copy of one SDK frame and the number of fixes.

    Only assistant thinking blocks where the ``signature`` key is absent are
    changed.  Existing empty, short, or valid signatures are preserved for the
    transcript cleanup policy to classify later.  Frames that need no change
    are returned by identity.
    """
    if data.get("type") != "assistant":
        return data, 0

    message = data.get("message")
    if not isinstance(message, dict):
        return data, 0
    content = message.get("content")
    if not isinstance(content, list):
        return data, 0

    missing = [
        index
        for index, block in enumerate(content)
        if isinstance(block, dict)
        and block.get("type") == "thinking"
        and "signature" not in block
    ]
    if not missing:
        return data, 0

    normalized_content = list(content)
    for index in missing:
        normalized_block = dict(content[index])
        normalized_block["signature"] = ""
        normalized_content[index] = normalized_block

    normalized_message = dict(message)
    normalized_message["content"] = normalized_content
    normalized_data = dict(data)
    normalized_data["message"] = normalized_message
    return normalized_data, len(missing)


class MuseLabSDKClient(ClaudeSDKClient):
    """Claude SDK client with MuseLab's tested streaming-input extensions."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The SDK transport serializes individual writes, but it cannot order a
        # multi-frame async iterable against a concurrent normal query/control
        # request.  MuseLab sends one frame today; keeping one app-level lock
        # makes that ordering explicit and safe if the SDK implementation
        # changes.
        self._muselab_input_lock = asyncio.Lock()

    async def query(
        self,
        prompt: str | AsyncIterable[dict[str, Any]],
        session_id: str = "default",
    ) -> None:
        """Serialize ordinary and steering writes on this client instance."""
        async with self._muselab_input_lock:
            await super().query(prompt, session_id=session_id)

    async def query_steering(
        self,
        prompt: str,
        *,
        session_id: str,
        command_uuid: str,
    ) -> None:
        """Queue a human prompt for the CLI's next mid-turn fold boundary.

        ``priority=next`` lets the CLI absorb the command after the current
        tool batch.  ``now`` would interrupt the active request, while
        ``later`` intentionally defers the command to a subsequent turn.
        """
        if not isinstance(prompt, str):
            raise TypeError("steering prompt must be a string")
        if not isinstance(command_uuid, str) or not command_uuid:
            raise ValueError("command_uuid must be a non-empty string")

        frame = {
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None,
            "session_id": session_id,
            "uuid": command_uuid,
            "priority": "next",
            "origin": {"kind": "human"},
            "shouldQuery": True,
        }

        async def _one_frame() -> AsyncIterator[dict[str, Any]]:
            yield frame

        async with self._muselab_input_lock:
            # Call the SDK implementation directly: going through our query()
            # override would acquire the same non-reentrant lock twice.
            await super().query(_one_frame(), session_id=session_id)

    def _normalize_incoming_frame(
        self, data: dict[str, Any],
    ) -> dict[str, Any]:
        return data

    async def receive_messages(
        self,
    ) -> AsyncIterator[Message | CommandLifecycleMessage]:
        """Yield SDK messages plus the lifecycle type upstream currently drops."""
        if not self._query:
            raise CLIConnectionError("Not connected. Call connect() first.")

        from claude_agent_sdk._internal.message_parser import parse_message

        async for data in self._query.receive_messages():
            normalized = self._normalize_incoming_frame(data)
            lifecycle = parse_command_lifecycle(normalized)
            if lifecycle is not None:
                yield lifecycle
                continue
            message = parse_message(normalized)
            if message is not None:
                yield message

    async def _send_muselab_control_request(
        self, request: dict[str, Any],
    ) -> dict[str, Any]:
        if not self._query:
            raise CLIConnectionError("Not connected. Call connect() first.")
        sender = getattr(self._query, "_send_control_request", None)
        if not callable(sender):
            raise CLIConnectionError(
                "Installed Claude Agent SDK does not expose control requests")
        async with self._muselab_input_lock:
            response = await sender(request)
        if not isinstance(response, dict):
            raise MessageParseError("Invalid SDK control response", response)
        return response

    async def cancel_async_message(self, command_uuid: str) -> bool:
        """Drop one UUID-stamped command that is still queued in the CLI."""
        if not isinstance(command_uuid, str) or not command_uuid:
            raise ValueError("command_uuid must be a non-empty string")
        response = await self._send_muselab_control_request({
            "subtype": "cancel_async_message",
            "message_uuid": command_uuid,
        })
        cancelled = response.get("cancelled")
        if not isinstance(cancelled, bool):
            raise MessageParseError(
                "Invalid cancel_async_message response", response)
        return cancelled

    async def interrupt(
        self, *, cancel_queued: bool = False,
    ) -> InterruptReceipt:
        """Interrupt the turn and retain the CLI's synchronous queue receipt."""
        request: dict[str, Any] = {"subtype": "interrupt"}
        if cancel_queued:
            request["cancel_queued"] = True
        response = await self._send_muselab_control_request(request)
        return InterruptReceipt(
            still_queued=_string_tuple_field(response, "still_queued"),
            cancelled=_string_tuple_field(response, "cancelled"),
        )


class UnsignedThinkingCompatibleClient(MuseLabSDKClient):
    """Claude SDK client that accepts unsigned vendor thinking blocks.

    The base class handles MuseLab's streaming-input protocol additions.  This
    subclass only normalizes vendor thinking frames before the same parser, so
    it cannot accidentally bypass command lifecycle handling.
    """

    def _normalize_incoming_frame(
        self, data: dict[str, Any],
    ) -> dict[str, Any]:
        normalized, count = normalize_missing_thinking_signatures(data)
        if count:
            model = (normalized.get("message") or {}).get("model", "")
            logger.warning(
                "accepted %d unsigned thinking block(s) from vendor model %s",
                count,
                model or "<unknown>",
            )
        return normalized
