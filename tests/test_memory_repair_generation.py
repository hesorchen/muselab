"""Synthetic generation only: real Dreamer gate, verifier, and read-only registry."""
import asyncio
import copy
import json
import stat
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from backend import memory_engine as module
    from backend.memory_config import MemoryConfig
    from backend.memory_store import MemoryStore

    store = MemoryStore(tmp_path / "source.sqlite")
    episode = store.get_or_create_episode("owner", "session", idle_seconds=60)["id"]
    evidence = store.add_evidence("owner", "session", "user", "Project widgets use blue paint.")
    store.attach_evidence(episode, [evidence])
    store.update_episode(episode, status="closed", outcome="success")
    job = store.enqueue("consolidate_episode", {"episode_id": episode}, owner_id="owner")
    store.finish_job(job, error="historical failure")
    engine = module.MemoryEngine(MemoryStore(store.path, read_only=True))
    cfg = MemoryConfig(owner_id="owner", generation_model="synthetic")
    monkeypatch.setattr(engine, "_config_async", AsyncMock(return_value=cfg))
    candidate = {"kind": "fact", "content": "Project widgets use blue paint.",
                 "source_ids": [evidence], "confidence": 0.9, "future_use": 0.9}
    dreamed = {"episode": {"title": "Widgets", "summary": "Project widgets use blue paint."},
               "memories": [candidate]}
    calls = []

    async def complete(provider, system, prompt, *, validator=None):
        calls.append(system)
        if validator:
            return validator(copy.deepcopy(dreamed))
        value = json.loads(prompt)
        content = value["candidate"]["content"]
        return {"decision": "accept", "supported": True, "conflict": False,
                "self_contained": True, "specific": True, "durable": True,
                "generic": False, "rewrite_required": False, "final_content": content,
                "supported_claims": [{"claim": content, "source_ids": [evidence],
                                      "evidence_type": "direct", "runtime_status": "verified"}],
                "unsupported_claims": [], "removed_claims": [], "prediction_value": 0.9}

    monkeypatch.setattr(module.GenerationProvider, "complete_json", complete)
    return store, engine, episode, evidence, job, dreamed, calls, complete


def dump(store):
    with store._connect() as conn:
        return list(conn.iterdump())


def prepare(setup, mode="full", source=None):
    store, engine, episode, _, job, *_ = setup

    async def run():
        try:
            return await engine.prepare_episode_repair(
                source if source is not None else store.repair_snapshot("owner", episode),
                mode=mode, job_ids=[job])
        finally:
            await engine.stop()

    return asyncio.run(run())


def test_full_prepares_verified_kwargs_without_any_business_writes(setup):
    from backend.memory_repair import stage_repair

    store, _, episode, _, job, _, calls, _ = setup
    before = dump(store)
    generated = prepare(setup)
    assert dump(store) == before
    assert len(calls) == 2
    frozen = stage_repair(store.repair_snapshot("owner", episode), mode="full",
                          job_ids=[job], result=generated["result"])
    memory = frozen["result"]["memories"][0]
    assert memory["authority"] == "inferred"
    assert memory["attributes"]["verification"]["supported"] is True
    assert "prediction_signals" in memory["attributes"]["verification"]
    assert generated["source_fingerprint"] == frozen["source_fingerprint"]


def test_summary_only_preserves_existing_metadata_and_never_verifies(setup):
    store, _, episode, evidence, *_ = setup
    store.update_episode(episode, title="Reviewed title", outcome="failure",
                         attributes_json={"reviewed": True}, extractor_version="old")
    store.create_memory("owner", "fact", "Reviewed memory", status="deleted",
                        sources=[{"source_type": "evidence", "source_id": evidence}])
    before = dump(store)
    generated = prepare(setup, "summary-only")
    assert generated["result"] == {"episode": {
        "title": "Reviewed title", "summary": "Project widgets use blue paint."}, "memories": []}
    assert len(setup[6]) == 1
    assert dump(store) == before


def test_invalid_last_dreamer_candidate_rejects_whole_batch(setup):
    setup[5]["memories"].append({**setup[5]["memories"][0], "source_ids": ["foreign"]})
    before = dump(setup[0])
    with pytest.raises(ValueError, match="invalid_schema"):
        prepare(setup)
    assert len(setup[6]) == 1
    assert dump(setup[0]) == before


def test_late_verifier_failure_has_zero_writes(setup, monkeypatch):
    from backend.memory_engine import GenerationProvider

    original = setup[7]
    setup[5]["memories"].append({**setup[5]["memories"][0],
                                 "content": "A different complete synthetic fact."})

    async def complete(*args, **kwargs):
        if len(setup[6]) == 2:
            raise TimeoutError("synthetic")
        return await original(*args, **kwargs)

    monkeypatch.setattr(GenerationProvider, "complete_json", complete)
    before = dump(setup[0])
    with pytest.raises(TimeoutError):
        prepare(setup)
    assert dump(setup[0]) == before


def test_verifier_rejection_keeps_summary_but_no_fact(setup, monkeypatch):
    from backend.memory_engine import GenerationProvider

    original = setup[7]

    async def complete(*args, **kwargs):
        result = await original(*args, **kwargs)
        if not kwargs.get("validator"):
            result["decision"] = "reject"
        return result

    monkeypatch.setattr(GenerationProvider, "complete_json", complete)
    before = dump(setup[0])
    assert prepare(setup)["result"]["memories"] == []
    assert dump(setup[0]) == before


