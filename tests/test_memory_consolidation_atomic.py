"""Online consolidation prepares every verifier result before a single commit."""
import asyncio
import sqlite3

import pytest

from backend import memory_engine as module
from backend.memory_config import MemoryConfig
from backend.memory_engine import MemoryEngine
from backend.memory_store import MemoryStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "registry.sqlite3")
    engine = MemoryEngine(store)
    cfg = MemoryConfig.model_validate({
        "mode": "active", "generation_model": "provider:test-model",
        "embedding": {"base_url": "http://unused/v1", "model": "fake"},
        "vector": {"url": "http://unused:6333"},
        "consolidation": {
            "dreamer_enabled": True, "verifier_enabled": True,
            "min_reflection_episodes": 20,
        },
    })
    monkeypatch.setattr(engine, "config", lambda: cfg)
    evidence_id = store.add_evidence(
        "default", "session", "user", "用户要求报告先核对数字，并且部署必须使用蓝绿发布。")
    episode = store.get_or_create_episode("default", "session", idle_seconds=60)
    store.attach_evidence(episode["id"], [evidence_id])
    store.update_episode(episode["id"], status="closed", outcome="success")
    candidates = [{
        "kind": "preference", "content": content, "source_ids": [evidence_id],
        "confidence": 0.95, "future_use": 0.95, "attributed_to": "user",
    } for content in ("用户要求报告先核对数字", "部署必须使用蓝绿发布")]
    return engine, cfg, episode["id"], candidates


def _snapshot(store):
    with sqlite3.connect(store.path) as conn:
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in ("episodes", "evidence", "episode_evidence", "memories",
                              "memory_sources", "memory_fts", "jobs", "audit")}


def _mock_provider(monkeypatch, setup, *, fail_at=None, reject=False):
    engine, _, _, candidates = setup
    before = _snapshot(engine.store)
    calls = []

    async def complete(_self, _system, prompt, *, validator=None):
        # This assertion also detects episode/audit mutations before the *last*
        # verifier, rather than only checking an eventual rollback afterwards.
        assert _snapshot(engine.store) == before
        assert not engine._wake.is_set()
        if "possibly_related_existing_memories" not in prompt:
            return {
                "episode": {"title": "已整理", "summary": "偏好已整理",
                            "outcome": "success", "entities": [], "attributes": {}},
                "memories": candidates,
            }
        candidate = candidates[len(calls)]
        calls.append(candidate)
        if len(calls) == fail_at:
            raise ConnectionError("late verifier unavailable")
        content = " ".join(candidate["content"].split())
        return {
            "decision": "reject" if reject else "accept",
            "supported": not reject, "conflict": False, "self_contained": True,
            "specific": True, "durable": True, "generic": False,
            "rewrite_required": False, "final_content": content,
            "supported_claims": [{
                "claim": content, "source_ids": candidate["source_ids"],
                "evidence_type": "direct", "runtime_status": "not_applicable",
            }],
            "unsupported_claims": [], "removed_claims": [], "prediction_value": 0.95,
        }

    monkeypatch.setattr(module.GenerationProvider, "complete_json", complete)
    return before, calls


def _consolidate(setup):
    engine, _, episode_id, _ = setup

    async def run():
        try:
            await engine._consolidate_episode(episode_id)
        finally:
            # stop() wakes workers itself; retain the handler's event for assertions.
            consolidation_wake = engine._wake
            engine._wake = asyncio.Event()
            try:
                await engine.stop()
            finally:
                engine._wake = consolidation_wake

    asyncio.run(run())


def test_late_verifier_failure_leaves_zero_mutations(setup, monkeypatch):
    engine, _, _, _ = setup
    before, calls = _mock_provider(monkeypatch, setup, fail_at=2)
    with pytest.raises(ConnectionError, match="late verifier"):
        _consolidate(setup)
    assert len(calls) == 2
    assert _snapshot(engine.store) == before
    assert not engine._wake.is_set()


