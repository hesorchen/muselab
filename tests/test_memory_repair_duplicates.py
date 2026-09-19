"""Commit-time exact duplicate guards; run on train-A, not production."""
import copy
import sqlite3

import pytest

from tests import test_memory_repair as helpers
current_backend_modules = helpers.current_backend_modules
target = helpers.target


def other_target(store, owner="owner"):
    episode = store.get_or_create_episode(owner, "other-session", idle_seconds=60)["id"]
    evidence = store.add_evidence(owner, "other-session", "user", "Use blue widgets for this project.")
    store.attach_evidence(episode, [evidence])
    store.update_episode(episode, status="closed")
    job = store.enqueue("consolidate_episode", {"episode_id": episode}, owner_id=owner)
    store.finish_job(job, error="historical failure")
    return store, episode, evidence, job


def assert_rejected_without_writes(store, prepared, monkeypatch):
    before = helpers.counts(store)
    episode_before = store.episode(prepared["episode_id"])
    statements = []
    original = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", tracked_connect)
        with pytest.raises(helpers.RepairConflict, match="duplicate existing"):
            store.apply_memory_repair(prepared)
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
                   for sql in statements)
    assert helpers.counts(store) == before
    assert store.episode(prepared["episode_id"]) == episode_before


def test_two_prepared_episodes_cannot_commit_same_fact(target, monkeypatch):
    store, _, _, _ = target
    second = other_target(store)
    prepared_a, prepared_b = helpers.freeze(target), helpers.freeze(second)
    receipt = store.apply_memory_repair(prepared_a)
    assert helpers.fingerprint(store.repair_snapshot("owner", second[1])) == prepared_b["source_fingerprint"]
    assert_rejected_without_writes(store, prepared_b, monkeypatch)
    assert store.apply_memory_repair(prepared_a) == receipt


@pytest.mark.parametrize("status", ["active", "pending_review", "deleted"])
@pytest.mark.parametrize("unicode_content", [False, True])
def test_fact_inserted_after_stage_in_other_episode_blocks_apply(target, monkeypatch, status, unicode_content):
    store, _, _, _ = target
    second = other_target(store)
    prepared = helpers.freeze(target)
    content = "  USE\nblue\twidgets for this PROJECT.  "
    if unicode_content:
        prepared["result"]["memories"][0]["content"] = "Straße café"
        helpers.reseal(prepared)
        content = "  STRASSE CAFÉ\n"
    existing = store.create_memory("owner", "fact", content, status=status,
                                   sources=[{"source_type": "episode", "source_id": second[1]}])
    assert helpers.fingerprint(store.repair_snapshot("owner", target[1])) == prepared["source_fingerprint"]
    assert_rejected_without_writes(store, prepared, monkeypatch)
    assert store.memory(existing["id"]) == existing


@pytest.mark.parametrize("owner,kind", [("other", "fact"), ("owner", "preference")])
def test_other_owner_or_kind_does_not_block_exact_content(target, owner, kind):
    store, _, _, _ = target
    second = other_target(store, owner)
    prepared = helpers.freeze(target)
    existing = store.create_memory(owner, kind, prepared["result"]["memories"][0]["content"],
                                   sources=[{"source_type": "episode", "source_id": second[1]}])
    receipt = store.apply_memory_repair(prepared)
    assert len(receipt["memory_ids"]) == 1
    assert store.memory(existing["id"]) == existing


def test_receipt_replay_precedes_new_duplicate_checks(target):
    store, _, _, _ = target
    second = other_target(store)
    prepared = helpers.freeze(target)
    receipt = store.apply_memory_repair(prepared)
    store.create_memory("owner", "fact", prepared["result"]["memories"][0]["content"],
                        status="deleted", sources=[{"source_type": "episode", "source_id": second[1]}])
    before = helpers.counts(store)
    assert helpers.MemoryStore(store.path).apply_memory_repair(prepared) == receipt
    assert helpers.counts(store) == before


def test_batch_unicode_duplicate_guard_is_preserved(target):
    store, episode, _, job = target
    prepared = helpers.freeze(target)
    first = prepared["result"]["memories"][0]
    first["content"] = "Straße café"
    duplicate = copy.deepcopy(first)
    duplicate["content"] = " STRASSE CAFÉ\n"
    prepared["result"]["memories"].append(duplicate)
    before = helpers.counts(store)
    with pytest.raises(helpers.RepairConflict, match="duplicate prepared"):
        helpers.stage_repair(store.repair_snapshot("owner", episode), mode="full", job_ids=[job],
                             result=prepared["result"])
    with pytest.raises(helpers.RepairConflict, match="duplicate prepared"):
        store.apply_memory_repair(helpers.reseal(prepared))
    assert helpers.counts(store) == before
    assert not store.episode(episode)["summary"]
