from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend import transcript_index as ti


def _entry(uid: str, typ: str, content, parent: str | None = None, **extra):
    return {
        "uuid": uid,
        "parentUuid": parent,
        "type": typ,
        "sessionId": "00000000-0000-4000-8000-000000000001",
        "message": {"content": content},
        **extra,
    }


def _append(path: Path, *entries: dict, final_newline: bool = True) -> None:
    text = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)
    if final_newline:
        text += "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _describe(entry: dict) -> dict:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        count = 0 if "<task-notification>" in content else int(bool(content.strip()))
        preview = content[:80] if entry.get("type") == "user" and count else ""
        notifications = []
    else:
        count = sum(1 for block in content or [] if block.get("type") in {
            "text", "thinking", "tool_use", "tool_result",
        })
        preview = ""
        notifications = []
    tools = [
        {"id": block.get("id", ""), "name": block.get("name", "")}
        for block in content or []
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ] if isinstance(content, list) else []
    return {
        "bubble_count": count,
        "user_preview": preview,
        "tool_uses": tools,
        "task_notifications": notifications,
    }


def test_incremental_append_partial_malformed_and_replace(tmp_path):
    transcript = tmp_path / "s.jsonl"
    index_path = tmp_path / "s.transcript-index.json"
    _append(transcript, _entry("u1", "user", "one"))

    first = ti.ensure_index("s", transcript, index_path, _describe)
    assert len(first["records"]) == 1
    assert first["source"]["scanned_bytes"] == transcript.stat().st_size
    generation1 = first["history_generation"]

    # A partial tail is not indexed and scanned_bytes stays at its beginning.
    partial = json.dumps(_entry("a1", "assistant", "two"), ensure_ascii=False)
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(partial[: len(partial) // 2])
    partial_index = ti.ensure_index("s", transcript, index_path, _describe)
    assert len(partial_index["records"]) == 1
    partial_start = partial_index["source"]["scanned_bytes"]
    assert partial_start < transcript.stat().st_size
    assert partial_index["history_generation"] == generation1

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(partial[len(partial) // 2:] + "\n{malformed}\n")
    appended = ti.ensure_index("s", transcript, index_path, _describe)
    assert [r["uuid"] for r in appended["records"]] == ["u1", "a1"]
    assert appended["source"]["scanned_bytes"] == transcript.stat().st_size
    assert appended["history_generation"] != generation1

    # Atomic replacement changes inode and forces a clean rebuild.
    replacement = tmp_path / "replacement"
    _append(replacement, _entry("u9", "user", "replacement"))
    os.replace(replacement, transcript)
    rebuilt = ti.ensure_index("s", transcript, index_path, _describe)
    assert [r["uuid"] for r in rebuilt["records"]] == ["u9"]

    # A stale schema is rejected and rebuilt rather than trusted.
    bad = json.loads(index_path.read_text())
    bad["schema"] = 999
    index_path.write_text(json.dumps(bad))
    schema_rebuilt = ti.ensure_index("s", transcript, index_path, _describe)
    assert schema_rebuilt["schema"] == ti.SCHEMA_VERSION
    assert [r["uuid"] for r in schema_rebuilt["records"]] == ["u9"]


def test_same_inode_growing_rewrite_rebuilds(tmp_path):
    transcript = tmp_path / "rewrite.jsonl"
    index_path = tmp_path / "rewrite.index.json"
    _append(transcript, _entry("old", "user", "old"))
    ti.ensure_index("rewrite", transcript, index_path, _describe)
    inode = transcript.stat().st_ino

    replacement_text = (
        json.dumps(_entry("new1", "user", "new one")) + "\n"
        + json.dumps(_entry("new2", "assistant", "new two", "new1")) + "\n"
    )
    transcript.write_text(replacement_text)
    assert transcript.stat().st_ino == inode
    rebuilt = ti.ensure_index("rewrite", transcript, index_path, _describe)
    assert [record["uuid"] for record in rebuilt["records"]] == ["new1", "new2"]


def test_same_inode_middle_rewrite_plus_growth_rebuilds(tmp_path):
    """Changing only the indexed middle must invalidate append metadata."""
    transcript = tmp_path / "middle-rewrite.jsonl"
    index_path = tmp_path / "middle-rewrite.index.json"
    entries = [
        _entry(f"u{i:02d}", "user", f"marker-{i:02d}-" + (str(i) * 6000))
        for i in range(9)
    ]
    _append(transcript, *entries)
    first = ti.ensure_index("middle-rewrite", transcript, index_path, _describe)
    generation = first["history_generation"]
    inode = transcript.stat().st_ino

    # Preserve the old first/last 4 KiB and total indexed length. The old
    # sparse prefix fingerprint accepted this as a pure append.
    rewritten = list(entries)
    rewritten[4] = _entry("x04", "user", "change-04-" + ("4" * 6000))
    old_line = json.dumps(entries[4], ensure_ascii=False)
    new_line = json.dumps(rewritten[4], ensure_ascii=False)
    assert len(old_line) == len(new_line)
    transcript.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in rewritten) + "\n",
        encoding="utf-8",
    )
    _append(transcript, _entry("u09", "assistant", "appended", "x04"))
    assert transcript.stat().st_ino == inode

    rebuilt = ti.ensure_index(
        "middle-rewrite", transcript, index_path, _describe)
    uuids = [record["uuid"] for record in rebuilt["records"]]
    assert "x04" in uuids
    assert "u04" not in uuids
    assert "u09" in uuids
    assert rebuilt["history_generation"] != generation


def test_normal_chain_matches_sdk_leaf_rules_and_full_keeps_file_order(tmp_path):
    transcript = tmp_path / "branch.jsonl"
    index_path = tmp_path / "branch.index.json"
    _append(
        transcript,
        _entry("u1", "user", "root"),
        _entry("a1", "assistant", "old", "u1"),
        _entry("side", "assistant", "side", "u1", isSidechain=True),
        _entry("u2", "user", "active", "a1"),
        _entry("a2", "assistant", "leaf", "u2"),
    )
    index = ti.ensure_index("branch", transcript, index_path, _describe)
    records = index["records"]
    assert [records[i]["uuid"] for i in index["orders"]["normal"]] == [
        "u1", "a1", "u2", "a2",
    ]
    assert [records[i]["uuid"] for i in index["orders"]["full"]] == [
        "u1", "a1", "side", "u2", "a2",
    ]
    assert index["bubble_prefix"]["normal"][-1] == 4


def test_same_sid_build_is_single_flight(tmp_path):
    transcript = tmp_path / "concurrent.jsonl"
    index_path = tmp_path / "concurrent.index.json"
    _append(transcript, *[_entry(f"u{i}", "user", str(i)) for i in range(30)])
    calls = 0

    def describe(entry):
        nonlocal calls
        calls += 1
        return _describe(entry)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: ti.ensure_index("same", transcript, index_path, describe),
            range(8),
        ))
    assert calls == 30
    assert all(len(result["records"]) == 30 for result in results)
    assert "same" not in ti._locks


