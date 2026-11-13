from __future__ import annotations

import asyncio

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


def test_signature_only_subagent_thinking_is_omitted_from_history():
    message = _session_message(
        "assistant",
        "signature-only",
        [
            {"type": "thinking", "thinking": "", "signature": "signed-value"},
            {"type": "text", "text": "visible result"},
        ],
    )

    blocks = chat_subagents.normalize_subagent_message(
        message, agent_id="agent-a")

    assert [(block["role"], block.get("text")) for block in blocks] == [
        ("assistant", "visible result"),
    ]
    assert "已加密推理" not in str(blocks)


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


def test_large_history_is_encoded_and_gzipped_off_loop(
    app_module, client, auth, monkeypatch,
):
    """A tens-of-MB body must not be serialized or compressed on the loop."""
    import gzip
    import json

    from backend import chat as chat_mod
    from backend import sessions as sess

    session = sess.create_session("large subagent history")
    text = "子任务输出 " * 4000
    expected = [{"agent_id": "agent-a", "blocks": [{"text": text}]}]
    monkeypatch.setattr(
        chat_mod.chat_subagents, "load_subagent_threads",
        lambda session_id, directory=None: expected)
    on_loop = []
    original = chat_mod._encode_json_body

    def encode(payload, gzip_ok):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return original(payload, gzip_ok)

    monkeypatch.setattr(chat_mod, "_encode_json_body", encode)

    path = f"/api/chat/sessions/{session['id']}/subagents"
    with client.stream(
        "GET", path, headers={**auth, "Accept-Encoding": "gzip"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-encoding"] == "gzip"
        compressed = b"".join(response.iter_raw())
    body = gzip.decompress(compressed)
    # Compressed exactly once: the middleware passed the encoded body through.
    assert json.loads(body) == {"session_id": session["id"], "threads": expected}
    assert on_loop == [False]

    plain = client.get(path, headers={**auth, "Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    assert plain.json()["threads"] == expected


def test_concurrent_history_reads_share_snapshot_then_refresh(
    app_module, monkeypatch,
):
    from backend import chat as chat_mod
    from backend import sessions as sess

    sid = sess.create_session("concurrent subagents")["id"]
    version = [1]
    load_calls = []

    def load(owner, directory=None):
        load_calls.append((owner, directory))
        return [{"agent_id": "agent-a", "message_count": version[0], "blocks": []}]

    monkeypatch.setattr(chat_mod.chat_subagents, "load_subagent_threads", load)

    async def exercise():
        original = chat_mod.obs.to_thread_io
        history_started = asyncio.Event()
        release_history = asyncio.Event()
        both_workspace_reads = asyncio.Event()
        workspace_reads = 0

        async def observed(site, owner, operation, *args, **kwargs):
            nonlocal workspace_reads
            if site == "chat.subagents_history_read":
                history_started.set()
                await release_history.wait()
                return operation(*args, **kwargs)
            result = await original(site, owner, operation, *args, **kwargs)
            if site == "chat.subagents_workspace_read":
                workspace_reads += 1
                if workspace_reads == 2:
                    both_workspace_reads.set()
            return result

        monkeypatch.setattr(chat_mod.obs, "to_thread_io", observed)
        first = asyncio.create_task(chat_mod._session_subagents_payload(sid))
        await asyncio.wait_for(history_started.wait(), 5)
        second = asyncio.create_task(chat_mod._session_subagents_payload(sid))
        await asyncio.wait_for(both_workspace_reads.wait(), 5)
        release_history.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result == second_result
        assert len(load_calls) == 1

        version[0] = 2
        refreshed = await chat_mod._session_subagents_payload(sid)
        assert refreshed["threads"][0]["message_count"] == 2
        assert len(load_calls) == 2

    asyncio.run(exercise())


def test_history_endpoint_rejects_unknown_session(client, auth):
    response = client.get(
        "/api/chat/sessions/00000000-0000-4000-8000-000000000000/subagents",
        headers=auth,
    )
    assert response.status_code == 404


def test_successor_history_inherits_only_attached_ancestor_threads(
    app_module, client, auth, monkeypatch,
):
    from copy import deepcopy
    from backend import chat as chat_mod
    from backend import sessions as sess

    source = sess.create_session("source")["id"]
    child = sess.create_session("successor")["id"]
    future = sess.create_session("later successor")["id"]
    assert sess.link_runtime_successor(source, child)
    assert sess.link_runtime_successor(child, future)

    def thread(owner, agent, parent, blocks=(), orphaned=False):
        return {
            "session_id": owner, "agent_id": agent,
            "parent_tool_use_id": parent, "parent_agent_id": None,
            "orphaned": orphaned, "message_count": len(blocks),
            "blocks": list(blocks),
        }

    root = thread(source, "old-agent", "copied-call", [{
        "role": "tool_use", "name": "Agent", "id": "nested-call",
        "block_id": "nested-launch", "session_id": source,
    }])
    nested = thread(source, "nested-agent", "nested-call", [{
        "role": "assistant", "text": "nested progress", "block_id": "nested-text",
        "session_id": source,
    }])
    own = thread(child, "own-agent", "own-call")
    inherited = [
        nested, root,
        thread(source, "after-fork-agent", "uncopied-call"),
        thread(source, "orphan-agent", None, orphaned=True),
        # A copied subagent transcript must not duplicate the child-owned one.
        thread(source, "own-agent", "own-call"),
    ]
    before = deepcopy(inherited)
    calls = []

    def load(owner, directory=None):
        calls.append(owner)
        assert owner in {source, child}, "must not read descendant sessions"
        return [own] if owner == child else inherited

    monkeypatch.setattr(chat_mod.chat_subagents, "load_subagent_threads", load)
    monkeypatch.setattr(chat_mod, "_shaped_ui_messages", lambda *a, **k: [
        {"role": "tool_use", "name": "Agent", "id": "copied-call"},
        {"role": "tool_use", "name": "Agent", "id": "own-call"},
    ])
    result = client.get(f"/api/chat/sessions/{child}/subagents", headers=auth)
    assert result.status_code == 200
    assert result.json() == {"session_id": child, "threads": [own, root, nested]}
    assert calls == [child, source]
    assert inherited == before


def test_successor_history_refresh_reads_growing_ancestor_transcript(
    app_module, client, auth, monkeypatch,
):
    from backend import chat as chat_mod
    from backend import sessions as sess

    source = sess.create_session("source")["id"]
    child = sess.create_session("successor")["id"]
    assert sess.link_runtime_successor(source, child)
    blocks = [{"role": "assistant", "text": "started", "block_id": "start"}]

    def load(owner, directory=None):
        if owner == child:
            return []
        return [{
            "session_id": source, "agent_id": "running-agent",
            "parent_tool_use_id": "copied-call", "orphaned": False,
            "blocks": list(blocks), "message_count": len(blocks),
        }]

    monkeypatch.setattr(chat_mod.chat_subagents, "load_subagent_threads", load)
    monkeypatch.setattr(chat_mod, "_shaped_ui_messages", lambda *a, **k: [
        {"role": "tool_use", "name": "Agent", "id": "copied-call"},
    ])
    url = f"/api/chat/sessions/{child}/subagents"
    first = client.get(url, headers=auth).json()
    assert first["threads"][0]["blocks"] == blocks
    blocks.append({"role": "tool_result", "text": "finished read", "block_id": "result"})
    second = client.get(url, headers=auth).json()
    assert len(first["threads"][0]["blocks"]) == 1
    assert second["threads"][0]["blocks"] == blocks


def _tree_threads(session_id):
    def block(parent, uuid, role, **extra):
        return {
            "session_id": session_id, "parent_tool_use_id": parent,
            "parent_agent_id": None, "agent_id": None, "message_uuid": uuid,
            "source_block_index": 0, "block_id": f"{parent}:{uuid}", "role": role,
            **extra,
        }
    return [
        {"session_id": session_id, "agent_id": "a", "parent_tool_use_id": "toolu_a",
         "parent_agent_id": None, "orphaned": False, "message_count": 3, "blocks": [
             block("toolu_a", "m1", "assistant", text="long reply " * 50),
             block("toolu_a", "m2", "tool_use", name="Agent", id="toolu_child",
                   summary="nested", input={"prompt": "x" * 500}),
             block("toolu_a", "m3", "tool_result", id="toolu_child",
                   text="out " * 200, preview="out", is_error=False),
         ]},
        {"session_id": session_id, "agent_id": "c", "parent_tool_use_id": "toolu_child",
         "parent_agent_id": "a", "orphaned": False, "message_count": 1, "blocks": [
             block("toolu_child", "m4", "assistant", text="child reply"),
         ]},
        {"session_id": session_id, "agent_id": "b", "parent_tool_use_id": "toolu_b",
         "parent_agent_id": None, "orphaned": False, "message_count": 1, "blocks": [
             block("toolu_b", "m5", "thinking", text="other card"),
         ]},
    ]


def _fake_reader(monkeypatch, module, full):
    """SDK reader stand-in; records the agent filter of each full read."""
    reads = []

    def load(session_id, directory=None, agent_ids=None):
        reads.append(None if agent_ids is None else set(agent_ids))
        return [t for t in full if agent_ids is None or t["agent_id"] in agent_ids]

    monkeypatch.setattr(module, "load_subagent_threads", load)
    # No SDK file listing: summaries read through the reader above.
    monkeypatch.setattr(module, "_agent_files", lambda session_id, directory: None)
    return reads


def test_summary_view_keeps_every_row_without_bodies(
    app_module, client, auth, monkeypatch,
):
    from backend import chat as chat_mod
    from backend import sessions as sess

    session = sess.create_session("subagent summary")
    full = _tree_threads(session["id"])
    _fake_reader(monkeypatch, chat_mod.chat_subagents, full)
    path = f"/api/chat/sessions/{session['id']}/subagents"

    summary = client.get(path + "?view=summary", headers=auth).json()["threads"]
    assert [t["agent_id"] for t in summary] == ["a", "c", "b"]
    for thread, original in zip(summary, full):
        assert thread["summary"] is True
        assert thread["block_count"] == len(original["blocks"])
        assert thread["message_count"] == original["message_count"]
        assert thread["parent_tool_use_id"] == original["parent_tool_use_id"]
    # Only the nested Agent call survives, as a stub without its input.
    assert summary[0]["blocks"] == [{
        "block_id": "toolu_a:m2", "role": "tool_use", "name": "Agent",
        "id": "toolu_child", "summary": "nested", "stub": True}]
    assert summary[1]["blocks"] == [] and summary[2]["blocks"] == []
    # The summary still finds a nested card's subtree.
    nested = client.get(path + "?view=summary&parent=toolu_a", headers=auth).json()["threads"]
    assert [t["agent_id"] for t in nested] == ["a", "c"]
    # The default view stays the full transcript.
    assert client.get(path, headers=auth).json()["threads"] == full
    assert client.get(path + "?view=bogus", headers=auth).status_code == 422


def test_parent_view_returns_one_card_subtree_with_bodies(
    app_module, client, auth, monkeypatch,
):
    from backend import chat as chat_mod
    from backend import sessions as sess

    session = sess.create_session("subagent subtree")
    full = _tree_threads(session["id"])
    reads = _fake_reader(monkeypatch, chat_mod.chat_subagents, full)
    path = f"/api/chat/sessions/{session['id']}/subagents"

    subtree = client.get(path + "?parent=toolu_a", headers=auth).json()["threads"]
    assert [t["agent_id"] for t in subtree] == ["a", "c"]
    assert subtree == full[:2]
    # Only the card's own agents were read in full.
    assert reads[-1] == {"a", "c"}
    leaf = client.get(path + "?parent=toolu_b", headers=auth).json()["threads"]
    assert leaf == full[2:]
    assert client.get(path + "?parent=toolu_missing", headers=auth).json()["threads"] == []


def test_send_failure_log_accepts_only_closed_fields(client, auth):
    good = {"sid8": "ebdc72dc", "stage": "mux_open", "error_kind": "mux_unavailable",
            "http_status": 0, "elapsed_ms": 5012, "mux_connected": False,
            "asset_version": "1794399292646"}
    assert client.post("/api/log/send-failure", json=good).status_code == 401
    assert client.post("/api/log/send-failure", json=good, headers=auth).json() == {"ok": True}
    for bad in ({**good, "stage": "x"}, {**good, "error_kind": "boom"},
                {**good, "prompt": "secret"}, {**good, "sid8": "ebdc72dc-full-id"},
                {**good, "elapsed_ms": "5"}):
        assert client.post("/api/log/send-failure", json=bad, headers=auth).status_code == 422


def test_summaries_reread_only_changed_agents(tmp_path, monkeypatch):
    """An unchanged transcript reuses its cached summary; a write rereads it."""
    from backend import chat_subagents as cs

    monkeypatch.setattr(cs, "_SUMMARY_CACHE", cs.OrderedDict())
    files = {}
    for agent in ("a", "b"):
        path = tmp_path / f"agent-{agent}.jsonl"
        path.write_text("{}\n")
        files[agent] = path
    monkeypatch.setattr(cs, "_agent_files",
                        lambda session_id, directory: list(files.items()))
    monkeypatch.setattr(cs, "_agent_metadata_sidecar_path",
                        lambda path: path.with_suffix(".meta.json"))
    reads = []

    def one(session_id, agent_id, directory):
        reads.append(agent_id)
        return {"session_id": session_id, "agent_id": agent_id,
                "parent_tool_use_id": "toolu_" + agent_id, "parent_agent_id": None,
                "orphaned": False, "message_count": len(reads),
                "blocks": [{"block_id": agent_id + str(len(reads)), "role": "assistant",
                            "text": "body"}]}

    monkeypatch.setattr(cs, "_load_one_thread", one)
    first = cs.load_subagent_summaries("sid")
    assert reads == ["a", "b"]
    assert [(t["summary"], t["block_count"], t["blocks"]) for t in first] == [
        (True, 1, []), (True, 1, [])]
    assert cs.load_subagent_summaries("sid") == first
    assert reads == ["a", "b"]  # nothing changed: no reads
    files["b"].write_text("{}\n{}\n")
    cs.load_subagent_summaries("sid")
    assert reads == ["a", "b", "b"]
    files["a"].with_suffix(".meta.json").write_text("{}")
    cs.load_subagent_summaries("sid")
    assert reads == ["a", "b", "b", "a"]  # a new sidecar is a change too


def test_cold_summary_reads_run_one_at_a_time(tmp_path, monkeypatch):
    """Concurrent cache misses never parse transcripts in parallel, and a
    request waiting on another's read of the same agent reuses it."""
    import threading
    import time

    from backend import chat_subagents as cs

    monkeypatch.setattr(cs, "_SUMMARY_CACHE", cs.OrderedDict())
    files = []
    for agent in ("a", "b", "c"):
        path = tmp_path / f"agent-{agent}.jsonl"
        path.write_text("{}\n")
        files.append((agent, path))
    monkeypatch.setattr(cs, "_agent_files", lambda session_id, directory: files)
    monkeypatch.setattr(cs, "_agent_metadata_sidecar_path",
                        lambda path: path.with_suffix(".meta.json"))
    active, peak, reads = [0], [0], []
    guard = threading.Lock()

    def one(session_id, agent_id, directory):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            reads.append(agent_id)
        time.sleep(0.05)
        with guard:
            active[0] -= 1
        return {"session_id": session_id, "agent_id": agent_id,
                "parent_tool_use_id": "toolu_" + agent_id, "parent_agent_id": None,
                "orphaned": False, "message_count": 1, "blocks": []}

    monkeypatch.setattr(cs, "_load_one_thread", one)
    workers = [threading.Thread(target=cs.load_subagent_summaries, args=("sid",))
               for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert peak[0] == 1
    assert sorted(reads) == ["a", "b", "c"]  # each agent read once in total
