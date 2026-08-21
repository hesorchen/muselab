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
import inspect
import json
import threading
from types import SimpleNamespace

import pytest
from claude_agent_sdk import (
    AssistantMessage, UserMessage, ResultMessage, StreamEvent,
    TextBlock, ToolUseBlock, ToolResultBlock,
    TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage,
    TaskUpdatedMessage, SystemMessage,
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

    # No real JSONL transcript. Model a committed current-turn boundary by
    # default; individual failure tests override this resolver explicitly.
    monkeypatch.setattr(chat_mod, "_get_session_msgs", lambda sid, model="": [])
    monkeypatch.setattr(
        chat_mod, "_real_turn_uuids_from_boundary_for_test",
        chat_mod._turn_uuids_from_boundary, raising=False)
    monkeypatch.setattr(
        chat_mod,
        "_turn_uuids_from_boundary",
        lambda *_args, **_kwargs: (
            "fixture-canonical-assistant", "fixture-canonical-user", True),
    )
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

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort="", service_tier=""):
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
    done = next(
        json.loads(data)
        for event, data in _parse_sse(response.text)
        if event == "done"
    )
    assert done["is_error"] is False
    assert done["assistant_uuid"] == "fixture-canonical-assistant"


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


@pytest.mark.asyncio
async def test_queue_drain_replays_one_wakeup_coalesced_during_rollover(
        app_module, monkeypatch):
    """A second enqueue during a long drain must not strand on the source."""
    from backend import chat as chat_mod

    sid = "queue-rekick-during-rollover"
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_finished = asyncio.Event()
    calls = 0

    async def fake_drain(got_sid):
        nonlocal calls
        assert got_sid == sid
        calls += 1
        if calls == 1:
            first_entered.set()
            await release_first.wait()
        else:
            second_finished.set()

    monkeypatch.setattr(chat_mod, "_maybe_drain_queue", fake_drain)
    chat_mod._schedule_queue_drain(sid)
    await asyncio.wait_for(first_entered.wait(), timeout=1)

    # Multiple triggers while the owner is running collapse to one retained
    # level-triggered retry, rather than one task per click/request.
    chat_mod._schedule_queue_drain(sid)
    chat_mod._schedule_queue_drain(sid)
    assert sid in chat_mod._queue_drain_rekicks
    assert calls == 1

    release_first.set()
    await asyncio.wait_for(second_finished.wait(), timeout=1)
    await asyncio.sleep(0)
    assert calls == 2
    assert sid not in chat_mod._queue_drain_rekicks


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

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort="", service_tier=""):
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
    assert done["turn_id"]
    assert done["total_cost_usd"] == pytest.approx(0.0042)
    assert done["model"] == "claude-sonnet-4-6"
    assert done["cancelled"] is False
    assert done["activity_source"] == "direct"
    assert done["duration_ms"] == 1500
    assert done["assistant_uuid"] == "assistant-final-uuid"
    assert isinstance(done["completed_at_ms"], int)
    assert done["completed_at_ms"] > 0
    assert "session_usage" in done
    annotations = chat_mod.sess.get_message_annotations(sid)
    assert annotations["assistant-final-uuid"]["ts"] == done["completed_at_ms"]
    assert annotations["assistant-final-uuid"]["elapsed_s"] == 1.5
    assert annotations["assistant-final-uuid"]["turn_status"] == "completed"

    # Turn reservation released after completion.
    assert sid not in chat_mod._active_turns


def test_tool_only_turn_persists_completion_annotation(
        stream_env, client, monkeypatch):
    """Completion metadata must survive turns with no streamed assistant text."""
    chat_mod = stream_env
    sid = _make_session(client)
    assistant_uuid = "assistant-tool-only-uuid"
    recall = {
        "id": "recall-tool-tail", "count": 1, "status": "ok",
        "latency_ms": 4, "items": [{
            "id": "memory-1", "kind": "preference",
            "content": "A non-private regression fixture",
        }],
    }
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
        session_id, model, permission="bypassPermissions", effort="", service_tier="",
    ):
        return _FakeStreamClient(messages)

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(
        chat_mod.mem0, "pop_recall_trace", lambda _sid: recall)
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
    assert done["memory_recall"] == recall
    annotations = chat_mod.sess.get_message_annotations(sid)
    assert annotations[assistant_uuid]["ts"] == done["completed_at_ms"]
    assert annotations[assistant_uuid]["elapsed_s"] == 2.5
    assert annotations[assistant_uuid]["turn_status"] == "completed"
    persisted_recall = annotations[assistant_uuid]["memory_recall"]
    assert persisted_recall == {
        "id": recall["id"], "count": 1, "latency_ms": 4, "status": "ok",
        "items": [{"id": "memory-1", "kind": "preference"}],
    }
    assert "content" not in persisted_recall["items"][0]
    persisted = chat_mod._RawMsg(
        assistant_uuid,
        "assistant",
        {"content": [{
            "type": "tool_use", "id": "tu_tool_only", "name": "Read",
            "input": {"file_path": "/tmp/tool-only.txt"},
        }]},
    )
    shaped = chat_mod._sdk_messages_to_ui([persisted], annotations)
    assert shaped[-1]["role"] == "tool_use"
    assert shaped[-1]["model"] == "claude-sonnet-4-6"
    assert shaped[-1]["turn_status"] == "completed"
    assert shaped[-1]["memoryRecall"] == persisted_recall
    chat_mod._complete_turn_footer_metadata(
        shaped, "claude-sonnet-4-6", has_later=False)
    assert shaped[-1]["memoryRecall"] == persisted_recall


def test_forced_interrupt_persists_refreshable_footer_and_private_snapshot(
        stream_env, client):
    """A Result-less forced stop must retain its footer after a reload."""
    chat_mod = stream_env
    sid = _make_session(client)
    bc = chat_mod.TurnBroadcast(
        session_id=sid, model="codex:gpt-5.6-sol")
    bc.user_text = "interrupt fixture"
    bc.cancelled = True
    bc.last_assistant_uuid = "assistant-interrupted-exact-uuid"
    bc.started_at = 1_700_000_000.0
    bc.cancelled_at_ms = 1_700_000_004_200
    bc.publish({
        "event": "text",
        "data": json.dumps({"text": "partial answer"}),
    })

    assert chat_mod._persist_cancelled_turn_snapshot(bc) is True

    annotations = chat_mod.sess.get_message_annotations(sid)
    footer = annotations[bc.last_assistant_uuid]
    assert footer == {
        "model": "codex:gpt-5.6-sol",
        "ts": bc.cancelled_at_ms,
        "turn_status": "cancelled",
        "elapsed_s": 4.2,
    }
    snapshots, generation = chat_mod._load_cancelled_turn_snapshots(sid)
    assert generation
    assert len(snapshots) == 1
    tail = snapshots[0]["messages"][-1]
    assert tail["role"] == "assistant"
    assert tail["text"] == "partial answer"
    assert tail["model"] == "codex:gpt-5.6-sol"
    assert tail["turn_status"] == "cancelled"
    assert tail["ts"] == bc.cancelled_at_ms
    assert tail["elapsed"] == 4.2

    path = chat_mod._cancelled_turn_snapshot_path(sid, bc.turn_id)
    assert path is not None and path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_failed_snapshot_survives_partial_canonical_assistant(
        stream_env, client, monkeypatch, tmp_path):
    """A partial JSONL assistant is not equivalent to the terminal error row."""
    chat_mod = stream_env
    sid = _make_session(client)
    bc = chat_mod.TurnBroadcast(
        session_id=sid, model="codex:gpt-5.6-sol")
    bc.user_text = "continue the long task"
    bc.started_at = 1_700_000_000.0
    bc.publish({
        "event": "text",
        "data": json.dumps({"text": "valid partial answer"}),
    })
    assert chat_mod._persist_failed_turn_snapshot(
        bc,
        "API Error: context window exceeded",
        terminal_at_ms=1_700_000_003_000,
        elapsed_s=3.0,
        canonical_terminal_published=False,
    ) is True

    # Simulate a delayed transcript flush containing this turn's legitimate
    # partial AssistantMessage. The healer may annotate it, but must not delete
    # the snapshot because canonical JSONL has no equivalent terminal error row.
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        chat_mod,
        "_ensure_transcript_index",
        lambda _sid: (transcript, {"records": []}),
    )
    monkeypatch.setattr(
        chat_mod,
        "_cancelled_snapshot_canonical_span",
        lambda *_args: (["user-current", "assistant-partial"],
                        "assistant-partial"),
    )

    snapshots, generation = chat_mod._load_cancelled_turn_snapshots(sid)
    assert generation
    assert len(snapshots) == 1
    assert [m.get("text") for m in snapshots[0]["messages"]
            if m.get("role") == "assistant"] == [
        "valid partial answer",
        "API Error: context window exceeded",
    ]
    assert chat_mod._cancelled_turn_snapshot_path(sid, bc.turn_id).exists()
    annotation = chat_mod.sess.get_message_annotations(sid)["assistant-partial"]
    assert annotation["turn_status"] == "failed"


def test_result_only_error_persists_tail_bubble_without_relabeling_old_turn(
        stream_env, client, monkeypatch):
    """A UUID-less Result error stays visible after reload and owns no old UUID."""
    chat_mod = stream_env
    sid = _make_session(client)
    chat_mod.sess.set_message_annotation(
        sid,
        "assistant-from-previous-turn",
        model="codex:gpt-5.6-sol",
        ts=1_700_000_000_000,
        turn_status="completed",
        elapsed_s=2.0,
    )
    result = ResultMessage(
        subtype="error", duration_ms=1500, duration_api_ms=1400,
        is_error=True, num_turns=1, session_id=sid,
        result="Your input exceeds the context window of this model",
        api_error_status=400,
    )
    fake = _FakeStreamClient([result])

    async def fake_get_client(
        session_id, model, permission="bypassPermissions", effort="",
        service_tier="",
    ):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=keep-going&model=codex:gpt-5.6-sol",
    )
    assert response.status_code == 200, response.text
    done = next(
        json.loads(data)
        for event, data in _parse_sse(response.text)
        if event == "done"
    )
    assert done["is_error"] is True
    assert done["assistant_uuid"] == ""
    assert done["snapshot_ready"] is True

    old = chat_mod.sess.get_message_annotations(sid)[
        "assistant-from-previous-turn"]
    assert old["turn_status"] == "completed"
    assert old["ts"] == 1_700_000_000_000

    history = client.get(
        f"/api/chat/sessions/{sid}",
        params={"tail": 80},
        headers={"X-Auth-Token": TEST_TOKEN},
    )
    assert history.status_code == 200, history.text
    messages = history.json()["messages"]
    assert any(m.get("role") == "user" and m.get("text") == "keep-going"
               for m in messages)
    terminal = messages[-1]
    assert terminal["role"] == "assistant"
    assert "context window" in terminal["text"]
    assert terminal["turn_status"] == "failed"


def test_prevent_continuation_cannot_be_overridden_by_success_result(
        stream_env, client, monkeypatch):
    """A rejected UserPromptSubmit stays failed even if Result says success."""
    chat_mod = stream_env
    sid = _make_session(client)
    fake = _FakeStreamClient([
        SystemMessage(
            subtype="informational",
            data={
                "content": (
                    "UserPromptSubmit operation blocked by hook: "
                    "callback timed out"
                ),
                "level": "warning",
                "preventContinuation": True,
            },
        ),
        ResultMessage(
            subtype="success", duration_ms=50, duration_api_ms=40,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.0, usage={},
        ),
    ])

    async def fake_get_client(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=must-survive-refresh&model=claude-sonnet-4-6",
    )
    assert response.status_code == 200, response.text
    done = next(
        json.loads(data)
        for event, data in _parse_sse(response.text)
        if event == "done"
    )

    assert done["is_error"] is True
    assert done["snapshot_ready"] is True
    assert done["assistant_uuid"] == ""
    assert "blocked by hook" in done["error"]
    assert done["kind"] == "network"
    assert done["retryable"] is True

    history = client.get(
        f"/api/chat/sessions/{sid}",
        params={"tail": 80},
        headers={"X-Auth-Token": TEST_TOKEN},
    )
    assert history.status_code == 200, history.text
    messages = history.json()["messages"]
    assert any(
        message.get("role") == "user"
        and message.get("text") == "must-survive-refresh"
        and message.get("_failed") is True
        for message in messages
    )
    assert messages[-1]["turn_status"] == "failed"


def test_prevent_continuation_without_result_surfaces_hook_error(
        stream_env, client, monkeypatch):
    chat_mod = stream_env
    sid = _make_session(client)
    fake = _FakeStreamClient([SystemMessage(
        subtype="informational",
        data={
            "content": "UserPromptSubmit operation blocked by hook timeout",
            "preventContinuation": True,
        },
    )])

    async def fake_get_client(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=no-result&model=claude-sonnet-4-6",
    )
    error = next(
        json.loads(data)
        for event, data in _parse_sse(response.text)
        if event == "error"
    )

    assert "blocked by hook timeout" in error["error"]
    assert "without a ResultMessage" not in error["error"]
    assert error["snapshot_ready"] is True


def test_success_result_without_canonical_turn_is_failed(
        stream_env, client, monkeypatch):
    """Result success is insufficient when the current turn never reached JSONL."""
    chat_mod = stream_env
    sid = _make_session(client)
    fake = _FakeStreamClient([ResultMessage(
        subtype="success", duration_ms=50, duration_api_ms=40,
        is_error=False, num_turns=1, session_id=sid,
        total_cost_usd=0.0, usage={},
    )])

    async def fake_get_client(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(
        chat_mod, "_turn_uuids_from_boundary",
        lambda *_args, **_kwargs: (None, None, True),
    )
    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=missing-canonical-turn&model=claude-sonnet-4-6",
    )
    assert response.status_code == 200, response.text
    done = next(
        json.loads(data)
        for event, data in _parse_sse(response.text)
        if event == "done"
    )

    assert done["is_error"] is True
    assert done["snapshot_ready"] is True
    assert done["assistant_uuid"] == ""
    assert "committed to conversation history" in done["error"]
    assert done["retryable"] is True


def test_canonical_commit_waits_for_late_transcript_flush(
        stream_env, client, monkeypatch):
    chat_mod = stream_env
    sid = _make_session(client)
    fake = _FakeStreamClient([ResultMessage(
        subtype="success", duration_ms=50, duration_api_ms=40,
        is_error=False, num_turns=1, session_id=sid,
        total_cost_usd=0.0, usage={},
    )])
    evidence = iter([
        (None, None, True),
        ("late-assistant", "late-user", True),
    ])
    calls = 0

    async def fake_get_client(*_args, **_kwargs):
        return fake

    def resolve(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return next(evidence)

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(chat_mod, "_turn_uuids_from_boundary", resolve)
    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=late-flush&model=claude-sonnet-4-6",
    )
    done = next(
        json.loads(data)
        for event, data in _parse_sse(response.text)
        if event == "done"
    )

    assert calls == 2
    assert done["is_error"] is False
    assert done["assistant_uuid"] == "late-assistant"


