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


def _compacted_entries():
    """Pre-compact turns, then the disconnected root /compact writes.

    The summary's parent is a `system` record whose own parentUuid is None, so
    walking parents back from the leaf stops AT the summary and never reaches
    u1..a2 — which is exactly why they fall out of the normal order.
    """
    return [
        _entry("u1", "user", "old one"),
        _entry("a1", "assistant", "old reply", "u1"),
        _entry("u2", "user", "old two"),
        _entry("a2", "assistant", "old reply two", "u2"),
        _entry("sys", "system", "boundary", None, subtype="compact_boundary"),
        _entry("c1", "user", "summary", "sys", isCompactSummary=True),
        _entry("u3", "user", "after compact", "c1"),
        _entry("a3", "assistant", "after reply", "u3"),
    ]


def test_pre_chain_bubbles_counts_stranded_pre_compact_history(tmp_path):
    transcript = tmp_path / "compacted.jsonl"
    index_path = tmp_path / "compacted.index.json"
    _append(transcript, *_compacted_entries())
    index = ti.ensure_index("compacted", transcript, index_path, _describe)
    records = index["records"]
    # The visible chain starts at the summary; four bubbles precede it.
    assert [records[i]["uuid"] for i in index["orders"]["normal"]] == [
        "c1", "u3", "a3",
    ]
    assert index["bubble_prefix"]["normal"][-1] == 3
    assert ti.pre_chain_bubbles(index) == 4


def test_pre_chain_bubbles_ignores_sidechain_only_divergence(tmp_path):
    """A subagent turn makes full longer than normal WITHOUT stranding history.

    This is the case that kills the naive `full_total - normal_total`: the
    difference is 1 here, but nothing is unreachable and the button must
    stay hidden.
    """
    transcript = tmp_path / "sidechain.jsonl"
    index_path = tmp_path / "sidechain.index.json"
    _append(
        transcript,
        _entry("u1", "user", "root"),
        _entry("a1", "assistant", "reply", "u1"),
        _entry("side", "assistant", "subagent", "u1", isSidechain=True),
        _entry("u2", "user", "next", "a1"),
        _entry("a2", "assistant", "leaf", "u2"),
    )
    index = ti.ensure_index("sidechain", transcript, index_path, _describe)
    full_total = index["bubble_prefix"]["full"][-1]
    normal_total = index["bubble_prefix"]["normal"][-1]
    assert full_total - normal_total == 1          # the trap
    assert ti.pre_chain_bubbles(index) == 0        # the answer


def test_pre_chain_bubbles_zero_on_empty_transcript(tmp_path):
    transcript = tmp_path / "empty.jsonl"
    index_path = tmp_path / "empty.index.json"
    transcript.write_text("", encoding="utf-8")
    index = ti.ensure_index("empty", transcript, index_path, _describe)
    assert ti.pre_chain_bubbles(index) == 0


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


def test_window_endpoint_interleaves_cancelled_snapshot_at_original_anchor(
    client, auth, app_module, tmp_path,
):
    """Partial JSONL rows are replaced by one durable display-only snapshot."""
    from backend import chat as chat_mod

    entries = [
        _entry("u0", "user", "before"),
        _entry("a0", "assistant", "before answer", "u0"),
        # The CLI managed to append part of the interrupted turn before its
        # process was force-stopped. These UUIDs must be hidden, not duplicated.
        _entry("u-cancel", "user", "cancel prompt", "a0"),
        _entry("a-cancel", "assistant", "canonical partial", "u-cancel"),
        # A later successful turn remains after the interrupted display layer.
        _entry("u-after", "user", "after prompt", "a-cancel"),
        _entry("a-after", "assistant", "after answer", "u-after"),
    ]
    sid, _ = _make_endpoint_session(client, auth, chat_mod, tmp_path, entries)
    turn_id = "00000000-0000-4000-8000-000000000099"
    path = chat_mod._cancelled_turn_snapshot_path(sid, turn_id)
    assert path is not None
    chat_mod.atomic_write_text(path, json.dumps({
        "schema": chat_mod._CANCELLED_TURN_SNAPSHOT_SCHEMA,
        "sid": sid,
        "turn_id": turn_id,
        "started_at_ms": 1_700_000_000_000,
        "interrupted_at_ms": 1_700_000_001_000,
        "anchors": {
            "normal": {"uuid": "a0", "total": 2},
            "full": {"uuid": "a0", "total": 2},
        },
        "hidden_uuids": ["u-cancel", "a-cancel"],
        "messages": [
            {
                "role": "user", "text": "cancel prompt",
                "_key": f"cancelled:{turn_id}:0", "_interrupted": True,
            },
            {
                "role": "assistant", "text": "live partial survives",
                "_key": f"cancelled:{turn_id}:1", "_interrupted": True,
            },
        ],
    }, ensure_ascii=False))

    response = client.get(
        f"/api/chat/sessions/{sid}", headers=auth, params={"tail": 20})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [(item["role"], item.get("text")) for item in body["messages"]] == [
        ("user", "before"),
        ("assistant", "before answer"),
        ("user", "cancel prompt"),
        ("assistant", "live partial survives"),
        ("user", "after prompt"),
        ("assistant", "after answer"),
    ]
    assert body["total"] == 6
    assert body["message_count"] == 6
    assert body["turn_count"] == 3
    assert "canonical partial" not in response.text
    assert "~cancelled-" in body["history_generation"]

    page = client.get(
        f"/api/chat/sessions/{sid}",
        headers=auth,
        params={
            "offset": 1,
            "limit": 4,
            "history_generation": body["history_generation"],
        },
    )
    assert page.status_code == 200, page.text
    assert [(item["role"], item.get("text")) for item in page.json()["messages"]] == [
        ("assistant", "before answer"),
        ("user", "cancel prompt"),
        ("assistant", "live partial survives"),
        ("user", "after prompt"),
    ]


