"""Episode consolidation, verification, hybrid recall and Skill approval."""
import asyncio
import sqlite3
import threading
import time

import httpx
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


def test_preflight_can_disable_background_memory_worker(tmp_path, monkeypatch):
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore

    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)
    monkeypatch.setenv("MUSELAB_MEMORY_WORKER_DISABLED", "1")

    async def scenario():
        instance.start()
        await asyncio.sleep(0)
        assert instance._workers == set()
        await instance.stop()

    _run(scenario())


def test_registry_lock_wait_does_not_freeze_event_loop(tmp_path, monkeypatch):
    """A ten-second SQLite busy wait must not stall unrelated coroutines."""
    from backend import memory_engine as module
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore

    path = tmp_path / "registry.sqlite3"
    instance = MemoryEngine(MemoryStore(path))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)
    evidence_id = instance.store.add_evidence(
        "default", "session", "user", "需要归档")
    episode = instance.store.get_or_create_episode(
        "default", "session", idle_seconds=60)
    instance.store.attach_evidence(episode["id"], [evidence_id])

    async def fake_json(_self, _system, _prompt):
        return {
            "episode": {
                "title": "归档", "summary": "已归档", "outcome": "success",
                "entities": [], "attributes": {},
            },
            "memories": [],
        }

    monkeypatch.setattr(module.GenerationProvider, "complete_json", fake_json)
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")

    async def scenario():
        task = asyncio.create_task(
            instance._consolidate_episode(episode["id"]))
        ticks = 0
        deadline = asyncio.get_running_loop().time() + 0.08
        while asyncio.get_running_loop().time() < deadline:
            ticks += 1
            await asyncio.sleep(0.005)
        assert not task.done()
        blocker.execute("ROLLBACK")
        await asyncio.wait_for(task, timeout=1)
        await instance.stop()
        return ticks

    try:
        ticks = _run(scenario())
    finally:
        if blocker.in_transaction:
            blocker.execute("ROLLBACK")
        blocker.close()

    assert ticks >= 3
    assert instance.store.episode(
        episode["id"], with_evidence=False)["summary"] == "已归档"


def test_registry_initialization_runs_on_actor_thread(tmp_path, monkeypatch):
    """Schema migration and first open stay off the asyncio thread."""
    from backend import memory_engine as module
    from backend.memory_engine import MemoryEngine

    path = tmp_path / "lazy.sqlite3"
    real_store = module.MemoryStore
    constructor_threads: list[int] = []

    def slow_store(store_path):
        constructor_threads.append(threading.get_ident())
        time.sleep(0.08)
        return real_store(store_path)

    monkeypatch.setattr(module, "MemoryStore", slow_store)
    monkeypatch.setattr(module, "database_path", lambda: path)
    instance = MemoryEngine()

    async def scenario():
        loop_thread = threading.get_ident()
        task = asyncio.create_task(
            instance._store_call(lambda store: store.stats("default")))
        ticks = 0
        deadline = asyncio.get_running_loop().time() + 0.04
        while asyncio.get_running_loop().time() < deadline:
            ticks += 1
            await asyncio.sleep(0.005)
        assert not task.done()
        stats = await asyncio.wait_for(task, timeout=1)
        await instance.stop()
        return loop_thread, ticks, stats

    loop_thread, ticks, stats = _run(scenario())
    assert ticks >= 2
    assert stats["memories"] == 0
    assert constructor_threads
    assert constructor_threads[0] != loop_thread


