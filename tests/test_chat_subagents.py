from __future__ import annotations

from claude_agent_sdk import (
    AssistantMessage,
    SessionMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from backend import chat_subagents


SESSION_ID = "550e8400-e29b-41d4-a716-446655440000"


def _session_message(
    message_type: str,
    uuid: str,
    content,
    *,
    parent_tool_use_id: str | None = "toolu_parent",
    parent_agent_id: str | None = None,
    session_id: str = SESSION_ID,
) -> SessionMessage:
    return SessionMessage(
        type=message_type,
        uuid=uuid,
        session_id=session_id,
        message={"role": message_type, "content": content},
        parent_tool_use_id=parent_tool_use_id,
        parent_agent_id=parent_agent_id,
    )


def test_load_threads_uses_sdk_top_level_history_and_normalizes_blocks(monkeypatch):
    messages = [
        _session_message("user", "user-1", "inspect the file"),
        _session_message("assistant", "assistant-1", [
            {"type": "thinking", "thinking": "check first", "signature": "sig"},
            {"type": "text", "text": "Reading."},
            {
                "type": "tool_use",
                "id": "toolu_read",
                "name": "Read",
                "input": {"file_path": "/tmp/example.py"},
            },
        ]),
        _session_message("user", "user-result", [{
            "type": "tool_result",
            "tool_use_id": "toolu_read",
            "content": "print('ok')",
            "is_error": False,
        }]),
    ]
    calls = []
    monkeypatch.setattr(
        chat_subagents, "list_subagents",
        lambda session_id, directory=None: ["z-agent", "a-agent"],
    )

    def get_messages(session_id, agent_id, directory=None):
        calls.append((session_id, agent_id, directory))
        return messages if agent_id == "a-agent" else []

    monkeypatch.setattr(chat_subagents, "get_subagent_messages", get_messages)

    threads = chat_subagents.load_subagent_threads(
        SESSION_ID, directory="/workspace")

    assert [thread["agent_id"] for thread in threads] == ["a-agent", "z-agent"]
    assert calls == [
        (SESSION_ID, "a-agent", "/workspace"),
        (SESSION_ID, "z-agent", "/workspace"),
    ]
    attached = threads[0]
    assert attached["parent_tool_use_id"] == "toolu_parent"
    assert attached["parent_agent_id"] is None
    assert attached["orphaned"] is False
    assert [block["role"] for block in attached["blocks"]] == [
        "thinking", "assistant", "tool_use", "tool_result",
    ]
    assert attached["blocks"][0]["source_block_index"] == 0
    assert attached["blocks"][1]["block_id"] == (
        "subagent:toolu_parent:assistant-1:1:assistant")
    assert attached["blocks"][2]["summary"] == "/tmp/example.py"
    assert attached["blocks"][3]["tool_name"] == "Read"
    assert attached["blocks"][3]["text"] == "print('ok')"

    empty = threads[1]
    assert empty["orphaned"] is True
    assert empty["orphan_reason"] == "empty_transcript"
    assert empty["parent_tool_use_id"] is None


def test_missing_parent_metadata_stays_orphaned_and_never_guesses_card(monkeypatch):
    messages = [
        _session_message(
            "assistant", "assistant-orphan",
            [{"type": "text", "text": "orphan output"}],
            parent_tool_use_id=None,
        ),
    ]
    monkeypatch.setattr(
        chat_subagents, "list_subagents",
        lambda session_id, directory=None: ["orphan-agent"],
    )
    monkeypatch.setattr(
        chat_subagents, "get_subagent_messages",
        lambda session_id, agent_id, directory=None: messages,
    )

    thread = chat_subagents.load_subagent_threads(SESSION_ID)[0]

    assert thread["orphaned"] is True
    assert thread["orphan_reason"] == "missing_parent_metadata"
    assert thread["parent_tool_use_id"] is None
    block = thread["blocks"][0]
    assert block["parent_tool_use_id"] is None
    assert block["block_id"].startswith("subagent:orphan%3Aorphan-agent:")
    assert "toolu_parent" not in block["block_id"]


def test_conflicting_parent_metadata_orphans_the_whole_thread(monkeypatch):
    messages = [
        _session_message(
            "assistant", "assistant-a", "A", parent_tool_use_id="toolu_a"),
        _session_message(
            "assistant", "assistant-b", "B", parent_tool_use_id="toolu_b"),
    ]
    monkeypatch.setattr(
        chat_subagents, "list_subagents",
        lambda session_id, directory=None: ["mixed-agent"],
    )
    monkeypatch.setattr(
        chat_subagents, "get_subagent_messages",
        lambda session_id, agent_id, directory=None: messages,
    )

    thread = chat_subagents.load_subagent_threads(SESSION_ID)[0]

    assert thread["orphaned"] is True
    assert thread["orphan_reason"] == "conflicting_parent_metadata"
    assert thread["parent_tool_use_id"] is None
    assert {block["parent_tool_use_id"] for block in thread["blocks"]} == {None}


def test_stream_mux_requires_parent_uuid_index_and_matching_session():
    mux = chat_subagents.SubagentStreamMux(SESSION_ID)
    delta = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "hello"},
    }

    assert mux.feed(StreamEvent(
        uuid="message-1", session_id=SESSION_ID, event=delta,
        parent_tool_use_id=None,
    )) == []
    assert mux.feed(StreamEvent(
        uuid="message-1", session_id="other-session", event=delta,
        parent_tool_use_id="toolu_parent",
    )) == []
    assert mux.feed(StreamEvent(
        uuid="message-1", session_id=SESSION_ID,
        event={**delta, "index": None},
        parent_tool_use_id="toolu_parent",
    )) == []


