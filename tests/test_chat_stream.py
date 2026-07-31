"""Integration test for the SSE streaming main path GET /api/chat/stream.

This is the most complex core path (an 800+ line handler). We monkeypatch
get_client to return a fake ClaudeSDKClient whose receive_response() yields
canned SDK messages, then drive the real handler through TestClient and
assert the SSE frames the frontend depends on (text → tool_use → tool_result
→ done), plus an error-classification frame on the failure path.

No real network, no real CLI subprocess, no Anthropic API.
"""
import asyncio
import base64
import collections
import json
from types import SimpleNamespace

import pytest
from claude_agent_sdk import (
    AssistantMessage, UserMessage, ResultMessage, StreamEvent,
    TextBlock, ToolUseBlock, ToolResultBlock,
    TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage,
    TaskUpdatedMessage,
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
        if hasattr(prompt_or_gen, "__aiter__"):
            items = []
            async for item in prompt_or_gen:
                items.append(item)
            self.queried.append(items)
        else:
            self.queried.append(prompt_or_gen)

    async def receive_response(self):
        for m in self._messages:
            yield m

    async def get_context_usage(self):
        return {"maxTokens": 200_000, "totalTokens": 1234}


class _FakeBatchedStreamClient(_FakeStreamClient):
    """One receive_response() batch per call, matching SDK Result boundaries."""

    def __init__(self, batches):
        super().__init__([])
        self._batches = list(batches)
        self.receive_calls = 0

    async def receive_response(self):
        self.receive_calls += 1
        batch = self._batches.pop(0) if self._batches else []
        for m in batch:
            yield m


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


def test_mem0_never_rewrites_canonical_user_query(
        stream_env, client, monkeypatch):
    """Recall is an SDK hook; client.query must receive only user-authored text."""
    chat_mod = stream_env
    sid = _make_session(client)
    messages = [ResultMessage(
        subtype="success", duration_ms=10, duration_api_ms=9,
        is_error=False, num_turns=1, session_id=sid,
        total_cost_usd=0.0, usage={"input_tokens": 1, "output_tokens": 1},
    )]
    fake = _FakeStreamClient(messages)

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort=""):
        return fake

    async def must_not_search_here(*args, **kwargs):
        raise AssertionError("recall must run in UserPromptSubmit hook")

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(chat_mod.mem0, "search_context", must_not_search_here)
    prompt = "the exact user-authored prompt"
    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        f"&prompt={prompt}&model=claude-sonnet-4-6")
    assert response.status_code == 200
    assert fake.queried == [prompt]


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
            uuid="assistant-final-uuid",
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
    monkeypatch.setattr(
        chat_mod,
        "_recent_turn_uuids",
        lambda _sid, _want_image_user: (
            "assistant-final-uuid", "user-final-uuid"),
    )

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
    assert done["duration_ms"] == 1500
    assert done["assistant_uuid"] == "assistant-final-uuid"
    assert isinstance(done["completed_at_ms"], int)
    assert done["completed_at_ms"] > 0
    assert "session_usage" in done
    annotations = chat_mod.sess.get_message_annotations(sid)
    assert annotations["assistant-final-uuid"]["ts"] == done["completed_at_ms"]
    assert annotations["assistant-final-uuid"]["elapsed_s"] == 1.5

    # Turn reservation released after completion.
    assert sid not in chat_mod._active_turns


def test_tool_only_turn_persists_completion_annotation(
        stream_env, client, monkeypatch):
    """Completion metadata must survive turns with no streamed assistant text."""
    chat_mod = stream_env
    sid = _make_session(client)
    assistant_uuid = "assistant-tool-only-uuid"
    messages = [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id="tu_tool_only",
                    name="Read",
                    input={"file_path": "/tmp/tool-only.txt"},
                ),
            ],
            model="claude-sonnet-4-6",
            usage={},
            uuid=assistant_uuid,
        ),
        ResultMessage(
            subtype="success", duration_ms=2500, duration_api_ms=2400,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.0, usage={},
        ),
    ]

    async def fake_get_client(
        session_id, model, permission="bypassPermissions", effort="",
    ):
        return _FakeStreamClient(messages)

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(
        chat_mod,
        "_recent_turn_uuids",
        lambda _sid, _want_image_user: (
            assistant_uuid, "user-tool-only-uuid"),
    )

    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        f"&prompt=inspect&model=claude-sonnet-4-6")
    assert response.status_code == 200, response.text
    events = _parse_sse(response.text)
    done = next(json.loads(data) for event, data in events if event == "done")

    assert done["assistant_uuid"] == assistant_uuid
    assert done["duration_ms"] == 2500
    annotations = chat_mod.sess.get_message_annotations(sid)
    assert annotations[assistant_uuid]["ts"] == done["completed_at_ms"]
    assert annotations[assistant_uuid]["elapsed_s"] == 2.5


def test_done_is_published_before_slow_post_turn_bookkeeping(
        stream_env, client, monkeypatch):
    """ResultMessage ends the UI turn before context/JSONL bookkeeping."""
    from backend import activity as activity_module

    chat_mod = stream_env
    sid = _make_session(client)

    async def exercise():
        context_entered = asyncio.Event()
        release_context = asyncio.Event()
        activity_transitions = []
        context_calls = 0

        class _SlowPostprocessClient(_FakeStreamClient):
            async def get_context_usage(self):
                nonlocal context_calls
                context_calls += 1
                if context_calls == 1:
                    # Preflight accounting happens before the model query and
                    # is intentionally not part of this regression.
                    return {
                        "maxTokens": 200_000,
                        "totalTokens": 1000,
                    }
                context_entered.set()
                await release_context.wait()
                return {
                    "maxTokens": 200_000,
                    "totalTokens": 1234,
                }

        fake = _SlowPostprocessClient([ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=9,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.0,
            usage={"input_tokens": 1, "output_tokens": 1},
        )])

        async def fake_get_client(*_args, **_kwargs):
            return fake

        monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
        monkeypatch.setattr(
            activity_module.activity,
            "start",
            lambda activity_sid, *, summary="": activity_transitions.append(
                ("start", activity_sid, summary)),
        )
        monkeypatch.setattr(
            activity_module.activity,
            "finish",
            lambda activity_sid, status: activity_transitions.append(
                ("finish", activity_sid, status)),
        )

        broadcast = await chat_mod._start_turn(sid, "quick reply")
        await asyncio.wait_for(context_entered.wait(), timeout=1)

        # The context refresh is deliberately blocked, yet both the browser
        # terminal event and the global activity completion already happened.
        assert any(
            event.get("event") == "done"
            for event in broadcast.replay_events()
        )
        assert activity_transitions == [
            ("start", sid, "quick reply"),
            ("finish", sid, "completed"),
        ]
        assert broadcast.done is False

        release_context.set()
        await asyncio.wait_for(broadcast.task, timeout=1)
        assert broadcast.done is True
        assert sid not in chat_mod._active_turns
        recent = chat_mod._recent_turns.pop(sid, None)
        if recent is not None:
            recent.close()

    asyncio.run(exercise())


def test_stream_drops_prior_turn_replay_but_keeps_late_task_lifecycle(
        stream_env, client, monkeypatch):
    """A pooled SDK queue may start with the preceding turn's delayed tail.

    Task lifecycle must still settle the original card, while old UUID-scoped
    text/tools/Result are discarded and cannot terminate the new query.
    """
    chat_mod = stream_env
    sid = _make_session(client)
    old_uuids = frozenset({"old-stream", "old-assistant", "old-user", "old-result"})
    monkeypatch.setattr(
        chat_mod, "_session_message_uuids", lambda _sid, _model: old_uuids)

    stale_batch = [
        TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="task-old",
            status="completed", output_file="/tmp/task-old.output",
            summary="old task completed", uuid="old-task-notification",
            session_id=sid, tool_use_id="tu-old"),
        StreamEvent(uuid="old-stream", session_id=sid, event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "OLD answer"},
        }),
        AssistantMessage(
            content=[
                TextBlock(text="OLD answer"),
                ToolUseBlock(id="tu-old", name="Agent", input={
                    "description": "old subagent", "prompt": "old prompt",
                    "subagent_type": "general-purpose",
                }),
            ],
            model="claude-sonnet-4-6", uuid="old-assistant"),
        UserMessage(
            content=[ToolResultBlock(
                tool_use_id="tu-old", content="OLD tool result", is_error=False)],
            uuid="old-user"),
        ResultMessage(
            subtype="success", duration_ms=100, duration_api_ms=90,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=9.99, usage={"input_tokens": 99, "output_tokens": 99},
            result="OLD answer", uuid="old-result"),
    ]
    current_batch = [
        StreamEvent(uuid="new-stream", session_id=sid, event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Current answer"},
        }),
        AssistantMessage(
            content=[TextBlock(text="Current answer")],
            model="claude-sonnet-4-6", uuid="new-assistant",
            usage={"input_tokens": 4, "output_tokens": 2}),
        ResultMessage(
            subtype="success", duration_ms=200, duration_api_ms=180,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.02, usage={"input_tokens": 4, "output_tokens": 2},
            result="Current answer", uuid="new-result"),
    ]
    fake = _FakeBatchedStreamClient([stale_batch, current_batch])

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort=""):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=current-question&model=claude-sonnet-4-6")
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    kinds = [e for e, _ in events]

    assert fake.receive_calls == 2, "stale Result should reopen receive_response"
    assert kinds.count("task_notification") == 1
    assert "tool_use" not in kinds
    assert "tool_result" not in kinds
    chunks = [json.loads(d)["text"] for e, d in events if e == "text"]
    assert "".join(chunks) == "Current answer"
    assert "OLD" not in r.text
    assert kinds.count("done") == 1
    done = next(json.loads(d) for e, d in events if e == "done")
    assert done["total_cost_usd"] == pytest.approx(0.02)
    assert sid not in chat_mod._active_turns