def test_late_canonical_flush_keeps_interrupted_footer_after_refresh(
    client, auth, app_module, tmp_path,
):
    """A delayed AssistantMessage must inherit the earlier interrupt truth.

    This reproduces the live race: Stop renders an interrupted footer while
    the CLI is being torn down, the browser refreshes, and only then does the
    canonical assistant row reach JSONL.  It must not be inferred completed
    merely because a later user turn now follows it.
    """
    from backend import chat as chat_mod

    initial = [
        _entry("u0", "user", "before",
               timestamp="2024-01-01T00:00:00.000Z"),
        _entry("a0", "assistant", "before answer", "u0",
               timestamp="2024-01-01T00:00:00.500Z"),
    ]
    sid, transcript = _make_endpoint_session(
        client, auth, chat_mod, tmp_path, initial)
    _, boundary = chat_mod._turn_transcript_boundary(
        sid, "claude-sonnet-4-6")
    bc = chat_mod.TurnBroadcast(
        session_id=sid, model="codex:gpt-5.6-sol")
    bc.user_text = "repeatable prompt"
    bc.started_at = 1_704_067_201.0
    bc.cancelled_at_ms = 1_704_067_202_500
    bc.cancelled = True
    bc.canonical_terminal_published = True
    bc.transcript_boundary = boundary
    bc.publish({
        "event": "text",
        "data": json.dumps({"text": "live partial"}),
    })

    try:
        # No AssistantMessage UUID exists yet.  The canonical-terminal flag
        # must no longer suppress the temporary interrupted snapshot.
        assert chat_mod._persist_cancelled_turn_snapshot(bc) is True
        snapshot_path = chat_mod._cancelled_turn_snapshot_path(sid, bc.turn_id)
        assert snapshot_path is not None and snapshot_path.exists()

        _append(
            transcript,
            _entry("u-cancel", "user", "repeatable prompt", "a0",
                   timestamp="2024-01-01T00:00:01.200Z"),
            _entry("a-cancel", "assistant", "late canonical partial", "u-cancel",
                   timestamp="2024-01-01T00:00:05.000Z"),
            _entry("u-after", "user", "repeatable prompt", "a-cancel",
                   timestamp="2024-01-01T00:00:06.000Z"),
            _entry("a-after", "assistant", "later success", "u-after",
                   timestamp="2024-01-01T00:00:07.000Z"),
        )

        response = client.get(
            f"/api/chat/sessions/{sid}", headers=auth, params={"tail": 20})
        assert response.status_code == 200, response.text
        body = response.json()
        assert [(item["uuid"], item["role"]) for item in body["messages"]] == [
            ("u0", "user"), ("a0", "assistant"),
            ("u-cancel", "user"), ("a-cancel", "assistant"),
            ("u-after", "user"), ("a-after", "assistant"),
        ]
        cancelled = next(
            item for item in body["messages"] if item["uuid"] == "a-cancel")
        assert cancelled["turn_status"] == "cancelled"
        assert cancelled["ts"] == bc.cancelled_at_ms
        assert cancelled["elapsed"] == 1.5
        assert cancelled["model"] == "codex:gpt-5.6-sol"
        assert not snapshot_path.exists()
        assert chat_mod.sess.get_message_annotations(sid)["a-cancel"][
            "turn_status"] == "cancelled"
    finally:
        bc.close()