def test_sidechain_predicate_still_suppresses_an_unrenderable_frame():
    malformed = StreamEvent(
        uuid="message-1",
        session_id=SESSION_ID,
        parent_tool_use_id="toolu_parent",
        event={
            "type": "content_block_delta",
            # No SDK content-block index: the mux must not invent one.
            "delta": {"type": "text_delta", "text": "unsafe"},
        },
    )
    mux = chat_subagents.SubagentStreamMux(SESSION_ID)

    assert chat_subagents.is_subagent_message(malformed) is True
    assert mux.feed(malformed) == []
    assert chat_subagents.is_subagent_message(AssistantMessage(
        content=[TextBlock(text="parent")],
        model="claude-sonnet-4-6",
        uuid="parent-message",
    )) is False


def test_stream_mux_interleaves_parents_without_cross_wiring_and_tracks_offsets():
    mux = chat_subagents.SubagentStreamMux(SESSION_ID)

    def delta(parent: str, text: str):
        return StreamEvent(
            uuid="shared-message",
            session_id=SESSION_ID,
            parent_tool_use_id=parent,
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        )

    a1 = mux.feed(delta("toolu_a", "A😀"))[0]["data"]
    b1 = mux.feed(delta("toolu_b", "B"))[0]["data"]
    a2 = mux.feed(delta("toolu_a", " tail"))[0]["data"]

    assert a1["parent_tool_use_id"] == "toolu_a"
    assert b1["parent_tool_use_id"] == "toolu_b"
    assert a1["block_id"] != b1["block_id"]
    assert a1["offset"] == 0
    assert b1["offset"] == 0
    # JavaScript String.length counts the emoji as two UTF-16 code units.
    assert a2["offset"] == 3
    assert a2["replace"] is False


def test_stream_mux_final_assistant_replaces_matching_delta_identity():
    mux = chat_subagents.SubagentStreamMux(SESSION_ID)
    streamed = mux.feed(StreamEvent(
        uuid="assistant-final",
        session_id=SESSION_ID,
        parent_tool_use_id="toolu_parent",
        event={
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "partial"},
        },
    ))[0]
    final = AssistantMessage(
        content=[TextBlock(text="partial answer")],
        model="claude-sonnet-4-6",
        uuid="assistant-final",
        session_id=SESSION_ID,
        parent_tool_use_id="toolu_parent",
    )

    event = mux.feed(final)[0]

    assert event["event"] == "subagent_block"
    assert event["data"]["replace"] is True
    assert event["data"]["block_id"] == streamed["data"]["block_id"]
    assert event["data"]["kind"] == "assistant"
    assert event["data"]["block"]["text"] == "partial answer"
    # Exact final-message replay is idempotent, and late deltas stay closed.
    assert mux.feed(final) == []
    assert mux.feed(StreamEvent(
        uuid="assistant-final",
        session_id=SESSION_ID,
        parent_tool_use_id="toolu_parent",
        event={
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": " duplicate"},
        },
    )) == []