def test_slash_command_success_does_not_require_chat_bubbles(
        stream_env, client, monkeypatch):
    chat_mod = stream_env
    sid = _make_session(client)
    fake = _FakeStreamClient([ResultMessage(
        subtype="success", duration_ms=50, duration_api_ms=40,
        is_error=False, num_turns=1, session_id=sid,
        total_cost_usd=0.0, usage={},
    )])

    async def fake_get_client(*_args, **_kwargs):
        return fake

    def forbidden_resolver(*_args, **_kwargs):
        raise AssertionError("slash command must use its SDK Result verdict")

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(
        chat_mod, "_turn_uuids_from_boundary", forbidden_resolver)
    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=/compact&model=claude-sonnet-4-6",
    )
    done = next(
        json.loads(data)
        for event, data in _parse_sse(response.text)
        if event == "done"
    )

    assert done["is_error"] is False


def test_unestablished_boundary_cannot_match_old_history(
        stream_env, monkeypatch):
    chat_mod = stream_env

    def forbidden_index(_sid):
        raise AssertionError("invalid anchor must fail before history lookup")

    monkeypatch.setattr(chat_mod, "_ensure_transcript_index", forbidden_index)
    assert chat_mod._real_turn_uuids_from_boundary_for_test(
        "sid",
        {"record_count": 0, "capture_ok": False},
        started_at_ms=1000,
        terminal_at_ms=2000,
    ) == (None, None, False)


def test_activity_hidden_turn_never_enters_global_task_center(
        stream_env, client, monkeypatch):
    """A lightweight side branch streams normally without ledger mutations."""
    from backend import activity as activity_module

    chat_mod = stream_env
    created = client.post(
        "/api/chat/sessions",
        headers={
            "X-Auth-Token": TEST_TOKEN,
            "Content-Type": "application/json",
        },
        json={
            "name": "side question fixture",
            "model": "claude-sonnet-4-6",
            "activity_hidden": True,
        },
    )
    assert created.status_code == 200, created.text
    sid = created.json()["id"]
    assistant_uuid = "activity-hidden-assistant"
    messages = [
        AssistantMessage(
            content=[TextBlock(text="lightweight answer")],
            model="claude-sonnet-4-6",
            usage={},
            uuid=assistant_uuid,
        ),
        ResultMessage(
            subtype="success", duration_ms=1100, duration_api_ms=1000,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.0, usage={},
        ),
    ]

    async def fake_get_client(
        session_id, model, permission="bypassPermissions", effort="", service_tier="",
    ):
        return _FakeStreamClient(messages)

    def forbidden_activity(*_args, **_kwargs):
        raise AssertionError("activity ledger must ignore lightweight branch")

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(
        chat_mod,
        "_recent_turn_uuids",
        lambda _sid, _want_image_user: (assistant_uuid, "hidden-user"),
    )
    monkeypatch.setattr(activity_module.activity, "start", forbidden_activity)
    monkeypatch.setattr(activity_module.activity, "finish", forbidden_activity)
    monkeypatch.setattr(activity_module.activity, "set_state", forbidden_activity)

    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        f"&prompt=ask&model=claude-sonnet-4-6&permission=default"
    )
    assert response.status_code == 200, response.text
    assert any(event == "done" for event, _ in _parse_sse(response.text))
    assert chat_mod.sess.get_session(sid)["activity_hidden"] is True


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
            lambda activity_sid, *, summary="", activity_source="", owner_id="": activity_transitions.append(
                ("start", activity_sid, summary)),
        )
        monkeypatch.setattr(
            activity_module.activity,
            "finish",
            lambda activity_sid, status, *, activity_source="", owner_id="", mark_read=None: activity_transitions.append(
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
        main_done = next(
            event for event in broadcast.replay_events()
            if event.get("event") == "done"
        )
        assert json.loads(main_done["data"])["activity_source"] == "direct"
        assert activity_transitions == [
            ("start", sid, "quick reply"),
            ("finish", sid, "completed"),
        ]
        assert broadcast.done is False

        release_context.set()
        await asyncio.wait_for(broadcast.task, timeout=1)
        assert broadcast.done is True
        assert sid not in chat_mod._active_turns
        # The ResultMessage already committed Activity before ``done``.  A
        # second terminal write after the browser ACK would make this visible
        # current-session completion unread again.
        assert activity_transitions == [
            ("start", sid, "quick reply"),
            ("finish", sid, "completed"),
        ]
        recent = chat_mod._recent_turns.pop(sid, None)
        if recent is not None:
            recent.close()

    asyncio.run(exercise())


def test_activity_stays_running_until_background_continuation_finishes(
        stream_env, client, monkeypatch):
    """A main ResultMessage is not the logical end while a task is detached.

    The tab derives its yellow dot from the task pin.  Activity Center must keep
    the same session running until that pin settles and the CLI's continuation
    reaches its own ResultMessage; otherwise opening the center shows no running
    indicator for work that is visibly still active in the tab strip.
    """
    from backend import activity as activity_module

    chat_mod = stream_env
    sid = _make_session(client)

    async def exercise():
        watcher_attached = asyncio.Event()
        release_watcher = asyncio.Event()
        activity_transitions = []

        started = TaskStartedMessage(
            subtype="task_started", data={}, task_id="task_deferred",
            description="sleep 20", uuid="task-start", session_id=sid,
            tool_use_id="tu-bg", task_type="bash",
        )
        main_result = ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=9,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.0,
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        notification = TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="task_deferred",
            status="completed", output_file="/tmp/task.out", summary="done",
            uuid="task-finish", session_id=sid, tool_use_id="tu-bg",
        )
        reaction = AssistantMessage(
            content=[TextBlock(text="后台任务已经完成。")],
            model="claude-sonnet-4-6", usage={}, uuid="continuation-asst",
        )
        continuation_result = ResultMessage(
            subtype="success", duration_ms=12, duration_api_ms=10,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.0, usage={},
        )

        class _DeferredBackgroundClient(_FakeStreamClient):
            async def receive_messages(self):
                watcher_attached.set()
                await release_watcher.wait()
                for message in (notification, reaction, continuation_result):
                    yield message

        fake = _DeferredBackgroundClient([started, main_result])

        async def fake_get_client(*_args, **_kwargs):
            return fake

        monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
        monkeypatch.setattr(
            activity_module.activity,
            "start",
            lambda activity_sid, *, summary="", activity_source="", owner_id="": activity_transitions.append(
                ("start", activity_sid, summary)),
        )
        monkeypatch.setattr(
            activity_module.activity,
            "finish",
            lambda activity_sid, status, *, activity_source="", owner_id="", mark_read=None: activity_transitions.append(
                ("finish", activity_sid, status)),
        )

        broadcast = await chat_mod._start_turn(sid, "run a background task")
        await asyncio.wait_for(watcher_attached.wait(), timeout=1)

        assert any(
            event.get("event") == "done"
            for event in broadcast.replay_events()
        )
        assert activity_transitions == [
            ("start", sid, "run a background task"),
        ]
        main_done = next(
            event for event in broadcast.replay_events()
            if event.get("event") == "done"
        )
        assert json.loads(main_done["data"])["activity_source"] == "background"
        assert chat_mod._sessions_with_inflight_tasks[sid] == {
            "task_deferred",
        }
        assert chat_mod._background_activity_finishes[sid] == (
            "completed", broadcast.turn_id)

        await asyncio.wait_for(broadcast.task, timeout=1)
        watcher = chat_mod._task_watchers[sid]
        release_watcher.set()
        await asyncio.wait_for(watcher, timeout=1)

        assert activity_transitions == [
            ("start", sid, "run a background task"),
            ("finish", sid, "completed"),
        ]
        assert sid not in chat_mod._sessions_with_inflight_tasks
        assert sid not in chat_mod._background_activity_finishes

    try:
        asyncio.run(exercise())
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)
        chat_mod._background_activity_finishes.pop(sid, None)
        chat_mod._task_watchers.pop(sid, None)
        chat_mod._active_turns.pop(sid, None)
        recent = chat_mod._recent_turns.pop(sid, None)
        if recent is not None:
            recent.close()
        chat_mod._delete_active_turn_sidecar(sid)


@pytest.mark.parametrize(
    ("deferred_status", "expected_status"),
    [("completed", "failed"), ("cancelled", "cancelled")],
)
def test_background_stream_eof_releases_dead_task_and_closes_activity(
        stream_env, monkeypatch, deferred_status, expected_status):
    """A closed CLI can never deliver the pending task's terminal marker.

    This is the force-teardown path behind the stale yellow tab / Activity
    Center row: unlike watcher replacement, the watcher is not cancelled; its
    shared stream ends cleanly with EOF while the task pin is still present.
    """
    from backend import activity as activity_module

    chat_mod = stream_env
    sid = f"sid-eof-{deferred_status}"
    task_id = f"task-eof-{deferred_status}"
    transitions = []

    class _ClosedClient:
        def __init__(self):
            self.stop_calls = []
            self.disconnect_calls = 0

        async def stop_task(self, task_id):
            self.stop_calls.append(task_id)

        async def disconnect(self):
            self.disconnect_calls += 1

        async def receive_messages(self):
            if False:  # pragma: no cover - make this an async generator
                yield None

    async def exercise():
        fake = _ClosedClient()
        chat_mod._pin_background_task(sid, task_id)
        chat_mod._bg_task_descriptions[task_id] = "sleep 30"
        chat_mod._background_activity_finishes[sid] = (
            deferred_status, "background-owner")
        await chat_mod._watch_inflight_tasks(
            sid, fake, {task_id: "sleep 30"})
        assert fake.stop_calls == [task_id]
        assert fake.disconnect_calls == 1

    monkeypatch.setattr(
        activity_module.activity,
        "finish",
        lambda activity_sid, status, *, activity_source="", owner_id="", mark_read=None: transitions.append(
            (activity_sid, status)),
    )
    try:
        asyncio.run(exercise())
        assert transitions == [(sid, expected_status)]
        assert sid not in chat_mod._sessions_with_inflight_tasks
        assert sid not in chat_mod._background_activity_finishes
        assert task_id not in chat_mod._bg_task_descriptions
        assert task_id not in chat_mod._bg_task_pinned_at
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)
        chat_mod._background_activity_finishes.pop(sid, None)
        chat_mod._bg_task_descriptions.pop(task_id, None)
        chat_mod._bg_task_pinned_at.pop(task_id, None)
        chat_mod._background_turn_started_at.pop(sid, None)
        chat_mod._background_origin_turn_id.pop(sid, None)
        chat_mod._delete_active_turn_sidecar(sid)


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
    # The delayed lifecycle record is only authoritative when it belongs to a
    # task launched by this runtime.  Keep that ownership durable but leave the
    # in-memory pin clear: this models a prior observer already releasing the
    # runtime while the typed terminal record remains buffered in the pooled
    # SDK queue.  The new turn may start, and the late record may still patch
    # the old card without resurrecting the task or creating a notification.
    assert chat_mod.sess.set_runtime_task_overlay(
        sid, "task-old", state="running", owner_session_id=sid,
        tool_use_id="tu-old",
    )

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

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort="", service_tier=""):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=current-question&model=claude-sonnet-4-6")
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    kinds = [e for e, _ in events]

    assert fake.receive_calls == 2, "stale Result should reopen receive_response"
    assert kinds.count("task_notification") == 1
    notification = next(
        json.loads(data)
        for kind, data in events
        if kind == "task_notification"
    )
    assert notification["already_reported"] is True
    assert notification["background_tasks_pending"] == 0
    assert chat_mod.sess.get_runtime_task_overlays(sid)["task-old"][
        "state"
    ] == "completed"
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


def test_activity_source_is_explicit_for_broadcasts_and_error_frames(stream_env):
    chat_mod = stream_env
    broadcast = chat_mod.TurnBroadcast(session_id="source-contract", model="m")
    assert broadcast.activity_source == "direct"

    broadcast.queue_item_id = "queue-item"
    assert broadcast.activity_source == "queued"

    broadcast.is_continuation = True
    assert broadcast.activity_source == "background"

    error = chat_mod._error_event(
        "queued turn failed", activity_source="queued")
    payload = json.loads(error["data"])
    assert payload["activity_source"] == "queued"


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


def test_run_sdk_command_checked_rejects_local_command_api_error(stream_env):
    """`/compact` may hide its API failure in a generic SystemMessage."""
    chat_mod = stream_env
    messages = [
        SystemMessage(
            subtype="local_command",
            data={
                "content": (
                    "API Error: 400 Your input exceeds the context window "
                    "of this model"
                ),
                # Real Claude CLI records this failure at info level.
                "level": "info",
            },
        ),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="sid", result="ok",
        ),
    ]

    class FakeClient:
        def __init__(self):
            self.queries = []

        async def query(self, prompt):
            self.queries.append(prompt)

        async def receive_response(self):
            for message in messages:
                yield message

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
    stale_system = SystemMessage(
        subtype="informational",
        data={"uuid": "old", "preventContinuation": True},
    )

    assert boundary.classify(lifecycle) == "forward"
    assert boundary.classify(old_result) == "stale_result"
    assert boundary.classify(stale_system) == "drop"
    assert boundary.classify(error_result) == "current_result"


def test_preflight_compact_failure_blocks_original_prompt(
        stream_env, client, monkeypatch, capsys):
    chat_mod = stream_env
    sid = _make_session(client)
    private_marker = "PRIVATE_UPSTREAM_DETAIL_MUST_NOT_BE_LOGGED"
    compact_error = ResultMessage(
        subtype="error", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id=sid,
        result=(
            "Your input exceeds the context window of this model "
            f"{private_marker}"
        ),
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

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort="", service_tier=""):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=must-not-send&model=claude-sonnet-4-6")
    events = _parse_sse(r.text)
    error = next(json.loads(d) for e, d in events if e == "error")

    assert fake.queried == ["/compact"]
    assert error["kind"] == "context_window"
    assert error["retryable"] is False
    assert private_marker not in capsys.readouterr().err


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

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort="", service_tier=""):
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

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort="", service_tier=""):
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

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort="", service_tier=""):
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


def test_direct_stream_attachment_still_consumes_available_ids_once(
        stream_env, client, monkeypatch):
    """Queue all-or-none claiming must not change ordinary direct sends."""
    chat_mod = stream_env
    sid = _make_session(client)
    body = b"direct attachment"
    aid = "direct-once"
    chat_mod._image_store[aid] = {
        "kind": "text",
        "mime": "text/plain",
        "name": "direct.txt",
        "raw": body,
        "text": body.decode(),
        "ts": chat_mod.time.time(),
    }
    fake = _FakeStreamClient([
        ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=9,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.0, usage={"input_tokens": 1, "output_tokens": 1},
        ),
    ])

    async def fake_get_client(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        f"&prompt=read-direct&image_ids={aid}&model=claude-sonnet-4-6"
    )

    assert response.status_code == 200, response.text
    assert len(fake.queried) == 1
    assert aid not in chat_mod._image_store
    assert (chat_mod._attachments_base() / sid / f"{aid}-direct.txt").read_bytes() == body


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

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort="", service_tier=""):
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


