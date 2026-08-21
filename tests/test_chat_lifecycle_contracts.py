"""Compatibility contracts for splitting chat lifecycle code across modules."""

import ast
import inspect
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


def test_chat_history_wrappers_keep_patchable_facade_and_shared_cache(
    app_module, monkeypatch, tmp_path,
):
    from backend import chat as chat_mod
    from backend import chat_history

    assert chat_mod._JSONL_PATH_CACHE is chat_history.JSONL_PATH_CACHE

    projects = tmp_path / "projects"
    first = projects / "-workspace" / "first.jsonl"
    second = projects / "-workspace" / "second.jsonl"
    first.parent.mkdir(parents=True)
    first.write_text("{}\n", encoding="utf-8")
    second.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(chat_mod, "_cli_project_roots", lambda: [projects])
    monkeypatch.setattr(chat_mod, "_JSONL_PATH_CACHE_MAX", 1)
    chat_mod._JSONL_PATH_CACHE.clear()

    assert chat_mod._find_session_jsonl("first") == first
    assert chat_mod._find_session_jsonl("second") == second
    assert chat_mod._JSONL_PATH_CACHE == {"second": second}

    calls = []

    def load_messages(sid, *, directory):
        calls.append((sid, directory))
        return ["loaded"]

    monkeypatch.setattr(chat_mod, "get_session_messages", load_messages)
    monkeypatch.setattr(
        chat_mod.sess, "session_workspace", lambda _sid: tmp_path / "workspace")
    assert chat_mod._get_session_msgs("sdk-session") == ["loaded"]
    assert calls == [("sdk-session", str(tmp_path / "workspace"))]
    chat_mod._JSONL_PATH_CACHE.clear()


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


def test_chat_presentation_facades_remain_patchable(app_module, monkeypatch):
    from backend import chat as chat_mod
    from backend import chat_presentation

    imports = [
        node
        for node in ast.walk(ast.parse(inspect.getsource(chat_presentation)))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not any(
        (isinstance(node, ast.ImportFrom) and node.module == "chat")
        or (isinstance(node, ast.Import)
            and any(alias.name.endswith(".chat") for alias in node.names))
        for node in imports
    )

    calls = []

    def strip(text):
        calls.append(text)
        return "patched"

    monkeypatch.setattr(chat_presentation, "strip_cli_slash_wrapper", strip)
    assert chat_mod._strip_cli_slash_wrapper("canonical") == "patched"
    assert calls == ["canonical"]

    monkeypatch.setattr(chat_mod, "_HISTORY_INLINE_BODY_CAP", 1)
    monkeypatch.setattr(chat_mod, "_HISTORY_BODY_PREVIEW_CAP", 1)
    message = {
        "role": "assistant",
        "text": "body",
        "block_id": "record:0:assistant",
    }
    chat_mod._defer_large_ui_bodies([message])
    assert message["text"] == "b"
    assert message["body_length"] == 4


def test_chat_overlay_module_keeps_runtime_boundary_and_shared_containers(
    app_module,
):
    from backend import chat as chat_mod
    from backend import chat_overlays

    tree = ast.parse(inspect.getsource(chat_overlays))
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not any(
        (
            isinstance(node, ast.ImportFrom)
            and any(alias.name == "chat" for alias in node.names)
        )
        or (
            isinstance(node, ast.Import)
            and any(alias.name.endswith(".chat") for alias in node.names)
        )
        for node in imports
    )
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "claude_agent_sdk"
        for node in imports
    )

    top_level_functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not {
        "get_client",
        "disconnect_client",
        "_start_turn",
        "_watch_background_tasks",
        "_create_runtime_successor",
    } & top_level_functions

    assert (
        chat_mod._runtime_continuation_delivery_tasks
        is chat_overlays.RUNTIME_CONTINUATION_DELIVERY_TASKS
    )
    assert (
        chat_mod._runtime_rollover_locks
        is chat_overlays.RUNTIME_CONTINUATION_FENCES
    )


