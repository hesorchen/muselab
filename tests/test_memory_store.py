"""Canonical memory registry invariants."""
from pathlib import Path

from backend.memory_store import MemoryStore


def test_evidence_is_idempotent_and_episode_keeps_provenance(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    first = store.add_evidence("u", "s", "user", "same message", source_ref="msg-1")
    second = store.add_evidence("u", "s", "user", "same message", source_ref="msg-1")
    assert first == second

    episode = store.get_or_create_episode("u", "s", idle_seconds=60)
    store.attach_evidence(episode["id"], [first])
    loaded = store.episode(episode["id"])
    assert loaded["turn_count"] == 1
    assert loaded["evidence"][0]["source_ref"] == "msg-1"


def test_confirm_correct_forget_and_lexical_search(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    old = store.create_memory(
        "u", "preference", "用户偏好先验证再提交",
        authority="confirmed", confidence=1.0,
        sources=[{"source_type": "user_action", "source_id": "ui",
                  "relation": "confirmed_by"}],
    )
    hits = store.lexical_search("u", "用户偏好先验证再提交")
    assert hits[0]["memory"]["id"] == old["id"]
    assert store.memory_sources([old["id"]])[old["id"]][0]["source_type"] == "user_action"

    new = store.supersede_memory(old["id"], "u", "用户偏好先测试，但提交前必须确认")
    assert store.memory(old["id"])["status"] == "superseded"
    assert new["authority"] == "confirmed"
    assert any(row["relation"] == "supersedes" for row in new["relations"])

    assert store.delete_memory(new["id"], "u") is True
    assert store.memory(new["id"])["status"] == "deleted"
    assert store.delete_memory(new["id"], "other-user") is False


def test_persistent_jobs_and_artifact_review_state(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    job_id = store.enqueue("consolidate_episode", {"episode_id": "ep-1"})
    claimed = store.claim_job()
    assert claimed["id"] == job_id
    store.finish_job(job_id, error="temporary", retry_seconds=0)
    assert store.claim_job()["id"] == job_id
    store.finish_job(job_id)
    assert store.list_jobs()[0]["status"] == "done"

    artifact = store.create_artifact(
        "u", "skill_candidate", "candidate", {"name": "safe-flow"},
        ["ep-1", "ep-2", "ep-3"])
    assert artifact["status"] == "pending_review"
    assert store.update_artifact(artifact["id"], status="rejected")["status"] == "rejected"


def test_orphaned_running_jobs_are_recovered(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    job_id = store.enqueue("reindex_memory", {"memory_id": "memory-1"})
    assert store.claim_job()["id"] == job_id
    assert store.list_jobs()[0]["status"] == "running"

    assert store.recover_running_jobs() == 1
    recovered = store.list_jobs()[0]
    assert recovered["status"] == "queued"
    assert "recovered" in recovered["last_error"]


def test_stats_and_reopen_preserve_registry(tmp_path: Path):
    path = tmp_path / "memory.sqlite3"
    first = MemoryStore(path)
    first.create_memory("u", "fact", "durable fact")
    first.get_or_create_episode("u", "s", idle_seconds=60)
    second = MemoryStore(path)
    assert second.stats("u")["memories"] == 1
    assert second.stats("u")["episodes"] == 1


def test_idle_episode_is_closed_for_background_consolidation(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    evidence = store.add_evidence("u", "s", "user", "one turn")
    episode = store.get_or_create_episode("u", "s", idle_seconds=60)
    store.attach_evidence(episode["id"], [evidence])
    closed = store.close_idle_episodes("u", cutoff=float("inf"))
    assert closed == [episode["id"]]
    assert store.episode(episode["id"], with_evidence=False)["status"] == "closed"


def test_lexical_search_matches_chinese_substrings(tmp_path: Path):
    """CJK must be searchable by fragment, not only by the exact full string.

    FTS5 unicode61 makes an unbroken Chinese run one token, so before bigram
    expansion a query like "记忆系统" could not match a memory containing it —
    the lexical channel returned nothing for Chinese and hybrid recall
    degraded to dense-only.
    """
    store = MemoryStore(tmp_path / "memory.sqlite3")
    target = store.create_memory(
        "u", "fact", "记忆系统的召回链路依赖 qdrant 与 bge-m3 向量检索")
    store.create_memory("u", "fact", "完全无关的另一条记录")

    for query in ("记忆系统", "召回链路", "记忆", "向量检索 是否正常"):
        hits = store.lexical_search("u", query)
        assert [hit["memory"]["id"] for hit in hits][:1] == [target["id"]], query

    # Latin tokens must keep working unchanged.
    assert store.lexical_search("u", "qdrant")[0]["memory"]["id"] == target["id"]
    assert store.lexical_search("u", "bge-m3")[0]["memory"]["id"] == target["id"]
    # A query sharing no fragment must stay empty rather than matching all.
    assert store.lexical_search("u", "xyzzy") == []


def test_reopening_an_old_registry_reindexes_fts_for_cjk(tmp_path: Path):
    """A registry written before bigram indexing becomes searchable on open.

    memory_fts holds the expanded form, so rows indexed by an older build are
    invisible to the new query expansion until rebuilt. `memories` is the
    source of truth; the migration replays it.
    """
    import sqlite3

    from backend.memory_store import _FTS_SCHEMA_VERSION

    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path)
    memory = store.create_memory("u", "fact", "融合层的排序权重需要小流量验证")

    # Simulate the pre-migration state: raw content in the index, no version.
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM memory_fts")
        conn.execute(
            "INSERT INTO memory_fts(memory_id,owner_id,kind,content) VALUES (?,?,?,?)",
            (memory["id"], "u", "fact", "融合层的排序权重需要小流量验证"))
        conn.execute("PRAGMA user_version=0")
    assert MemoryStore(path).lexical_search("u", "排序权重")[0]["memory"]["id"] \
        == memory["id"]
    with sqlite3.connect(path) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) \
            == _FTS_SCHEMA_VERSION


def test_portable_snapshot_round_trip_preserves_governance_and_provenance(
    tmp_path: Path,
):
    source = MemoryStore(tmp_path / "source.sqlite3")
    evidence_id = source.add_evidence(
        "owner-a", "session-a", "user", "证据内容",
        source_ref="message-1", metadata={"model": "test-model"})
    episode = source.get_or_create_episode(
        "owner-a", "session-a", idle_seconds=60)
    source.attach_evidence(episode["id"], [evidence_id])
    source.update_episode(
        episode["id"], status="closed", title="一次成功操作",
        summary="完整摘要", outcome="success", ended_at=10,
        entities_json=["实体"], attributes_json={"quality": "verified"})
    memory = source.create_memory(
        "owner-a", "decision", "上线前必须验证",
        authority="confirmed", confidence=0.97,
        entities=["上线"], attributes={"verification": {"supported": True}},
        tags=["release"], valid_from=5,
        sources=[
            {"source_type": "episode", "source_id": episode["id"],
             "relation": "derived_from"},
            {"source_type": "evidence", "source_id": evidence_id,
             "relation": "supported_by"},
        ])
    source.create_artifact(
        "owner-a", "reflection_run", "复盘",
        {"conclusion": "继续验证"}, [episode["id"]],
        model="test-model", status="active")

    snapshot = source.export_snapshot("owner-a")
    restored = MemoryStore(tmp_path / "restored.sqlite3")
    counts = restored.import_snapshot(snapshot, "owner-b")
    assert counts["memories"] == 1
    assert counts["episodes"] == 1
    assert counts["evidence"] == 1

    loaded = restored.memory(memory["id"])
    assert loaded["owner_id"] == "owner-b"
    assert loaded["authority"] == "confirmed"
    assert loaded["confidence"] == 0.97
    assert loaded["entities"] == ["上线"]
    assert loaded["attributes"]["verification"]["supported"] is True
    assert loaded["tags"] == ["release"]
    assert loaded["embedding_state"] == "pending"
    assert {row["source_type"] for row in loaded["sources"]} == {
        "episode", "evidence"}
    restored_episode = restored.episode(episode["id"])
    assert restored_episode["summary"] == "完整摘要"
    assert restored_episode["evidence"][0]["source_ref"] == "message-1"
    assert restored.list_artifacts("owner-b")[0]["payload"] == {
        "conclusion": "继续验证"}

    replay = restored.import_snapshot(snapshot, "owner-b")
    assert all(value == 0 for value in replay.values())


def test_recall_stats_backfill_live_updates_and_feedback_validation(tmp_path: Path):
    import json
    import sqlite3

    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path)
    memory = store.create_memory("u", "fact", "可召回事实")
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO recall_logs
               (id,owner_id,session_id,query,results_json,latency_ms,status,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            ("legacy-recall", "u", "s", "q", json.dumps([
                {"id": memory["id"]}, {"id": memory["id"]}]),
             1.0, "ok", 10.0),
        )
        conn.execute("DELETE FROM memory_migrations WHERE name='memory-recall-stats-v1'")
    reopened = MemoryStore(path)
    stats = reopened.memory_recall_stats("u", [memory["id"]])[memory["id"]]
    assert stats["recall_count"] == 1
    assert stats["first_recalled_at"] == 10.0
    assert stats["last_recalled_at"] == 10.0

    recall_id = reopened.log_recall(
        "u", "s", "next", [{"id": memory["id"]}, {"id": memory["id"]}],
        2.0, "ok", created_at=20.0)
    stats = reopened.memory_recall_stats("u", [memory["id"]])[memory["id"]]
    assert stats["recall_count"] == 2
    assert stats["last_recalled_at"] == 20.0
    assert reopened.feedback_memory(
        "u", memory["id"], useful=True, recall_id=recall_id
    )["helpful_count"] == 1
    try:
        reopened.feedback_memory(
            "u", memory["id"], useful=False, recall_id="missing")
    except ValueError:
        pass
    else:
        raise AssertionError("feedback must validate its recall receipt")


def test_traceback_resolves_owned_sessions_without_exposing_paths(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    session_id = "9cc9a6c1-fcc0-46fe-a67e-503411a07ea6"
    message_id = "f941e29e-9627-43d1-81c1-0754866a31c6"
    evidence_id = store.add_evidence("u", session_id, "user", "证据")
    episode = store.get_or_create_episode("u", session_id, idle_seconds=60)
    store.attach_evidence(episode["id"], [evidence_id])
    memory = store.create_memory("u", "fact", "事实", sources=[
        {"source_type": "episode", "source_id": episode["id"],
         "relation": "derived_from"},
        {"source_type": "evidence", "source_id": evidence_id,
         "relation": "supports"},
        {"source_type": "message", "source_id": f"{session_id}:{message_id}",
         "relation": "confirmed_from"},
    ])
    sites = store.memory_traceback("u", memory["id"])
    assert any(site["message_id"] == message_id for site in sites)
    assert all("path" not in site for site in sites)
    try:
        store.memory_traceback("other", memory["id"])
    except KeyError:
        pass
    else:
        raise AssertionError("traceback must be owner fenced")


def test_verified_online_backup_has_receipt_hash_and_private_permissions(
        tmp_path: Path):
    import hashlib
    import sqlite3

    store = MemoryStore(tmp_path / "registry" / "memory.sqlite3")
    store.create_memory("u", "fact", "备份内容")
    backup_dir = tmp_path / "trusted-backups"
    receipt = store.create_backup("u", backup_dir)
    target = backup_dir / receipt["filename"]
    assert target.is_file()
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert oct(backup_dir.stat().st_mode & 0o777) == "0o700"
    assert hashlib.sha256(target.read_bytes()).hexdigest() == receipt["sha256"]
    with sqlite3.connect(target) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
    listed = store.list_backups("u", backup_dir)
    assert listed[0]["id"] == receipt["id"]
    assert listed[0]["exists"] is True


def test_imported_active_skill_requires_local_reapproval(tmp_path: Path):
    source = MemoryStore(tmp_path / "source.sqlite3")
    skill = source.create_artifact(
        "owner-a",
        "skill_candidate",
        "portable workflow",
        {
            "name": "portable-workflow",
            "skill_markdown": (
                "---\nname: portable-workflow\n"
                "description: Portable workflow\n---\n\n# Workflow\n"
            ),
            "installed_path": "/tmp/snapshot-controlled/SKILL.md",
            "approved_at": 123.0,
        },
        [],
        status="active",
    )

    restored = MemoryStore(tmp_path / "restored.sqlite3")
    restored.import_snapshot(source.export_snapshot("owner-a"), "owner-b")

    imported = restored.artifact(skill["id"])
    assert imported["owner_id"] == "owner-b"
    assert imported["status"] == "pending_review"
    assert "installed_path" not in imported["payload"]
    assert "approved_at" not in imported["payload"]