def test_sdk_error_extractors_keep_result_and_assistant_detail(stream_env):
    chat_mod = stream_env
    assistant = AssistantMessage(
        content=[TextBlock(text="API Error: input exceeds the context window")],
        model="<synthetic>", error="invalid_request",
    )
    result = ResultMessage(
        subtype="error", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id="sid",
        result="502 unknown provider for model claude-opus-4-8",
        errors=["502 unknown provider for model claude-opus-4-8"],
        api_error_status=502,
    )

    a = chat_mod._sdk_assistant_error(assistant)
    r = chat_mod._sdk_result_error(result)
    merged = chat_mod._merge_sdk_errors([a, r])

    assert "context window" in a["message"]
    assert merged["message"].count("502 unknown provider") == 1
    assert merged["api_error_status"] == 502


def test_run_sdk_command_checked_rejects_in_band_result_error(stream_env):
    chat_mod = stream_env
    result = ResultMessage(
        subtype="error", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id="sid",
        result="Your input exceeds the context window of this model",
        api_error_status=400,
    )

    class FakeClient:
        def __init__(self):
            self.queries = []

        async def query(self, prompt):
            self.queries.append(prompt)

        async def receive_response(self):
            yield result

    fake = FakeClient()
    with pytest.raises(chat_mod._SDKCommandError, match="context window"):
        asyncio.run(chat_mod._run_sdk_command_checked(fake, "/compact"))
    assert fake.queries == ["/compact"]


def test_turn_response_boundary_accepts_lifecycle_and_uuid_less_error(stream_env):
    chat_mod = stream_env
    boundary = chat_mod._TurnResponseBoundary({"old"})
    old_result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id="sid", uuid="old")
    lifecycle = TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="t", status="completed",
        output_file="", summary="done", uuid="old", session_id="sid")
    error_result = ResultMessage(
        subtype="error", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id="sid", uuid=None)

    assert boundary.classify(lifecycle) == "forward"
    assert boundary.classify(old_result) == "stale_result"
    assert boundary.classify(error_result) == "current_result"


def test_preflight_compact_failure_blocks_original_prompt(stream_env, client, monkeypatch):
    chat_mod = stream_env
    sid = _make_session(client)
    compact_error = ResultMessage(
        subtype="error", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id=sid,
        result="Your input exceeds the context window of this model",
        api_error_status=400,
    )
    fake = _FakeStreamClient([compact_error])

    async def near_limit_context():
        return {
            "maxTokens": 200_000,
            "rawMaxTokens": 200_000,
            "autoCompactThreshold": 160_000,
            "totalTokens": 190_000,
        }

    fake.get_context_usage = near_limit_context

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort=""):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=must-not-send&model=claude-sonnet-4-6")
    events = _parse_sse(r.text)
    error = next(json.loads(d) for e, d in events if e == "error")

    assert fake.queried == ["/compact"]
    assert error["kind"] == "context_window"
    assert error["retryable"] is False


def test_stream_done_classifies_synthetic_context_error(stream_env, client, monkeypatch):
    chat_mod = stream_env
    sid = _make_session(client)
    messages = [
        AssistantMessage(
            content=[TextBlock(text="API Error: Your input exceeds the context window")],
            model="<synthetic>", error="invalid_request",
        ),
        ResultMessage(
            subtype="error", duration_ms=1, duration_api_ms=1,
            is_error=True, num_turns=1, session_id=sid,
            result="API Error: Your input exceeds the context window of this model",
            api_error_status=400,
        ),
    ]
    fake = _FakeStreamClient(messages)

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort=""):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=continue&model=claude-sonnet-4-6")
    events = _parse_sse(r.text)
    assert not [d for e, d in events if e == "text"], "synthetic error is not assistant text"
    done = next(json.loads(d) for e, d in events if e == "done")
    assert done["is_error"] is True
    assert done["kind"] == "context_window"
    assert done["cta"] == "compact_or_fork"
    assert done["retryable"] is False
    assert "context window" in done["error"]


def test_stream_pdf_attachment_persists_path_fallback(stream_env, client, monkeypatch):
    """PDF attachments keep the native document block, and also expose a
    local Read-able file path for Anthropic-compatible backends that ignore
    document blocks."""
    chat_mod = stream_env
    sid = _make_session(client)
    pdf_bytes = b"%PDF-1.4\nminimal test pdf\n%%EOF\n"
    chat_mod._image_store["pdf1"] = {
        "kind": "pdf",
        "mime": "application/pdf",
        "name": "doc.pdf",
        "b64": base64.b64encode(pdf_bytes).decode("ascii"),
        "ts": 9999999999,
    }

    messages = [
        ResultMessage(
            subtype="success", duration_ms=100, duration_api_ms=90,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.0, usage={"input_tokens": 1, "output_tokens": 1},
        ),
    ]
    fake = _FakeStreamClient(messages)

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort=""):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)

    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=please read it&image_ids=pdf1&model=claude-sonnet-4-6")
    assert r.status_code == 200, r.text

    # Filename is `{aid}-{sanitised original name}` — the aid keeps two
    # same-named uploads from clobbering each other, the readable half tells
    # the agent what it's about to Read.
    attach_path = chat_mod._attachments_base() / sid / "pdf1-doc.pdf"
    assert attach_path.read_bytes() == pdf_bytes

    assert fake.queried, "stream handler never called client.query"
    sent = fake.queried[0][0]
    content = sent["message"]["content"]
    assert content[0]["type"] == "document"
    assert content[0]["source"]["media_type"] == "application/pdf"
    text = content[1]["text"]
    assert "please read it" in text
    assert "Files attached to this message (on disk)" in text
    assert "doc.pdf" in text
    assert str(attach_path) in text


def test_stream_text_attachment_goes_to_disk_not_into_prompt(
        stream_env, client, monkeypatch):
    """The whole point of the 2026-07-25 change: a text attachment is written
    to disk and referenced by PATH. Its contents must not appear in the
    prompt — inlining put the file in the transcript forever, so every later
    turn in the session re-sent it, and a 200 KB CSV quietly ate the context
    window that the user actually wanted for the conversation."""
    chat_mod = stream_env
    sid = _make_session(client)
    secret = "COLUMN_HEADER_THAT_MUST_NOT_BE_INLINED"
    body = f"a,b,c\n1,2,{secret}\n".encode("utf-8")
    chat_mod._image_store["txt1"] = {
        "kind": "text",
        "mime": "text/csv",
        "name": "数据 表.csv",
        "raw": body,
        "text": body.decode("utf-8"),
        "ts": 9999999999,
    }

    fake = _FakeStreamClient([
        ResultMessage(
            subtype="success", duration_ms=100, duration_api_ms=90,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.0, usage={"input_tokens": 1, "output_tokens": 1},
        ),
    ])

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort=""):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)

    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=summarise it&image_ids=txt1&model=claude-sonnet-4-6")
    assert r.status_code == 200, r.text

    # Whitespace in the original name is sanitised to `_`; CJK survives.
    attach_path = chat_mod._attachments_base() / sid / "txt1-数据_表.csv"
    assert attach_path.read_bytes() == body

    # _FakeStreamClient.query records a bare string for a plain prompt and a
    # LIST of message dicts for an async-generator payload. With no image/PDF
    # blocks there's nothing to structure, so this turn is the string case —
    # indexing [0][0] the way the PDF test does would grab one character.
    call = fake.queried[0]
    if isinstance(call, (list, tuple)):
        content = call[0]["message"]["content"]
        text = content if isinstance(content, str) else "".join(
            b.get("text", "") for b in content if isinstance(b, dict))
    else:
        text = call
    assert "summarise it" in text
    assert str(attach_path) in text
    assert "数据 表.csv" in text          # display name stays the original
    assert secret not in text            # …but the CONTENTS never ship