def test_successor_suppresses_predecessor_orphan_notification(
        stream_env, client, monkeypatch):
    """A resumed child must not surface CLI's synthetic predecessor stop."""
    chat_mod = stream_env
    source = chat_mod.sess.create_session("runtime owner")
    child_sid = _make_session(client)
    assert chat_mod.sess.link_runtime_successor(source["id"], child_sid)
    chat_mod._pin_background_task(source["id"], "task-inherited")
    assert chat_mod._record_background_task_launch(
        source["id"], "task-inherited", tool_use_id="tool-inherited")

    messages = [
        TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="task-inherited",
            status="stopped", output_file="",
            summary="No completion record was found in the previous session.",
            uuid="synthetic-stop", session_id=child_sid,
            tool_use_id="tool-inherited",
        ),
        AssistantMessage(
            content=[TextBlock(text="current reply")],
            model="claude-sonnet-4-6", uuid="current-assistant",
            usage={"input_tokens": 4, "output_tokens": 2},
        ),
        ResultMessage(
            subtype="success", duration_ms=20, duration_api_ms=18,
            is_error=False, num_turns=1, session_id=child_sid,
            total_cost_usd=0.0,
            usage={"input_tokens": 4, "output_tokens": 2},
            result="current reply", uuid="current-result",
        ),
    ]
    fake = _FakeStreamClient(messages)

    async def fake_get_client(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    try:
        response = client.get(
            f"/api/chat/stream?token={TEST_TOKEN}&session_id={child_sid}"
            "&prompt=current&model=claude-sonnet-4-6")
        assert response.status_code == 200, response.text
        events = _parse_sse(response.text)
        assert "task_notification" not in [kind for kind, _ in events]
        assert "done" in [kind for kind, _ in events]
        assert chat_mod._sessions_with_inflight_tasks[source["id"]] == {
            "task-inherited"
        }
        for sid in (source["id"], child_sid):
            overlay = chat_mod.sess.get_runtime_task_overlays(
                sid)["task-inherited"]
            assert overlay["state"] == "running"
            assert overlay["owner_session_id"] == source["id"]
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(source["id"], None)
        chat_mod._bg_task_pinned_at.pop("task-inherited", None)
        chat_mod._bg_task_tool_use_ids.pop("task-inherited", None)


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
    derives the set from the session-level pin set and enriches it with any
    turn-local launch metadata."""
    chat_mod = stream_env
    sid = "sid-orphan"
    try:
        # Prior-turn task still pinned at session level + description cached,
        # but NOT in this turn's local inflight dict. `task_now` is a launch
        # from THIS turn — every launch path pins (_pin_background_task), so a
        # live turn-local task is always in the pin set too.
        chat_mod._pin_background_task(sid, "task_prior")
        chat_mod._pin_background_task(sid, "task_now")
        chat_mod._bg_task_descriptions["task_prior"] = "deep research"
        turn_local = {"task_now": {"tool_use_id": "tu_now",
                                   "description": "this turn"}}

        merged = chat_mod._merge_session_inflight(sid, turn_local)

        # Both the just-launched task and the orphaned prior task are covered.
        assert set(merged) == {"task_now", "task_prior"}
        assert merged["task_now"]["description"] == "this turn"
        assert merged["task_now"]["tool_use_id"] == "tu_now"
        assert merged["task_prior"]["description"] == "deep research"
        # Turn-local entry is not mutated (defensive copy).
        assert "task_prior" not in turn_local

        # A session with no pins → nothing in flight, whatever the turn-local
        # dict still holds (see the no-resurrect test).
        assert chat_mod._merge_session_inflight("sid-none", turn_local) == {}
        # Empty everything → empty (no spurious watcher spawn).
        assert chat_mod._merge_session_inflight("sid-none", {}) == {}
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)
        chat_mod._bg_task_descriptions.pop("task_prior", None)
        for tid in ("task_prior", "task_now"):
            chat_mod._bg_task_pinned_at.pop(tid, None)


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
        subtype="success", duration_ms=1120, duration_api_ms=1100,
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
        assert done["duration_ms"] == 1120
        assert done["assistant_uuid"] == "continuation-assistant-uuid"
        assert done["activity_source"] == "background"
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
        annotations = chat_mod.sess.get_message_annotations(sid)
        assert annotations["continuation-assistant-uuid"] == {
            "model": done["model"],
            "ts": done["completed_at_ms"],
            "turn_status": "completed",
            "elapsed_s": 1.1,
        }
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


@pytest.mark.asyncio
async def test_hidden_runtime_continuation_projects_one_agent_bubble_to_leaf(
        stream_env):
    """A rolled-over owner must deliver prose, not a toast, to the child.

    Keep a source turn in `_active_turns` to cover the settle-vs-finalizer race:
    the task notification may use that live carrier, but a private continuation
    collector must still retain the following AssistantMessage for projection.
    """
    chat_mod = stream_env
    source_sid = "11111111-2222-4333-8444-555555555555"
    child_sid = "22222222-3333-4444-8555-666666666666"
    chat_mod.sess.register_session(
        source_sid, name="hidden owner", model="claude-sonnet-4-6")
    chat_mod.sess.register_session(
        child_sid,
        name="visible leaf",
        model="claude-sonnet-4-6",
        runtime_predecessor=source_sid,
    )
    assert chat_mod.sess.link_runtime_successor(source_sid, child_sid)
    chat_mod._pin_background_task(source_sid, "task-project")
    assert chat_mod._record_background_task_launch(
        source_sid, "task-project", tool_use_id="tool-project")
    occupied = chat_mod.TurnBroadcast(
        source_sid, model="claude-sonnet-4-6")
    chat_mod._active_turns[source_sid] = occupied

    notification = TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="task-project",
        status="completed", output_file="/tmp/private-result.md",
        summary="done", uuid="notify-project", session_id=source_sid,
        tool_use_id="tool-project")
    reaction_text = "后台分析已经完成，这是 Agent 的续答。"
    reaction = AssistantMessage(
        content=[TextBlock(text=reaction_text)],
        model="claude-sonnet-4-6", usage={},
        uuid="source-only-assistant-uuid")
    result = ResultMessage(
        subtype="success", duration_ms=1250, duration_api_ms=1200,
        is_error=False, num_turns=1, session_id=source_sid,
        total_cost_usd=0.0, usage={})

    try:
        await chat_mod._watch_inflight_tasks(
            source_sid,
            _FakeWatchClient([notification, reaction, result]),
            {"task-project": "background analysis"},
        )
        deliveries = [
            task for (sid, _), task
            in chat_mod._runtime_continuation_delivery_tasks.items()
            if sid == source_sid
        ]
        if deliveries:
            await asyncio.wait_for(
                asyncio.gather(*deliveries), timeout=2)

        snapshots, revision = chat_mod._load_cancelled_turn_snapshots(
            child_sid)
        runtime_snapshots = [
            item for item in snapshots
            if item.get("kind") == "runtime_continuation"
        ]
        assert len(runtime_snapshots) == 1
        bubble = runtime_snapshots[0]["messages"][0]
        assert bubble == {
            "role": "assistant",
            "text": reaction_text,
            "model": "claude-sonnet-4-6",
            "ts": bubble["ts"],
            "elapsed": 1.2,
            "turn_status": "completed",
            "display_kind": "runtime_continuation",
            "runtime_event_id": runtime_snapshots[0]["runtime_event_id"],
            "presentation_only": True,
            "forkable": False,
            "_key": (
                "runtime-continuation:"
                f"{runtime_snapshots[0]['runtime_event_id']}:0"
            ),
        }
        assert "uuid" not in bubble
        assert "forkUuid" not in bubble
        assert revision
        assert not chat_mod._session_has_runtime_continuation_outbox(
            source_sid)

        response = chat_mod.get_session_api(
            child_sid,
            full=False,
            tail=50,
            offset=-1,
            limit=0,
            history_generation="",
            around_uuid="",
            before=0,
            after=0,
        )
        projected = [
            item for item in response["messages"]
            if item.get("display_kind") == "runtime_continuation"
        ]
        assert len(projected) == 1
        assert projected[0]["text"] == reaction_text
        assert response["runtime_ui_revision"] == revision
        assert response["turn_count"] == 0
        pending, active_revision = (
            chat_mod._runtime_continuation_projection_state(source_sid))
        assert pending is False
        assert active_revision == revision

        # A later rollover must carry the presentation event forward even
        # though the SDK JSONL (correctly) has no such assistant record.
        grandchild_sid = "33333333-4444-4555-8666-777777777777"
        chat_mod.sess.register_session(
            grandchild_sid,
            name="visible leaf",
            model="claude-sonnet-4-6",
            runtime_predecessor=child_sid,
        )
        assert chat_mod.sess.link_runtime_successor(
            child_sid, grandchild_sid)
        chat_mod._sync_runtime_successor_postlude(child_sid)
        grandchild_snapshots, _ = (
            chat_mod._load_cancelled_turn_snapshots(grandchild_sid))
        assert [
            item["messages"][0]["text"]
            for item in grandchild_snapshots
            if item.get("kind") == "runtime_continuation"
        ] == [reaction_text]
        grandchild_response = chat_mod.get_session_api(
            grandchild_sid,
            full=False,
            tail=50,
            offset=-1,
            limit=0,
            history_generation="",
            around_uuid="",
            before=0,
            after=0,
        )
        assert sum(
            item.get("runtime_event_id")
            == runtime_snapshots[0]["runtime_event_id"]
            for item in grandchild_response["messages"]
        ) == 1
    finally:
        chat_mod._active_turns.pop(source_sid, None)
        occupied.close()
        chat_mod._sessions_with_inflight_tasks.pop(source_sid, None)
        chat_mod._bg_task_pinned_at.pop("task-project", None)
        chat_mod._bg_task_tool_use_ids.pop("task-project", None)
        chat_mod._recent_turns.pop(source_sid, None)
        for key, task in tuple(
                chat_mod._runtime_continuation_delivery_tasks.items()):
            if key[0] == source_sid:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                chat_mod._runtime_continuation_delivery_tasks.pop(key, None)
        chat_mod._delete_runtime_continuation_outboxes(source_sid)
        chat_mod._delete_cancelled_turn_snapshots(child_sid)
        chat_mod._delete_cancelled_turn_snapshots(
            "33333333-4444-4555-8666-777777777777")


@pytest.mark.asyncio
async def test_runtime_continuation_waits_for_visible_leaf_turn_boundary(
        stream_env):
    """Never insert a hidden-owner reply into the middle of a child turn."""
    chat_mod = stream_env
    source_sid = "44444444-5555-4666-8777-888888888888"
    child_sid = "55555555-6666-4777-8888-999999999999"
    chat_mod.sess.register_session(
        source_sid, name="source", model="claude-sonnet-4-6")
    chat_mod.sess.register_session(
        child_sid,
        name="child",
        model="claude-sonnet-4-6",
        runtime_predecessor=source_sid,
    )
    assert chat_mod.sess.link_runtime_successor(source_sid, child_sid)

    continuation = chat_mod.TurnBroadcast(
        source_sid, model="claude-sonnet-4-6")
    continuation.is_continuation = True
    continuation.publish({
        "event": "text",
        "data": json.dumps({"text": "延迟到当前回复结束后显示。"}),
    })
    event_id = chat_mod._persist_runtime_continuation_outbox(
        source_sid,
        continuation,
        completed_at_ms=1_800_000_000_000,
        elapsed_s=2.0,
        terminal_status="completed",
    )
    assert event_id
    child_turn = chat_mod.TurnBroadcast(
        child_sid, model="claude-sonnet-4-6")
    chat_mod._active_turns[child_sid] = child_turn
    delivery = chat_mod._schedule_runtime_continuation_delivery(
        source_sid, event_id)
    assert delivery is not None

    try:
        await asyncio.sleep(0.05)
        snapshots, _ = chat_mod._load_cancelled_turn_snapshots(child_sid)
        assert not any(
            item.get("kind") == "runtime_continuation"
            for item in snapshots
        )
        assert chat_mod._session_has_runtime_continuation_outbox(source_sid)
        pending, _ = chat_mod._runtime_continuation_projection_state(child_sid)
        assert pending is True

        child_turn.finish()
        chat_mod._active_turns.pop(child_sid, None)
        await asyncio.wait_for(delivery, timeout=2)
        snapshots, _ = chat_mod._load_cancelled_turn_snapshots(child_sid)
        projected = [
            item for item in snapshots
            if item.get("kind") == "runtime_continuation"
        ]
        assert len(projected) == 1
        assert projected[0]["messages"][0]["text"] == (
            "延迟到当前回复结束后显示。")
        pending, revision = (
            chat_mod._runtime_continuation_projection_state(child_sid))
        assert pending is False
        assert revision
    finally:
        chat_mod._active_turns.pop(child_sid, None)
        child_turn.close()
        continuation.close()
        if not delivery.done():
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
        chat_mod._runtime_continuation_delivery_tasks.pop(
            (source_sid, event_id), None)
        chat_mod._delete_runtime_continuation_outboxes(source_sid)
        chat_mod._delete_cancelled_turn_snapshots(child_sid)


@pytest.mark.asyncio
async def test_fast_continuation_survives_result_to_rollover_link_gap(
        stream_env):
    """A very fast task may finish before the eager fork publishes its link."""
    chat_mod = stream_env
    source_sid = "66666666-7777-4888-8999-aaaaaaaaaaaa"
    child_sid = "77777777-8888-4999-8aaa-bbbbbbbbbbbb"
    chat_mod.sess.register_session(
        source_sid, name="source", model="claude-sonnet-4-6")
    chat_mod.sess.register_session(
        child_sid,
        name="child",
        model="claude-sonnet-4-6",
        runtime_predecessor=source_sid,
    )
    hold_prewarm = asyncio.Event()
    prewarm = asyncio.create_task(hold_prewarm.wait())
    chat_mod._runtime_prewarm_tasks[source_sid] = prewarm
    continuation = chat_mod.TurnBroadcast(
        source_sid, model="claude-sonnet-4-6")
    continuation.publish({
        "event": "text",
        "data": json.dumps({"text": "快速任务也不能丢失续答。"}),
    })
    event_id = chat_mod._persist_runtime_continuation_outbox(
        source_sid,
        continuation,
        completed_at_ms=1_800_000_100_000,
        elapsed_s=1.0,
        terminal_status="completed",
    )
    assert event_id
    delivery = chat_mod._schedule_runtime_continuation_delivery(
        source_sid, event_id)
    assert delivery is not None

    try:
        await asyncio.sleep(0.05)
        assert chat_mod._session_has_runtime_continuation_outbox(source_sid)
        snapshots, _ = chat_mod._load_cancelled_turn_snapshots(child_sid)
        assert snapshots == []

        assert chat_mod.sess.link_runtime_successor(source_sid, child_sid)
        await asyncio.wait_for(delivery, timeout=2)
        snapshots, _ = chat_mod._load_cancelled_turn_snapshots(child_sid)
        assert [
            item["messages"][0]["text"]
            for item in snapshots
            if item.get("kind") == "runtime_continuation"
        ] == ["快速任务也不能丢失续答。"]
    finally:
        hold_prewarm.set()
        await asyncio.gather(prewarm, return_exceptions=True)
        if chat_mod._runtime_prewarm_tasks.get(source_sid) is prewarm:
            chat_mod._runtime_prewarm_tasks.pop(source_sid, None)
        continuation.close()
        if not delivery.done():
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
        chat_mod._runtime_continuation_delivery_tasks.pop(
            (source_sid, event_id), None)
        chat_mod._delete_runtime_continuation_outboxes(source_sid)
        chat_mod._delete_cancelled_turn_snapshots(child_sid)


@pytest.mark.asyncio
async def test_unconditional_outbox_survives_source_lock_to_link_gap(stream_env):
    """The write-ahead bubble exists before rollover intent becomes visible."""
    chat_mod = stream_env
    source_sid = "88888888-9999-4aaa-8bbb-cccccccccccc"
    child_sid = "99999999-aaaa-4bbb-8ccc-dddddddddddd"
    chat_mod.sess.register_session(
        source_sid, name="source", model="claude-sonnet-4-6")
    chat_mod.sess.register_session(
        child_sid,
        name="child",
        model="claude-sonnet-4-6",
        runtime_predecessor=source_sid,
    )
    continuation = chat_mod.TurnBroadcast(
        source_sid, model="claude-sonnet-4-6")
    continuation.publish({
        "event": "text",
        "data": json.dumps({"text": "先完成，再发布 rollover link。"}),
    })
    event_id = chat_mod._persist_runtime_continuation_outbox(
        source_sid,
        continuation,
        completed_at_ms=1_800_000_200_000,
        elapsed_s=1.0,
        terminal_status="completed",
    )
    assert event_id
    rollover_lock = chat_mod._runtime_rollover_lock_for(source_sid)
    await rollover_lock.acquire()
    delivery = chat_mod._schedule_runtime_continuation_delivery(
        source_sid, event_id)
    assert delivery is not None

    try:
        await asyncio.sleep(0.05)
        assert chat_mod._session_has_runtime_continuation_outbox(source_sid)
        assert chat_mod.sess.link_runtime_successor(source_sid, child_sid)
        rollover_lock.release()
        await asyncio.wait_for(delivery, timeout=2)
        snapshots, _ = chat_mod._load_cancelled_turn_snapshots(child_sid)
        assert [
            item["messages"][0]["text"]
            for item in snapshots
            if item.get("kind") == "runtime_continuation"
        ] == ["先完成，再发布 rollover link。"]
    finally:
        if rollover_lock.locked():
            rollover_lock.release()
        continuation.close()
        if not delivery.done():
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
        chat_mod._runtime_continuation_delivery_tasks.pop(
            (source_sid, event_id), None)
        chat_mod._delete_runtime_continuation_outboxes(source_sid)
        chat_mod._delete_cancelled_turn_snapshots(child_sid)


@pytest.mark.asyncio
async def test_startup_recovery_delivers_durable_agent_bubble(stream_env):
    chat_mod = stream_env
    source_sid = "90909090-abab-4cdc-8efe-121212121212"
    child_sid = "a0a0a0a0-bcbc-4ded-8f0f-232323232323"
    chat_mod.sess.register_session(
        source_sid, name="source", model="claude-sonnet-4-6")
    chat_mod.sess.register_session(
        child_sid,
        name="child",
        model="claude-sonnet-4-6",
        runtime_predecessor=source_sid,
    )
    assert chat_mod.sess.link_runtime_successor(source_sid, child_sid)
    continuation = chat_mod.TurnBroadcast(
        source_sid, model="claude-sonnet-4-6")
    continuation.publish({
        "event": "text",
        "data": json.dumps({"text": "重启后仍应出现的 Agent 回复。"}),
    })
    event_id = chat_mod._persist_runtime_continuation_outbox(
        source_sid,
        continuation,
        completed_at_ms=1_800_000_250_000,
        elapsed_s=1.0,
        terminal_status="completed",
    )
    assert event_id
    try:
        assert await chat_mod.recover_runtime_continuation_outboxes_at_startup() == 1
        delivery = chat_mod._runtime_continuation_delivery_tasks[
            (source_sid, event_id)]
        await asyncio.wait_for(delivery, timeout=2)
        snapshots, _ = chat_mod._load_cancelled_turn_snapshots(child_sid)
        assert [
            item["messages"][0]["text"]
            for item in snapshots
            if item.get("kind") == "runtime_continuation"
        ] == ["重启后仍应出现的 Agent 回复。"]
        assert not chat_mod._session_has_runtime_continuation_outbox(source_sid)
    finally:
        continuation.close()
        task = chat_mod._runtime_continuation_delivery_tasks.pop(
            (source_sid, event_id), None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        chat_mod._delete_runtime_continuation_outboxes(source_sid)
        chat_mod._delete_cancelled_turn_snapshots(child_sid)


@pytest.mark.asyncio
async def test_continuation_waits_for_latest_edge_rollback(stream_env):
    """A -> B -> provisional C delivery barriers on B, then falls back to B."""
    chat_mod = stream_env
    source_sid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    middle_sid = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
    provisional_sid = "cccccccc-dddd-4eee-8fff-000000000000"
    for sid, predecessor in (
        (source_sid, ""),
        (middle_sid, source_sid),
        (provisional_sid, middle_sid),
    ):
        chat_mod.sess.register_session(
            sid,
            name=sid[:8],
            model="claude-sonnet-4-6",
            runtime_predecessor=predecessor,
        )
    assert chat_mod.sess.link_runtime_successor(source_sid, middle_sid)
    continuation = chat_mod.TurnBroadcast(
        source_sid, model="claude-sonnet-4-6")
    continuation.publish({
        "event": "text",
        "data": json.dumps({"text": "回滚后应落到稳定的 B。"}),
    })
    event_id = chat_mod._persist_runtime_continuation_outbox(
        source_sid,
        continuation,
        completed_at_ms=1_800_000_300_000,
        elapsed_s=1.0,
        terminal_status="completed",
    )
    latest_edge_lock = chat_mod._runtime_rollover_lock_for(middle_sid)
    await latest_edge_lock.acquire()
    assert chat_mod.sess.link_runtime_successor(middle_sid, provisional_sid)
    delivery = chat_mod._schedule_runtime_continuation_delivery(
        source_sid, event_id)
    assert delivery is not None

    try:
        await asyncio.sleep(0.05)
        assert not delivery.done()
        provisional, _ = chat_mod._load_cancelled_turn_snapshots(
            provisional_sid)
        assert provisional == []
        assert chat_mod.sess.unlink_runtime_successor(
            middle_sid, provisional_sid)
        chat_mod.sess.delete_session(provisional_sid)
        latest_edge_lock.release()
        await asyncio.wait_for(delivery, timeout=2)
        stable, _ = chat_mod._load_cancelled_turn_snapshots(middle_sid)
        assert [
            item["messages"][0]["text"]
            for item in stable
            if item.get("kind") == "runtime_continuation"
        ] == ["回滚后应落到稳定的 B。"]
    finally:
        if latest_edge_lock.locked():
            latest_edge_lock.release()
        continuation.close()
        if not delivery.done():
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
        chat_mod._runtime_continuation_delivery_tasks.pop(
            (source_sid, event_id), None)
        chat_mod._delete_runtime_continuation_outboxes(source_sid)
        chat_mod._delete_cancelled_turn_snapshots(middle_sid)
        chat_mod._delete_cancelled_turn_snapshots(provisional_sid)


def test_runtime_outbox_merges_prose_and_quarantines_corruption(stream_env):
    chat_mod = stream_env
    source_sid = "dddddddd-eeee-4fff-8000-111111111111"
    continuation = chat_mod.TurnBroadcast(
        source_sid, model="claude-sonnet-4-6")
    continuation.publish({
        "event": "text", "data": json.dumps({"text": "第一段"})})
    continuation.publish({
        "event": "tool_use", "data": json.dumps({"name": "Read"})})
    continuation.publish({
        "event": "text", "data": json.dumps({"text": "第二段"})})
    event_id = chat_mod._persist_runtime_continuation_outbox(
        source_sid,
        continuation,
        completed_at_ms=1_800_000_400_000,
        elapsed_s=1.0,
        terminal_status="completed",
    )
    try:
        outbox = chat_mod._load_runtime_continuation_outbox(
            source_sid, event_id)
        assert outbox["message"]["text"] == "第一段\n\n第二段"
        assert chat_mod._persist_runtime_continuation_outbox(
            source_sid,
            continuation,
            completed_at_ms=1_800_000_400_001,
            elapsed_s=1.0,
            terminal_status="cancelled",
        ) == ""

        corrupt_id = "eeeeeeee-ffff-4000-8111-222222222222"
        corrupt_path = chat_mod._runtime_continuation_outbox_path(
            source_sid, corrupt_id)
        corrupt_path.parent.mkdir(parents=True, exist_ok=True)
        corrupt_path.write_text("not-json", encoding="utf-8")
        assert chat_mod._load_runtime_continuation_outbox(
            source_sid, corrupt_id) is None
        assert not corrupt_path.exists()
        assert list(corrupt_path.parent.glob(f"{corrupt_path.name}.invalid*"))
    finally:
        continuation.close()
        chat_mod._delete_runtime_continuation_outboxes(source_sid)


def test_around_uuid_uses_virtual_snapshot_coordinates(stream_env, monkeypatch):
    chat_mod = stream_env
    index = {
        "records": [
            {"uuid": "u1", "bubble_count": 1},
            {"uuid": "u2", "bubble_count": 1},
        ],
        "orders": {"full": [0, 1], "normal": [0, 1]},
        "bubble_prefix": {"full": [0, 1, 2], "normal": [0, 1, 2]},
    }
    snapshot = {
        "turn_id": "terminal-1",
        "started_at_ms": 10,
        "anchors": {
            "full": {"uuid": "u1", "total": 1},
            "normal": {"uuid": "u1", "total": 1},
        },
        "hidden_uuids": [],
        "messages": [{
            "role": "assistant",
            "text": "后台 Agent 气泡",
            "display_kind": "runtime_continuation",
        }],
    }

    def shaped(_path, shaped_index, record_ids, _annotations):
        return [
            {"role": "assistant", "uuid": shaped_index["records"][i]["uuid"]}
            for i in record_ids
        ]

    monkeypatch.setattr(chat_mod, "_indexed_ui_records", shaped)
    result = chat_mod._interrupted_history_window_around_uuid(
        chat_mod.Path("/unused"),
        index,
        [snapshot],
        {},
        "u2",
        before=1,
        after=0,
    )
    assert result is not None
    window, total, offset, has_later = result
    assert [item.get("text") or item.get("uuid") for item in window] == [
        "后台 Agent 气泡", "u2"]
    assert (total, offset, has_later) == (3, 1, False)


@pytest.mark.asyncio
async def test_runtime_snapshot_copy_cannot_recreate_after_purge(
        stream_env, monkeypatch):
    chat_mod = stream_env
    source_sid = "ffffffff-0000-4111-8222-333333333333"
    target_sid = "00000000-1111-4222-8333-444444444444"
    chat_mod.sess.register_session(
        target_sid, name="target", model="claude-sonnet-4-6")
    entered = threading.Event()
    release = threading.Event()
    real_loader = chat_mod._load_cancelled_turn_snapshots
    snapshot = {
        "kind": "runtime_continuation",
        "sid": source_sid,
        "turn_id": "11111111-2222-4333-8444-555555555555",
        "runtime_event_id": "11111111-2222-4333-8444-555555555555",
        "started_at_ms": 10,
        "anchors": {
            "full": {"uuid": "", "total": 0},
            "normal": {"uuid": "", "total": 0},
        },
        "hidden_uuids": [],
        "messages": [{"role": "assistant", "text": "private"}],
    }

    def blocking_loader(sid):
        if sid == source_sid:
            entered.set()
            assert release.wait(timeout=2)
            return [snapshot], "revision"
        return real_loader(sid)

    def missing_sdk(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(
        chat_mod, "_load_cancelled_turn_snapshots", blocking_loader)
    monkeypatch.setattr(chat_mod, "sdk_delete_session", missing_sdk)
    copy_task = asyncio.create_task(asyncio.to_thread(
        chat_mod._copy_runtime_continuation_snapshots,
        source_sid,
        target_sid,
        {},
    ))
    assert await asyncio.to_thread(entered.wait, 1)
    purge_task = asyncio.create_task(asyncio.to_thread(
        chat_mod._purge_single_session_storage, target_sid))
    await asyncio.sleep(0.05)
    assert not purge_task.done()
    release.set()
    await asyncio.wait_for(copy_task, timeout=2)
    assert await asyncio.wait_for(purge_task, timeout=2) is True
    target_path = chat_mod._cancelled_turn_snapshot_path(
        target_sid, snapshot["runtime_event_id"])
    assert target_path is not None
    assert not target_path.exists()


@pytest.mark.asyncio
async def test_queue_claim_waits_for_ready_agent_bubble(stream_env, monkeypatch):
    chat_mod = stream_env
    source_sid = "12121212-3434-4567-8899-abababababab"
    leaf_sid = "23232323-4545-4678-899a-bcbcbcbcbcbc"
    chat_mod.sess.register_session(
        source_sid, name="source", model="claude-sonnet-4-6")
    chat_mod.sess.register_session(
        leaf_sid,
        name="leaf",
        model="claude-sonnet-4-6",
        runtime_predecessor=source_sid,
    )
    assert chat_mod.sess.link_runtime_successor(source_sid, leaf_sid)
    queued = chat_mod.sess.enqueue_message(leaf_sid, "queued after bubble")
    continuation = chat_mod.TurnBroadcast(
        source_sid, model="claude-sonnet-4-6")
    continuation.publish({
        "event": "text",
        "data": json.dumps({"text": "必须先显示的后台 Agent 回复。"}),
    })
    event_id = chat_mod._persist_runtime_continuation_outbox(
        source_sid,
        continuation,
        completed_at_ms=1_800_000_500_000,
        elapsed_s=1.0,
        terminal_status="completed",
    )
    assert event_id
    real_claim = chat_mod.sess.claim_queue_message
    sequence = []

    def guarded_claim(sid):
        snapshots, _ = chat_mod._load_cancelled_turn_snapshots(leaf_sid)
        assert [
            item["messages"][0]["text"]
            for item in snapshots
            if item.get("kind") == "runtime_continuation"
        ] == ["必须先显示的后台 Agent 回复。"]
        assert not chat_mod._session_has_runtime_continuation_outbox(source_sid)
        sequence.append("claim")
        return real_claim(sid)

    async def fake_start(_sid, _text, **kwargs):
        sequence.append(("start", kwargs["queue_item_id"]))

    monkeypatch.setattr(
        chat_mod.sess, "claim_queue_message", guarded_claim)
    monkeypatch.setattr(chat_mod, "_start_turn", fake_start)
    try:
        await chat_mod._maybe_drain_queue(leaf_sid)
        assert sequence == [
            "claim", ("start", queued["item"]["id"])]
    finally:
        continuation.close()
        chat_mod.sess.release_queue_claim(
            leaf_sid, queued["item"]["id"])
        chat_mod._delete_runtime_continuation_outboxes(source_sid)
        chat_mod._delete_cancelled_turn_snapshots(leaf_sid)


@pytest.mark.asyncio
async def test_watcher_shutdown_partial_never_projects_completed_bubble(
        stream_env):
    chat_mod = stream_env
    source_sid = "34343434-5656-4789-8aab-cdcdcdcdcdcd"
    child_sid = "45454545-6767-489a-8bbc-dededededede"
    task_id = "shutdown-partial"
    chat_mod.sess.register_session(
        source_sid, name="source", model="claude-sonnet-4-6")
    chat_mod.sess.register_session(
        child_sid,
        name="child",
        model="claude-sonnet-4-6",
        runtime_predecessor=source_sid,
    )
    assert chat_mod.sess.link_runtime_successor(source_sid, child_sid)
    chat_mod._pin_background_task(source_sid, task_id)
    assert chat_mod._record_background_task_launch(source_sid, task_id)
    partial_seen = asyncio.Event()
    never = asyncio.Event()
    notification = TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status="completed",
        output_file="/tmp/private",
        summary="done",
        uuid="shutdown-notification",
        session_id=source_sid,
        tool_use_id="shutdown-tool",
    )
    partial = AssistantMessage(
        content=[TextBlock(text="这只是尚未完成的前缀")],
        model="claude-sonnet-4-6",
        usage={},
        uuid="shutdown-partial-assistant",
    )

    class _PartialClient:
        async def receive_messages(self):
            yield notification
            yield partial
            partial_seen.set()
            await never.wait()

        async def query(self, _prompt):
            return None

    watcher = asyncio.create_task(chat_mod._watch_inflight_tasks(
        source_sid,
        _PartialClient(),
        {task_id: "background"},
    ))
    try:
        await asyncio.wait_for(partial_seen.wait(), timeout=1)
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher
        assert not chat_mod._session_has_runtime_continuation_outbox(source_sid)
        snapshots, _ = chat_mod._load_cancelled_turn_snapshots(child_sid)
        assert not any(
            item.get("kind") == "runtime_continuation"
            for item in snapshots
        )
        recent = chat_mod._recent_turns.get(source_sid)
        assert recent is not None
        done = next(
            event for event in recent.replay_events()
            if event.get("event") == "done"
        )
        assert json.loads(done["data"])["cancelled"] is True
    finally:
        if not watcher.done():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        chat_mod._sessions_with_inflight_tasks.pop(source_sid, None)
        chat_mod._bg_task_pinned_at.pop(task_id, None)
        chat_mod._bg_task_tool_use_ids.pop(task_id, None)
        recent = chat_mod._recent_turns.pop(source_sid, None)
        if recent is not None:
            recent.close()
        chat_mod._active_turns.pop(source_sid, None)
        chat_mod._delete_runtime_continuation_outboxes(source_sid)
        chat_mod._delete_cancelled_turn_snapshots(child_sid)


def test_continuation_terminal_precedes_annotation_bookkeeping(stream_env):
    """Queue done, annotate its exact UUID, then release the event loop."""
    import inspect

    source = inspect.getsource(stream_env._watch_inflight_tasks)
    done_at = source.index(
        'b.publish({"event": "done", "data": json.dumps(done_payload)})')
    annotate_at = source.index("sess.set_message_annotation(", done_at)
    finish_at = source.index("b.finish()", annotate_at)
    assert done_at < annotate_at
    assert annotate_at < finish_at
    close_source = source[source.index("async def _close_continuation"):finish_at]
    assert "_recent_turn_uuids" not in close_source
    assert "asyncio.to_thread" not in close_source
    assert '(state or {}).get("assistant_uuid")' in close_source
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


def test_restart_orphan_becomes_durable_failed_history(
        stream_env, client, monkeypatch):
    """A crash before canonical UserMessage commit must not erase the prompt."""
    chat_mod = stream_env
    sid = _make_session(client)
    turn = chat_mod.TurnBroadcast(
        session_id=sid, model="claude-sonnet-4-6")
    turn.user_text = "原始发送内容必须保留"
    turn.user_images = [{"id": "image-one", "url": "/private/image-one"}]
    turn.user_docs = [{"id": "doc-one", "name": "notes.md"}]
    turn.transcript_boundary = {
        "record_count": 7,
        "source_dev": 11,
        "source_inode": 13,
        "capture_ok": True,
    }
    chat_mod._write_active_turn_sidecar(turn)
    monkeypatch.setattr(
        chat_mod, "_turn_uuids_from_boundary",
        lambda *_args, **_kwargs: (None, None, True),
    )
    sidecar = chat_mod._active_turn_path(sid)
    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    chat_mod._interrupted_at_startup[sid] = persisted

    try:
        history = client.get(
            f"/api/chat/sessions/{sid}",
            params={"tail": 80},
            headers={"X-Auth-Token": TEST_TOKEN},
        )
        assert history.status_code == 200, history.text
        messages = history.json()["messages"]
        user = next(m for m in messages if m.get("role") == "user")
        assert user["text"] == "原始发送内容必须保留"
        assert user["images"] == turn.user_images
        assert user["docs"] == turn.user_docs
        assert user["_failed"] is True
        assert user["_error_kind"] == "unknown"
        assert user["_error_cta"] == "retry"
        assert user["_error_retryable"] is True
        assert "restarted" in user["_error_text"]
        assert messages[-1]["turn_status"] == "failed"
        assert not sidecar.exists()

        snapshots, _ = chat_mod._load_cancelled_turn_snapshots(sid)
        assert snapshots[0]["transcript_boundary"] == {
            "record_count": 7,
            "source_dev": 11,
            "source_inode": 13,
        }
    finally:
        chat_mod._interrupted_at_startup.pop(sid, None)
        chat_mod._delete_active_turn_sidecar(sid)


def test_restart_sidecar_does_not_duplicate_already_canonical_turn(
        stream_env, client, monkeypatch):
    chat_mod = stream_env
    sid = _make_session(client)
    turn = chat_mod.TurnBroadcast(
        session_id=sid, model="claude-sonnet-4-6")
    turn.user_text = "already committed"
    turn.transcript_boundary = {
        "record_count": 3,
        "source_dev": 5,
        "source_inode": 7,
        "capture_ok": True,
    }
    chat_mod._write_active_turn_sidecar(turn)
    chat_mod._interrupted_at_startup[sid] = json.loads(
        chat_mod._active_turn_path(sid).read_text(encoding="utf-8"))
    monkeypatch.setattr(
        chat_mod, "_turn_uuids_from_boundary",
        lambda *_args, **_kwargs: ("canonical-assistant", "canonical-user", True),
    )
    persist_calls = []
    monkeypatch.setattr(
        chat_mod, "_persist_failed_turn_snapshot",
        lambda *_args, **_kwargs: persist_calls.append(True) or True,
    )

    try:
        assert chat_mod._recover_interrupted_turn_snapshot(sid) is True
        assert persist_calls == []
        assert not chat_mod._active_turn_path(sid).exists()
    finally:
        chat_mod._interrupted_at_startup.pop(sid, None)
        chat_mod._delete_active_turn_sidecar(sid)


def test_interrupted_dismiss_fails_closed_until_history_is_durable(
        stream_env, client, monkeypatch):
    chat_mod = stream_env
    sid = _make_session(client)
    turn = chat_mod.TurnBroadcast(
        session_id=sid, model="claude-sonnet-4-6")
    turn.user_text = "do not discard me"
    chat_mod._write_active_turn_sidecar(turn)
    chat_mod._interrupted_at_startup[sid] = json.loads(
        chat_mod._active_turn_path(sid).read_text(encoding="utf-8"))
    monkeypatch.setattr(
        chat_mod, "_persist_failed_turn_snapshot", lambda *_args, **_kwargs: False)

    try:
        response = client.post(
            f"/api/chat/interrupted-turns/{sid}/dismiss",
            headers={"X-Auth-Token": TEST_TOKEN},
        )
        assert response.status_code == 503
        assert sid in chat_mod._interrupted_at_startup
        assert chat_mod._active_turn_path(sid).exists()
    finally:
        chat_mod._interrupted_at_startup.pop(sid, None)
        chat_mod._delete_active_turn_sidecar(sid)


def test_active_turn_sidecar_releases_after_canonical_main_turn(stream_env):
    """Detached tasks use runtime state, not the already-committed user intent."""
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
            assert chat_mod._release_active_turn_sidecar(sid) is True
            assert not chat_mod._active_turn_path(sid).exists()
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
        assert d["activity_source"] == "background", d
        assert d["turn_id"] == cont.turn_id

        # Once a reconnect subscriber has consumed it, /active must stop
        # advertising it — otherwise the 8s poller re-reconnects every tick
        # within the 60s TTL → duplicate reaction bubbles (the live-test
        # regression). The consumed flag is what GET /stream's reconnect sets.
        cont.continuation_consumed = True
        r = client.get(f"/api/chat/sessions/{sid}/active",
                       headers={"X-Auth-Token": TEST_TOKEN})
        assert r.json()["active"] is False, r.json()
        assert r.json()["activity_source"] == "background", r.json()
    finally:
        chat_mod._recent_turns.pop(sid, None)

    # 2) A grace-kept PLAIN turn (not a continuation) → active:false.
    plain = chat_mod.TurnBroadcast(session_id=sid, model="")
    plain.is_continuation = False
    plain.queue_item_id = "queued-item"
    plain.finish()
    chat_mod._recent_turns[sid] = plain
    try:
        r = client.get(f"/api/chat/sessions/{sid}/active",
                       headers={"X-Auth-Token": TEST_TOKEN})
        assert r.status_code == 200, r.text
        assert r.json()["active"] is False, r.json()
        assert r.json()["activity_source"] == "queued", r.json()
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
        assert data["activity_source"] == "background"
        assert data["background_tasks_pending"] == 1
        assert data["started_at"] == original_started_at
        assert data["turn_id"] == "origin-turn"
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)
        chat_mod._background_turn_started_at.pop(sid, None)
        chat_mod._background_origin_turn_id.pop(sid, None)


def test_start_turn_is_queued_while_background_task_pending(
    stream_env, client,
):
    """A pending task retains the response boundary until its watcher exits."""
    chat_mod = stream_env
    sid = _make_session(client)
    chat_mod._sessions_with_inflight_tasks[sid] = {"task-1"}
    try:
        with pytest.raises(chat_mod._TurnBusy):
            asyncio.run(chat_mod._start_turn(sid, "new user prompt"))
    finally:
        bc = chat_mod._active_turns.pop(sid, None)
        if bc is not None:
            bc.finish()
            bc.close()
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)


def test_draining_reservation_rechecks_background_owner(stream_env, client):
    """Interrupted-turn handoff must not bypass the background queue gate."""
    chat_mod = stream_env
    sid = _make_session(client)

    async def exercise():
        draining = chat_mod.TurnBroadcast(sid, model="claude-sonnet-4-6")
        draining.cancelled = True
        chat_mod._active_turns[sid] = draining

        async def hand_off_to_watcher():
            await asyncio.sleep(0)
            chat_mod._sessions_with_inflight_tasks[sid] = {"task-1"}
            draining.finish()
            chat_mod._active_turns.pop(sid, None)

        handoff = asyncio.create_task(hand_off_to_watcher())
        try:
            with pytest.raises(chat_mod._TurnBusy):
                await chat_mod._start_turn(sid, "queue after interrupt")
            await handoff
        finally:
            draining.close()
            chat_mod._active_turns.pop(sid, None)
            chat_mod._sessions_with_inflight_tasks.pop(sid, None)

    asyncio.run(exercise())


def test_stream_busy_response_is_machine_queueable(stream_env, client):
    chat_mod = stream_env
    sid = _make_session(client)
    chat_mod._sessions_with_inflight_tasks[sid] = {"task-1"}
    try:
        response = client.get(
            f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
            "&prompt=queue-me&model=claude-sonnet-4-6"
        )
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)

    assert response.status_code == 200
    assert '"kind": "turn_busy"' in response.text
    assert '"cta": "queue"' in response.text


def test_pooled_stream_attaches_before_query_and_parks_leftovers(stream_env):
    """Keep the fast-response ordering and lifecycle handoff explicit."""
    source = inspect.getsource(stream_env._start_turn)
    pooled = source[source.index("stream = _stream_for(client)"):]

    assert pooled.index("turn_q = stream.attach_turn()") < pooled.index(
        "await _send_query()"
    )
    assert "stream.park_unconsumed(turn_q)" in pooled


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


def test_replay_spool_uses_private_configured_runtime_dir(
        stream_env, monkeypatch, tmp_path):
    chat_mod = stream_env
    runtime_dir = tmp_path / "durable-runtime"
    monkeypatch.setenv("MUSELAB_RUNTIME_DIR", str(runtime_dir))

    spool = chat_mod._ReplaySpool()
    try:
        assert spool.path.parent == runtime_dir
        assert runtime_dir.stat().st_mode & 0o777 == 0o700
        assert spool.path.stat().st_mode & 0o777 == 0o600
    finally:
        spool.close()


def test_replay_spool_rolls_back_partial_enospc(
        stream_env, monkeypatch, tmp_path):
    chat_mod = stream_env
    monkeypatch.setenv("MUSELAB_RUNTIME_DIR", str(tmp_path / "runtime"))
    spool = chat_mod._ReplaySpool()
    real_write = chat_mod.os.write
    calls = 0

    def partial_then_full(fd, blob):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, blob[:max(1, len(blob) // 2)])
        raise OSError(28, "disk full private detail")

    try:
        monkeypatch.setattr(chat_mod.os, "write", partial_then_full)
        with pytest.raises(OSError) as failure:
            spool.append({"event": "done", "data": "{}"})
        assert failure.value.errno == 28
        assert spool.size() == 0
        assert len(spool) == 0
        assert spool.path.read_bytes() == b""

        monkeypatch.setattr(chat_mod.os, "write", real_write)
        spool.append({"event": "done", "data": "{}"})
        assert list(spool) == [{"event": "done", "data": "{}"}]
    finally:
        spool.close()


def test_replay_spool_becomes_unusable_when_partial_rollback_fails(
        stream_env, monkeypatch, tmp_path):
    chat_mod = stream_env
    monkeypatch.setenv("MUSELAB_RUNTIME_DIR", str(tmp_path / "runtime"))
    spool = chat_mod._ReplaySpool()
    real_write = chat_mod.os.write
    calls = 0

    def partial_then_enospc(fd, blob):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, blob[:1])
        raise OSError(28, "disk full")

    monkeypatch.setattr(chat_mod.os, "write", partial_then_enospc)
    monkeypatch.setattr(
        chat_mod.os, "ftruncate",
        lambda *_args: (_ for _ in ()).throw(OSError(5, "rollback failed")),
    )
    try:
        with pytest.raises(OSError) as failure:
            spool.append({"event": "done", "data": "{}"})
        assert failure.value.errno == 28
        with pytest.raises(RuntimeError, match="unusable"):
            spool.append({"event": "done", "data": "{}"})
    finally:
        spool.close()


def test_replay_corruption_is_typed_and_subscriber_resyncs_once(
        stream_env, monkeypatch, tmp_path):
    chat_mod = stream_env
    monkeypatch.setenv("MUSELAB_RUNTIME_DIR", str(tmp_path / "runtime"))
    spool = chat_mod._ReplaySpool()
    spool.path.write_bytes(b'{"event":"done","data":7}\n')
    try:
        with pytest.raises(chat_mod._ReplayRecordCorruption):
            list(spool)

        async def consume():
            subscriber = chat_mod._TurnSubscriber(spool.open_reader())
            first = await subscriber.get()
            second = await subscriber.get()
            return first, second

        first, second = asyncio.run(consume())
        assert first["event"] == "resync"
        assert json.loads(first["data"]) == {
            "reason": "replay_corrupt", "retryable": True,
        }
        assert second is None
    finally:
        spool.close()


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


def test_slow_live_subscriber_receives_final_text_before_done(stream_env):
    """A terminal spool row must never overtake queued live text deltas."""
    import asyncio
    import json

    chat_mod = stream_env

    async def exercise():
        bc = chat_mod.TurnBroadcast(session_id="ordered-final-text")
        live = bc.subscribe()
        # Deliberately do not consume between publishes. This is the browser
        # backpressure shape that used to produce done -> text and lose the
        # final bubble when EventSource closed on done.
        bc.publish({"event": "text", "data": json.dumps({"text": "FINAL"})})
        bc.publish({"event": "done", "data": "{}"})
        bc.finish()

        received = []
        while True:
            event = await live.get()
            if event is None:
                break
            received.append(event)
        assert [event["event"] for event in received] == ["text", "done"]
        assert json.loads(received[0]["data"])["text"] == "FINAL"
        bc.close()

    asyncio.run(exercise())


def test_slow_live_subscriber_preserves_text_tool_text_done_order(stream_env):
    """Each coalesced segment drains only to its own live delimiter."""
    import asyncio
    import json

    chat_mod = stream_env

    async def exercise():
        bc = chat_mod.TurnBroadcast(session_id="ordered-multi-segment")
        live = bc.subscribe()
        for chunk in ("A1", "A2"):
            bc.publish({"event": "text", "data": json.dumps({"text": chunk})})
        bc.publish({"event": "tool_result", "data": json.dumps({"id": "tool"})})
        for chunk in ("B1", "B2"):
            bc.publish({"event": "text", "data": json.dumps({"text": chunk})})
        bc.publish({"event": "done", "data": "{}"})
        bc.finish()

        received = []
        while True:
            event = await live.get()
            if event is None:
                break
            received.append(event)
        assert [event["event"] for event in received] == [
            "text", "text", "tool_result", "text", "text", "done",
        ]
        assert [
            json.loads(event["data"])["text"]
            for event in received if event["event"] == "text"
        ] == ["A1", "A2", "B1", "B2"]
        bc.close()

    asyncio.run(exercise())


def test_waiting_live_subscriber_emits_resync_after_backlog_overflow(
        stream_env, monkeypatch):
    """Overflow can close the replay reader while get() is asleep."""
    chat_mod = stream_env
    monkeypatch.setattr(chat_mod, "_BROADCAST_LIVE_DELTA_MAX", 2)

    async def exercise():
        bc = chat_mod.TurnBroadcast(session_id="overflow-resync")
        live = bc.subscribe()
        waiting = asyncio.create_task(live.get())
        await asyncio.sleep(0)
        # No await between publishes: resync closes the reader in the same
        # event-loop tick that wakes the sleeping subscriber.
        for text in ("A", "B", "C"):
            bc.publish({"event": "text", "data": json.dumps({"text": text})})
        event = await waiting
        assert event["event"] == "resync"
        assert json.loads(event["data"])["reason"] == "live_backlog"
        assert await live.get() is None
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


def test_shutdown_closes_active_and_recent_broadcast_spools(
        stream_env, monkeypatch, tmp_path):
    chat_mod = stream_env
    monkeypatch.setenv("MUSELAB_RUNTIME_DIR", str(tmp_path / "runtime"))
    active = chat_mod.TurnBroadcast("shutdown-active")
    recent = chat_mod.TurnBroadcast("shutdown-recent")
    active_path = active.events.path
    recent_path = recent.events.path
    chat_mod._active_turns[active.session_id] = active
    chat_mod._recent_turns[recent.session_id] = recent

    asyncio.run(chat_mod.shutdown_runtime())

    assert not active_path.exists()
    assert not recent_path.exists()
    assert active.session_id not in chat_mod._active_turns
    assert recent.session_id not in chat_mod._recent_turns


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

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort="", service_tier=""):
        return _BoomClient()

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)

    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=hi&model=claude-sonnet-4-6")
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    err = next((json.loads(d) for e, d in events if e == "error"), None)
    assert err is not None, f"no error frame: {events}"
    assert err["kind"] == "auth", f"misclassified: {err}"
    assert err["activity_source"] == "direct"
    assert err["cta"] == "open_settings"
    assert err["retryable"] is False
    # Reservation released even on error so the user can retry.
    assert sid not in chat_mod._active_turns


def test_terminal_snapshot_failure_keeps_primary_error_frame(
        stream_env, client, monkeypatch, capsys):
    chat_mod = stream_env
    sid = _make_session(client)
    primary = "HTTP 401 primary-auth-failure"
    secondary = "PRIVATE_SNAPSHOT_DETAIL"

    class BoomClient:
        async def query(self, _prompt):
            return None

        async def receive_response(self):
            raise RuntimeError(primary)
            yield  # pragma: no cover

    async def fake_get_client(*_args, **_kwargs):
        return BoomClient()

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(
        chat_mod,
        "_persist_failed_turn_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(secondary)),
    )

    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=hi&model=claude-sonnet-4-6")
    error = next(
        json.loads(data)
        for event, data in _parse_sse(response.text)
        if event == "error"
    )

    assert primary in error["error"]
    assert error["snapshot_ready"] is False
    diagnostics = capsys.readouterr().err
    assert "stage=terminal_snapshot" in diagnostics
    assert secondary not in diagnostics
    assert primary not in diagnostics


def test_terminal_replay_failure_keeps_primary_failure_state(
        stream_env, client, monkeypatch, capsys):
    chat_mod = stream_env
    sid = _make_session(client)
    primary = "PRIMARY_STREAM_FAILURE"
    secondary = "PRIVATE_REPLAY_FAILURE"
    recorded = []

    class BoomClient:
        async def query(self, _prompt):
            return None

        async def receive_response(self):
            raise RuntimeError(primary)
            yield  # pragma: no cover

    async def fake_get_client(*_args, **_kwargs):
        return BoomClient()

    real_append = chat_mod._ReplaySpool.append

    def fail_error_record(spool, event):
        if event.get("event") == "error":
            raise OSError(28, secondary)
        return real_append(spool, event)

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(chat_mod, "_persist_failed_turn_snapshot",
                        lambda *_args, **_kwargs: True)
    monkeypatch.setattr(chat_mod._ReplaySpool, "append", fail_error_record)
    monkeypatch.setattr(
        chat_mod.mem0, "schedule_failed",
        lambda *args, **_kwargs: recorded.append(args) or True,
    )

    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=hi&model=claude-sonnet-4-6")

    assert response.status_code == 200
    assert len(recorded) == 1
    assert primary in recorded[0][4]
    assert secondary not in recorded[0][4]
    diagnostics = capsys.readouterr().err
    assert "stage=terminal_replay" in diagnostics
    assert secondary not in diagnostics
    assert primary not in diagnostics
    assert sid not in chat_mod._active_turns


def test_stream_refuses_submission_when_pending_intent_write_fails(
        stream_env, client, monkeypatch):
    chat_mod = stream_env
    sid = _make_session(client)
    client_calls = []

    async def should_not_start(*_args, **_kwargs):
        client_calls.append(True)
        raise AssertionError("SDK startup must follow durable intent commit")

    monkeypatch.setattr(chat_mod, "get_client", should_not_start)
    monkeypatch.setattr(
        chat_mod, "_write_active_turn_sidecar", lambda _broadcast: False)

    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=keep-me&model=claude-sonnet-4-6",
    )
    assert response.status_code == 200
    error = next(
        json.loads(data)
        for event, data in _parse_sse(response.text)
        if event == "error"
    )
    assert "could not be persisted" in error["error"]
    assert client_calls == []
    assert sid not in chat_mod._active_turns


def test_stream_early_get_client_failure_emits_error_frame(stream_env, client, monkeypatch):
    """If get_client itself raises (e.g. auth pre-check), the handler must
    surface an SSE error frame, NOT bubble a 500 — the FE can only render
    typed errors from the frame, not from a 500."""
    from backend import activity as activity_module

    chat_mod = stream_env
    sid = _make_session(client)
    activity_transitions = []

    async def boom_get_client(session_id, model, permission="bypassPermissions", effort="", service_tier=""):
        from claude_agent_sdk import ClaudeSDKError
        raise ClaudeSDKError("Claude model requires auth: run `claude login`")

    monkeypatch.setattr(chat_mod, "get_client", boom_get_client)
    monkeypatch.setattr(
        activity_module.activity,
        "start",
        lambda activity_sid, *, summary="", activity_source="", owner_id="": activity_transitions.append(
            ("start", activity_sid, summary)),
    )
    monkeypatch.setattr(
        activity_module.activity,
        "finish",
        lambda activity_sid, status, *, activity_source="", owner_id="", mark_read=None: activity_transitions.append(
            ("finish", activity_sid, status)),
    )

    r = client.get(f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
                   f"&prompt=hi&model=claude-sonnet-4-6")
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    err = next((json.loads(d) for e, d in events if e == "error"), None)
    assert err is not None, f"no error frame: {events}"
    assert err["kind"] == "auth"
    assert err["activity_source"] == "direct"
    assert sid not in chat_mod._active_turns
    assert activity_transitions == [
        ("start", sid, "hi"),
        ("finish", sid, "failed"),
    ]
    assert not chat_mod._active_turn_path(sid).exists()
    history = client.get(
        f"/api/chat/sessions/{sid}",
        params={"tail": 20},
        headers={"X-Auth-Token": TEST_TOKEN},
    )
    user = next(
        message for message in history.json()["messages"]
        if message.get("role") == "user"
    )
    assert user["text"] == "hi"
    assert user["_failed"] is True
    assert user["_error_kind"] == "auth"
    assert user["_error_cta"] == "open_settings"
    assert user["_error_retryable"] is False


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

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort="", service_tier=""):
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

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort="", service_tier=""):
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

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort="", service_tier=""):
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
        chat_mod._ensure_session_stream(("sid", "m", "auto", ""), fake)
        try:
            # The bug's signature was a hang, so the bound is the assertion.
            got = await asyncio.wait_for(
                chat_mod._run_sdk_command_checked(fake, "/compact"), 5)
        finally:
            await chat_mod._drop_session_streams("sid")
        assert fake.queries == ["/compact"]
        return got

    assert asyncio.run(go()) is result


def test_compact_tail_outcome_reads_only_new_native_records(
        stream_env, tmp_path):
    chat_mod = stream_env
    transcript = tmp_path / "compact-tail.jsonl"
    transcript.write_text(
        json.dumps({
            "type": "system", "subtype": "local_command",
            "content": "old context window error",
        }) + "\n",
        encoding="utf-8",
    )
    offset = transcript.stat().st_size
    with transcript.open("a", encoding="utf-8") as handle:
        for entry in (
            {"type": "system", "subtype": "compact_boundary"},
            {"type": "user", "isCompactSummary": True},
            {
                "type": "system", "subtype": "local_command",
                "data": {"content": "API Error: input exceeds the context window"},
            },
        ):
            handle.write(json.dumps(entry) + "\n")

    assert chat_mod._compact_tail_outcome(transcript, offset) == {
        "boundary": True,
        "summary": True,
        "context_error": True,
    }


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
        key = ("dead-session", "glm-5.2-internal", "auto", "")
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
        stream = chat_mod._SessionStream(("sid", "m", "auto", ""), IdleClient())
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

    async def fake_get_client(session_id, model, permission="bypassPermissions", effort="", service_tier=""):
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


def test_preflight_queues_while_background_watcher_owns_sdk_stream(
        stream_env, client, monkeypatch):
    """Neither compact nor a prompt may cross a watcher's response boundary."""
    chat_mod = stream_env
    sid = _make_session(client)
    answer = [
        AssistantMessage(
            content=[TextBlock(text="foreground reply")],
            model="claude-sonnet-4-6", uuid="a-foreground",
            usage={"input_tokens": 4, "output_tokens": 2},
        ),
        ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=9,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.0,
            usage={"input_tokens": 4, "output_tokens": 2},
            result="foreground reply", uuid="r-foreground",
        ),
    ]
    fake = _FakeStreamClient(answer)

    async def near_limit_context():
        return {
            "maxTokens": 200_000, "rawMaxTokens": 200_000,
            "autoCompactThreshold": 160_000, "totalTokens": 190_000,
        }

    fake.get_context_usage = near_limit_context

    async def fake_get_client(
            session_id, model, permission="bypassPermissions", effort="",
            service_tier=""):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(chat_mod, "_session_has_live_watcher", lambda _sid: True)
    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=send-once&model=claude-sonnet-4-6",
    )
    events = _parse_sse(response.text)

    assert fake.queried == []
    assert not [event for event, _data in events if event == "compact_progress"]
    busy = [json.loads(data) for event, data in events if event == "error"]
    assert busy and busy[-1]["kind"] == "turn_busy"
    assert busy[-1]["cta"] == "queue"


