"""Episode consolidation, verification, hybrid recall and Skill approval."""
import asyncio

import pytest


def _config(mode="active"):
    from backend.memory_config import MemoryConfig
    return MemoryConfig.model_validate({
        "mode": mode,
        "generation_model": "provider:test-model",
        "embedding": {
            "base_url": "http://embed/v1", "model": "bge", "dimensions": 3,
        },
        "vector": {
            "provider": "qdrant", "url": "http://qdrant:6333",
            "collection": "memory",
        },
        "consolidation": {
            "episode_turns": 2, "episode_idle_minutes": 30,
            "dreamer_enabled": True, "verifier_enabled": True,
            "skill_learning_enabled": True,
            "min_reflection_episodes": 2, "min_skill_success_episodes": 3,
        },
    })


def _run(coro):
    return asyncio.run(coro)


def test_dreamer_and_verifier_create_traceable_memory(tmp_path, monkeypatch):
    from backend import memory_engine as module
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore
    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)

    user = instance.store.add_evidence(
        "default", "s", "user", "以后所有报告先核对数字", source_ref="u-1")
    assistant = instance.store.add_evidence(
        "default", "s", "assistant", "收到", source_ref="a-1")
    episode = instance.store.get_or_create_episode("default", "s", idle_seconds=60)
    instance.store.attach_evidence(episode["id"], [user, assistant])
    instance.store.update_episode(
        episode["id"], status="closed", outcome="success", ended_at=2)

    async def fake_json(_self, _system, prompt):
        if "possibly_related_existing_memories" in prompt:
            return {
                "supported": True, "conflict": False,
                "prediction_value": 0.9, "reason": "direct user evidence",
            }
        return {
            "episode": {
                "title": "报告核对偏好", "summary": "用户要求报告先核对数字",
                "outcome": "success", "entities": ["报告"], "attributes": {},
            },
            "memories": [{
                "kind": "preference", "content": "用户要求报告先核对数字",
                "source_ids": [user], "confidence": 0.95, "future_use": 0.9,
                "reuse_conditions": ["生成报告时"], "attributed_to": "user",
            }],
        }

    monkeypatch.setattr(module.GenerationProvider, "complete_json", fake_json)
    _run(instance._consolidate_episode(episode["id"]))

    memories = instance.store.list_memories("default", status="active")
    assert len(memories) == 1
    detail = instance.store.memory(memories[0]["id"])
    assert {source["source_type"] for source in detail["sources"]} == {
        "episode", "evidence"}
    assert detail["attributes"]["verification"]["supported"] is True
    assert instance.store.episode(episode["id"])["extractor_version"].startswith(
        "dreamer-v1:")
    assert any(job["kind"] == "reindex_memory" for job in instance.store.list_jobs())


def test_shadow_mode_keeps_inference_pending_review(tmp_path, monkeypatch):
    from backend import memory_engine as module
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore
    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config("shadow")
    monkeypatch.setattr(instance, "config", lambda: cfg)
    ev = instance.store.add_evidence("default", "s", "user", "稳定偏好")
    ep = instance.store.get_or_create_episode("default", "s", idle_seconds=60)
    instance.store.attach_evidence(ep["id"], [ev])

    async def fake_json(_self, _system, _prompt):
        return {
            "supported": True, "conflict": False,
            "prediction_value": 0.9, "reason": "supported",
        }

    monkeypatch.setattr(module.GenerationProvider, "complete_json", fake_json)
    result = _run(instance._verify_and_store({
        "kind": "preference", "content": "稳定偏好", "confidence": 0.8,
        "future_use": 0.8, "attributed_to": "user",
    }, ep["id"], [ev]))
    assert result["status"] == "pending_review"
    assert _run(instance.recall("稳定偏好", "s")) == []


def test_cancelled_turn_does_not_poison_previous_success_episode(
        tmp_path, monkeypatch):
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore
    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)

    _run(instance.record_turn("s", "model", "第一问", "第一答"))
    cancelled_id = _run(instance.record_cancelled_turn(
        "s", "第二问", turn_id="turn-2"))

    episodes = instance.store.list_episodes("default")
    previous = next(item for item in episodes if item["id"] != cancelled_id)
    cancelled = next(item for item in episodes if item["id"] == cancelled_id)
    assert previous["status"] == "closed"
    assert previous["outcome"] == "success"
    assert cancelled["outcome"] == "cancelled"
    assert any(
        job["kind"] == "consolidate_episode"
        and job["payload"]["episode_id"] == previous["id"]
        for job in instance.store.list_jobs())
    assert not any(
        job["kind"] == "consolidate_episode"
        and job["payload"]["episode_id"] == cancelled["id"]
        for job in instance.store.list_jobs())