def test_stream_background_task_messages_flow_through(stream_env, client, monkeypatch):
    """SDK-native background-task lifecycle (run_in_background=true) must reach
    the FE as task_started / task_progress / task_notification frames carrying
    the SDK fields verbatim (task_id, tool_use_id, status, summary,
    output_file). muselab used to silently drop these SystemMessage subclasses.

    Scripts the rare in-turn case (task terminates before ResultMessage) so a
    single SSE response carries the whole lifecycle; the common cross-turn case
    is Phase 2's watcher, tested separately.
    """
    chat_mod = stream_env
    sid = _make_session(client)

    messages = [
        # The Agent tool_use that launches the background subagent.
        AssistantMessage(
            content=[
                ToolUseBlock(id="tu_bg", name="Agent",
                             input={"description": "deep research",
                                    "prompt": "go", "run_in_background": True}),
            ],
            model="claude-sonnet-4-6",
            usage={"input_tokens": 50, "output_tokens": 10,
                   "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": 0},
        ),
        TaskStartedMessage(
            subtype="task_started", data={}, task_id="task_1",
            description="deep research", uuid="t-u1", session_id=sid,
            tool_use_id="tu_bg", task_type="general-purpose",
        ),
        TaskProgressMessage(
            subtype="task_progress", data={}, task_id="task_1",
            description="deep research",
            usage={"total_tokens": 1200, "tool_uses": 3, "duration_ms": 4200},
            uuid="t-u2", session_id=sid, tool_use_id="tu_bg",
            last_tool_name="Grep",
        ),
        TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="task_1",
            status="completed", output_file="/tmp/task_1_output.md",
            summary="Found 3 sources.", uuid="t-u3", session_id=sid,
            tool_use_id="tu_bg",
            usage={"total_tokens": 2400, "tool_uses": 5, "duration_ms": 8800},
        ),
        ResultMessage(
            subtype="success", duration_ms=1500, duration_api_ms=1400,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.01,
            usage={"input_tokens": 50, "output_tokens": 10},
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

    assert "task_started" in kinds, f"no task_started frame: {kinds}"
    assert "task_progress" in kinds, f"no task_progress frame: {kinds}"
    assert "task_notification" in kinds, f"no task_notification frame: {kinds}"

    started = next(json.loads(d) for e, d in events if e == "task_started")
    assert started["task_id"] == "task_1"
    assert started["tool_use_id"] == "tu_bg"   # ties the card to the Agent call
    assert started["description"] == "deep research"

    prog = next(json.loads(d) for e, d in events if e == "task_progress")
    assert prog["task_id"] == "task_1"
    assert prog["last_tool_name"] == "Grep"
    assert prog["usage"]["total_tokens"] == 1200

    note = next(json.loads(d) for e, d in events if e == "task_notification")
    assert note["task_id"] == "task_1"
    assert note["tool_use_id"] == "tu_bg"
    assert note["status"] == "completed"
    assert note["summary"] == "Found 3 sources."
    assert note["output_file"] == "/tmp/task_1_output.md"

    # Turn still completes normally — the done frame is not blocked by tasks.
    assert "done" in kinds, f"no done frame: {kinds}"
    assert sid not in chat_mod._active_turns
    # In-turn settle removed the pin; nothing left dangling for this session.
    assert sid not in chat_mod._sessions_with_inflight_tasks


class _FakeWatchClient:
    """Minimal client exposing only receive_messages() — the surface the
    cross-turn watcher reads."""

    def __init__(self, messages):
        self._messages = messages

    async def receive_messages(self):
        for m in self._messages:
            yield m


class _BatchedWatchClient:
    """A watcher client whose stream ends once before an explicit query.

    This mirrors the real failure mode: the terminal TaskNotification is
    delivered, receive_messages() reaches EOF, then a new query opens a fresh
    response stream containing the assistant reaction and ResultMessage.
    """

    def __init__(self, batches):
        self._batches = list(batches)
        self.queries = []
        self._receive_count = 0

    async def query(self, prompt_or_gen):
        items = []
        async for item in prompt_or_gen:
            items.append(item)
        self.queries.append(items)

    async def receive_messages(self):
        index = self._receive_count
        self._receive_count += 1
        batch = self._batches[index] if index < len(self._batches) else []
        for message in batch:
            yield message


def test_settle_background_task_dedups(stream_env):
    """Two observers (in-turn dispatch + cross-turn watcher) can both see the
    same terminal notification; _settle unpins exactly ONCE — the first caller
    gets True, the second sees the task_id already gone and returns False."""
    chat_mod = stream_env

    sid = "sid-settle"
    chat_mod._sessions_with_inflight_tasks[sid] = {"task_1"}
    chat_mod._bg_task_descriptions["task_1"] = "deep research"

    try:
        first = chat_mod._settle_background_task(sid, "task_1")
        second = chat_mod._settle_background_task(sid, "task_1")
        assert first is True, "first settle should win"
        assert second is False, "second settle should be a no-op"
        # Pin released + description cache consumed (no leak).
        assert sid not in chat_mod._sessions_with_inflight_tasks
        assert "task_1" not in chat_mod._bg_task_descriptions
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)
        chat_mod._bg_task_descriptions.pop("task_1", None)


def test_merge_session_inflight_recovers_orphaned_task(stream_env):
    """Watcher replacement must retain every session-level task even when this
    turn's local inflight_tasks doesn't contain it. _merge_session_inflight
    unions the turn-local launches with the session-level pin set."""
    chat_mod = stream_env
    sid = "sid-orphan"
    try:
        # Prior-turn task still pinned at session level + description cached,
        # but NOT in this turn's local inflight dict.
        chat_mod._sessions_with_inflight_tasks[sid] = {"task_prior"}
        chat_mod._bg_task_descriptions["task_prior"] = "deep research"
        turn_local = {"task_now": {"tool_use_id": "tu_now",
                                   "description": "this turn"}}

        merged = chat_mod._merge_session_inflight(sid, turn_local)

        # Both the just-launched task and the orphaned prior task are covered.
        assert set(merged) == {"task_now", "task_prior"}
        assert merged["task_now"]["description"] == "this turn"
        assert merged["task_prior"]["description"] == "deep research"
        # Turn-local entry is not mutated (defensive copy).
        assert "task_prior" not in turn_local

        # A session with no pins → just the turn-local set, unchanged.
        assert chat_mod._merge_session_inflight("sid-none", turn_local) == \
            turn_local
        # Empty everything → empty (no spurious watcher spawn).
        assert chat_mod._merge_session_inflight("sid-none", {}) == {}
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)
        chat_mod._bg_task_descriptions.pop("task_prior", None)


def test_watcher_opens_continuation_turn_and_unpins(stream_env):
    """Redesign (2026-06-03): the cross-turn watcher no longer rings a bell.
    The probe proved the terminal TaskNotification lands AFTER ResultMessage,
    then the CLI auto-continues a short reaction (AssistantMessage + its own
    ResultMessage). The watcher reads all of it off receive_messages() and
    surfaces it LIVE: it opens a headless CONTINUATION TurnBroadcast carrying
    the task_notification (card flip) + the reaction text + a done sentinel,
    finishes it (grace-kept for a slightly-late FE reconnect), and releases the
    client pin once nothing is left in flight."""
    import asyncio

    chat_mod = stream_env

    sid = "sid-watch"
    chat_mod._sessions_with_inflight_tasks[sid] = {"task_9"}
    origin_started_at = 1_700_000_100.5
    chat_mod._background_turn_started_at[sid] = origin_started_at
    chat_mod._background_origin_turn_id[sid] = "origin-user-turn"
    notif = TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="task_9",
        status="completed", output_file="/tmp/o.md", summary="done",
        uuid="u", session_id=sid, tool_use_id="tu")
    # CLI auto-continue: the model reacts to the finished task.
    reaction = AssistantMessage(
        content=[TextBlock(text="Background research finished — summary above.")],
        model="claude-sonnet-4-6", usage={},
        uuid="continuation-assistant-uuid")
    result = ResultMessage(
        subtype="success", duration_ms=120, duration_api_ms=100,
        is_error=False, num_turns=1, session_id=sid,
        total_cost_usd=0.0, usage={})
    fake_client = _FakeWatchClient([notif, reaction, result])

    async def run():
        await chat_mod._watch_inflight_tasks(
            sid, fake_client, {"task_9": "deep research"})

    try:
        asyncio.run(run())
        # Continuation finished → popped from _active_turns, grace-kept.
        assert sid not in chat_mod._active_turns
        bc = chat_mod._recent_turns.get(sid)
        assert bc is not None, "continuation broadcast not grace-kept"
        assert bc.is_continuation is True
        assert bc.started_at == origin_started_at
        assert bc.parent_turn_id == "origin-user-turn"
        kinds = [e.get("event") for e in bc.events]
        assert "task_notification" in kinds, f"no card flip: {kinds}"
        assert "text" in kinds, f"no reaction text: {kinds}"
        assert kinds[-1] == "done", f"missing terminal done: {kinds}"
        done_ev = next(e for e in bc.events if e.get("event") == "done")
        done = json.loads(done_ev["data"])
        assert done["duration_ms"] == 120
        assert done["assistant_uuid"] == "continuation-assistant-uuid"
        assert isinstance(done["completed_at_ms"], int)
        assert done["completed_at_ms"] > 0
        # The task_notification carries the launching card's tool_use_id so the
        # FE can flip it, plus the terminal status + artifact link.
        notif_ev = next(e for e in bc.events
                        if e.get("event") == "task_notification")
        payload = json.loads(notif_ev["data"])
        assert payload["task_id"] == "task_9"
        assert payload["tool_use_id"] == "tu"
        assert payload["status"] == "completed"
        assert payload["output_file"] == "/tmp/o.md"
        # All pending settled → pin released, client reclaimable.
        assert sid not in chat_mod._sessions_with_inflight_tasks
        assert sid not in chat_mod._task_watchers
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)
        chat_mod._background_turn_started_at.pop(sid, None)
        chat_mod._background_origin_turn_id.pop(sid, None)
        chat_mod._task_watchers.pop(sid, None)
        chat_mod._active_turns.pop(sid, None)
        chat_mod._recent_turns.pop(sid, None)


def test_continuation_terminal_precedes_annotation_bookkeeping(stream_env):
    """The footer terminal event must not wait for transcript sidecars."""
    import inspect

    source = inspect.getsource(stream_env._watch_inflight_tasks)
    done_at = source.index(
        'b.publish({"event": "done", "data": json.dumps(done_payload)})')
    annotate_at = source.index("_recent_turn_uuids, session_id, False")
    assert done_at < annotate_at
    assert stream_env._CONTINUATION_GRACE <= 8