def test_codex_preflight_rebuilds_stalled_runtime_and_retries_once(
        stream_env, client, monkeypatch):
    """An old Codex CLI runtime must not leave a session permanently dead.

    A no-op /compact is retried once on a fresh process, whose explicit
    auto-compact window can recover the existing transcript.  The user's real
    prompt is sent exactly once, and only after a measured token drop.
    """
    chat_mod = stream_env
    response = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN,
                 "Content-Type": "application/json"},
        json={"name": "codex compact recovery", "model": "codex:gpt-5.6-sol"},
    )
    assert response.status_code == 200, response.text
    sid = response.json()["id"]
    chat_mod.sess.update_model(sid, "codex:gpt-5.6-sol")

    compact_error = ResultMessage(
        subtype="error", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id=sid,
        result="Your input exceeds the context window of this model",
        api_error_status=400,
    )
    answer = [
        AssistantMessage(
            content=[TextBlock(text="recovered")],
            model="gpt-5.6-sol", uuid="a-recovered",
            usage={"input_tokens": 4, "output_tokens": 2},
        ),
        ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=9,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.0,
            usage={"input_tokens": 4, "output_tokens": 2},
            result="recovered", uuid="r-recovered",
        ),
    ]
    stale = _FakeStreamClient([compact_error])
    fresh = _FakeBatchedStreamClient([answer])
    stale_reads = 0

    async def stale_context():
        nonlocal stale_reads
        stale_reads += 1
        return {
            "maxTokens": 320_000, "rawMaxTokens": 320_000,
            "autoCompactThreshold": 287_000, "totalTokens": 310_000,
        }

    async def fresh_context():
        return {
            "maxTokens": 320_000, "rawMaxTokens": 320_000,
            "autoCompactThreshold": 287_000,
            "totalTokens": 60_000,
        }

    stale.get_context_usage = stale_context
    fresh.get_context_usage = fresh_context
    clients = [stale, fresh]

    async def fake_get_client(
            session_id, model, permission="bypassPermissions", effort="",
            service_tier="", plan_return_permission=""):
        return clients.pop(0)

    disconnected = []

    async def fake_disconnect(session_id):
        disconnected.append(session_id)

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(chat_mod, "disconnect_client", fake_disconnect)
    monkeypatch.setattr(chat_mod, "_is_codex_gateway_model", lambda _model: True)
    capability = chat_mod._capability_from_model_item(
        {"max_input_tokens": 320_000}, source="test")
    monkeypatch.setattr(
        chat_mod, "_detect_gateway_context_capability",
        lambda _model: asyncio.sleep(0, result=capability),
    )

    result = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=send-once&model=codex:gpt-5.6-sol",
    )
    events = _parse_sse(result.text)

    assert disconnected == [sid]
    assert stale.queried == ["/compact"]
    # The fresh runtime sees the compact boundary written by the stale one;
    # never summarize the already-shrunk transcript a second time.
    assert fresh.queried == ["send-once"]
    assert not [data for event, data in events if event == "error"]
    assert any(event == "text" and "recovered" in data
               for event, data in events)