@pytest.mark.parametrize("status", ["active", "pending_review", "deleted"])
def test_existing_other_episode_duplicate_never_reinserted_or_relinked(setup, status):
    store = setup[0]
    old = store.create_memory("owner", "fact", setup[5]["memories"][0]["content"],
                              status=status, sources=[{"source_type": "episode", "source_id": "other"}])
    before = dump(store)
    generated = prepare(setup)
    assert generated["result"]["memories"] == []
    assert generated["duplicates"] == [{"memory_id": old["id"], "source_link_review_required": True}]
    assert dump(store) == before


def test_batch_duplicates_are_not_inserted_twice(setup):
    setup[5]["memories"] *= 2
    before = dump(setup[0])
    assert len(prepare(setup)["result"]["memories"]) == 1
    assert dump(setup[0]) == before


def test_snapshot_changed_before_generation_makes_no_provider_calls(setup):
    from backend.memory_repair import RepairConflict

    store, _, episode, *_ = setup
    source = store.repair_snapshot("owner", episode)
    store.update_episode(episode, outcome="failure")
    with pytest.raises(RepairConflict, match="snapshot changed"):
        prepare(setup, source=source)
    assert not setup[6]


def test_snapshot_changed_during_generation_rejects_preparation(setup, monkeypatch):
    from backend.memory_engine import GenerationProvider
    from backend.memory_repair import RepairConflict

    original = setup[7]

    async def complete(*args, **kwargs):
        result = await original(*args, **kwargs)
        if kwargs.get("validator"):
            setup[0].update_episode(setup[2], outcome="failure")
        return result

    monkeypatch.setattr(GenerationProvider, "complete_json", complete)
    with pytest.raises(RepairConflict, match="snapshot changed"):
        prepare(setup)
    assert setup[0].episode(setup[2])["summary"] == ""
    assert setup[0].repair_snapshot("owner", setup[2])["memories"] == []


@pytest.mark.parametrize("invalid", ["mode", "owner", "writable"])
def test_generation_fences_before_provider_calls(setup, invalid):
    from backend.memory_repair import RepairConflict

    if invalid == "owner":
        setup[1]._config_async.return_value.owner_id = "other"
    if invalid == "writable":
        setup[1]._store = setup[0]
    with pytest.raises(RepairConflict):
        prepare(setup, mode="implicit" if invalid == "mode" else "full")
    assert not setup[6]


def test_old_verify_interface_returns_existing_without_insert(setup):
    store, engine, episode, evidence, _, dreamed, *_ = setup
    old = store.create_memory("owner", "fact", dreamed["memories"][0]["content"],
                              sources=[{"source_type": "episode", "source_id": "other"}])
    before = dump(store)

    async def run():
        try:
            return await engine._verify_and_store(dreamed["memories"][0], episode, [evidence])
        finally:
            await engine.stop()

    assert asyncio.run(run())["id"] == old["id"]
    assert dump(store) == before


@pytest.mark.parametrize("mode, expected_status", [("active", "active"), ("shadow", "pending_review")])
def test_old_verify_interface_still_creates_and_indexes_by_status(setup, mode, expected_status):
    store, engine, episode, evidence, _, dreamed, *_ = setup
    engine._store = store
    engine._config_async.return_value.mode = mode
    jobs_before = len(store.list_jobs())

    async def run():
        try:
            return await engine._verify_and_store(dreamed["memories"][0], episode, [evidence])
        finally:
            await engine.stop()

    memory = asyncio.run(run())
    assert memory["status"] == expected_status
    assert store.memory(memory["id"])["content"] == dreamed["memories"][0]["content"]
    assert len(store.list_jobs()) == jobs_before + (expected_status == "active")


def test_atomic_private_json_never_overwrites_or_leaves_partial_output(tmp_path):
    from scripts.repair_memory_once import save_new

    output = tmp_path / "frozen.json"
    with pytest.raises(ValueError):
        save_new(output, {"bad": float("nan")})
    assert not output.exists()
    save_new(output, {"safe": "完整文本"})
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text()) == {"safe": "完整文本"}
    with pytest.raises(FileExistsError):
        save_new(output, {"replacement": True})
    assert json.loads(output.read_text()) == {"safe": "完整文本"}
    assert list(tmp_path.iterdir()) == [output]


def test_cli_generate_failure_leaves_no_output(setup, tmp_path, monkeypatch, capsys):
    from scripts import repair_memory_once as cli

    store, engine, episode, _, job, *_ = setup
    monkeypatch.setattr(type(engine), "_config_async", engine._config_async)
    setup[5]["memories"].append({"invalid": "private-value"})
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"owner_id": "owner", "items": [
        {"episode_id": episode, "mode": "full", "job_ids": [job]}]}))
    output = tmp_path / "frozen.json"
    before = dump(store)
    args = ["--db", str(store.path), "--manifest", str(manifest)]
    assert cli.main(args) == 0
    assert not setup[6]  # Default dry-run has no generation calls.
    assert cli.main([*args, "--action", "generate", "--output", str(output)]) == 2
    assert not output.exists()
    assert dump(store) == before
    assert "private-value" not in capsys.readouterr().out


def test_cli_generate_freezes_source_bound_result_without_writes(setup, tmp_path, monkeypatch, capsys):
    from scripts import repair_memory_once as cli

    store, engine, episode, _, job, *_ = setup
    monkeypatch.setattr(type(engine), "_config_async", engine._config_async)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"owner_id": "owner", "items": [
        {"episode_id": episode, "mode": "full", "job_ids": [job]}]}))
    output = tmp_path / "frozen.json"
    before = dump(store)
    assert cli.main(["--db", str(store.path), "--manifest", str(manifest),
                     "--action", "generate", "--output", str(output)]) == 0
    bundle = json.loads(output.read_text())
    assert len(bundle["items"][0]["result"]["memories"]) == 1
    assert bundle["items"][0]["source_fingerprint"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert dump(store) == before
    assert "blue paint" not in capsys.readouterr().out