def test_failed_turn_gets_a_separate_episode(tmp_path, monkeypatch):
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore
    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)

    successful_id = _run(instance.record_turn(
        "s", "model", "先完成一件事", "已经完成"))
    failed_id = _run(instance.record_failed_turn(
        "s", "model", "再完成一件事", "", "工具失败", turn_id="turn-2"))

    assert failed_id != successful_id
    successful = instance.store.episode(successful_id)
    failed = instance.store.episode(failed_id)
    assert successful["outcome"] == "success"
    assert successful["status"] == "closed"
    assert failed["outcome"] == "failure"
    assert all(
        item["metadata"]["turn_outcome"] == "failure"
        for item in failed["evidence"])


def test_cross_episode_dream_rejects_copied_evidence(
        tmp_path, monkeypatch):
    from backend import memory_engine as module
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore
    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)
    episode_ids = []
    for session_id in ("original", "fork"):
        user = instance.store.add_evidence(
            "default", session_id, "user", "每次上线前先做小流量验证",
            source_ref=f"{session_id}:user")
        assistant = instance.store.add_evidence(
            "default", session_id, "assistant", "收到",
            source_ref=f"{session_id}:assistant")
        episode = instance.store.get_or_create_episode(
            "default", session_id, idle_seconds=60)
        instance.store.attach_evidence(episode["id"], [user, assistant])
        instance.store.update_episode(
            episode["id"], status="closed", summary="上线前小流量验证",
            outcome="success", ended_at=2)
        episode_ids.append(episode["id"])

    async def must_not_generate(*_args, **_kwargs):
        raise AssertionError("copied evidence is not independent")

    monkeypatch.setattr(module.GenerationProvider, "complete_json", must_not_generate)
    _run(instance._cross_episode_dream(episode_ids))
    assert instance.store.list_artifacts(
        "default", kind="reflection_run") == []


def test_hybrid_recall_fuses_channels_and_exposes_trace(tmp_path, monkeypatch):
    from backend import memory_engine as module
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore
    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)
    memory = instance.store.create_memory(
        "default", "decision", "推荐系统上线前需要小流量验证",
        authority="confirmed", confidence=1.0)

    class FakeEmbedding:
        def __init__(self, _config): pass

        async def embed(self, _texts):
            return [[1.0, 0.0, 0.0]]

    class FakeVector:
        async def search(self, _vector, *, owner_id, limit):
            assert owner_id == "default"
            return [{"id": memory["id"], "score": 0.9, "channel": "dense"}]

    monkeypatch.setattr(module, "EmbeddingProvider", FakeEmbedding)
    monkeypatch.setattr(module, "vector_store", lambda _config: FakeVector())
    rows = _run(instance.recall("推荐系统 小流量 验证", "session-x"))
    assert rows[0]["id"] == memory["id"]
    assert "dense" in rows[0]["channels"]
    trace = instance.pop_recall_trace("session-x")
    assert trace["count"] == 1
    assert trace["items"][0]["sources"] == []


def test_skill_draft_is_inert_until_explicit_approval(tmp_path, monkeypatch):
    from backend import memory_engine as module
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore
    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)
    candidate = instance.store.create_artifact(
        "default", "skill_candidate", "safe workflow", {
            "name": "safe-workflow",
            "description": "A reviewed workflow",
            "skill_markdown": "---\nname: safe-workflow\ndescription: Safe\n---\n\n# Flow\n",
        }, ["ep-1", "ep-2", "ep-3"])
    home = tmp_path / "home"
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: home))

    discoverable = home / ".claude" / "skills" / "muselab-generated-safe-workflow"
    assert not discoverable.exists()
    approved = instance.approve_skill(candidate["id"])
    assert approved["status"] == "active"
    assert (discoverable / "SKILL.md").is_file()
    disabled = instance.disable_skill(candidate["id"])
    assert disabled["status"] == "disabled"
    assert not (discoverable / "SKILL.md").exists()