def test_store_actor_cancellation_preserves_fifo_and_stop_drains(tmp_path):
    """Cancellation drops the waiter, never a submitted DB transaction."""
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore

    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []
    actor_threads: list[int] = []

    def first(store):
        actor_threads.append(threading.get_ident())
        order.append("first-start")
        started.set()
        assert release.wait(timeout=1)
        store.create_memory("default", "fact", "committed after cancellation")
        order.append("first-commit")

    def second(store):
        actor_threads.append(threading.get_ident())
        order.append("second-read")
        return store.stats("default")["memories"]

    async def scenario():
        first_task = asyncio.create_task(instance._store_call(first))
        async with asyncio.timeout(1):
            while not started.is_set():
                await asyncio.sleep(0.001)
        second_task = asyncio.create_task(instance._store_call(second))
        await asyncio.sleep(0)
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task

        stop_task = asyncio.create_task(instance.stop())
        await asyncio.sleep(0.03)
        assert not stop_task.done()
        release.set()
        await asyncio.wait_for(stop_task, timeout=1)
        count = await second_task
        with pytest.raises(RuntimeError, match="closed"):
            await instance._store_call(lambda store: store.stats("default"))
        return count

    try:
        count = _run(scenario())
    finally:
        release.set()

    assert count == 1
    assert order == ["first-start", "first-commit", "second-read"]
    assert len(set(actor_threads)) == 1
    actor_thread = actor_threads[0]
    assert not any(thread.ident == actor_thread for thread in threading.enumerate())


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
                "decision": "accept", "supported": True, "conflict": False,
                "self_contained": True, "specific": True, "durable": True,
                "generic": False, "rewrite_required": False,
                "final_content": "用户要求报告先核对数字",
                "rewritten_content": "",
                "supported_claims": [{
                    "claim": "用户要求报告先核对数字",
                    "source_ids": [user], "evidence_type": "direct",
                    "runtime_status": "not_applicable",
                }],
                "unsupported_claims": [], "removed_claims": [],
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
    assert instance.store.episode(episode["id"])["extractor_version"] == (
        "dreamer-v3:model=provider:test-model")
    assert detail["attributes"]["dreamer_prompt_version"] == "dreamer-v3"
    assert detail["attributes"]["verifier_prompt_version"] == "verifier-v3"
    assert any(job["kind"] == "reindex_memory" for job in instance.store.list_jobs())


def test_verifier_minimally_rewrites_fragment_and_rejects_generic_candidate(
        tmp_path, monkeypatch):
    from backend import memory_engine as module
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore

    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)
    evidence_id = instance.store.add_evidence(
        "default", "s", "user", "MuseLab 上线前必须先运行冷启动预检")
    episode = instance.store.get_or_create_episode(
        "default", "s", idle_seconds=60)
    instance.store.attach_evidence(episode["id"], [evidence_id])
    responses = [{"schema": {
        "decision": "rewrite", "supported": True, "conflict": False,
        "self_contained": True, "specific": True, "durable": True,
        "generic": False, "rewrite_required": True,
        "final_content": "MuseLab 上线前必须先运行冷启动预检。",
        "rewritten_content": "MuseLab 上线前必须先运行冷启动预检。",
        "supported_claims": [{
            "claim": "MuseLab 上线前必须先运行冷启动预检",
            "source_ids": [evidence_id], "evidence_type": "direct",
            "runtime_status": "not_applicable",
        }],
        "unsupported_claims": [],
        "removed_claims": [{
            "claim": "这个上线前要先预检", "reason": "dangling reference",
        }],
        "prediction_value": 0.9, "reason": "adds the missing subject only",
    }}, {
        "decision": "reject", "supported": False, "conflict": False,
        "self_contained": True, "specific": False, "durable": True,
        "generic": True, "rewrite_required": False,
        "final_content": "", "rewritten_content": "",
        "supported_claims": [],
        "unsupported_claims": [{
            "claim": "面对重复流程，应当重视自动化", "reason": "generic advice",
        }],
        "removed_claims": [], "prediction_value": 0.8,
        "reason": "generic advice",
    }]

    async def fake_json(_self, _system, _prompt):
        return responses.pop(0)

    monkeypatch.setattr(module.GenerationProvider, "complete_json", fake_json)
    rewritten = _run(instance._verify_and_store({
        "kind": "decision", "content": "这个上线前要先预检",
        "confidence": 0.9, "future_use": 0.9,
    }, episode["id"], [evidence_id]))
    assert rewritten["content"] == "MuseLab 上线前必须先运行冷启动预检。"
    assert rewritten["attributes"]["verification"]["rewrite_applied"] is True
    assert rewritten["attributes"]["verification"]["conflict"] is False
    rejected = _run(instance._verify_and_store({
        "kind": "reflection", "content": "面对重复流程，应当重视自动化。",
        "confidence": 0.8, "future_use": 0.8,
    }, episode["id"], [evidence_id]))
    assert rejected is None