def test_codex_preflight_fresh_runtime_retries_compact_once_when_still_full(
        stream_env, client, monkeypatch):
    """A genuinely unchanged fresh runtime gets one, and only one, retry."""
    chat_mod = stream_env
    sid = _make_session(client)
    compact_error = ResultMessage(
        subtype="error", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id=sid,
        result="Your input exceeds the context window of this model",
        api_error_status=400,
    )
    compact_ok = ResultMessage(
        subtype="success", duration_ms=2, duration_api_ms=1,
        is_error=False, num_turns=1, session_id=sid, result="Compacted",
    )
    answer = [
        AssistantMessage(
            content=[TextBlock(text="recovered after retry")],
            model="gpt-5.6-sol", uuid="a-retry", usage={}),
        ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=9,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.0, usage={}, result="recovered", uuid="r-retry"),
    ]
    stale = _FakeStreamClient([compact_error])
    fresh = _FakeBatchedStreamClient([[compact_ok], answer])

    async def stale_context():
        return {"maxTokens": 320_000, "rawMaxTokens": 320_000,
                "autoCompactThreshold": 287_000, "totalTokens": 310_000}

    fresh_reads = 0

    async def fresh_context():
        nonlocal fresh_reads
        fresh_reads += 1
        return {"maxTokens": 320_000, "rawMaxTokens": 320_000,
                "autoCompactThreshold": 287_000,
                "totalTokens": 310_000 if fresh_reads == 1 else 60_000}

    stale.get_context_usage = stale_context
    fresh.get_context_usage = fresh_context
    clients = [stale, fresh]

    async def fake_get_client(*_args, **_kwargs):
        return clients.pop(0)

    disconnected = []

    async def fake_disconnect(target_sid):
        disconnected.append(target_sid)

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(chat_mod, "disconnect_client", fake_disconnect)
    monkeypatch.setattr(chat_mod, "_is_codex_gateway_model", lambda _m: True)
    capability = chat_mod._capability_from_model_item(
        {"max_input_tokens": 320_000}, source="test")
    monkeypatch.setattr(
        chat_mod, "_detect_gateway_context_capability",
        lambda _m: asyncio.sleep(0, result=capability),
    )

    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=send-once&model=codex:gpt-5.6-sol",
    )
    events = _parse_sse(response.text)
    assert disconnected == [sid]
    assert stale.queried == ["/compact"]
    assert fresh.queried == ["/compact", "send-once"]
    assert not [data for event, data in events if event == "error"]
    assert any(event == "text" and "recovered after retry" in data
               for event, data in events)