def test_stream_mux_thinking_tool_use_and_user_tool_result():
    mux = chat_subagents.SubagentStreamMux(SESSION_ID)
    thinking_delta = mux.feed(StreamEvent(
        uuid="assistant-tools",
        session_id=SESSION_ID,
        parent_tool_use_id="toolu_parent",
        event={
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "inspect"},
        },
    ))[0]
    assistant = AssistantMessage(
        content=[
            ThinkingBlock(thinking="inspect first", signature="sig"),
            ToolUseBlock(
                id="toolu_read", name="Read", input={"file_path": "/tmp/a"}),
        ],
        model="claude-sonnet-4-6",
        uuid="assistant-tools",
        session_id=SESSION_ID,
        parent_tool_use_id="toolu_parent",
    )

    complete = mux.feed(assistant)
    result = mux.feed(UserMessage(
        content=[ToolResultBlock(
            tool_use_id="toolu_read", content="contents", is_error=False)],
        uuid="user-tool-result",
        parent_tool_use_id="toolu_parent",
    ))

    assert thinking_delta["data"]["kind"] == "thinking"
    assert [event["data"]["kind"] for event in complete] == [
        "thinking", "tool_use",
    ]
    assert complete[0]["data"]["block_id"] == thinking_delta["data"]["block_id"]
    assert result[0]["data"]["kind"] == "tool_result"
    assert result[0]["data"]["block"]["tool_name"] == "Read"
    assert result[0]["data"]["block"]["text"] == "contents"


def test_history_and_live_final_use_the_same_block_shape():
    history = _session_message(
        "assistant",
        "same-message",
        [{"type": "text", "text": "same answer"}],
    )
    history_block = chat_subagents.normalize_subagent_message(
        history, agent_id="agent-a")[0]
    mux = chat_subagents.SubagentStreamMux(SESSION_ID)
    live_block = mux.feed(AssistantMessage(
        content=[TextBlock(text="same answer")],
        model="claude-sonnet-4-6",
        uuid="same-message",
        session_id=SESSION_ID,
        parent_tool_use_id="toolu_parent",
    ))[0]["data"]["block"]

    comparable_fields = {
        "session_id", "parent_tool_use_id", "parent_agent_id",
        "message_uuid", "source_block_index", "block_id", "role", "text",
    }
    assert {key: history_block[key] for key in comparable_fields} == {
        key: live_block[key] for key in comparable_fields
    }
    assert history_block["agent_id"] == "agent-a"
    assert live_block["agent_id"] is None


def test_authenticated_history_endpoint_uses_sdk_projection(
    app_module, client, auth, monkeypatch,
):
    from backend import chat as chat_mod
    from backend import sessions as sess

    session = sess.create_session("subagent history")
    expected = [{
        "session_id": session["id"],
        "agent_id": "agent-a",
        "parent_tool_use_id": "toolu_parent",
        "parent_agent_id": None,
        "orphaned": False,
        "message_count": 1,
        "blocks": [],
    }]
    calls = []

    def load(session_id, directory=None):
        calls.append((session_id, directory))
        return expected

    monkeypatch.setattr(
        chat_mod.chat_subagents, "load_subagent_threads", load)

    path = f"/api/chat/sessions/{session['id']}/subagents"
    assert client.get(path).status_code == 401
    response = client.get(path, headers=auth)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "session_id": session["id"],
        "threads": expected,
    }
    assert calls == [(session["id"], str(sess.session_workspace(session["id"])))]


def test_history_endpoint_rejects_unknown_session(client, auth):
    response = client.get(
        "/api/chat/sessions/00000000-0000-4000-8000-000000000000/subagents",
        headers=auth,
    )
    assert response.status_code == 404