def test_verifier_v3_enforces_claim_sources_and_untested_marker(
        tmp_path, monkeypatch):
    from backend import memory_engine as module
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore

    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)
    evidence_id = instance.store.add_evidence(
        "default", "s", "tool", "静态符号提示 8+8 可能支持，但尚未实测")
    episode = instance.store.get_or_create_episode(
        "default", "s", idle_seconds=60)
    instance.store.attach_evidence(episode["id"], [evidence_id])
    responses = [{
        "decision": "accept", "supported": True, "conflict": False,
        "self_contained": True, "specific": True, "durable": True,
        "generic": False, "rewrite_required": False,
        "final_content": "8+8 是候选方案，尚未实测。", "rewritten_content": "",
        "supported_claims": [{
            "claim": "8+8 是尚未实测的候选方案",
            "source_ids": ["mem_not_evidence"], "evidence_type": "derived",
            "runtime_status": "untested",
        }],
        "unsupported_claims": [], "removed_claims": [],
        "prediction_value": 0.8, "reason": "wrong source type",
    }, {
        "decision": "rewrite", "supported": True, "conflict": False,
        "self_contained": True, "specific": True, "durable": True,
        "generic": False, "rewrite_required": True,
        "final_content": "8+8 可以运行。",
        "rewritten_content": "8+8 可以运行。",
        "supported_claims": [{
            "claim": "8+8 可能支持", "source_ids": [evidence_id],
            "evidence_type": "derived", "runtime_status": "untested",
        }],
        "unsupported_claims": [], "removed_claims": [],
        "prediction_value": 0.8, "reason": "missing untested marker",
    }, {
        "decision": "rewrite", "supported": True, "conflict": False,
        "self_contained": True, "specific": True, "durable": True,
        "generic": False, "rewrite_required": True,
        "final_content": "8+8 是静态上可能支持的候选方案，尚未实测。",
        "rewritten_content": "8+8 是静态上可能支持的候选方案，尚未实测。",
        "supported_claims": [{
            "claim": "8+8 是尚未实测的候选方案",
            "source_ids": [evidence_id], "evidence_type": "derived",
            "runtime_status": "untested",
        }],
        "unsupported_claims": [],
        "removed_claims": [{
            "claim": "8+8 可以运行", "reason": "no runtime success evidence",
        }],
        "prediction_value": 0.8, "reason": "certainty downgraded",
    }]

    async def fake_json(_self, _system, _prompt):
        return responses.pop(0)

    monkeypatch.setattr(module.GenerationProvider, "complete_json", fake_json)
    candidate = {
        "kind": "decision", "content": "8+8 可以运行。",
        "confidence": 0.8, "future_use": 0.9,
    }
    assert _run(instance._verify_and_store(
        candidate, episode["id"], [evidence_id])) is None
    assert _run(instance._verify_and_store(
        candidate, episode["id"], [evidence_id])) is None
    accepted = _run(instance._verify_and_store(
        candidate, episode["id"], [evidence_id]))
    assert accepted["content"] == "8+8 是静态上可能支持的候选方案，尚未实测。"


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
            "decision": "accept", "supported": True, "conflict": False,
            "self_contained": True, "specific": True, "durable": True,
            "generic": False, "rewrite_required": False,
            "final_content": "稳定偏好", "rewritten_content": "",
            "supported_claims": [{
                "claim": "稳定偏好", "source_ids": [ev],
                "evidence_type": "direct", "runtime_status": "not_applicable",
            }],
            "unsupported_claims": [], "removed_claims": [],
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
    event_loop_thread = threading.get_ident()
    io_threads = {}
    original_hydrate = instance.store.memories_by_ids
    original_log = instance.store.log_recall

    def tracked_hydrate(memory_ids):
        io_threads["hydrate"] = threading.get_ident()
        return original_hydrate(memory_ids)

    def tracked_log(*args, **kwargs):
        io_threads["log"] = threading.get_ident()
        return original_log(*args, **kwargs)

    monkeypatch.setattr(instance.store, "memories_by_ids", tracked_hydrate)
    monkeypatch.setattr(instance.store, "log_recall", tracked_log)

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
    assert io_threads["hydrate"] != event_loop_thread
    assert io_threads["log"] != event_loop_thread