def test_codex_preflight_probe_context_error_recovers_from_cached_usage(
        stream_env, client, monkeypatch):
    """A poisoned control probe recovers without sending the real prompt."""
    chat_mod = stream_env
    sid = _make_session(client)
    chat_mod.sess.update_model(sid, "codex:gpt-5.6-sol")
    fake = _FakeStreamClient([])

    async def failed_context_probe():
        raise RuntimeError(
            "API Error: 400 Your input exceeds the context window of this model"
        )

    fake.get_context_usage = failed_context_probe
    recovered_id = "3e41f694-7481-49d3-94c7-2fca83c2f8a3"
    recoveries = []

    async def fake_get_client(*_args, **_kwargs):
        return fake

    async def fake_recover(target_sid, model, *, pre_tokens, context_limit):
        recoveries.append((target_sid, model, pre_tokens, context_limit))
        return {
            "session": {
                "id": recovered_id,
                "session_id": recovered_id,
                "name": "Recovered",
                "model": model,
            },
            "stats": {"estimated_post_tokens": 24_000},
        }

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(chat_mod, "_recover_context_session", fake_recover)
    monkeypatch.setattr(chat_mod, "_is_codex_gateway_model", lambda _m: True)
    monkeypatch.setattr(
        chat_mod,
        "_heal_unreachable_locked_model",
        lambda _sid, locked, _requested: locked,
    )
    monkeypatch.setitem(chat_mod._session_usage, sid, {
        "input_tokens": 300_000,
        "cache_read_tokens": 42_000,
        "cache_creation_tokens": 22_270,
        "context_used": 364_270,
        "context_limit": 353_400,
    })

    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=must-not-send&model=codex:gpt-5.6-sol",
    )
    events = _parse_sse(response.text)
    error = next(json.loads(data) for event, data in events if event == "error")

    assert fake.queried == []
    assert recoveries == [(
        sid, "codex:gpt-5.6-sol", 364_270, 353_400,
    )]
    assert error["kind"] == "context_window"
    assert error["recovered_session"]["id"] == recovered_id