def test_watcher_explicitly_resumes_when_auto_continuation_stream_ends(stream_env):
    """A completed task must not silently end with only its notification.

    When the CLI closes the notification stream before auto-continuing, the
    watcher sends one metadata-only resume query and drains its fresh response
    through the terminal ResultMessage.
    """
    import asyncio

    chat_mod = stream_env
    sid = "sid-watch-explicit-resume"
    chat_mod._sessions_with_inflight_tasks[sid] = {"task_resume"}
    notif = TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="task_resume",
        status="completed", output_file="/tmp/result.md", summary="done",
        uuid="u-resume", session_id=sid, tool_use_id="tu-resume")
    reaction = AssistantMessage(
        content=[TextBlock(text="最终汇总已经完成。")],
        model="claude-sonnet-4-6", usage={})
    result = ResultMessage(
        subtype="success", duration_ms=100, duration_api_ms=80,
        is_error=False, num_turns=1, session_id=sid,
        total_cost_usd=0.0, usage={})
    fake_client = _BatchedWatchClient([
        [notif],
        [reaction, result],
    ])
    sidecar_turn = chat_mod.TurnBroadcast(
        session_id=sid, model="claude-sonnet-4-6")
    sidecar_turn.user_text = "完成调研报告"
    chat_mod._write_active_turn_sidecar(sidecar_turn)

    async def run():
        await chat_mod._watch_inflight_tasks(
            sid, fake_client, {"task_resume": "research"})

    try:
        asyncio.run(run())
        assert len(fake_client.queries) == 1
        sent = fake_client.queries[0][0]
        assert sent["type"] == "user"
        assert sent["isMeta"] is True
        assert "provide the final response" in sent["message"]["content"]

        bc = chat_mod._recent_turns.get(sid)
        assert bc is not None
        kinds = [event.get("event") for event in bc.events]
        assert kinds.count("task_notification") == 1
        assert "text" in kinds
        done = json.loads(bc.events[-1]["data"])
        assert bc.events[-1]["event"] == "done"
        assert not done.get("is_error")
        assert sid not in chat_mod._sessions_with_inflight_tasks
        assert not chat_mod._active_turn_path(sid).exists()
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)
        chat_mod._task_watchers.pop(sid, None)
        chat_mod._active_turns.pop(sid, None)
        chat_mod._recent_turns.pop(sid, None)
        chat_mod._delete_active_turn_sidecar(sid)


def test_active_turn_sidecar_survives_detached_background_gap(stream_env):
    """A main Result is not clean completion while its watcher is alive."""
    import asyncio

    chat_mod = stream_env
    sid = "sid-sidecar-background-gap"
    turn = chat_mod.TurnBroadcast(
        session_id=sid, model="claude-sonnet-4-6")
    turn.user_text = "生成最终报告"
    chat_mod._write_active_turn_sidecar(turn)
    chat_mod._sessions_with_inflight_tasks[sid] = {"task_gap"}

    async def run():
        blocker = asyncio.create_task(asyncio.sleep(60))
        chat_mod._task_watchers[sid] = blocker
        try:
            assert chat_mod._delete_active_turn_sidecar_if_idle(sid) is False
            assert chat_mod._active_turn_path(sid).exists()
        finally:
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)
        chat_mod._task_watchers.pop(sid, None)
        chat_mod._delete_active_turn_sidecar(sid)


def test_watcher_marks_incomplete_if_explicit_resume_also_ends(stream_env):
    """A second EOF is an explicit incomplete state, never silent success."""
    import asyncio

    chat_mod = stream_env
    sid = "sid-watch-resume-incomplete"
    chat_mod._sessions_with_inflight_tasks[sid] = {"task_incomplete"}
    notif = TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="task_incomplete",
        status="completed", output_file=None, summary="done",
        uuid="u-incomplete", session_id=sid, tool_use_id="tu-incomplete")
    fake_client = _BatchedWatchClient([[notif], []])

    async def run():
        await chat_mod._watch_inflight_tasks(
            sid, fake_client, {"task_incomplete": "research"})

    try:
        asyncio.run(run())
        assert len(fake_client.queries) == 1
        bc = chat_mod._recent_turns.get(sid)
        assert bc is not None
        done = json.loads(bc.events[-1]["data"])
        assert done["is_error"] is True
        assert done["kind"] == "background_continuation_incomplete"
        assert "没有生成最终答复" in done["error"]
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)
        chat_mod._task_watchers.pop(sid, None)
        chat_mod._active_turns.pop(sid, None)
        chat_mod._recent_turns.pop(sid, None)


def test_watcher_settles_from_terminal_task_updated_without_notification(stream_env):
    """SDK 0.2.101+ may report a terminal task only via task_updated."""
    import asyncio

    chat_mod = stream_env
    sid = "sid-watch-updated"
    chat_mod._sessions_with_inflight_tasks[sid] = {"task_killed"}
    updated = TaskUpdatedMessage(
        subtype="task_updated",
        data={},
        task_id="task_killed",
        patch={"status": "killed", "summary": "Stopped by user"},
        status="killed",
        session_id=sid,
        uuid="u-updated",
    )
    fake_client = _FakeWatchClient([updated])

    async def run():
        await chat_mod._watch_inflight_tasks(
            sid, fake_client, {"task_killed": "long command"})

    try:
        asyncio.run(run())
        bc = chat_mod._recent_turns.get(sid)
        assert bc is not None
        terminal_event = next(
            event for event in bc.events if event.get("event") == "task_notification"
        )
        payload = json.loads(terminal_event["data"])
        assert payload["task_id"] == "task_killed"
        assert payload["status"] == "stopped"
        assert payload["tool_use_id"] is None
        assert sid not in chat_mod._sessions_with_inflight_tasks
        assert sid not in chat_mod._task_watchers
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)
        chat_mod._task_watchers.pop(sid, None)
        chat_mod._active_turns.pop(sid, None)
        chat_mod._recent_turns.pop(sid, None)


def test_watcher_opens_continuation_from_usertext_notification(stream_env):
    """DEFENSIVE FALLBACK path (corrected 2026-06-03, spec §13): a bg task's
    terminal completion arrives as a plain UserMessage whose content IS the
    <task-notification> XML instead of a typed TaskNotificationMessage. NOTE:
    the clean-test ground truth is that idle Bash bg completion is delivered
    TYPED (covered by test_watcher_opens_continuation_turn_and_unpins); the
    earlier "completion is user-text" claim was a contamination artifact. This
    user-text branch is kept as a fallback (future SDK / Agent-task shapes), and
    must still open the headless continuation, publish the card-flip event
    (parsed from the XML), stream the auto-continue reaction, and release the
    pin."""
    import asyncio

    chat_mod = stream_env

    sid = "sid-watch-text"
    chat_mod._sessions_with_inflight_tasks[sid] = {"b0xdpx1hv"}
    # Verbatim shape of the persisted/streamed completion record.
    notif = UserMessage(content=(
        "<task-notification>\n"
        "<task-id>b0xdpx1hv</task-id>\n"
        "<tool-use-id>toolu_01Q3bMNFQf3HAgjZ3mVoMeeo</tool-use-id>\n"
        "<output-file>/tmp/claude-1000/x/" + sid + "/tasks/b0xdpx1hv.output</output-file>\n"
        "<status>completed</status>\n"
        "<summary>Background command \"Sleep 60s\" completed (exit code 0)</summary>\n"
        "</task-notification>"))
    reaction = AssistantMessage(
        content=[TextBlock(text="后台任务完成 ✅ output 正常。")],
        model="claude-sonnet-4-6", usage={})
    result = ResultMessage(
        subtype="success", duration_ms=120, duration_api_ms=100,
        is_error=False, num_turns=1, session_id=sid,
        total_cost_usd=0.0, usage={})
    fake_client = _FakeWatchClient([notif, reaction, result])

    async def run():
        await chat_mod._watch_inflight_tasks(
            sid, fake_client, {"b0xdpx1hv": "Sleep 60s"})

    try:
        asyncio.run(run())
        assert sid not in chat_mod._active_turns
        bc = chat_mod._recent_turns.get(sid)
        assert bc is not None, "continuation broadcast not grace-kept"
        assert bc.is_continuation is True
        kinds = [e.get("event") for e in bc.events]
        assert "task_notification" in kinds, f"no card flip: {kinds}"
        assert "text" in kinds, f"no reaction text streamed live: {kinds}"
        assert kinds[-1] == "done", f"missing terminal done: {kinds}"
        notif_ev = next(e for e in bc.events
                        if e.get("event") == "task_notification")
        payload = json.loads(notif_ev["data"])
        assert payload["task_id"] == "b0xdpx1hv"
        assert payload["tool_use_id"] == "toolu_01Q3bMNFQf3HAgjZ3mVoMeeo"
        assert payload["status"] == "completed"
        assert payload["output_file"].endswith("/tasks/b0xdpx1hv.output")
        # Reaction text really made it into the live stream.
        texts = [json.loads(e["data"]).get("text", "")
                 for e in bc.events if e.get("event") == "text"]
        assert any("✅" in t for t in texts), texts
        assert sid not in chat_mod._sessions_with_inflight_tasks
        assert sid not in chat_mod._task_watchers
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)
        chat_mod._task_watchers.pop(sid, None)
        chat_mod._active_turns.pop(sid, None)
        chat_mod._recent_turns.pop(sid, None)


def test_usermsg_task_notification_text_extracts_and_guards():
    """Helper returns the text only for a UserMessage actually carrying a
    <task-notification>; everything else (assistant msgs, plain user prose,
    list-of-text-blocks without the tag) returns ""."""
    from backend import chat as chat_mod
    xml = "<task-notification><task-id>t1</task-id></task-notification>"
    # string content
    assert chat_mod._usermsg_task_notification_text(
        UserMessage(content=xml)) == xml
    # list-of-blocks content
    assert chat_mod._usermsg_task_notification_text(
        UserMessage(content=[TextBlock(text=xml)])) == xml
    # plain user prose → ""
    assert chat_mod._usermsg_task_notification_text(
        UserMessage(content="just a normal message")) == ""
    # assistant message → ""
    assert chat_mod._usermsg_task_notification_text(
        AssistantMessage(content=[TextBlock(text=xml)],
                         model="m", usage={})) == ""