def test_recall_returns_before_slow_telemetry_and_shutdown_drains(
        tmp_path, monkeypatch):
    from backend import memory_engine as module
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore

    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)
    memory = instance.store.create_memory(
        "default", "fact", "召回遥测不能阻塞回答",
        authority="confirmed", confidence=1.0)
    real_log = instance.store.log_recall

    def slow_log(*args, **kwargs):
        time.sleep(0.15)
        return real_log(*args, **kwargs)

    monkeypatch.setattr(instance.store, "log_recall", slow_log)

    class FakeEmbedding:
        def __init__(self, _config): pass

        async def embed(self, _texts):
            return [[1.0, 0.0, 0.0]]

    class FakeVector:
        async def search(self, _vector, *, owner_id, limit):
            return [{"id": memory["id"], "channel": "dense"}]

    monkeypatch.setattr(module, "EmbeddingProvider", FakeEmbedding)
    monkeypatch.setattr(module, "vector_store", lambda _config: FakeVector())

    async def scenario():
        started = time.perf_counter()
        rows = await instance.recall("遥测", "session")
        elapsed = time.perf_counter() - started
        assert rows[0]["id"] == memory["id"]
        assert elapsed < 0.12
        assert instance._telemetry_tasks
        await instance.stop(timeout=1)
        return elapsed

    _run(scenario())
    stats = instance.store.memory_recall_stats(
        "default", [memory["id"]])[memory["id"]]
    assert stats["recall_count"] == 1


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
    approved = _run(instance.approve_skill(candidate["id"]))
    assert approved["status"] == "active"
    assert (discoverable / "SKILL.md").is_file()
    disabled = _run(instance.disable_skill(candidate["id"]))
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
        _run(instance.disable_skill(artifact["id"]))

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


def _run_worker_job(tmp_path, monkeypatch, failures, *, kind="reindex_memory"):
    from backend import memory_engine as module, memory_store as store_module
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore

    instance = MemoryEngine(MemoryStore(tmp_path / "worker.sqlite3"))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)
    clock = [1000.0]
    monkeypatch.setattr(store_module, "_now", lambda: clock[0])
    events = []
    monkeypatch.setattr(
        module, "perf_event",
        lambda event, **fields: events.append((event, fields)),
    )
    calls = 0

    async def handle(_memory_id):
        nonlocal calls
        calls += 1
        if failures:
            raise failures.pop(0)

    monkeypatch.setattr(instance, "_index_memory", handle)
    job_id = instance.store.enqueue(
        kind, {"memory_id": "memory-1"}, owner_id=cfg.owner_id)
    finish_job = instance.store.finish_job

    def finish_and_advance(job_id, *, error=None, retry_seconds=None):
        finish_job(job_id, error=error, retry_seconds=retry_seconds)
        if retry_seconds is not None:
            clock[0] += retry_seconds
        else:
            instance._closing = True

    monkeypatch.setattr(instance.store, "finish_job", finish_and_advance)
    _run(instance._worker())
    job = next(row for row in instance.store.list_jobs() if row["id"] == job_id)
    return calls, job, events