def test_codex_preflight_compact_context_error_recovers_when_probe_unavailable(
        stream_env, client, monkeypatch):
    """Repeated context rejects recover even when neither probe is available."""
    chat_mod = stream_env
    sid = _make_session(client)
    chat_mod.sess.update_model(sid, "codex:gpt-5.6-sol")

    compact_error = ResultMessage(
        subtype="error", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id=sid,
        result="Your input exceeds the context window of this model",
        api_error_status=400,
    )
    stale = _FakeStreamClient([compact_error])
    fresh = _FakeStreamClient([compact_error])
    context_reads = {"stale": 0, "fresh": 0}

    async def stale_context():
        context_reads["stale"] += 1
        if context_reads["stale"] > 1:
            raise RuntimeError("post-compact context probe unavailable")
        return {"maxTokens": 320_000, "rawMaxTokens": 320_000,
                "autoCompactThreshold": 287_000, "totalTokens": 310_000}

    async def fresh_context():
        context_reads["fresh"] += 1
        raise RuntimeError("fresh context probe unavailable")

    stale.get_context_usage = stale_context
    fresh.get_context_usage = fresh_context
    clients = [stale, fresh]
    get_calls = []

    async def fake_get_client(*args, **_kwargs):
        get_calls.append(args)
        return clients.pop(0)

    recoveries = []

    async def fake_recover(target_sid, model, *, pre_tokens, context_limit):
        recoveries.append((target_sid, model, pre_tokens, context_limit))
        recovered_id = "b0dc1a95-b1ab-42d6-bd0c-acde3b4bdb20"
        return {
            "session": {
                "id": recovered_id,
                "session_id": recovered_id,
                "name": "Recovered",
                "model": model,
            },
            "stats": {
                "included_messages": 12,
                "omitted_messages": 4,
                "truncated_messages": 1,
                "estimated_post_tokens": 24000,
            },
        }

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(chat_mod, "_recover_context_session", fake_recover)
    monkeypatch.setattr(chat_mod, "_is_codex_gateway_model", lambda _m: True)
    monkeypatch.setattr(
        chat_mod, "_heal_unreachable_locked_model",
        lambda _sid, locked, _requested: locked,
    )
    monkeypatch.setattr(
        chat_mod, "_detect_gateway_context_capability",
        lambda _m: asyncio.sleep(0, result={
            "context_limit": 320_000,
            "context_raw_limit": 320_000,
            "context_max_limit": 320_000,
            "context_effective_percent": 100,
            "catalog_auto_compact_threshold": 0,
            "context_limit_source": "test_catalog",
            "context_limit_is_estimate": False,
        }),
    )

    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=must-not-send&model=codex:gpt-5.6-sol",
    )
    events = _parse_sse(response.text)
    error = next(json.loads(data) for event, data in events if event == "error")
    assert error["kind"] == "context_window"
    assert error["recovered_session"]["id"] == (
        "b0dc1a95-b1ab-42d6-bd0c-acde3b4bdb20")
    assert error["recovery_stats"]["estimated_post_tokens"] == 24000
    assert len(get_calls) == 2
    assert context_reads == {"stale": 2, "fresh": 2}
    assert stale.queried == ["/compact"]
    assert fresh.queried == ["/compact"]
    assert recoveries == [(
        sid, "codex:gpt-5.6-sol", 310_000, 320_000,
    )]


def test_codex_preflight_context_tail_skips_pointless_second_compact(
        stream_env, client, monkeypatch):
    """A transcript-level context 400 goes straight to offline recovery."""
    chat_mod = stream_env
    sid = _make_session(client)
    chat_mod.sess.update_model(sid, "codex:gpt-5.6-sol")
    compact_error = ResultMessage(
        subtype="error", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id=sid,
        result="Your input exceeds the context window of this model",
        api_error_status=400,
    )
    stale = _FakeStreamClient([compact_error])

    async def full_context():
        return {"maxTokens": 372_000, "rawMaxTokens": 372_000,
                "autoCompactThreshold": 335_000, "totalTokens": 364_270}

    stale.get_context_usage = full_context
    recovered_id = "99fc776a-a812-4a53-baa2-c932fd0a4412"
    recovery_calls = []

    async def fake_recover(target_sid, model, *, pre_tokens, context_limit):
        recovery_calls.append((target_sid, pre_tokens, context_limit))
        return {
            "session": {
                "id": recovered_id, "session_id": recovered_id,
                "name": "oversize · recovery", "model": model,
            },
            "stats": {"estimated_post_tokens": 20_000},
        }

    get_calls = []

    async def fake_get_client(*args, **_kwargs):
        get_calls.append(args)
        return stale

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(chat_mod, "_is_codex_gateway_model", lambda _m: True)
    monkeypatch.setattr(
        chat_mod, "_heal_unreachable_locked_model",
        lambda _sid, locked, _requested: locked,
    )
    capability = chat_mod._capability_from_model_item(
        {"max_input_tokens": 372_000}, source="test")
    monkeypatch.setattr(
        chat_mod, "_detect_gateway_context_capability",
        lambda _m: asyncio.sleep(0, result=capability),
    )
    monkeypatch.setattr(
        chat_mod, "_compact_tail_outcome",
        lambda _path, _offset: {
            "boundary": False, "summary": False, "context_error": True,
        },
    )
    monkeypatch.setattr(chat_mod, "_recover_context_session", fake_recover)

    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=must-not-hit-gateway&model=codex:gpt-5.6-sol",
    )
    error = next(
        json.loads(data) for event, data in _parse_sse(response.text)
        if event == "error"
    )

    assert len(get_calls) == 1
    assert stale.queried == ["/compact"]
    assert recovery_calls == [(sid, 364_270, 372_000)]
    assert error["recovered_session"]["id"] == recovered_id
    assert error["activity_source"] == "direct"


def test_preflight_failure_snapshot_is_anchored_after_long_history(
        stream_env, client, monkeypatch, tmp_path):
    """A compact failure appends its durable bubble instead of jumping to top."""
    chat_mod = stream_env
    sid = _make_session(client)
    transcript = tmp_path / f"{sid}.jsonl"
    entries = []
    parent = None
    for index in range(12):
        user_uuid = f"old-user-{index}"
        assistant_uuid = f"old-assistant-{index}"
        entries.extend([
            {
                "uuid": user_uuid,
                "parentUuid": parent,
                "type": "user",
                "sessionId": sid,
                "message": {"content": f"old prompt {index}"},
            },
            {
                "uuid": assistant_uuid,
                "parentUuid": user_uuid,
                "type": "assistant",
                "sessionId": sid,
                "message": {"content": f"old answer {index}"},
            },
        ])
        parent = assistant_uuid
    transcript.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries),
        encoding="utf-8",
    )
    chat_mod._JSONL_PATH_CACHE[sid] = transcript

    compact_error = ResultMessage(
        subtype="error", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id=sid,
        result="Your input exceeds the context window of this model",
        api_error_status=400,
    )
    fake = _FakeStreamClient([compact_error])

    async def full_context():
        return {"maxTokens": 320_000, "rawMaxTokens": 320_000,
                "autoCompactThreshold": 287_000, "totalTokens": 310_000}

    fake.get_context_usage = full_context

    async def fake_get_client(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(chat_mod, "_is_codex_gateway_model", lambda _m: False)
    try:
        response = client.get(
            f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
            "&prompt=new-failed-prompt&model=codex:gpt-5.6-sol",
        )
        error = next(
            json.loads(data)
            for event, data in _parse_sse(response.text)
            if event == "error"
        )
        assert error["snapshot_ready"] is True

        history = client.get(
            f"/api/chat/sessions/{sid}",
            headers={"X-Auth-Token": TEST_TOKEN},
            params={"tail": 6},
        )
        assert history.status_code == 200, history.text
        messages = history.json()["messages"]
        assert messages[-2]["role"] == "user"
        assert messages[-2]["text"] == "new-failed-prompt"
        assert messages[-1]["role"] == "assistant"
        assert "Your input exceeds the context window" in messages[-1]["text"]
        assert messages[-3]["text"] == "old answer 11"
        assert messages[-1]["turn_status"] == "failed"
    finally:
        chat_mod._JSONL_PATH_CACHE.pop(sid, None)


def test_codex_preflight_fresh_probe_recovers_after_stale_measurement_failure(
        stream_env, client, monkeypatch):
    """A successful boundary observed by the new process skips duplicate compact."""
    chat_mod = stream_env
    sid = _make_session(client)
    compact_ok = ResultMessage(
        subtype="success", duration_ms=2, duration_api_ms=1,
        is_error=False, num_turns=1, session_id=sid, result="Compacted",
    )
    answer = [
        AssistantMessage(
            content=[TextBlock(text="recovered from probe")],
            model="gpt-5.6-sol", uuid="a-probe", usage={}),
        ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=9,
            is_error=False, num_turns=1, session_id=sid,
            total_cost_usd=0.0, usage={}, result="ok", uuid="r-probe"),
    ]
    stale = _FakeStreamClient([compact_ok])
    fresh = _FakeBatchedStreamClient([answer])
    stale_reads = 0

    async def stale_context():
        nonlocal stale_reads
        stale_reads += 1
        if stale_reads > 1:
            raise RuntimeError("stale probe transport closed")
        return {"maxTokens": 320_000, "rawMaxTokens": 320_000,
                "autoCompactThreshold": 287_000, "totalTokens": 310_000}

    async def fresh_context():
        return {"maxTokens": 320_000, "rawMaxTokens": 320_000,
                "autoCompactThreshold": 287_000, "totalTokens": 60_000}

    stale.get_context_usage = stale_context
    fresh.get_context_usage = fresh_context
    clients = [stale, fresh]

    async def fake_get_client(*_args, **_kwargs):
        return clients.pop(0)

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(chat_mod, "disconnect_client", lambda _sid: asyncio.sleep(0))
    monkeypatch.setattr(chat_mod, "_is_codex_gateway_model", lambda _m: True)
    capability = chat_mod._capability_from_model_item(
        {"max_input_tokens": 320_000}, source="test")
    monkeypatch.setattr(
        chat_mod, "_detect_gateway_context_capability",
        lambda _m: asyncio.sleep(0, result=capability),
    )

    response = client.get(
        f"/api/chat/stream?token={TEST_TOKEN}&session_id={sid}"
        "&prompt=send-once&model=codex:gpt-5.6-sol",
    )
    events = _parse_sse(response.text)
    assert stale.queried == ["/compact"]
    assert fresh.queried == ["send-once"]
    assert not [data for event, data in events if event == "error"]