def test_active_surfaces_grace_kept_continuation(stream_env, client):
    """`/active` must surface a still-fresh HEADLESS CONTINUATION from
    _recent_turns, not only live _active_turns. The continuation broadcast
    sits in _active_turns for just ~2s (while its reaction streams) before
    _close_continuation drains it to _recent_turns; the FE's 8s poller almost
    always polls AFTER that, so without this fallback the running card never
    flips live. Only continuations are surfaced — a plain finished turn must
    still report active:false (else the poller fires spurious reconnects)."""
    chat_mod = stream_env
    sid = _make_session(client)

    # 1) A grace-kept CONTINUATION → active:true, continuation:true.
    cont = chat_mod.TurnBroadcast(session_id=sid, model="")
    cont.is_continuation = True
    cont.finish()                       # sets done + finished_at = now
    chat_mod._recent_turns[sid] = cont
    try:
        r = client.get(f"/api/chat/sessions/{sid}/active",
                       headers={"X-Auth-Token": TEST_TOKEN})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["active"] is True, d
        assert d["continuation"] is True, d
        assert d["turn_id"] == cont.turn_id

        # Once a reconnect subscriber has consumed it, /active must stop
        # advertising it — otherwise the 8s poller re-reconnects every tick
        # within the 60s TTL → duplicate reaction bubbles (the live-test
        # regression). The consumed flag is what GET /stream's reconnect sets.
        cont.continuation_consumed = True
        r = client.get(f"/api/chat/sessions/{sid}/active",
                       headers={"X-Auth-Token": TEST_TOKEN})
        assert r.json()["active"] is False, r.json()
    finally:
        chat_mod._recent_turns.pop(sid, None)

    # 2) A grace-kept PLAIN turn (not a continuation) → active:false.
    plain = chat_mod.TurnBroadcast(session_id=sid, model="")
    plain.is_continuation = False
    plain.finish()
    chat_mod._recent_turns[sid] = plain
    try:
        r = client.get(f"/api/chat/sessions/{sid}/active",
                       headers={"X-Auth-Token": TEST_TOKEN})
        assert r.status_code == 200, r.text
        assert r.json()["active"] is False, r.json()
    finally:
        chat_mod._recent_turns.pop(sid, None)


def test_active_reports_background_reader_as_busy_not_attachable(
    stream_env, client,
):
    """The gap after ResultMessage is still a live logical turn.

    There is no continuation broadcast to attach to until a task settles, but
    the frontend must keep its footer active and queue follow-up input.
    """
    chat_mod = stream_env
    sid = _make_session(client)
    chat_mod._sessions_with_inflight_tasks[sid] = {"task-1"}
    original_started_at = 1_700_123_456.25
    chat_mod._background_turn_started_at[sid] = original_started_at
    chat_mod._background_origin_turn_id[sid] = "origin-turn"
    try:
        r = client.get(
            f"/api/chat/sessions/{sid}/active",
            headers={"X-Auth-Token": TEST_TOKEN},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["active"] is True
        assert data["background"] is True
        assert data["attachable"] is False
        assert data["continuation"] is False
        assert data["background_tasks_pending"] == 1
        assert data["started_at"] == original_started_at
        assert data["turn_id"] == "origin-turn"
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)
        chat_mod._background_turn_started_at.pop(sid, None)
        chat_mod._background_origin_turn_id.pop(sid, None)


def test_start_turn_allowed_while_background_task_pending(
    stream_env, client,
):
    """A pending background task must NOT block the user from sending.

    This used to raise _TurnBusy: the detached watcher was the sole reader of
    the session's SDK stream, so a concurrent turn was refused and the user's
    message was parked on the queue instead. The session pump owns the stream
    now, so the turn is allowed and the task's completion simply arrives later
    as its own message in the conversation.
    """
    chat_mod = stream_env
    sid = _make_session(client)
    chat_mod._sessions_with_inflight_tasks[sid] = {"task-1"}
    try:
        # The contract is only that we are not REFUSED as busy. Whether the
        # turn then survives is environmental — there is no real CLI behind
        # this session, so the detached pump tears the broadcast down again
        # (racing any assertion on _active_turns).
        try:
            asyncio.run(chat_mod._start_turn(sid, "new user prompt"))
        except chat_mod._TurnBusy:
            pytest.fail("a pending background task must not block a new turn")
        except Exception:
            pass
    finally:
        bc = chat_mod._active_turns.pop(sid, None)
        if bc is not None:
            bc.finish()
            bc.close()
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)


def test_turn_does_not_consume_buffered_continuation(stream_env):
    """The invariant the old _TurnBusy gate was really protecting.

    A background task's auto-continuation can land while no consumer is
    attached; the pump parks it. A turn that attaches afterwards must not
    inherit it — otherwise the follow-up renders the previous task's reply as
    the answer to the new prompt. It belongs to the watcher.
    """
    chat_mod = stream_env

    async def exercise():
        stream = chat_mod._SessionStream.__new__(chat_mod._SessionStream)
        # Build the routing state directly; a real pump needs a live CLI.
        stream._turn = None
        stream._background = None
        stream._orphans = collections.deque(maxlen=8)
        stream._closed = False
        stream._orphans.append("continuation-of-previous-task")

        turn_q = stream.attach_turn()
        assert turn_q.empty(), "a new turn must not inherit parked messages"

        bg_q = stream.attach_background()
        assert bg_q.get_nowait() == "continuation-of-previous-task"

    asyncio.run(exercise())


def test_subscribe_broadcast_marks_continuation_consumed(stream_env):
    """Attaching a reconnect subscriber to a CONTINUATION broadcast must flip
    continuation_consumed so /active stops re-advertising it. A normal turn's
    flag stays False (no effect)."""
    import asyncio
    chat_mod = stream_env

    async def drain(b):
        chunks = []
        async for ev in chat_mod._subscribe_broadcast(b):
            chunks.append(ev)
        return chunks

    # Continuation: finished broadcast → subscribe replays + sentinel, and the
    # consumed flag flips.
    cont = chat_mod.TurnBroadcast(session_id="sid-c", model="")
    cont.is_continuation = True
    cont.publish({"event": "task_notification", "data": "{}"})
    cont.finish()
    assert cont.continuation_consumed is False
    asyncio.run(drain(cont))
    assert cont.continuation_consumed is True

    # Normal turn: flag untouched.
    plain = chat_mod.TurnBroadcast(session_id="sid-p", model="")
    plain.finish()
    asyncio.run(drain(plain))
    assert plain.continuation_consumed is False


def test_broadcast_replay_compacts_100k_deltas_into_bounded_chunks(stream_env):
    """Replay stays exact without retaining one envelope per token in memory."""
    import json

    chat_mod = stream_env
    bc = chat_mod.TurnBroadcast(
        session_id="stress-replay",
        replay_max_events=128,
        replay_max_bytes=512_000,
    )
    for _ in range(100_000):
        bc.publish({"event": "text", "data": '{"text":"x"}'})

    replay = list(bc.replay_events())
    assert len(bc.events) <= 20
    assert sum(
        len(json.loads(event["data"])["text"])
        for event in replay
    ) == 100_000
    assert bc._replay_bytes <= 512_000


def test_mobile_and_desktop_both_receive_complete_turn(stream_env):
    """Mobile is no longer a truncated second-class subscriber.

    This used to assert the opposite: past a 512-event replay window a mobile
    subscriber was handed `resync("replay_truncated")` and no live stream at
    all. The window existed only because every token delta was spooled, so a
    normal reply blew past it in seconds. The spool now records one coalesced
    event per message, so mobile and desktop get byte-identical replays.
    """
    import asyncio

    chat_mod = stream_env

    async def exercise():
        bc = chat_mod.TurnBroadcast(session_id="stress-subscribers")
        mobile_live = bc.subscribe(mobile=True)
        desktop_live = bc.subscribe()
        stalled_live = bc.subscribe()
        mobile_received = []
        desktop_received = []

        async def consume(subscriber, target):
            while True:
                event = await subscriber.get()
                if event is None:
                    return
                target.append(event)

        consumers = [
            asyncio.create_task(consume(mobile_live, mobile_received)),
            asyncio.create_task(consume(desktop_live, desktop_received)),
        ]
        for i in range(2_000):
            bc.publish({
                "event": "tool_result",
                "data": '{"id":"%d"}' % i,
            })
            await asyncio.sleep(0)
        bc.finish()
        await asyncio.gather(*consumers)

        assert len(mobile_received) == 2_000
        assert len(desktop_received) == 2_000
        assert stalled_live.qsize() == 0
        stalled_received = []
        while True:
            event = await stalled_live.get()
            if event is None:
                break
            stalled_received.append(event)
        assert len(stalled_received) == 2_000
        assert len(bc.events) == 2_000

        # A late mobile subscriber replays the whole turn, exactly like desktop.
        mobile_replay = bc.subscribe(mobile=True)
        mobile_replayed = []
        while True:
            event = await mobile_replay.get()
            if event is None:
                break
            mobile_replayed.append(event)
        assert len(mobile_replayed) == 2_000
        assert all(event["event"] != "resync" for event in mobile_replayed)

        desktop_replay = bc.subscribe()
        replayed = []
        while True:
            event = await desktop_replay.get()
            if event is None:
                break
            replayed.append(event)
        assert replayed == list(bc.events)
        assert len(replayed) == 2_000

    asyncio.run(exercise())


def test_token_deltas_are_coalesced_into_one_spool_event(stream_env):
    """Replay length must scale with MESSAGES, not tokens.

    This is the core of the fix. The spool used to take one entry per token
    delta whenever any subscriber was attached, so a single ordinary reply
    produced tens of thousands of replay events — which is what the old
    512-event mobile window was really reacting to.
    """
    import asyncio
    import json

    chat_mod = stream_env

    async def exercise():
        bc = chat_mod.TurnBroadcast(session_id="coalesce")
        # Attached — this is precisely the condition that used to force a
        # spool write per delta.
        live = bc.subscribe()
        for i in range(500):
            bc.publish({"event": "text", "data": json.dumps({"text": f"t{i}"})})
        bc.publish({"event": "tool_result", "data": "{}"})
        bc.finish()

        # 500 deltas + 1 tool_result => 2 spool entries, not 501.
        assert len(bc.events) == 2

        replay = bc.subscribe()
        events = []
        while True:
            event = await replay.get()
            if event is None:
                break
            events.append(event)
        assert [e["event"] for e in events] == ["text", "tool_result"]
        assert (json.loads(events[0]["data"])["text"]
                == "".join(f"t{i}" for i in range(500)))
        # The internal marker must never reach the wire.
        assert all("_coalesced" not in e for e in events)
        live.close()
        bc.close()

    asyncio.run(exercise())


