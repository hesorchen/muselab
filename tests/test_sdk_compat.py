from __future__ import annotations

import asyncio
import json

import pytest

from claude_agent_sdk import AssistantMessage, ThinkingBlock
from claude_agent_sdk._errors import MessageParseError

from backend.sdk_compat import (
    CommandLifecycleMessage,
    InterruptReceipt,
    MuseLabSDKClient,
    UnsignedThinkingCompatibleClient,
    normalize_missing_thinking_signatures,
    parse_command_lifecycle,
)


def _assistant_frame(*blocks: dict) -> dict:
    return {
        "type": "assistant",
        "session_id": "session-1",
        "message": {
            "model": "glm-5.2-internal",
            "content": list(blocks),
        },
    }


def test_normalize_missing_thinking_signature_without_mutating_raw_frame():
    raw = _assistant_frame(
        {"type": "thinking", "thinking": "scratch"},
        {"type": "text", "text": "answer"},
    )

    normalized, count = normalize_missing_thinking_signatures(raw)

    assert count == 1
    assert "signature" not in raw["message"]["content"][0]
    assert normalized["message"]["content"][0]["signature"] == ""
    assert normalized["message"]["content"][1] is raw["message"]["content"][1]


@pytest.mark.parametrize("signature", ["", "short", "x" * 88])
def test_normalize_preserves_existing_signature(signature):
    raw = _assistant_frame({
        "type": "thinking",
        "thinking": "scratch",
        "signature": signature,
    })

    normalized, count = normalize_missing_thinking_signatures(raw)

    assert count == 0
    assert normalized is raw
    assert normalized["message"]["content"][0]["signature"] == signature


@pytest.mark.asyncio
async def test_compat_client_yields_assistant_with_missing_signature():
    raw = _assistant_frame(
        {"type": "thinking", "thinking": "scratch"},
        {"type": "text", "text": "answer"},
    )

    class FakeQuery:
        async def receive_messages(self):
            yield raw

    client = UnsignedThinkingCompatibleClient()
    client._query = FakeQuery()

    messages = [message async for message in client.receive_messages()]

    assert len(messages) == 1
    assert isinstance(messages[0], AssistantMessage)
    thinking = messages[0].content[0]
    assert isinstance(thinking, ThinkingBlock)
    assert thinking.thinking == "scratch"
    assert thinking.signature == ""


def _lifecycle_frame(state: str = "queued") -> dict:
    return {
        "type": "command_lifecycle",
        "command_uuid": "11111111-1111-4111-8111-111111111111",
        "state": state,
        "session_id": "session-1",
        "uuid": "22222222-2222-4222-8222-222222222222",
    }


def test_parse_command_lifecycle_preserves_both_uuids():
    message = parse_command_lifecycle(_lifecycle_frame("started"))

    assert message == CommandLifecycleMessage(
        command_uuid="11111111-1111-4111-8111-111111111111",
        state="started",
        session_id="session-1",
        uuid="22222222-2222-4222-8222-222222222222",
    )


def test_parse_command_lifecycle_rejects_unknown_state():
    with pytest.raises(MessageParseError, match="Invalid command_lifecycle state"):
        parse_command_lifecycle(_lifecycle_frame("future-state"))


@pytest.mark.asyncio
async def test_client_yields_command_lifecycle_before_sdk_parser_drops_it():
    raw = _lifecycle_frame("completed")

    class FakeQuery:
        async def receive_messages(self):
            yield raw

    client = MuseLabSDKClient()
    client._query = FakeQuery()

    messages = [message async for message in client.receive_messages()]

    assert messages == [CommandLifecycleMessage(
        command_uuid=raw["command_uuid"],
        state="completed",
        session_id="session-1",
        uuid=raw["uuid"],
    )]


@pytest.mark.asyncio
async def test_query_steering_writes_exact_priority_next_user_frame():
    writes: list[dict] = []

    class FakeTransport:
        async def write(self, line: str):
            writes.append(json.loads(line))

    client = MuseLabSDKClient()
    client._query = object()
    client._transport = FakeTransport()

    await client.query_steering(
        "Please adjust the current task",
        session_id="session-1",
        command_uuid="11111111-1111-4111-8111-111111111111",
    )

    assert writes == [{
        "type": "user",
        "message": {
            "role": "user",
            "content": "Please adjust the current task",
        },
        "parent_tool_use_id": None,
        "session_id": "session-1",
        "uuid": "11111111-1111-4111-8111-111111111111",
        "priority": "next",
        "origin": {"kind": "human"},
        "shouldQuery": True,
    }]


@pytest.mark.asyncio
async def test_query_and_steering_writes_are_serialized_per_client():
    active = 0
    max_active = 0

    class SlowTransport:
        async def write(self, _line: str):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            active -= 1

    client = MuseLabSDKClient()
    client._query = object()
    client._transport = SlowTransport()

    await asyncio.gather(
        client.query("ordinary", session_id="session-1"),
        client.query_steering(
            "steer",
            session_id="session-1",
            command_uuid="11111111-1111-4111-8111-111111111111",
        ),
    )

    assert max_active == 1


@pytest.mark.asyncio
async def test_cancel_async_message_returns_cli_receipt():
    class FakeQuery:
        requests: list[dict] = []

        async def _send_control_request(self, request: dict):
            self.requests.append(request)
            return {"cancelled": True}

    query = FakeQuery()
    client = MuseLabSDKClient()
    client._query = query

    cancelled = await client.cancel_async_message(
        "11111111-1111-4111-8111-111111111111")

    assert cancelled is True
    assert query.requests == [{
        "subtype": "cancel_async_message",
        "message_uuid": "11111111-1111-4111-8111-111111111111",
    }]


@pytest.mark.asyncio
async def test_interrupt_cancel_queued_preserves_full_receipt():
    class FakeQuery:
        requests: list[dict] = []

        async def _send_control_request(self, request: dict):
            self.requests.append(request)
            return {
                "still_queued": [],
                "cancelled": ["11111111-1111-4111-8111-111111111111"],
            }

    query = FakeQuery()
    client = MuseLabSDKClient()
    client._query = query

    receipt = await client.interrupt(cancel_queued=True)

    assert receipt == InterruptReceipt(
        still_queued=(),
        cancelled=("11111111-1111-4111-8111-111111111111",),
    )
    assert query.requests == [{
        "subtype": "interrupt",
        "cancel_queued": True,
    }]


@pytest.mark.asyncio
async def test_interrupt_distinguishes_missing_receipt_from_empty_queue():
    class OldCLIQuery:
        async def _send_control_request(self, _request: dict):
            return {}

    client = MuseLabSDKClient()
    client._query = OldCLIQuery()

    receipt = await client.interrupt()

    assert receipt == InterruptReceipt(still_queued=None, cancelled=None)


def test_unsigned_thinking_client_keeps_muselab_protocol_adapter():
    assert issubclass(UnsignedThinkingCompatibleClient, MuseLabSDKClient)