def test_watcher_publishes_settlement_into_live_turn_when_slot_is_busy(stream_env):
    """A background task settling in the turn-teardown window must still be
    reported.

    The pump routes to `self._turn or self._background`; a turn detaches its
    queue at ResultMessage while `_active_turns[sid]` is only popped later, in
    _pump_gen_to_broadcast's finally. A task settling inside that window is
    therefore handed to the WATCHER (the in-turn dispatch is already gone, so it
    can never report it) while _open_continuation still refuses to take the
    occupied slot. The old code published only `if cont is not None`, so dedup
    was won here and delivery happened nowhere: no toast, no card flip
    (2026-08-04, task b97zswye9). The live turn's broadcast is the carrier.
    """
    import asyncio

    chat_mod = stream_env
    sid = "sid-busy-slot"
    chat_mod._sessions_with_inflight_tasks[sid] = {"task_race"}
    # A live (not done) turn occupying the slot — exactly the teardown window.
    live = chat_mod.TurnBroadcast(session_id=sid, model="m")
    chat_mod._active_turns[sid] = live

    notif = TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="task_race",
        status="completed", output_file="/tmp/race.md", summary="ok",
        uuid="u-race", session_id=sid, tool_use_id="tu-race")
    fake_client = _FakeWatchClient([notif])

    async def run():
        await chat_mod._watch_inflight_tasks(
            sid, fake_client, {"task_race": "sleep 20"})

    try:
        asyncio.run(run())
        # No continuation was opened (the slot was busy) — so the event must
        # have landed on the live turn instead of being dropped.
        assert chat_mod._active_turns.get(sid) is live
        kinds = [e.get("event") for e in live.events]
        assert "task_notification" in kinds, f"settlement dropped: {kinds}"
        payload = json.loads(next(
            e for e in live.events
            if e.get("event") == "task_notification")["data"])
        assert payload["task_id"] == "task_race"
        assert payload["tool_use_id"] == "tu-race"
        assert payload["status"] == "completed"
        assert payload["output_file"] == "/tmp/race.md"
        # Settlement still unpinned the task.
        assert sid not in chat_mod._sessions_with_inflight_tasks
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)
        chat_mod._task_watchers.pop(sid, None)
        chat_mod._active_turns.pop(sid, None)
        chat_mod._recent_turns.pop(sid, None)


def test_merge_session_inflight_does_not_resurrect_watcher_settled_task(stream_env):
    """Only the in-turn dispatch pops the turn-local `inflight_tasks`, so a task
    the WATCHER settled stays in that dict. Merging it back in re-pinned a
    finished task into a fresh watcher generation, and the session then reported
    active:true while waiting for a notification that can never arrive twice
    (2026-08-04: `generation=3 pending=['b97zswye9', ...]`). The pin set is the
    sole authority."""
    chat_mod = stream_env
    sid = "sid-no-resurrect"
    try:
        # Watcher already settled task_gone → not in the pin set. task_live is.
        chat_mod._sessions_with_inflight_tasks[sid] = {"task_live"}
        turn_local = {
            "task_gone": {"tool_use_id": "tu_gone", "description": "settled"},
            "task_live": {"tool_use_id": "tu_live", "description": "running"},
        }

        merged = chat_mod._merge_session_inflight(sid, turn_local)

        assert set(merged) == {"task_live"}, \
            "a watcher-settled task was resurrected into the next watcher"
        # Turn-local metadata still enriches the surviving pin.
        assert merged["task_live"]["tool_use_id"] == "tu_live"
        assert merged["task_live"]["description"] == "running"
        # No pins at all → no spurious watcher spawn.
        assert chat_mod._merge_session_inflight("sid-none", turn_local) == {}
        assert chat_mod._merge_session_inflight("sid-none", {}) == {}
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)


def test_read_only_status_never_reaps_expired_background_fence(
    stream_env, client,
):
    """A timeout is not proof that an SDK-owned child process stopped.

    Session-list and /active polling used to synchronously reap an old pin,
    opening a queue-drain race outside the watcher.  Both read paths must keep
    reporting the fence until lifecycle recovery confirms a terminal message
    or disconnect.
    """
    import time as _time

    chat_mod = stream_env
    sid = _make_session(client)
    task_id = "task-expired-but-unconfirmed"
    try:
        chat_mod._pin_background_task(sid, task_id)
        chat_mod._bg_task_pinned_at[task_id] = (
            _time.time() - chat_mod._TASK_WATCH_TIMEOUT - 1)

        listing = client.get(
            "/api/chat/sessions",
            headers={"X-Auth-Token": TEST_TOKEN},
        )
        assert listing.status_code == 200
        listed = next(row for row in listing.json()["sessions"]
                      if row["id"] == sid)
        assert listed["background_active"] is True

        active = client.get(
            f"/api/chat/sessions/{sid}/active",
            headers={"X-Auth-Token": TEST_TOKEN},
        )
        assert active.status_code == 200
        assert active.json()["background_tasks_pending"] == 1
        assert chat_mod._sessions_with_inflight_tasks[sid] == {task_id}
    finally:
        chat_mod._release_task_pins(sid, {task_id})


def test_watcher_timeout_keeps_absolute_task_deadline_across_respawns(
    stream_env, monkeypatch,
):
    chat_mod = stream_env
    now = 10_000.0
    timeout = float(chat_mod._TASK_WATCH_TIMEOUT)
    monkeypatch.setattr(chat_mod.time, "time", lambda: now)
    try:
        chat_mod._bg_task_pinned_at["task_old"] = now - timeout + 100
        chat_mod._bg_task_pinned_at["task_new"] = now - timeout + 800

        first = chat_mod._task_watch_timeout_remaining(
            {"task_old", "task_new"})
        assert first == 800

        # A replacement watcher gets the remaining lease, not a fresh timeout.
        now += 125
        replacement = chat_mod._task_watch_timeout_remaining(
            {"task_old", "task_new"})
        assert replacement == 675
    finally:
        chat_mod._bg_task_pinned_at.pop("task_old", None)
        chat_mod._bg_task_pinned_at.pop("task_new", None)


def test_watcher_timeout_waits_for_terminal_stop_before_queue_drain(
    stream_env, monkeypatch,
):
    """A stop_task control ack alone cannot release the session fence."""
    chat_mod = stream_env
    sid = "sid-timeout-terminal"
    task_id = "task-timeout-terminal"
    queue_drains = []

    notification = TaskNotificationMessage(
        subtype="task_notification", data={}, task_id=task_id,
        status="stopped", output_file="", summary="stopped at deadline",
        uuid="task-timeout-terminal-msg", session_id=sid,
        tool_use_id="tool-timeout-terminal",
    )

    class _TimeoutStopClient:
        def __init__(self):
            self.stop_calls = []
            self.stop_requested = asyncio.Event()
            self.release_terminal = asyncio.Event()

        async def stop_task(self, requested):
            self.stop_calls.append(requested)
            self.stop_requested.set()

        async def receive_messages(self):
            # A fresh SDK receive iterator is attached for timeout recovery;
            # the iterator cancelled by the outer deadline may not be reused.
            await self.stop_requested.wait()
            await self.release_terminal.wait()
            yield notification

    fake = _TimeoutStopClient()

    async def fake_drain(drain_sid):
        queue_drains.append(drain_sid)

    async def exercise():
        chat_mod._pin_background_task(sid, task_id)
        monkeypatch.setattr(
            chat_mod, "_task_watch_timeout_remaining", lambda _ids: 0.01)
        monkeypatch.setattr(chat_mod, "_TASK_STOP_SETTLE_GRACE_S", 1.0)
        monkeypatch.setattr(chat_mod, "_maybe_drain_queue", fake_drain)
        watcher = asyncio.create_task(chat_mod._watch_inflight_tasks(
            sid, fake, {task_id: "sleep forever"}))
        chat_mod._task_watchers[sid] = watcher
        await fake.stop_requested.wait()
        # stop_task returned, but the terminal marker has not. The pin remains
        # and queue drain cannot run in this gap.
        assert chat_mod._sessions_with_inflight_tasks[sid] == {task_id}
        assert queue_drains == []
        fake.release_terminal.set()
        await asyncio.wait_for(watcher, timeout=1)

    try:
        asyncio.run(exercise())
        assert fake.stop_calls == [task_id]
        assert sid not in chat_mod._sessions_with_inflight_tasks
        assert queue_drains == [sid]
    finally:
        chat_mod._release_task_pins(sid, {task_id})
        chat_mod._task_watchers.pop(sid, None)
        chat_mod._active_turns.pop(sid, None)
        recent = chat_mod._recent_turns.pop(sid, None)
        if recent is not None:
            recent.close()


def test_watcher_timeout_disconnect_failure_keeps_fence_and_queue_paused(
    stream_env, monkeypatch,
):
    """Unconfirmed process cleanup remains fenced until a later retry wins."""
    chat_mod = stream_env
    sid = "sid-timeout-disconnect"
    task_id = "task-timeout-disconnect"
    queue_drains = []
    disconnect_attempts = []
    retry_seen = asyncio.Event()
    release_disconnect = asyncio.Event()

    class _TimeoutDisconnectClient:
        async def stop_task(self, _task_id):
            return None

        async def receive_messages(self):
            await asyncio.Event().wait()
            if False:  # pragma: no cover - make this an async generator
                yield None

    fake = _TimeoutDisconnectClient()

    async def fake_disconnect(disconnect_sid, disconnect_client):
        assert disconnect_sid == sid
        assert disconnect_client is fake
        disconnect_attempts.append(disconnect_sid)
        if len(disconnect_attempts) == 1:
            retry_seen.set()
            raise chat_mod.RuntimeCleanupTimeout("still stopping")
        await release_disconnect.wait()

    async def fake_drain(drain_sid):
        queue_drains.append(drain_sid)

    async def exercise():
        chat_mod._pin_background_task(sid, task_id)
        monkeypatch.setattr(
            chat_mod, "_task_watch_timeout_remaining", lambda _ids: 0.01)
        monkeypatch.setattr(chat_mod, "_TASK_STOP_SETTLE_GRACE_S", 0.01)
        monkeypatch.setattr(chat_mod, "_TASK_TERMINATION_RETRY_S", 0.01)
        monkeypatch.setattr(
            chat_mod, "_disconnect_background_task_owner", fake_disconnect)
        monkeypatch.setattr(chat_mod, "_maybe_drain_queue", fake_drain)
        watcher = asyncio.create_task(chat_mod._watch_inflight_tasks(
            sid, fake, {task_id: "sleep forever"}))
        chat_mod._task_watchers[sid] = watcher
        await asyncio.wait_for(retry_seen.wait(), timeout=1)
        # Cleanup has failed once and the retry is not yet confirmed. Nothing
        # may turn this into an unpin/drain window.
        assert chat_mod._sessions_with_inflight_tasks[sid] == {task_id}
        assert queue_drains == []
        release_disconnect.set()
        await asyncio.wait_for(watcher, timeout=1)

    try:
        asyncio.run(exercise())
        assert len(disconnect_attempts) >= 2
        assert sid not in chat_mod._sessions_with_inflight_tasks
        assert queue_drains == [sid]
    finally:
        chat_mod._release_task_pins(sid, {task_id})
        chat_mod._task_watchers.pop(sid, None)
        chat_mod._active_turns.pop(sid, None)
        recent = chat_mod._recent_turns.pop(sid, None)
        if recent is not None:
            recent.close()


def test_background_owner_disconnect_retry_clears_transient_failure(stream_env):
    """The safety fence must permit a later exact-owner cleanup to recover."""
    chat_mod = stream_env
    sid = "sid-disconnect-recovery"

    class _RetryDisconnectClient:
        def __init__(self):
            self.calls = 0

        async def disconnect(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient cleanup failure")

    fake = _RetryDisconnectClient()

    async def exercise():
        with pytest.raises(chat_mod.RuntimeCleanupTimeout):
            await chat_mod._disconnect_background_task_owner(sid, fake)
        # Let the tracking callback publish its sticky failure state.
        await asyncio.sleep(0)
        assert sid in chat_mod._session_disconnect_failed
        await chat_mod._disconnect_background_task_owner(sid, fake)
        await asyncio.sleep(0)

    try:
        asyncio.run(exercise())
        assert fake.calls == 2
        assert sid not in chat_mod._session_disconnect_failed
    finally:
        chat_mod._session_disconnect_failed.discard(sid)
        chat_mod._session_disconnect_tasks.pop(sid, None)


def test_watcher_without_a_task_pin_is_not_user_visible_active(stream_env):
    chat_mod = stream_env
    sid = "sid-watcher-without-pin"

    class LiveWatcher:
        @staticmethod
        def done():
            return False

    try:
        chat_mod._task_watchers[sid] = LiveWatcher()
        assert chat_mod.session_active_status(sid) == {
            "active": False,
            "background_tasks_pending": 0,
            "runtime_background_tasks_pending": 0,
            "runtime_continuation_pending": False,
            "runtime_ui_revision": "",
            "activity_source": "",
        }

        chat_mod._pin_background_task(sid, "task_live")
        active = chat_mod.session_active_status(sid)
        assert active["active"] is True
        assert active["background"] is True
        assert active["background_tasks_pending"] == 1
    finally:
        chat_mod._task_watchers.pop(sid, None)
        chat_mod._release_task_pins(sid, {"task_live"})


def test_unpinned_watcher_is_retired_after_foreground_consumes_terminal(
        stream_env):
    """A watcher that missed its notification must not live for a full lease."""
    chat_mod = stream_env
    sid = "sid-stale-watcher-after-foreground"

    async def run():
        blocker = asyncio.create_task(asyncio.Event().wait())
        chat_mod._task_watchers[sid] = blocker
        chat_mod._pin_background_task(sid, "task-foreground-won")
        # The foreground turn wins routing of TaskNotification and clears the
        # authoritative pin. The old watcher still waits on its own queue.
        assert chat_mod._settle_background_task(
            sid, "task-foreground-won") is True
        await chat_mod._retire_unpinned_task_watcher(sid)
        assert blocker.cancelled()
        assert sid not in chat_mod._task_watchers
        assert chat_mod._session_has_live_watcher(sid) is False

    try:
        asyncio.run(run())
    finally:
        chat_mod._task_watchers.pop(sid, None)
        chat_mod._release_task_pins(sid, {"task-foreground-won"})