@pytest.mark.parametrize("helper", ["_insert_memory", "_insert_job"])
def test_transaction_failure_rolls_back_entire_batch(setup, monkeypatch, helper):
    engine, _, _, _ = setup
    before, calls = _mock_provider(monkeypatch, setup)
    original = getattr(engine.store, helper)
    inserted = []

    def fail_after_second_insert(*args, **kwargs):
        result = original(*args, **kwargs)
        inserted.append(result)
        if len(inserted) == 2:
            raise sqlite3.OperationalError("injected transaction failure")
        return result

    monkeypatch.setattr(engine.store, helper, fail_after_second_insert)
    with pytest.raises(sqlite3.OperationalError, match="injected transaction failure"):
        _consolidate(setup)
    assert len(calls) == len(inserted) == 2
    assert _snapshot(engine.store) == before
    assert not engine._wake.is_set()


@pytest.mark.parametrize("mode", ["active", "shadow"])
def test_success_commits_summary_sources_fts_and_only_active_index_jobs(
        setup, monkeypatch, mode):
    engine, cfg, episode_id, _ = setup
    cfg.mode = mode
    _mock_provider(monkeypatch, setup)
    _consolidate(setup)
    rows = engine.store.list_memories("default")
    assert len(rows) == 2
    assert {row["status"] for row in rows} == {
        "active" if mode == "active" else "pending_review"}
    for row in rows:
        sources = engine.store.memory(row["id"])["sources"]
        assert {source["source_type"] for source in sources} == {"episode", "evidence"}
    after = _snapshot(engine.store)
    assert len(after["memory_fts"]) == len(after["audit"]) == 2
    jobs = engine.store.list_jobs()
    assert len(jobs) == (2 if mode == "active" else 0)
    assert {job["payload"]["memory_id"] for job in jobs} == (
        {row["id"] for row in rows} if mode == "active" else set())
    assert engine.store.episode(episode_id)["summary"] == "偏好已整理"
    assert engine._wake.is_set() == (mode == "active")


@pytest.mark.parametrize("reject", [False, True])
def test_zero_accepted_facts_still_commits_summary(setup, monkeypatch, reject):
    engine, _, episode_id, candidates = setup
    if not reject:
        candidates.clear()
    _mock_provider(monkeypatch, setup, reject=reject)
    _consolidate(setup)
    assert engine.store.episode(episode_id)["summary"] == "偏好已整理"
    assert not engine.store.list_memories("default")
    assert not engine.store.list_jobs()
    assert not engine._wake.is_set()


def test_existing_duplicates_are_not_reinserted_or_modified(setup, monkeypatch):
    engine, _, episode_id, candidates = setup
    for candidate in candidates:
        engine.store.create_memory("default", candidate["kind"], candidate["content"])
    before, calls = _mock_provider(monkeypatch, setup)
    _consolidate(setup)
    assert len(calls) == 2
    after = _snapshot(engine.store)
    for table in before.keys() - {"episodes"}:
        assert after[table] == before[table]
    assert engine.store.episode(episode_id)["summary"] == "偏好已整理"
    assert not engine._wake.is_set()


def test_batch_normalized_duplicate_is_verified_but_inserted_once(setup, monkeypatch):
    engine, _, _, candidates = setup
    candidates[:] = [dict(candidates[0], content="Reports must verify all figures"),
                     dict(candidates[0], content="  Reports  must verify all figures  ")]
    _, calls = _mock_provider(monkeypatch, setup)
    _consolidate(setup)
    assert len(calls) == 2
    assert len(engine.store.list_memories("default")) == 1
    assert len(engine.store.list_jobs()) == 1


def test_reflection_reads_and_wakes_only_after_commit(setup, monkeypatch):
    engine, cfg, episode_id, candidates = setup
    cfg.consolidation.min_reflection_episodes = 2
    previous = engine.store.get_or_create_episode(
        "default", "previous-session", idle_seconds=60)
    engine.store.update_episode(previous["id"], status="closed", summary="前次整理")
    candidates.clear()
    _mock_provider(monkeypatch, setup)
    original = engine.store.list_episodes
    observed = []

    def list_after_commit(*args, **kwargs):
        observed.append(engine.store.episode(episode_id)["summary"])
        assert observed[-1] == "偏好已整理"
        return original(*args, **kwargs)

    monkeypatch.setattr(engine.store, "list_episodes", list_after_commit)
    _consolidate(setup)
    assert observed == ["偏好已整理"]
    assert [job["kind"] for job in engine.store.list_jobs()] == ["cross_episode_dream"]
    assert engine._wake.is_set()
