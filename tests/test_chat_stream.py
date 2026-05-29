"""Integration test for the SSE streaming main path GET /api/chat/stream.

This is the most complex core path (an 800+ line handler). We monkeypatch
get_client to return a fake ClaudeSDKClient whose receive_response() yields
canned SDK messages, then drive the real handler through TestClient and
assert the SSE frames the frontend depends on (text → tool_use → tool_result
→ done), plus an error-classification frame on the failure path.

No real network, no real CLI subprocess, no Anthropic API.
"""
import json

import pytest
from claude_agent_sdk import (
    AssistantMessage, ResultMessage, StreamEvent,
    TextBlock, ToolUseBlock, ToolResultBlock,
)

from tests.conftest import TEST_TOKEN


class _FakeStreamClient:
    """Replays a scripted list of SDK messages from receive_response().
    query() is a no-op record. Mirrors the surface chat.stream uses:
    query(), receive_response(), get_context_usage()."""

    def __init__(self, messages):
        self._messages = messages
        self.queried = []

    async def query(self, prompt_or_gen):
        self.queried.append(prompt_or_gen)

    async def receive_response(self):
        for m in self._messages:
            yield m

    async def get_context_usage(self):
        return {"maxTokens": 200_000, "totalTokens": 1234}


@pytest.fixture()
def stream_env(app_module, monkeypatch):
    """Patch out everything the stream handler touches that would require a
    real CLI / disk transcript / push backend, leaving the frame-emission
    logic itself untouched."""
    from backend import chat as chat_mod

    # No real JSONL transcript — result handler tolerates an empty list.
    monkeypatch.setattr(chat_mod, "_get_session_msgs", lambda sid, model="": [])
    # Skip jsonl signature cleanup (would scan disk).
    from backend import jsonl_cleanup
    monkeypatch.setattr(jsonl_cleanup, "clean_session", lambda sid: None)
    # Pretend a device is active so the turn-done push fan-out is skipped.
    from backend import presence
    monkeypatch.setattr(presence, "recently_active", lambda: True)
    return chat_mod


def _make_session(client):
    r = client.post("/api/chat/sessions",
                    headers={"X-Auth-Token": TEST_TOKEN,
                             "Content-Type": "application/json"},
                    json={"name": "stream test", "model": "claude-sonnet-4-6"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _parse_sse(raw: str):
    """Parse an SSE response body into a list of (event, data) tuples."""
    events = []
    cur_event = None
    cur_data = []
    for line in raw.splitlines():
        if line.startswith("event:"):
            cur_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            cur_data.append(line[len("data:"):].strip())
        elif line == "":
            if cur_event is not None or cur_data:
                events.append((cur_event, "\n".join(cur_data)))
            cur_event, cur_data = None, []
    if cur_event is not None or cur_data:
        events.append((cur_event, "\n".join(cur_data)))
    return events


def test_stream_happy_path_text_tooluse_result_done(stream_env, client, monkeypatch):
    """Happy path: assistant text → tool_use → tool_result → done. Assert
    every key frame flows through with the expected shape."""
    chat_mod = stream_env
    sid = _make_session(client)

    messages = [
        # token-stream delta (fast feedback path)
        StreamEvent(uuid="u1", session_id=sid, event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hello "},
        }),
        StreamEvent(uuid="u2", session_id=sid, event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "world"},
        }),
        # AssistantMessage carries the consolidated blocks + a tool call.
        AssistantMessage(
            content=[
                TextBlock(text="Hello world"),
                ToolUseBlock(id="tu_1", name="Read",
                             input={"file_path": "/tmp/x.py"}),
            ],
            model="claude-sonnet-4-6",
            usage={"input_tokens": 100, "output_tokens": 20,
                   "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": 0},
        ),
        # SDK emits the tool result wrapped in the AssistantMessage's
        # follow-up; here we send it as a ToolResultBlock-bearing assistant
        # turn (handler forwards it as a tool_result event).
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="tu_1",
                                content="def x(): pass", is_error=False),
            ],
            model="claude-sonnet-4-6",
            usage={},
        ),
        ResultMessage(
            subtype="success", duration_ms=1500, duration_api_ms=1400,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.0042,
            usage={"input_tokens": 100, "output_tokens": 20},
        ),
    ]

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort=""):
        return _FakeStreamClient(messages)

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)

    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=hi&model=claude-sonnet-4-6")
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    kinds = [e for e, _ in events]

    # The frontend-critical frame sequence.
    assert "text" in kinds, f"no text frame: {kinds}"
    assert "tool_use" in kinds, f"no tool_use frame: {kinds}"
    assert "tool_result" in kinds, f"no tool_result frame: {kinds}"
    assert "done" in kinds, f"no done frame: {kinds}"
    # No error frame on the happy path.
    assert "error" not in kinds, f"unexpected error frame: {events}"

    # Text content accumulates the deltas.
    text_chunks = [json.loads(d)["text"] for e, d in events if e == "text"]
    assert "".join(text_chunks).startswith("Hello world")

    # tool_use carries the tool name + file_path.
    tu = next(json.loads(d) for e, d in events if e == "tool_use")
    assert tu["name"] == "Read"
    assert tu["input"]["file_path"] == "/tmp/x.py"

    # tool_result is tagged with the tool name (looked up via tool_use_id).
    tr = next(json.loads(d) for e, d in events if e == "tool_result")
    assert tr["tool_name"] == "Read"

    # done carries cost + model + cumulative session usage.
    done = next(json.loads(d) for e, d in events if e == "done")
    assert done["total_cost_usd"] == pytest.approx(0.0042)
    assert done["model"] == "claude-sonnet-4-6"
    assert done["cancelled"] is False
    assert "session_usage" in done

    # Turn reservation released after completion.
    assert sid not in chat_mod._active_turns


