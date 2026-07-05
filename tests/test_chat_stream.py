"""Integration test for the SSE streaming main path GET /api/chat/stream.

This is the most complex core path (an 800+ line handler). We monkeypatch
get_client to return a fake ClaudeSDKClient whose receive_response() yields
canned SDK messages, then drive the real handler through TestClient and
assert the SSE frames the frontend depends on (text → tool_use → tool_result
→ done), plus an error-classification frame on the failure path.

No real network, no real CLI subprocess, no Anthropic API.
"""
import base64
import json
from types import SimpleNamespace

import pytest
from claude_agent_sdk import (
    AssistantMessage, UserMessage, ResultMessage, StreamEvent,
    TextBlock, ToolUseBlock, ToolResultBlock,
    TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage,
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

    attach_path = chat_mod._attachments_base() / sid / "pdf1.pdf"
    assert attach_path.read_bytes() == pdf_bytes

    assert fake.queried, "stream handler never called client.query"
    sent = fake.queried[0][0]
    content = sent["message"]["content"]
    assert content[0]["type"] == "document"
    assert content[0]["source"]["media_type"] == "application/pdf"
    text = content[1]["text"]
    assert "please read it" in text
    assert "Attached PDF files available on disk" in text
    assert "doc.pdf" in text
    assert str(attach_path) in text


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
    """Spec §13 orphan bug: a task launched in a prior turn whose watcher got
    cancelled by an intervening turn must be re-covered at turn end even though
    this turn's local inflight_tasks doesn't contain it. _merge_session_inflight
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
    notif = TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="task_9",
        status="completed", output_file="/tmp/o.md", summary="done",
        uuid="u", session_id=sid, tool_use_id="tu")
    # CLI auto-continue: the model reacts to the finished task.
    reaction = AssistantMessage(
        content=[TextBlock(text="Background research finished — summary above.")],
        model="claude-sonnet-4-6", usage={})
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
        kinds = [e.get("event") for e in bc.events]
        assert "task_notification" in kinds, f"no card flip: {kinds}"
        assert "text" in kinds, f"no reaction text: {kinds}"
        assert kinds[-1] == "done", f"missing terminal done: {kinds}"
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