def _make_endpoint_session(client, auth, chat_mod, tmp_path, entries):
    response = client.post(
        "/api/chat/sessions", headers=auth,
        json={"name": "indexed", "model": "claude-sonnet-4-6"},
    )
    assert response.status_code == 200, response.text
    sid = response.json()["id"]
    transcript = tmp_path / f"{sid}.jsonl"
    _append(transcript, *entries)
    chat_mod._JSONL_PATH_CACHE[sid] = transcript
    return sid, transcript


def test_window_endpoint_matches_full_oracle_and_adds_stable_keys(
    client, auth, app_module, tmp_path,
):
    from backend import chat as chat_mod
    entries = [
        _entry("u1", "user", "hello"),
        _entry("a1", "assistant", [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "before"},
            {"type": "tool_use", "id": "toolu_1", "name": "Read",
             "input": {"file_path": "/tmp/a"}},
            {"type": "text", "text": "after"},
        ], "u1"),
        _entry("u2", "user", "next", "a1"),
    ]
    sid, _ = _make_endpoint_session(client, auth, chat_mod, tmp_path, entries)
    oracle = chat_mod._sdk_messages_to_ui([
        chat_mod._RawMsg(e["uuid"], e["type"], e["message"]) for e in entries
    ], {})

    response = client.get(
        f"/api/chat/sessions/{sid}", headers=auth,
        params={"offset": 1, "limit": 4},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [(m["role"], m.get("text"), m.get("id")) for m in body["messages"]] == [
        (m["role"], m.get("text"), m.get("id")) for m in oracle[1:5]
    ]
    assert body["total"] == len(oracle)
    assert body["has_more"] is True
    assert body["has_later"] is True
    assert body["history_generation"]
    assert len({m["_key"] for m in body["messages"]}) == len(body["messages"])


def test_cross_window_tool_context_task_status_generation_and_around(
    client, auth, app_module, tmp_path,
):
    from backend import chat as chat_mod
    notification = (
        "<task-notification>\n"
        "<tool-use-id>toolu_bg</tool-use-id>\n"
        "<task-id>t1</task-id><status>completed</status>"
        "<summary>done</summary><output-file>/tmp/t1.output</output-file>"
        "</task-notification>"
    )
    entries = [
        _entry("u1", "user", "start"),
        _entry("a1", "assistant", [
            {"type": "tool_use", "id": "toolu_bg", "name": "Bash",
             "input": {"command": "printf hi", "run_in_background": True}},
        ], "u1"),
        _entry("u2", "user", [
            {"type": "tool_result", "tool_use_id": "toolu_bg",
             "content": "<stdout>hi</stdout><stderr></stderr><exit_code>0</exit_code>"},
        ], "a1"),
        _entry("u3", "user", notification, "u2"),
        _entry("a2", "assistant", "finished", "u3"),
    ]
    sid, transcript = _make_endpoint_session(
        client, auth, chat_mod, tmp_path, entries)

    tool_result = client.get(
        f"/api/chat/sessions/{sid}", headers=auth,
        params={"offset": 2, "limit": 1},
    ).json()
    assert tool_result["messages"][0]["role"] == "tool_result"
    assert tool_result["messages"][0]["tool_name"] == "Bash"
    assert "bash" in tool_result["messages"][0]
    generation = tool_result["history_generation"]
    assert chat_mod.sess.get_session_meta(sid)["turn_count"] == 1

    tool_card = client.get(
        f"/api/chat/sessions/{sid}", headers=auth,
        params={"offset": 1, "limit": 1},
    ).json()["messages"][0]
    assert tool_card["task_status"]["state"] == "completed"
    assert tool_card["task_status"]["summary"] == "done"

    around = client.get(
        f"/api/chat/sessions/{sid}", headers=auth,
        params={"around_uuid": "u2", "limit": 3},
    )
    assert around.status_code == 200, around.text
    # limit is a UI-bubble budget.  The zero-bubble task notification between
    # u2 and a2 does not consume it, so one visible bubble on either side of
    # the target yields a1/u2/a2 and reaches the end of full-order history.
    assert {m["uuid"] for m in around.json()["messages"]} == {"a1", "u2", "a2"}
    assert around.json()["has_later"] is False

    _append(transcript, _entry("u4", "user", "new", "a2"))
    stale = client.get(
        f"/api/chat/sessions/{sid}", headers=auth,
        params={"tail": 2, "history_generation": generation},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "history_generation_mismatch"


def test_around_uuid_limit_is_in_bubbles_and_keeps_target(
    client, auth, app_module, tmp_path,
):
    from backend import chat as chat_mod

    def rich(uid: str, parent: str) -> dict:
        return _entry(uid, "assistant", [
            {"type": "thinking", "thinking": f"think-{uid}"},
            {"type": "text", "text": f"text-a-{uid}"},
            {"type": "tool_use", "id": f"tool-{uid}", "name": "Read",
             "input": {"file_path": "/tmp/x"}},
            {"type": "text", "text": f"text-b-{uid}"},
        ], parent)

    entries = [_entry("u0", "user", "root")]
    parent = "u0"
    for i in range(8):
        assistant = f"a{i}"
        user = f"u{i + 1}"
        entries.append(rich(assistant, parent))
        entries.append(_entry(user, "user", f"prompt-{i + 1}", assistant))
        parent = user
    sid, _ = _make_endpoint_session(client, auth, chat_mod, tmp_path, entries)

    response = client.get(
        f"/api/chat/sessions/{sid}", headers=auth,
        params={"around_uuid": "u4", "limit": 5},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["messages"]) <= 5
    assert any(item["uuid"] == "u4" for item in body["messages"])
    assert body["history_order"] == "full"
    assert body["has_more"] is True
    assert body["has_later"] is True

    # The returned offset is explicitly full-order. Continuing with full=1
    # yields the immediately preceding bubbles instead of interpreting it in
    # the compact/active normal coordinate space.
    older_start = max(0, body["offset"] - 3)
    older = client.get(
        f"/api/chat/sessions/{sid}", headers=auth,
        params={
            "full": 1,
            "offset": older_start,
            "limit": body["offset"] - older_start,
            "history_generation": body["history_generation"],
        },
    )
    assert older.status_code == 200, older.text
    assert older.json()["history_order"] == "full"
    assert older.json()["offset"] == older_start


def test_tail_reconciles_pending_attachments_in_transcript_order(
    client, auth, app_module, tmp_path,
):
    from backend import chat as chat_mod

    image = {"type": "image", "source": {"media_type": "image/png"}}
    entries = [
        _entry("u1", "user", [image]),
        _entry("a1", "assistant", "one", "u1"),
        _entry("u2", "user", [image], "a1"),
    ]
    sid, _ = _make_endpoint_session(client, auth, chat_mod, tmp_path, entries)
    chat_mod.sess.append_pending_attachments(
        sid, images=[{"mime": "image/png", "thumb": "first"}])
    chat_mod.sess.append_pending_attachments(
        sid, images=[{"mime": "image/png", "thumb": "second"}])

    response = client.get(
        f"/api/chat/sessions/{sid}", headers=auth, params={"tail": 1})
    assert response.status_code == 200, response.text
    assert response.json()["messages"][0]["images"][0]["thumb"] == "second"
    annotations = chat_mod.sess.get_message_annotations(sid)
    assert annotations["u1"]["images"][0]["thumb"] == "first"
    assert annotations["u2"]["images"][0]["thumb"] == "second"


def test_outline_uses_index_and_excludes_compact_summary(
    client, auth, app_module, tmp_path,
):
    from backend import chat as chat_mod
    entries = [
        _entry("u1", "user", "# First prompt"),
        _entry("c1", "user", "compacted", "u1", isCompactSummary=True),
        _entry("a1", "assistant", "answer", "c1"),
        _entry("u2", "user", [
            {"type": "image", "source": {"media_type": "image/png"}},
        ], "a1"),
    ]
    sid, _ = _make_endpoint_session(client, auth, chat_mod, tmp_path, entries)
    response = client.get(f"/api/chat/sessions/{sid}/outline", headers=auth)
    assert response.status_code == 200
    assert response.json()["outline"] == [
        {"preview": "First prompt", "uuid": "u1"},
        {"preview": "(empty)", "uuid": "u2"},
    ]
    assert response.json()["history_generation"]