def test_stream_error_path_classifies_auth_error(stream_env, client, monkeypatch):
    """If the SDK stream raises an auth-shaped error, the handler emits an
    `error` frame carrying the classification (kind=auth, non-retryable)."""
    chat_mod = stream_env
    sid = _make_session(client)

    class _BoomClient:
        async def query(self, p):
            return None

        async def receive_response(self):
            raise RuntimeError("HTTP 401 invalid api key")
            yield  # pragma: no cover  (makes this an async generator)

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort=""):
        return _BoomClient()

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)

    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=hi&model=claude-sonnet-4-6")
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    err = next((json.loads(d) for e, d in events if e == "error"), None)
    assert err is not None, f"no error frame: {events}"
    assert err["kind"] == "auth", f"misclassified: {err}"
    assert err["cta"] == "open_settings"
    assert err["retryable"] is False
    # Reservation released even on error so the user can retry.
    assert sid not in chat_mod._active_turns


def test_stream_early_get_client_failure_emits_error_frame(stream_env, client, monkeypatch):
    """If get_client itself raises (e.g. auth pre-check), the handler must
    surface an SSE error frame, NOT bubble a 500 — the FE can only render
    typed errors from the frame, not from a 500."""
    chat_mod = stream_env
    sid = _make_session(client)

    async def boom_get_client(session_id, model, permission="bypassPermissions", effort=""):
        from claude_agent_sdk import ClaudeSDKError
        raise ClaudeSDKError("Claude model requires auth: run `claude login`")

    monkeypatch.setattr(chat_mod, "get_client", boom_get_client)

    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=hi&model=claude-sonnet-4-6")
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    err = next((json.loads(d) for e, d in events if e == "error"), None)
    assert err is not None, f"no error frame: {events}"
    assert err["kind"] == "auth"
    assert sid not in chat_mod._active_turns


def test_stream_reconnect_no_active_turn(stream_env, client):
    """Empty prompt + no in-flight turn = reconnect mode that finds nothing,
    yielding a single 'no active turn' error frame (not a 500)."""
    sid = _make_session(client)
    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}&prompt=")
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    err = next((json.loads(d) for e, d in events if e == "error"), None)
    assert err is not None
    # "no active turn" is unknown-kind, retryable.
    assert err["kind"] == "unknown"
