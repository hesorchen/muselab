import asyncio
from pathlib import Path
from types import SimpleNamespace

from claude_agent_sdk.types import SessionMessage

from tests.conftest import TEST_TOKEN


def _message(
    session_id: str,
    uuid: str,
    role: str,
    content,
) -> SessionMessage:
    return SessionMessage(
        type=role,
        uuid=uuid,
        session_id=session_id,
        message={"role": role, "content": content},
    )


def _capture_options(chat_mod, monkeypatch):
    captured = {}

    class FakeOptions:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            captured["connected"] = True

    monkeypatch.setattr(chat_mod, "ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr(chat_mod, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(chat_mod, "MuseLabSDKClient", FakeClient)
    monkeypatch.setattr(chat_mod, "UnsignedThinkingCompatibleClient", FakeClient)
    return captured


def test_retry_last_turn_creates_sdk_native_point_in_time_fork(
    app_module,
    client,
    monkeypatch,
):
    from backend import chat as chat_mod
    from backend import sessions as sess

    source = sess.create_session(
        "source conversation",
        model="claude-sonnet-4-6",
        permission="default",
    )
    first_user = "10000000-0000-4000-8000-000000000001"
    first_reply = "10000000-0000-4000-8000-000000000002"
    last_user = "10000000-0000-4000-8000-000000000003"
    last_reply = "10000000-0000-4000-8000-000000000004"
    history = [
        _message(source["id"], first_user, "user", "first"),
        _message(
            source["id"], first_reply, "assistant",
            [{"type": "text", "text": "first answer"}],
        ),
        _message(
            source["id"], last_user, "user",
            [{"type": "text", "text": "retry this exactly"}],
        ),
        _message(
            source["id"], last_reply, "assistant",
            [{"type": "text", "text": "old answer"}],
        ),
    ]
    monkeypatch.setattr(chat_mod, "_get_session_msgs", lambda *_args: history)
    child_uuid = "10000000-0000-4000-8000-000000000099"
    forked = {}

    def fake_fork(session_id, **kwargs):
        forked.update({"session_id": session_id, **kwargs})
        return SimpleNamespace(session_id=child_uuid)

    monkeypatch.setattr(chat_mod, "sdk_fork_session", fake_fork)

    response = client.post(
        f"/api/chat/sessions/{source['id']}/retry-last-turn",
        headers={"X-Auth-Token": TEST_TOKEN},
        json={"user_message_id": last_user},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    child_sid = payload["session_id"]
    assert payload["prompt"] == "retry this exactly"
    assert payload["retry_mode"] == "native_fork"
    assert child_sid == child_uuid
    assert forked == {
        "session_id": source["id"],
        "directory": str(sess.session_workspace(source["id"])),
        "up_to_message_id": first_reply,
        "title": "source conversation · Retry",
    }
    child = sess.get_session_meta(child_sid)
    assert "retry_source_session_id" not in child
    assert "retry_target_user_uuid" not in child
    assert "retry_resume_session_at" not in child
    assert child["forked_from"] == source["id"]
    assert child["message_count"] == 2
    assert child["turn_count"] == 1

    captured = _capture_options(chat_mod, monkeypatch)
    monkeypatch.setattr(
        chat_mod,
        "_find_session_jsonl",
        lambda sid: Path("/native/fork.jsonl") if sid == child_sid else None,
    )
    asyncio.run(chat_mod._build_and_connect_client(
        child_sid,
        child["model"],
        child["permission"],
        child["effort"],
    ))

    assert captured["connected"] is True
    assert captured["resume"] == child_sid
    assert "session_id" not in captured
    assert "fork_session" not in captured
    assert "resume_session_at" not in captured
    assert "resume_drops_turn" not in captured


def test_retry_first_turn_uses_fresh_child_without_resume(
    app_module,
    client,
    monkeypatch,
):
    from backend import chat as chat_mod
    from backend import sessions as sess

    source = sess.create_session(
        "one turn", model="claude-sonnet-4-6", permission="default")
    user_uuid = "20000000-0000-4000-8000-000000000001"
    history = [
        _message(source["id"], user_uuid, "user", "only prompt"),
        _message(
            source["id"],
            "20000000-0000-4000-8000-000000000002",
            "assistant",
            [{"type": "text", "text": "old answer"}],
        ),
    ]
    monkeypatch.setattr(chat_mod, "_get_session_msgs", lambda *_args: history)

    response = client.post(
        f"/api/chat/sessions/{source['id']}/retry-last-turn",
        headers={"X-Auth-Token": TEST_TOKEN},
        json={"user_message_id": user_uuid},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["retry_mode"] == "fresh"
    child = sess.get_session_meta(payload["session_id"])
    assert "retry_source_session_id" not in child
    captured = _capture_options(chat_mod, monkeypatch)
    monkeypatch.setattr(chat_mod, "_find_session_jsonl", lambda _sid: None)
    asyncio.run(chat_mod._build_and_connect_client(
        child["id"], child["model"], child["permission"], child["effort"]))
    assert captured["session_id"] == child["id"]
    assert "resume" not in captured
    assert "resume_drops_turn" not in captured


def test_retry_refuses_non_tail_and_attachment_turns(
    app_module,
    client,
    monkeypatch,
):
    from backend import chat as chat_mod
    from backend import sessions as sess

    source = sess.create_session(
        "guarded source", model="claude-sonnet-4-6", permission="default")
    old_user = "30000000-0000-4000-8000-000000000001"
    last_user = "30000000-0000-4000-8000-000000000003"
    history = [
        _message(source["id"], old_user, "user", "old"),
        _message(
            source["id"],
            "30000000-0000-4000-8000-000000000002",
            "assistant",
            [{"type": "text", "text": "answer"}],
        ),
        _message(
            source["id"], last_user, "user",
            [
                {"type": "image", "source": {"type": "base64", "data": "x"}},
                {"type": "text", "text": "inspect this"},
            ],
        ),
    ]
    monkeypatch.setattr(chat_mod, "_get_session_msgs", lambda *_args: history)

    non_tail = client.post(
        f"/api/chat/sessions/{source['id']}/retry-last-turn",
        headers={"X-Auth-Token": TEST_TOKEN},
        json={"user_message_id": old_user},
    )
    attachment = client.post(
        f"/api/chat/sessions/{source['id']}/retry-last-turn",
        headers={"X-Auth-Token": TEST_TOKEN},
        json={"user_message_id": last_user},
    )

    assert non_tail.status_code == 409
    assert "latest user turn" in non_tail.json()["detail"]
    assert attachment.status_code == 409
    assert "attachment" in attachment.json()["detail"]


def test_existing_child_transcript_consumes_stale_retry_intent(
    app_module,
    client,
    monkeypatch,
):
    from backend import chat as chat_mod
    from backend import sessions as sess

    source = sess.create_session(
        "reconcile", model="claude-sonnet-4-6", permission="default")
    assistant_one = "40000000-0000-4000-8000-000000000002"
    user_two = "40000000-0000-4000-8000-000000000003"
    child_sid = "40000000-0000-4000-8000-000000000099"
    child = sess.register_session(
        child_sid,
        name="legacy retry intent",
        model=source["model"],
        permission=source["permission"],
        retry_source_session_id=source["id"],
        retry_target_user_uuid=user_two,
        retry_resume_session_at=assistant_one,
    )

    captured = _capture_options(chat_mod, monkeypatch)
    monkeypatch.setattr(
        chat_mod,
        "_find_session_jsonl",
        lambda sid: Path("/already/materialized.jsonl") if sid == child_sid else None,
    )
    asyncio.run(chat_mod._build_and_connect_client(
        child_sid, child["model"], child["permission"], child["effort"]))

    assert captured["resume"] == child_sid
    assert "session_id" not in captured
    assert "resume_drops_turn" not in captured
    repaired = sess.get_session_meta(child_sid)
    assert "retry_source_session_id" not in repaired
    assert "retry_target_user_uuid" not in repaired
