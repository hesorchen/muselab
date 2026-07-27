from __future__ import annotations

import pytest

from claude_agent_sdk import AssistantMessage, ThinkingBlock

from backend.sdk_compat import (
    UnsignedThinkingCompatibleClient,
    normalize_missing_thinking_signatures,
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