def test_chat_overlay_facades_remain_patchable(app_module, monkeypatch):
    from backend import chat as chat_mod
    from backend import chat_overlays

    calls = []

    def combine(base, overlay):
        calls.append((base, overlay))
        return "patched-generation"

    monkeypatch.setattr(chat_overlays, "_combined_history_generation", combine)
    assert chat_mod._combined_history_generation(
        "canonical", "overlay",
    ) == "patched-generation"
    assert calls == [("canonical", "overlay")]


def test_chat_history_window_facades_remain_patchable(app_module, monkeypatch):
    from backend import chat as chat_mod
    from backend import chat_history_window

    imports = [
        node
        for node in ast.walk(ast.parse(inspect.getsource(chat_history_window)))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not any(
        (isinstance(node, ast.ImportFrom) and node.module == "chat")
        or (isinstance(node, ast.Import)
            and any(alias.name.endswith(".chat") for alias in node.names))
        for node in imports
    )

    calls = []

    def assemble(index, snapshots, order):
        calls.append((index, snapshots, order))
        return ([{"kind": "snapshot", "count": 1}], 1)

    monkeypatch.setattr(chat_history_window, "history_segments", assemble)
    index = {"records": [], "orders": {"normal": []}}
    snapshots = [{"messages": [{"role": "assistant", "text": "overlay"}]}]
    assert chat_mod._interrupted_history_segments(
        index, snapshots, "normal",
    ) == ([{"kind": "snapshot", "count": 1}], 1)
    assert calls == [(index, snapshots, "normal")]

    assert chat_mod._combined_history_generation("canonical", "") == "canonical"
    assert chat_mod._combined_history_generation(
        "canonical", "display",
    ) == "canonical~cancelled-display"


def test_chat_successor_module_keeps_runtime_boundary_and_shared_state(
    app_module,
):
    from backend import chat as chat_mod
    from backend import chat_overlays, chat_successor

    tree = ast.parse(inspect.getsource(chat_successor))
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not any(
        (
            isinstance(node, ast.ImportFrom)
            and any(alias.name == "chat" for alias in node.names)
        )
        or (
            isinstance(node, ast.Import)
            and any(alias.name.endswith(".chat") for alias in node.names)
        )
        for node in imports
    )
    assert not any(
        (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("claude_agent_sdk")
        )
        or (
            isinstance(node, ast.Import)
            and any(
                alias.name.startswith("claude_agent_sdk")
                for alias in node.names
            )
        )
        for node in imports
    )

    top_level_functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {
        "commit_fork_lifecycle",
        "fork_session",
        "continue_detached_runtime",
        "schedule_detached_successor_prewarm",
    } <= top_level_functions
    assert not {
        "get_client",
        "disconnect_client",
        "_start_turn",
        "_watch_inflight_tasks",
        "_apply_runtime_task_overlays",
    } & top_level_functions

    assert (
        chat_mod._runtime_rollover_locks
        is chat_successor.RUNTIME_ROLLOVER_LOCKS
        is chat_overlays.RUNTIME_CONTINUATION_FENCES
    )
    assert (
        chat_mod._runtime_prewarm_tasks
        is chat_successor.RUNTIME_PREWARM_TASKS
    )
    assert chat_mod._session_title_locks is chat_successor.SESSION_TITLE_LOCKS


def test_chat_successor_routes_keep_fastapi_contract(app_module):
    from backend import chat as chat_mod

    contract = {}
    for route in chat_mod.router.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path not in {
            "/api/chat/sessions/{sid}/fork",
            "/api/chat/sessions/{sid}/continue-detached",
        }:
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

    assert contract == {
        ("/api/chat/sessions/{sid}/fork", "POST"): (
            ("require_token",),
            dict,
        ),
        ("/api/chat/sessions/{sid}/continue-detached", "POST"): (
            ("require_token",),
            dict,
        ),
    }


def test_chat_successor_facades_remain_patchable(app_module, monkeypatch):
    from backend import chat as chat_mod
    from backend import chat_successor

    calls = []

    def boundary(sid, meta):
        calls.append((sid, meta))
        return "patched-boundary"

    monkeypatch.setattr(chat_successor, "runtime_fork_boundary", boundary)
    meta = {"runtime_boundary_message_id": "canonical-boundary"}
    assert chat_mod._runtime_fork_boundary("source", meta) == "patched-boundary"
    assert calls == [("source", meta)]
