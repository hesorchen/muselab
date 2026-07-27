"""Narrow compatibility shims for Anthropic-compatible vendor endpoints.

The Claude Agent SDK treats ``signature`` as a required field on every
assistant ``thinking`` block.  Some third-party endpoints emit the thinking
text but omit that field entirely, which makes the SDK raise
``MessageParseError`` before callers can receive the remaining assistant and
result messages.

Muselab only uses :class:`UnsignedThinkingCompatibleClient` for third-party
providers.  It supplies an empty in-memory signature for parsing, without
mutating the raw SDK frame or pretending that the vendor produced a valid
cryptographic signature.  ``backend.jsonl_cleanup`` removes the unverifiable
block from the persisted transcript after the turn completes.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk._errors import CLIConnectionError
from claude_agent_sdk.types import Message


logger = logging.getLogger(__name__)


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


class UnsignedThinkingCompatibleClient(ClaudeSDKClient):
    """Claude SDK client that accepts unsigned vendor thinking blocks.

    This mirrors the SDK's small ``receive_messages`` adapter so normalization
    happens before its strict private parser.  The SDK is deliberately pinned
    below 0.3 in ``pyproject.toml``; remove this subclass once upstream makes
    missing thinking signatures non-fatal.
    """

    async def receive_messages(self) -> AsyncIterator[Message]:
        if not self._query:
            raise CLIConnectionError("Not connected. Call connect() first.")

        from claude_agent_sdk._internal.message_parser import parse_message

        async for data in self._query.receive_messages():
            normalized, count = normalize_missing_thinking_signatures(data)
            if count:
                model = (normalized.get("message") or {}).get("model", "")
                logger.warning(
                    "accepted %d unsigned thinking block(s) from vendor model %s",
                    count,
                    model or "<unknown>",
                )
            message = parse_message(normalized)
            if message is not None:
                yield message