def test_attached_subscriber_gets_deltas_not_the_coalesced_duplicate(stream_env):
    """A live reader sees the message once, token by token — never twice."""
    import asyncio
    import json

    chat_mod = stream_env

    async def exercise():
        bc = chat_mod.TurnBroadcast(session_id="no-dup")
        live = bc.subscribe()
        received = []

        async def consume():
            while True:
                event = await live.get()
                if event is None:
                    return
                received.append(event)

        task = asyncio.create_task(consume())
        for chunk in ("Hel", "lo ", "world"):
            bc.publish({"event": "text", "data": json.dumps({"text": chunk})})
            await asyncio.sleep(0)
        bc.publish({"event": "tool_result", "data": "{}"})
        bc.finish()
        await task

        texts = [json.loads(e["data"])["text"]
                 for e in received if e["event"] == "text"]
        # Three deltas and no fourth, coalesced copy of the same sentence.
        assert texts == ["Hel", "lo ", "world"]
        bc.close()

    asyncio.run(exercise())


def test_mid_message_join_receives_the_head_it_missed(stream_env):
    """Attaching while a bubble is streaming must not start mid-word.

    This is the case that used to strand mobile clients: reconnecting during a
    long reply meant no live stream at all until the turn ended.
    """
    import asyncio
    import json

    chat_mod = stream_env

    async def exercise():
        bc = chat_mod.TurnBroadcast(session_id="mid-join")
        early = bc.subscribe()
        bc.publish({"event": "text", "data": json.dumps({"text": "Hello "})})
        await asyncio.sleep(0)
        # Half the bubble has already streamed when this client shows up.
        late = bc.subscribe()
        bc.publish({"event": "text", "data": json.dumps({"text": "world"})})
        bc.publish({"event": "done", "data": "{}"})
        bc.finish()

        async def drain(subscriber):
            out = []
            while True:
                event = await subscriber.get()
                if event is None:
                    return out
                out.append(event)

        def text_of(events):
            return "".join(json.loads(e["data"])["text"]
                           for e in events if e["event"] == "text")

        assert text_of(await drain(late)) == "Hello world"
        assert text_of(await drain(early)) == "Hello world"
        bc.close()

    asyncio.run(exercise())


def test_attached_reader_receives_text_delta_without_waiting_for_chunk(stream_env):
    """Disk-backed replay must preserve live token cadence for active readers."""
    import asyncio
    import json

    chat_mod = stream_env

    async def exercise():
        bc = chat_mod.TurnBroadcast(session_id="live-text-cadence")
        subscriber = bc.subscribe()
        bc.publish({"event": "text", "data": json.dumps({"text": "now"})})
        event = await asyncio.wait_for(subscriber.get(), timeout=1)
        assert event["event"] == "text"
        assert json.loads(event["data"])["text"] == "now"
        bc.finish()

    asyncio.run(exercise())


def test_desktop_replay_boundary_delivers_live_tail_once(stream_env):
    """Events published while replay drains are neither lost nor duplicated."""
    import asyncio
    import json

    chat_mod = stream_env

    async def exercise():
        bc = chat_mod.TurnBroadcast(
            session_id="replay-live-boundary",
            subscriber_max_events=32,
            subscriber_max_bytes=4096,
        )
        for i in range(10):
            bc.publish({"event": "tool_result", "data": json.dumps({"id": i})})
        subscriber = bc.subscribe()
        first = await subscriber.get()
        for i in range(10, 20):
            bc.publish({"event": "tool_result", "data": json.dumps({"id": i})})
        bc.finish()
        received = [first]
        while True:
            event = await subscriber.get()
            if event is None:
                break
            received.append(event)
        assert [json.loads(event["data"])["id"] for event in received] == list(range(20))

    asyncio.run(exercise())


def test_headless_turn_replays_complete_output_to_desktop(stream_env):
    """A queued turn can finish before any browser attaches."""
    import asyncio

    chat_mod = stream_env
    bc = chat_mod.TurnBroadcast(
        session_id="headless-desktop-replay",
        replay_max_events=4,
        replay_max_bytes=256,
        subscriber_max_events=2,
        subscriber_max_bytes=128,
    )
    for i in range(100):
        event = {"event": "tool_result", "data": '{"id":"%d"}' % i}
        bc.publish(event)
    bc.finish()

    async def collect():
        subscriber = bc.subscribe()
        replayed = []
        while True:
            event = await subscriber.get()
            if event is None:
                return replayed
            replayed.append(event)

    replayed = asyncio.run(collect())
    payloads = [json.loads(event["data"]) for event in replayed]
    assert [payload["id"] for payload in payloads] == [
        str(i) for i in range(100)
    ]
    assert {payload["turn_id"] for payload in payloads} == {bc.turn_id}
    assert [payload["event_seq"] for payload in payloads] == list(range(1, 101))


def test_turn_broadcast_stamps_stable_identity_and_sequence(stream_env):
    chat_mod = stream_env
    bc = chat_mod.TurnBroadcast(session_id="turn-protocol")
    bc.parent_turn_id = "parent-turn"
    bc.publish({"event": "thinking", "data": '{"text":"a"}'})
    bc.publish({"event": "tool_use", "data": '{"name":"Read"}'})
    bc.finish()

    payloads = [json.loads(event["data"]) for event in bc.events]
    assert [payload["event_seq"] for payload in payloads] == [1, 2]
    assert all(payload["turn_id"] == bc.turn_id for payload in payloads)
    assert all(payload["parent_turn_id"] == "parent-turn" for payload in payloads)


def test_reconnect_turn_id_mismatch_resyncs_instead_of_attaching(
        stream_env, client):
    """A late reconnect for A must never consume newer turn B's replay."""
    chat_mod = stream_env
    sid = _make_session(client)
    bc = chat_mod.TurnBroadcast(session_id=sid)
    bc.publish({"event": "text", "data": '{"text":"turn B"}'})
    chat_mod._active_turns[sid] = bc
    try:
        response = client.post(
            "/api/chat/stream/start",
            headers={"X-Auth-Token": TEST_TOKEN},
            json={
                "prompt": "",
                "session_id": sid,
                "turn_id": "stale-turn-A",
            },
        )
        assert response.status_code == 200, response.text
        ticket = response.json()["ticket"]
        streamed = client.get(f"/api/chat/stream?ticket={ticket}")
        events = _parse_sse(streamed.text)
        payload = next(json.loads(data) for event, data in events
                       if event == "resync")
        assert payload["reason"] == "turn_changed"
        assert payload["requested_turn_id"] == "stale-turn-A"
        assert payload["current_turn_id"] == bc.turn_id
        assert all(event != "text" for event, _ in events)
    finally:
        chat_mod._active_turns.pop(sid, None)
        bc.close()


def test_stream_ticket_replays_complete_turn_for_mobile_and_desktop(
        stream_env, client):
    """End-to-end counterpart of the unit test above: a `mobile: true` ticket
    replays the same complete turn a desktop ticket does, with no resync."""
    chat_mod = stream_env
    sid = _make_session(client)
    bc = chat_mod.TurnBroadcast(session_id=sid)
    for i in range(10):
        bc.publish({"event": "tool_result", "data": '{"id":"%d"}' % i})
    bc.finish()
    chat_mod._recent_turns[sid] = bc

    try:
        def mint(mobile):
            response = client.post(
                "/api/chat/stream/start",
                headers={"X-Auth-Token": TEST_TOKEN},
                json={
                    "prompt": "",
                    "session_id": sid,
                    "model": "claude-sonnet-4-6",
                    "mobile": mobile,
                },
            )
            assert response.status_code == 200, response.text
            return response.json()["ticket"]

        mobile_response = client.get(f"/api/chat/stream?ticket={mint(True)}")
        mobile_events = _parse_sse(mobile_response.text)
        assert sum(event == "tool_result" for event, _ in mobile_events) == 10
        assert all(event != "resync" for event, _ in mobile_events)

        desktop_response = client.get(f"/api/chat/stream?ticket={mint(False)}")
        desktop_events = _parse_sse(desktop_response.text)
        assert sum(event == "tool_result" for event, _ in desktop_events) == 10
        assert all(event != "resync" for event, _ in desktop_events)
    finally:
        chat_mod._recent_turns.pop(sid, None)
        bc.close()


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
    from backend import activity as activity_module

    chat_mod = stream_env
    sid = _make_session(client)
    activity_transitions = []

    async def boom_get_client(session_id, model, permission="bypassPermissions", effort=""):
        from claude_agent_sdk import ClaudeSDKError
        raise ClaudeSDKError("Claude model requires auth: run `claude login`")

    monkeypatch.setattr(chat_mod, "get_client", boom_get_client)
    monkeypatch.setattr(
        activity_module.activity,
        "start",
        lambda activity_sid, *, summary="": activity_transitions.append(
            ("start", activity_sid, summary)),
    )
    monkeypatch.setattr(
        activity_module.activity,
        "finish",
        lambda activity_sid, status: activity_transitions.append(
            ("finish", activity_sid, status)),
    )

    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=hi&model=claude-sonnet-4-6")
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    err = next((json.loads(d) for e, d in events if e == "error"), None)
    assert err is not None, f"no error frame: {events}"
    assert err["kind"] == "auth"
    assert sid not in chat_mod._active_turns
    assert activity_transitions == [
        ("start", sid, "hi"),
        ("finish", sid, "failed"),
    ]