def test_interrupted_snapshot_never_steals_post_interrupt_resend(
    client, auth, app_module, tmp_path,
):
    """A result-less stop must not mark a fast same-text resend cancelled."""
    from backend import chat as chat_mod

    sid, transcript = _make_endpoint_session(
        client, auth, chat_mod, tmp_path, [
            _entry("u0", "user", "before",
                   timestamp="2024-01-01T00:00:00.000Z"),
            _entry("a0", "assistant", "before answer", "u0",
                   timestamp="2024-01-01T00:00:00.500Z"),
        ])
    _, boundary = chat_mod._turn_transcript_boundary(
        sid, "claude-sonnet-4-6")
    bc = chat_mod.TurnBroadcast(
        session_id=sid, model="codex:gpt-5.6-sol")
    bc.user_text = "same prompt"
    bc.started_at = 1_704_067_201.0
    bc.cancelled_at_ms = 1_704_067_202_000
    bc.cancelled = True
    bc.transcript_boundary = boundary
    bc.publish({
        "event": "text", "data": json.dumps({"text": "stopped partial"}),
    })

    try:
        assert chat_mod._persist_cancelled_turn_snapshot(bc) is True
        snapshot_path = chat_mod._cancelled_turn_snapshot_path(sid, bc.turn_id)
        assert snapshot_path is not None and snapshot_path.exists()
        # The interrupted query wrote no canonical user row.  A new turn with
        # identical text starts after the click and must remain completed.
        _append(
            transcript,
            _entry("u-resend", "user", "same prompt", "a0",
                   timestamp="2024-01-01T00:00:03.000Z"),
            _entry("a-resend", "assistant", "successful resend", "u-resend",
                   timestamp="2024-01-01T00:00:04.000Z"),
        )
        response = client.get(
            f"/api/chat/sessions/{sid}", headers=auth, params={"tail": 20})
        assert response.status_code == 200, response.text
        body = response.json()
        resend = next(
            item for item in body["messages"] if item.get("uuid") == "a-resend")
        assert resend["turn_status"] == "completed"
        assert snapshot_path.exists()
        assert "a-resend" not in chat_mod.sess.get_message_annotations(sid)
    finally:
        bc.close()


def test_endpoint_reports_pre_total_and_zeroes_it_in_full_order(
    client, auth, app_module, tmp_path,
):
    """`pre_total` is what makes "Load earlier" appear on a compacted session.

    The normal-order tail reports offset 0 / total 3 — by every pre-existing
    signal the client is looking at the whole conversation. `pre_total` is the
    only thing that says otherwise. Once the client crosses into full order
    those bubbles are inside `total`, so the field has to go quiet or the
    button would never switch off at the real start.
    """
    from backend import chat as chat_mod
    sid, _ = _make_endpoint_session(
        client, auth, chat_mod, tmp_path, _compacted_entries())

    normal = client.get(
        f"/api/chat/sessions/{sid}", headers=auth, params={"tail": 10})
    assert normal.status_code == 200, normal.text
    body = normal.json()
    assert body["history_order"] == "normal"
    assert body["offset"] == 0
    assert body["has_more"] is False       # nothing earlier IN THIS ORDER…
    assert body["pre_total"] == 4          # …but four bubbles are stranded

    full = client.get(
        f"/api/chat/sessions/{sid}", headers=auth,
        params={"tail": 10, "full": 1})
    assert full.status_code == 200, full.text
    full_body = full.json()
    assert full_body["history_order"] == "full"
    assert full_body["total"] == body["total"] + 4
    assert full_body["pre_total"] == 0


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