@pytest.mark.parametrize(("exc", "retryable", "category", "status"), [
    (TimeoutError("secret prompt"), True, "timeout", None),
    (httpx.ConnectError(
        "secret transport", request=httpx.Request(
            "GET", "https://token.example/private")), True, "transport", None),
    (httpx.HTTPStatusError(
        "secret response", request=httpx.Request(
            "GET", "https://token.example/private"),
        response=httpx.Response(409)), True, "transient_http", 409),
    (httpx.HTTPStatusError(
        "secret response", request=httpx.Request(
            "GET", "https://token.example/private"),
        response=httpx.Response(401)), False, "authentication", 401),
    (ValueError("secret config"), False, "invalid_value", None),
    (TypeError("secret body"), False, "invalid_type", None),
    (KeyError("secret key"), False, "missing_key", None),
    (FileNotFoundError("/private/path"), False, "file_not_found", None),
    (PermissionError("/private/path"), False, "permission", None),
    (RuntimeError("unknown secret"), False, "unclassified", None),
])
def test_failure_classifier_is_explicit_and_private(exc, retryable, category, status):
    from backend.memory_engine import classify_memory_failure

    actual_retryable, detail = classify_memory_failure(exc)
    assert actual_retryable is retryable
    assert detail == {
        "category": category,
        "exception_class": type(exc).__name__,
        **({"status": status} if status is not None else {}),
    }
    assert "secret" not in repr(detail)
    assert "/private" not in repr(detail)
    assert "token.example" not in repr(detail)


def test_worker_retries_safe_transient_three_times_without_secret_leakage(
        tmp_path, monkeypatch, caplog):
    import httpx

    secret = "sk-super-secret-worker-token"
    failures = [
        httpx.HTTPStatusError(
            f"provider body {secret}",
            request=httpx.Request("POST", f"https://example.test/{secret}"),
            response=httpx.Response(500),
        )
        for _ in range(3)
    ]
    calls, job, events = _run_worker_job(tmp_path, monkeypatch, failures)

    assert calls == 3
    assert job["attempts"] == 3
    assert job["status"] == "failed"
    assert job["last_error"] == (
        '{"category":"transient_http","exception_class":'
        '"HTTPStatusError","status":500}'
    )
    assert [fields["outcome"] for _, fields in events] == [
        "retry", "retry", "failed"]
    assert all(event == "memory.job" for event, _ in events)
    assert secret not in job["last_error"]
    assert secret not in caplog.text
    assert "example.test" not in caplog.text


def test_worker_does_not_retry_terminal_generation_error(tmp_path, monkeypatch):
    from backend.memory_providers import GenerationError

    calls, job, events = _run_worker_job(tmp_path, monkeypatch, [GenerationError(
        retryable=False, category="invalid_configuration", api_error_status=400)])

    assert calls == 1
    assert job["attempts"] == 1
    assert job["status"] == "failed"
    assert job["last_error"] == (
        '{"category":"invalid_configuration","exception_class":'
        '"GenerationError","status":400}'
    )
    assert events[0][1]["outcome"] == "failed"


def test_generation_error_retry_flag_is_authoritative():
    from backend.memory_engine import classify_memory_failure
    from backend.memory_providers import GenerationError

    retryable, detail = classify_memory_failure(GenerationError(
        retryable=True, category="provider_overloaded", api_error_status=400))
    assert retryable is True
    assert detail == {
        "category": "provider_overloaded",
        "exception_class": "GenerationError",
        "status": 400,
    }


def test_worker_treats_unknown_job_kind_as_terminal(tmp_path, monkeypatch):
    calls, job, events = _run_worker_job(
        tmp_path, monkeypatch, [], kind="private-unknown-kind")

    assert calls == 0
    assert job["attempts"] == 1
    assert job["status"] == "failed"
    assert job["last_error"] == (
        '{"category":"unknown_job","exception_class":"UnknownMemoryJobError"}'
    )
    assert events[0][1]["kind"] == "unknown"
    assert events[0][1]["outcome"] == "failed"


def test_reindex_all_queues_one_batch_job(tmp_path, monkeypatch):
    from backend.memory_engine import MemoryEngine
    from backend.memory_store import MemoryStore

    instance = MemoryEngine(MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config()
    monkeypatch.setattr(instance, "config", lambda: cfg)
    for index in range(5):
        instance.store.create_memory(
            "default", "fact", f"批量索引 {index}")

    assert _run(instance.reindex_all()) == 5
    jobs = instance.store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "reindex_memories"
    assert len(jobs[0]["payload"]["memory_ids"]) == 5