def _ok_turn(sid):
    """Minimal successful SDK turn: one text block + a success result."""
    return [
        AssistantMessage(
            content=[TextBlock(text="ok")],
            model="claude-sonnet-4-6",
            usage={"input_tokens": 1, "output_tokens": 1},
        ),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.0,
            usage={"input_tokens": 1, "output_tokens": 1},
        ),
    ]


def test_turn_has_no_wall_clock_cap_by_default(stream_env, client, monkeypatch):
    """A turn must run unbounded unless an operator opts in.

    The old hard 1800s cap couldn't tell "wedged" from "busy", so it killed
    turns that were actively producing output — and the kill was total loss,
    because the SDK only writes the JSONL on completion and the abort reason
    only ever existed as a live SSE frame. Pin the absence of a deadline
    directly: `asyncio.timeout(None)` arms nothing.
    """
    import asyncio as _asyncio
    chat_mod = stream_env
    sid = _make_session(client)
    seen: list[object] = []
    real_timeout = _asyncio.timeout

    def spy(delay):
        seen.append(delay)
        return real_timeout(delay)

    monkeypatch.setattr(_asyncio, "timeout", spy)
    monkeypatch.delenv("MUSELAB_TURN_TIMEOUT_S", raising=False)

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort=""):
        return _FakeStreamClient(_ok_turn(sid))

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=hi&model=claude-sonnet-4-6")
    assert r.status_code == 200, r.text
    # The turn wrapper is the only asyncio.timeout on this path, and it must
    # have been handed None. A number here means the cap got re-armed.
    assert seen, "asyncio.timeout was never called on the turn path"
    assert all(d is None for d in seen), f"a deadline was armed: {seen}"


def test_turn_cap_is_opt_in_via_env(stream_env, client, monkeypatch):
    """The escape hatch still works for anyone who wants a ceiling back."""
    chat_mod = stream_env
    sid = _make_session(client)
    monkeypatch.setenv("MUSELAB_TURN_TIMEOUT_S", "1")

    class _SlowClient(_FakeStreamClient):
        async def receive_response(self):
            import asyncio as _a
            await _a.sleep(3)          # outlives the 1s cap
            for m in self._messages:
                yield m

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort=""):
        return _SlowClient(_ok_turn(sid))

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=hi&model=claude-sonnet-4-6")
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    err = next((json.loads(d) for e, d in events if e == "error"), None)
    assert err is not None, f"no error frame: {events}"
    assert "turn exceeded" in err["error"]
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


# ---------------------------------------------------------------------------
# Background-task completion → durable card flip via JSONL history rebuild.
#
# In muselab's real flow the terminal task notification does NOT arrive as a
# typed TaskNotificationMessage on the stream; it round-trips through the
# session log as a plain user-role message whose entire content is a
# <task-notification> XML block sharing the launching tool_use's id. These
# tests pin the rebuild contract: _sdk_messages_to_ui parses that record,
# stamps the card's terminal task_status, and drops the raw XML bubble.
# ---------------------------------------------------------------------------
def _sm(uuid_, typ, content):
    return SimpleNamespace(uuid=uuid_, type=typ, message={"content": content})


def test_parse_task_notifications_happy_and_guard():
    from backend import chat as chat_mod
    block = (
        "<task-notification>\n"
        "<task-id>bribl9m26</task-id>\n"
        "<tool-use-id>toolu_01AtVR95NpYK3fMDhpp2JwzG</tool-use-id>\n"
        "<output-file>/tmp/x/bribl9m26.output</output-file>\n"
        "<status>completed</status>\n"
        '<summary>Background command "sleep" completed (exit code 0)</summary>\n'
        "</task-notification>"
    )
    recs = chat_mod._parse_task_notifications(block)
    assert len(recs) == 1
    r = recs[0]
    assert r["tool_use_id"] == "toolu_01AtVR95NpYK3fMDhpp2JwzG"
    assert r["task_id"] == "bribl9m26"
    assert r["status"] == "completed"
    assert r["output_file"].endswith("bribl9m26.output")
    assert "exit code 0" in r["summary"]

    # Guard: prose that merely MENTIONS the tag (e.g. a context summary
    # describing the protocol) must NOT be parsed as a completion record.
    prose = ("Here we publish into it the <task-notification> event so the "
             "FE flips the card. <task-notification><tool-use-id>toolu_x"
             "</tool-use-id></task-notification>")
    assert chat_mod._parse_task_notifications(prose) == []
    assert chat_mod._parse_task_notifications("") == []
    assert chat_mod._parse_task_notifications("just text") == []


def test_parse_bg_launch_happy_and_guard():
    from backend import chat as chat_mod
    # Real Bash run_in_background launch tool_result body (verbatim shape).
    body = (
        "Command running in background with ID: bj2dz0fkk. Output is being "
        "written to: /tmp/claude-1000/-home-you/SID/tasks/"
        "bj2dz0fkk.output. You will be notified when it completes. To check "
        "interim output, use Read on that file path."
    )
    got = chat_mod._parse_bg_launch(body)
    assert got is not None
    assert got["task_id"] == "bj2dz0fkk"
    assert got["output_file"].endswith("bj2dz0fkk.output")
    # Guards: unrelated tool output / empty / None must not match.
    assert chat_mod._parse_bg_launch("total 12\n-rw-r--r-- 1 u u 0 file") is None
    assert chat_mod._parse_bg_launch("") is None
    assert chat_mod._parse_bg_launch(None) is None


def test_rebuild_stamps_terminal_task_status_and_hides_xml():
    from backend import chat as chat_mod
    tuid = "toolu_01AtVR95NpYK3fMDhpp2JwzG"
    sm_list = [
        # 1) assistant turn that launched the bg bash task
        _sm("u1", "assistant", [
            {"type": "text", "text": "launching"},
            {"type": "tool_use", "id": tuid, "name": "Bash",
             "input": {"command": "sleep 25", "run_in_background": True}},
        ]),
        # 2) the completion record (plain user-string content)
        _sm("u2", "user",
            "<task-notification>\n"
            f"<tool-use-id>{tuid}</tool-use-id>\n"
            "<task-id>t1</task-id>\n"
            "<status>completed</status>\n"
            "<output-file>/tmp/t1.output</output-file>\n"
            "<summary>done</summary>\n"
            "</task-notification>"),
    ]
    out = chat_mod._sdk_messages_to_ui(sm_list, {})
    cards = [m for m in out if m.get("role") == "tool_use"]
    assert len(cards) == 1
    ts = cards[0].get("task_status")
    assert ts is not None, "card was not stamped with task_status"
    assert ts["state"] == "completed"
    assert ts["output_file"] == "/tmp/t1.output"
    assert ts["summary"] == "done"
    # The raw <task-notification> XML must NOT render as a user bubble.
    assert not any(
        m.get("role") == "user" and "task-notification" in (m.get("text") or "")
        for m in out), "raw task-notification XML leaked into a bubble"


def test_rebuild_failed_status_maps_through():
    from backend import chat as chat_mod
    tuid = "toolu_fail"
    sm_list = [
        _sm("u1", "assistant", [
            {"type": "tool_use", "id": tuid, "name": "Bash",
             "input": {"command": "false", "run_in_background": True}},
        ]),
        _sm("u2", "user",
            "<task-notification>"
            f"<tool-use-id>{tuid}</tool-use-id>"
            "<status>failed</status>"
            "</task-notification>"),
    ]
    out = chat_mod._sdk_messages_to_ui(sm_list, {})
    card = next(m for m in out if m.get("role") == "tool_use")
    assert card["task_status"]["state"] == "failed"


def test_rebuild_hides_only_cli_interrupt_user_messages():
    from backend import chat as chat_mod

    marker_string = _sm(
        "interrupt-string", "user", "[Request interrupted by user]")
    marker_list = _sm("interrupt-list", "user", [
        {"type": "text", "text": "[Request interrupted by user for tool use]"},
    ])
    real_user = _sm("real-user", "user", [
        {"type": "text", "text": "Please explain user interruption handling"},
    ])

    out = chat_mod._sdk_messages_to_ui(
        [marker_string, marker_list, real_user], {})
    assert [m["text"] for m in out if m.get("role") == "user"] == [
        "Please explain user interruption handling",
    ]
    assert chat_mod._is_real_user_prompt(marker_string) is False
    assert chat_mod._is_real_user_prompt(marker_list) is False
    assert chat_mod._is_real_user_prompt(real_user) is True


# --- GET /api/chat/task-output (serve bg-task .output from /tmp) ---------

def _make_task_output(tmp_path, sid, name="abc.output", body="task stdout\n"):
    """Build a real file at a path that matches the endpoint's tasks-dir
    shape: /tmp/claude-<digits>/<project>/<sid>/tasks/<name>.output. We can't
    use pytest's tmp_path for the served path itself (the regex hard-codes the
    /tmp/claude-<uid> prefix), so create it under a unique /tmp subtree and
    clean it up by hand."""
    import os
    base = f"/tmp/claude-99999/testproj-{os.getpid()}/{sid}/tasks"
    os.makedirs(base, exist_ok=True)
    p = f"{base}/{name}"
    with open(p, "w") as f:
        f.write(body)
    return p


def test_task_output_serves_valid_path(client, auth, tmp_path):
    sid = "1fc3ce90-e7f3-4726-b21c-4a8a85287037"
    p = _make_task_output(tmp_path, sid, body="hello from bg task\n")
    try:
        r = client.get("/api/chat/task-output",
                       params={"session_id": sid, "path": p}, headers=auth)
        assert r.status_code == 200, r.text
        assert r.text == "hello from bg task\n"
    finally:
        import os
        os.remove(p)