def test_reload_footer_recovers_hidden_continuation_start_and_four_fields(
    client, auth, app_module, tmp_path,
):
    """A zero-bubble task notification is the continuation's turn boundary."""
    from backend import chat as chat_mod

    notification = (
        "<task-notification><tool-use-id>toolu_bg</tool-use-id>"
        "<task-id>bg1</task-id><status>completed</status>"
        "<summary>done</summary></task-notification>"
    )
    entries = [
        _entry("u1", "user", "launch", timestamp="2026-08-07T10:35:20Z"),
        _entry("a1", "assistant", [{
            "type": "tool_use", "id": "toolu_bg", "name": "Bash",
            "input": {"command": "sleep 20", "run_in_background": True},
        }], "u1", timestamp="2026-08-07T10:35:22Z"),
        _entry("tr1", "user", [{
            "type": "tool_result", "tool_use_id": "toolu_bg",
            "content": "started",
        }], "a1", timestamp="2026-08-07T10:35:23Z"),
        # This record renders no user bubble and therefore is outside a normal
        # bubble-window read, but it is the true start of the auto-continuation.
        _entry("notify1", "user", notification, "tr1",
               timestamp="2026-08-07T10:35:34Z"),
        _entry("a2", "assistant", "background task finished", "notify1",
               timestamp="2026-08-07T10:35:40Z"),
        _entry("u2", "user", "next", "a2",
               timestamp="2026-08-07T10:37:26Z"),
        _entry("a3", "assistant", "next answer", "u2",
               timestamp="2026-08-07T10:37:29Z"),
    ]
    sid, _ = _make_endpoint_session(client, auth, chat_mod, tmp_path, entries)

    response = client.get(
        f"/api/chat/sessions/{sid}", headers=auth, params={"tail": 50})
    assert response.status_code == 200, response.text
    messages = response.json()["messages"]
    continuation_tail = next(item for item in messages if item["uuid"] == "a2")

    assert continuation_tail["turn_status"] == "completed"
    assert continuation_tail["ts"] == 1_786_098_940_000
    assert continuation_tail["elapsed"] == 6.0
    assert continuation_tail["model"] == "claude-sonnet-4-6"
    assert continuation_tail["turn_started_at"] == 1_786_098_934_000
    assert continuation_tail["_turn_origin_uuid"] == "notify1"


def test_reload_footer_moves_cancel_annotation_to_actual_tool_result_tail(
    client, auth, app_module, tmp_path,
):
    """The footer mounts after tool_result, not on the annotated assistant."""
    from backend import chat as chat_mod

    entries = [
        _entry("u1", "user", "run", timestamp="2026-08-07T11:34:00Z"),
        _entry("a1", "assistant", [{
            "type": "tool_use", "id": "toolu_stop", "name": "Bash",
            "input": {"command": "sleep 30"},
        }], "u1", timestamp="2026-08-07T11:34:01Z"),
        _entry("tr1", "user", [{
            "type": "tool_result", "tool_use_id": "toolu_stop",
            "content": "stopped",
        }], "a1", timestamp="2026-08-07T11:34:06Z"),
        _entry("u2", "user", "after", "tr1",
               timestamp="2026-08-07T11:35:00Z"),
    ]
    sid, _ = _make_endpoint_session(client, auth, chat_mod, tmp_path, entries)
    chat_mod.sess.set_message_annotation(
        sid, "a1",
        model="codex:gpt-5.6-sol",
        ts=1_786_102_446_000,
        turn_status="cancelled",
        elapsed_s=6.0,
    )

    response = client.get(
        f"/api/chat/sessions/{sid}", headers=auth, params={"tail": 50})
    assert response.status_code == 200, response.text
    tail = next(item for item in response.json()["messages"]
                if item["uuid"] == "tr1")

    assert tail["turn_status"] == "cancelled"
    assert tail["ts"] == 1_786_102_446_000
    assert tail["elapsed"] == 6.0
    assert tail["model"] == "codex:gpt-5.6-sol"


def test_reload_footer_exposes_four_fields_for_active_canonical_tail(
    client, auth, app_module, tmp_path,
):
    from backend import chat as chat_mod

    entries = [
        _entry("u1", "user", "run", timestamp="2026-08-07T11:34:00Z"),
        _entry("a1", "assistant", "partial", "u1",
               timestamp="2026-08-07T11:34:02Z"),
    ]
    sid, _ = _make_endpoint_session(client, auth, chat_mod, tmp_path, entries)
    active = chat_mod.TurnBroadcast(sid, model="codex:gpt-5.6-sol")
    active.last_assistant_uuid = "a1"
    chat_mod._active_turns[sid] = active
    try:
        response = client.get(
            f"/api/chat/sessions/{sid}", headers=auth, params={"tail": 50})
        assert response.status_code == 200, response.text
        tail = response.json()["messages"][-1]

        assert tail["turn_status"] == "running"
        assert tail["turn_started_at"] == 1_786_102_440_000
        assert tail["elapsed"] >= 0
        assert tail["model"] == "codex:gpt-5.6-sol"
    finally:
        chat_mod._active_turns.pop(sid, None)
        active.close()


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