def test_disable_skill_rejects_snapshot_controlled_path(tmp_path, monkeypatch):
    from backend import memory_engine as module
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore
    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)
    home = tmp_path / "home"
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: home))
    victim = tmp_path / "must-not-move.txt"
    victim.write_text("private", encoding="utf-8")
    artifact = instance.store.create_artifact(
        "default",
        "skill_candidate",
        "untrusted active workflow",
        {
            "name": "untrusted-workflow",
            "installed_path": str(victim),
        },
        [],
        status="active",
    )

    with pytest.raises(ValueError, match="escapes"):
        instance.disable_skill(artifact["id"])

    assert victim.read_text(encoding="utf-8") == "private"
    assert instance.store.artifact(artifact["id"])["status"] == "active"


def test_recall_bounds_the_text_sent_to_the_embedder(tmp_path, monkeypatch):
    """Long prior turns must not inflate the embedding input without bound.

    Local CPU embedding latency scales with input length, so joining two
    1000-char prior turns onto the query pushed the dense channel past the
    soft timeout on exactly the long-context turns where recall matters.
    """
    from backend import memory_engine as module
    from backend.memory_engine import MemoryEngine, _RECALL_QUERY_CHARS
    from backend.memory_store import MemoryStore
    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)
    for index in range(2):
        instance.store.add_evidence(
            "default", "session-long", "user", f"{index}" + "旧上下文" * 400,
            source_ref=f"msg-{index}")
    question = "当前召回是否正常"
    seen: list[str] = []

    class FakeEmbedding:
        def __init__(self, _config): pass

        async def embed(self, texts):
            seen.extend(texts)
            return [[1.0, 0.0, 0.0]]

    class FakeVector:
        async def search(self, _vector, *, owner_id, limit):
            return []

    monkeypatch.setattr(module, "EmbeddingProvider", FakeEmbedding)
    monkeypatch.setattr(module, "vector_store", lambda _config: FakeVector())
    _run(instance.recall(question, "session-long"))
    assert seen and len(seen[0]) <= _RECALL_QUERY_CHARS
    # The tail carries the actual question; truncation must keep it.
    assert seen[0].endswith(question)


def test_default_soft_timeout_exceeds_a_single_embedding_call():
    """Guards the budget against regressing below one embedding round-trip."""
    from backend.memory_config import RetrievalConfig
    assert RetrievalConfig().soft_timeout_ms >= 1000


def test_transcript_reconciliation_stops_at_next_real_user_turn(
    tmp_path, monkeypatch,
):
    import json

    from backend import chat
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore

    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    user_id = instance.store.add_evidence(
        "default", "session-a", "user", "第一问")
    episode = instance.store.get_or_create_episode(
        "default", "session-a", idle_seconds=60)
    instance.store.attach_evidence(episode["id"], [user_id])
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "user", "uuid": "u1",
         "message": {"content": "第一问"}},
        {"type": "assistant", "uuid": "a1", "message": {"content": [
            {"type": "tool_use", "id": "tool-1", "name": "Read",
             "input": {"file_path": "one.md"}},
        ]}},
        {"type": "user", "uuid": "r1", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tool-1", "content": "one"},
        ]}},
        {"type": "user", "uuid": "u2",
         "message": {"content": "第二问"}},
        {"type": "assistant", "uuid": "a2", "message": {"content": [
            {"type": "tool_use", "id": "tool-2", "name": "Read",
             "input": {"file_path": "two.md"}},
        ]}},
    ]
    transcript.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in records),
        encoding="utf-8",
    )
    monkeypatch.setattr(chat, "_find_session_jsonl", lambda _sid: transcript)

    _run(instance._reconcile_transcript(
        episode["id"], user_id, "session-a"))
    event_types = [
        row["event_type"] for row in instance.store.episode(episode["id"])["evidence"]
    ]
    assert event_types == ["message", "tool_use", "tool_result"]


def test_reindex_all_queues_one_batch_job(tmp_path, monkeypatch):
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore

    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)
    for index in range(5):
        instance.store.create_memory(
            "default", "fact", f"批量索引 {index}")

    assert instance.reindex_all() == 5
    jobs = instance.store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "reindex_memories"
    assert len(jobs[0]["payload"]["memory_ids"]) == 5
