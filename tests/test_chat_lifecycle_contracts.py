"""Compatibility contracts for splitting chat lifecycle code across modules."""

import json

from fastapi.routing import APIRoute


def _chat_route_contract(chat_mod):
    contract = {}
    for route in chat_mod.router.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path not in {"/api/chat/sessions", "/api/chat/sessions/{sid}"}:
            continue
        dependencies = tuple(
            dependency.call.__name__
            for dependency in route.dependant.dependencies
        )
        for method in route.methods:
            contract[(route.path, method)] = (
                dependencies,
                route.response_model,
            )
    return contract


def test_chat_lifecycle_routes_keep_fastapi_contract(app_module):
    from backend import chat as chat_mod

    assert _chat_route_contract(chat_mod) == {
        ("/api/chat/sessions", "GET"): (
            ("require_token", "resolve_workspace_root"),
            None,
        ),
        ("/api/chat/sessions", "POST"): (("require_token",), dict),
        ("/api/chat/sessions/{sid}", "GET"): (("require_token",), dict),
        ("/api/chat/sessions/{sid}", "PATCH"): (("require_token",), dict),
        ("/api/chat/sessions/{sid}", "DELETE"): (("require_token",), dict),
    }


def test_chat_lifecycle_routes_keep_response_shapes(
    app_module, client, auth, monkeypatch,
):
    from backend import chat as chat_mod

    unauthenticated = client.get("/api/chat/sessions")
    assert unauthenticated.status_code == 401

    created = client.post(
        "/api/chat/sessions",
        headers=auth,
        json={"name": "lifecycle contract"},
    )
    assert created.status_code == 200, created.text
    session = created.json()
    assert {"id", "name", "model", "permission", "message_count"} <= session.keys()
    sid = session["id"]

    history = client.get(f"/api/chat/sessions/{sid}", headers=auth)
    assert history.status_code == 200, history.text
    assert {
        "id",
        "messages",
        "total",
        "offset",
        "has_more",
        "has_later",
        "history_generation",
        "history_order",
    } <= history.json().keys()
    assert history.json()["messages"] == []

    monkeypatch.setattr(chat_mod, "sdk_rename_session", lambda *_args, **_kwargs: None)
    patched = client.patch(
        f"/api/chat/sessions/{sid}",
        headers=auth,
        json={"name": "renamed lifecycle contract"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json() == {"ok": True}
    assert chat_mod.sess.get_session(sid)["name"] == "renamed lifecycle contract"

    deleted = client.delete(f"/api/chat/sessions/{sid}", headers=auth)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True}


def test_history_endpoint_uses_cli_jsonl_and_applies_overlay_only_for_presentation(
    app_module, client, auth, monkeypatch, tmp_path,
):
    from backend import chat as chat_mod

    created = client.post(
        "/api/chat/sessions",
        headers=auth,
        json={"name": "canonical transcript"},
    )
    assert created.status_code == 200, created.text
    sid = created.json()["id"]
    user_uuid = "11111111-1111-4111-8111-111111111111"
    assistant_uuid = "22222222-2222-4222-8222-222222222222"
    tool_use_id = "toolu_canonical_history"
    transcript = tmp_path / f"{sid}.jsonl"
    entries = [
        {
            "uuid": user_uuid,
            "parentUuid": None,
            "type": "user",
            "sessionId": sid,
            "message": {"content": "canonical user prompt"},
        },
        {
            "uuid": assistant_uuid,
            "parentUuid": user_uuid,
            "type": "assistant",
            "sessionId": sid,
            "message": {
                "content": [
                    {"type": "text", "text": "canonical assistant reply"},
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": "Task",
                        "input": {"description": "background work"},
                    },
                ],
            },
        },
    ]
    canonical_bytes = (
        "\n".join(json.dumps(entry) for entry in entries) + "\n"
    ).encode()
    transcript.write_bytes(canonical_bytes)

    facade_calls = []

    def find_session_jsonl(requested_sid):
        facade_calls.append(requested_sid)
        return transcript

    monkeypatch.setattr(chat_mod, "_find_session_jsonl", find_session_jsonl)
    assert chat_mod.sess.set_runtime_task_overlay(
        sid,
        "task-canonical",
        tool_use_id=tool_use_id,
        owner_session_id=sid,
        state="completed",
        summary="presentation-only completion",
    )

    response = client.get(
        f"/api/chat/sessions/{sid}",
        headers=auth,
        params={"tail": 50},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [message["role"] for message in body["messages"]] == [
        "user", "assistant", "tool_use",
    ]
    tool_card = next(
        message for message in body["messages"]
        if message["role"] == "tool_use"
    )
    assert tool_card["id"] == tool_use_id
    assert tool_card["task_status"]["state"] == "completed"
    assert tool_card["task_status"]["summary"] == "presentation-only completion"

    assert facade_calls and set(facade_calls) == {sid}
    assert transcript.read_bytes() == canonical_bytes
    canonical_messages = chat_mod._sdk_messages_to_ui(
        chat_mod._full_session_msgs(sid),
        {},
    )
    canonical_tool_card = next(
        message for message in canonical_messages
        if message["role"] == "tool_use"
    )
    assert "task_status" not in canonical_tool_card
    assert b"presentation-only completion" not in transcript.read_bytes()