def test_task_output_rejects_foreign_session(client, auth, tmp_path):
    """A path whose embedded session segment isn't the requested session_id
    must be rejected (the regex pins THIS session)."""
    sid = "aaaaaaaa-0000-0000-0000-000000000000"
    p = _make_task_output(tmp_path, "bbbbbbbb-1111-1111-1111-111111111111")
    try:
        r = client.get("/api/chat/task-output",
                       params={"session_id": sid, "path": p}, headers=auth)
        assert r.status_code == 400, r.text
    finally:
        import os
        os.remove(p)


def test_task_output_rejects_traversal_and_bad_shape(client, auth):
    sid = "1fc3ce90-e7f3-4726-b21c-4a8a85287037"
    for bad in (
        f"/tmp/claude-1/proj/{sid}/tasks/../../../etc/passwd",
        "/etc/passwd",
        f"/tmp/claude-1/proj/{sid}/tasks/abc.txt",   # wrong suffix
        f"/home/x/{sid}/tasks/abc.output",           # not /tmp/claude-<n>
    ):
        r = client.get("/api/chat/task-output",
                       params={"session_id": sid, "path": bad}, headers=auth)
        assert r.status_code == 400, (bad, r.text)


def test_task_output_404_when_missing(client, auth):
    sid = "1fc3ce90-e7f3-4726-b21c-4a8a85287037"
    p = f"/tmp/claude-99999/proj/{sid}/tasks/does-not-exist.output"
    r = client.get("/api/chat/task-output",
                   params={"session_id": sid, "path": p}, headers=auth)
    assert r.status_code == 404, r.text


def test_task_output_requires_token(client):
    sid = "1fc3ce90-e7f3-4726-b21c-4a8a85287037"
    p = f"/tmp/claude-99999/proj/{sid}/tasks/abc.output"
    r = client.get("/api/chat/task-output",
                   params={"session_id": sid, "path": p})
    assert r.status_code in (401, 403), r.text


def test_preflight_compact_announces_itself_over_sse(stream_env, client, monkeypatch):
    """The auto-compact must be visible while it runs, not only in the logs.

    2026-07-25: a 186229/200000 session sat behind the generic "thinking"
    bubble for 9m19s of /compact and then died. The FE has a dedicated 📦
    placeholder driven by the per-tab `compacting` flag; these events are what
    turn it on and off for a compact the user didn't ask for.
    """
    chat_mod = stream_env
    sid = _make_session(client)
    compact_error = ResultMessage(
        subtype="error", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id=sid,
        result="Your input exceeds the context window of this model",
        api_error_status=400,
    )
    fake = _FakeStreamClient([compact_error])

    async def near_limit_context():
        return {
            "maxTokens": 200_000,
            "rawMaxTokens": 200_000,
            "autoCompactThreshold": 160_000,
            "totalTokens": 190_000,
        }

    fake.get_context_usage = near_limit_context

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort=""):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=must-not-send&model=claude-sonnet-4-6")
    events = _parse_sse(r.text)
    prog = [json.loads(d) for e, d in events if e == "compact_progress"]

    assert [p["phase"] for p in prog] == ["start", "end"]
    # "start" carries the numbers that justify the wait — the FE toasts them.
    assert prog[0]["source"] == "auto"
    assert prog[0]["used"] == 190_000
    assert prog[0]["limit"] == 200_000
    # Terminal phase precedes the error event, so the bubble stops before the
    # failure toast rather than spinning under it.
    assert prog[1]["ok"] is False
    kinds = [e for e, _ in events]
    assert kinds.index("compact_progress") < kinds.index("error")
    assert kinds.count("compact_progress") == 2


def test_sdk_command_reads_through_the_session_pump(stream_env):
    """A slash command must not open a SECOND iterator over the client stream.

    Regression for 2026-07-26. `_SessionStream`'s pump has owned
    `receive_messages()` since client creation, so the `receive_response()`
    this helper used to open lost every race: /compact's ResultMessage was
    routed to the pump and parked in `_orphans`, and the helper waited on a
    stream nobody was feeding. Two auto-compacts reported failure after 600s
    and 9m19s while the transcript shows both compactions had finished in
    ~150s — `query()` is a pure transport write, so the command always ran.
    """
    chat_mod = stream_env
    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id="sid", uuid="r1")

    class PumpedClient:
        """Only `receive_messages()` works — `receive_response()` is a trap."""

        def __init__(self):
            self.queries = []
            self._q = asyncio.Queue()

        async def query(self, prompt):
            self.queries.append(prompt)
            self._q.put_nowait(result)

        async def receive_messages(self):
            while True:
                yield await self._q.get()

        async def receive_response(self):
            raise AssertionError(
                "opened a second iterator instead of attaching to the pump")
            yield  # pragma: no cover — keeps this an async generator

    async def go():
        fake = PumpedClient()
        chat_mod._ensure_session_stream(("sid", "m", ""), fake)
        try:
            # The bug's signature was a hang, so the bound is the assertion.
            got = await asyncio.wait_for(
                chat_mod._run_sdk_command_checked(fake, "/compact"), 5)
        finally:
            await chat_mod._drop_session_streams("sid")
        assert fake.queries == ["/compact"]
        return got

    assert asyncio.run(go()) is result


def test_failed_session_stream_evicts_dead_cached_client(stream_env):
    """A parser/transport failure must not poison every later turn.

    Regression for the GLM missing-thinking-signature failure: the pump closed,
    but `_clients[key]` still pointed at the terminated CLI process. Forking
    appeared to fix the conversation only because the new session id missed
    that stale cache entry.
    """
    chat_mod = stream_env

    class BrokenClient:
        def __init__(self):
            self.release = asyncio.Event()
            self.disconnected = False

        async def receive_messages(self):
            await self.release.wait()
            raise RuntimeError("missing thinking signature")
            yield  # pragma: no cover

        async def disconnect(self):
            self.disconnected = True

    async def go():
        key = ("dead-session", "glm-5.2-internal", "")
        client = BrokenClient()
        async with chat_mod._lock:
            chat_mod._clients[key] = client
            chat_mod._client_permission[key] = "bypassPermissions"
            chat_mod._client_lru.append(key)
        stream = chat_mod._SessionStream(key, client)
        chat_mod._session_streams[key] = stream
        queue = stream.attach_turn()
        client.release.set()

        marker = await asyncio.wait_for(queue.get(), timeout=1)
        await asyncio.wait_for(stream.task, timeout=1)

        assert marker is chat_mod._STREAM_EOF
        assert isinstance(stream._failure, RuntimeError)
        assert key not in chat_mod._clients
        assert key not in chat_mod._client_permission
        assert key not in chat_mod._client_lru
        assert key not in chat_mod._session_streams
        assert client.disconnected is True

    asyncio.run(go())


def test_park_unconsumed_hands_leftovers_back_to_the_orphan_park(stream_env):
    """Stopping at our own Result must not swallow what the pump queued after it.

    A slash command breaks on its ResultMessage, but the pump routes
    everything to the attached queue — a background task's notification can
    already be sitting behind it. Letting the queue fall out of scope would
    drop that message with no trace.
    """
    chat_mod = stream_env
    later = TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="t", status="completed",
        output_file="", summary="done", uuid="bg", session_id="sid")

    class IdleClient:
        async def receive_messages(self):
            await asyncio.Event().wait()
            yield  # pragma: no cover — never reached

    async def go():
        stream = chat_mod._SessionStream(("sid", "m", ""), IdleClient())
        try:
            q = stream.attach_turn()
            q.put_nowait(later)
            q.put_nowait(chat_mod._STREAM_EOF)
            stream.detach_turn(q)
            stream.park_unconsumed(q)
        finally:
            await stream.aclose()
        return list(stream._orphans)

    # EOF is a wake-up sentinel, not a message — it must not be re-parked.
    assert asyncio.run(go()) == [later]


def test_preflight_compact_trusts_the_token_count_over_the_verdict(
        stream_env, client, monkeypatch):
    """A compaction that succeeded must not lose the turn it made room for.

    How the command REPORTS itself is a hint; whether the context shrank is
    the fact. On 2026-07-26 the two were opposites — /compact finished in
    ~150s and freed the window, then the read side timed out and the preflight
    killed the user's prompt anyway. Belt and braces for the pump fix above:
    even if the verdict is lost again, an observably smaller context wins.
    """
    chat_mod = stream_env
    sid = _make_session(client)
    compact_error = ResultMessage(
        subtype="error", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id=sid,
        result="/compact ended without a ResultMessage", api_error_status=None)
    answer = [
        AssistantMessage(
            content=[TextBlock(text="room to think")],
            model="claude-sonnet-4-6", uuid="a1",
            usage={"input_tokens": 4, "output_tokens": 2}),
        ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=9,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.01, usage={"input_tokens": 4, "output_tokens": 2},
            result="room to think", uuid="r1"),
    ]
    fake = _FakeBatchedStreamClient([[compact_error], answer])
    reads = []

    async def shrinking_context():
        # First read is the preflight's trigger; every later read sees the
        # compaction's effect, which is what the recovery hinges on.
        reads.append(len(reads))
        return {
            "maxTokens": 200_000, "rawMaxTokens": 200_000,
            "autoCompactThreshold": 160_000,
            "totalTokens": 190_000 if len(reads) == 1 else 50_000,
        }

    fake.get_context_usage = shrinking_context

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort=""):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=still-send-me&model=claude-sonnet-4-6")
    events = _parse_sse(r.text)
    kinds = [e for e, _ in events]
    prog = [json.loads(d) for e, d in events if e == "compact_progress"]

    assert "error" not in kinds
    assert prog[-1]["phase"] == "end" and prog[-1]["ok"] is True
    assert prog[-1]["used"] == 50_000
    # The prompt survived the scare — it was sent after the compact, not dropped.
    assert fake.queried[0] == "/compact"
    assert len(fake.queried) == 2
