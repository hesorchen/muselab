"""Run on train-A: pytest -q tests/test_memory_repair.py."""
import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.memory_repair import RepairConflict, fingerprint, stage_repair
from backend.memory_store import MemoryStore


@pytest.fixture(autouse=True)
def current_backend_modules(monkeypatch):
    # app_module removes backend modules between API tests; resolve the same
    # generation used by MemoryStore's deferred repair imports, not collection.
    import sys
    from backend import memory_repair, memory_store

    module = sys.modules[__name__]
    for name in ("RepairConflict", "fingerprint", "stage_repair"):
        monkeypatch.setattr(module, name, getattr(memory_repair, name), raising=False)
    monkeypatch.setattr(module, "MemoryStore", memory_store.MemoryStore, raising=False)


@pytest.fixture
def target(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    episode = store.get_or_create_episode("owner", "session", idle_seconds=60)["id"]
    evidence = store.add_evidence("owner", "session", "user", "Use blue widgets for this project.")
    store.attach_evidence(episode, [evidence])
    store.update_episode(episode, status="closed")
    job = store.enqueue("consolidate_episode", {"episode_id": episode}, owner_id="owner")
    store.finish_job(job, error="historical failure")
    return store, episode, evidence, job


def freeze(target, mode="full", memories=True):
    store, episode, evidence, job = target
    memory = {"kind": "fact", "content": "Use blue widgets for this project.",
              "status": "active", "authority": "inferred", "confidence": 0.9,
              "attributes": {"verification": {"supported": True}},
              "sources": [{"source_type": "episode", "source_id": episode,
                           "relation": "derived_from"},
                          {"source_type": "evidence", "source_id": evidence,
                           "relation": "supports"}]}
    return stage_repair(store.repair_snapshot("owner", episode), mode=mode,
                        job_ids=[job], result={"episode": {"title": "Widgets", "summary": "Blue widgets."},
                                              "memories": [memory] if memories else []})


def counts(store):
    with store._connect() as conn:
        return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("memories", "memory_sources", "memory_fts", "jobs", "memory_repair_receipts")}


def test_full_atomic_receipt_and_restart(target):
    store, episode, _, job = target
    before = store.list_jobs()
    prepared = freeze(target)
    receipt = store.apply_memory_repair(prepared)
    assert counts(store) == {"memories": 1, "memory_sources": 2, "memory_fts": 1,
                             "jobs": 2, "memory_repair_receipts": 1}
    assert store.episode(episode)["summary"] == "Blue widgets."
    assert next(j for j in store.list_jobs() if j["id"] == job) == before[0]
    restarted = MemoryStore(store.path)
    assert restarted.apply_memory_repair(prepared) == receipt
    assert len(receipt["index_job_ids"]) == 1
    assert counts(store)["memories"] == 1


def test_summary_only_preserves_review_and_sources(target):
    store, episode, evidence, _ = target
    memory = store.create_memory("owner", "fact", "Human reviewed text", authority="confirmed",
                                 status="deleted", sources=[{"source_type": "evidence", "source_id": evidence}])
    before = store.memory(memory["id"])
    counts_before = counts(store)
    prepared = freeze(target, mode="summary-only", memories=False)
    store.apply_memory_repair(prepared)
    assert store.memory(memory["id"]) == before
    assert counts(store) == {**counts_before, "memory_repair_receipts": 1}
    assert store.episode(episode)["summary"]


def test_partial_products_refused_in_all_statuses(target):
    store, episode, _, _ = target
    store.create_memory("owner", "fact", "Old partial write", status="deleted",
                        sources=[{"source_type": "episode", "source_id": episode}])
    with pytest.raises(RepairConflict, match="partial existing"):
        freeze(target)


@pytest.mark.parametrize("change", ["evidence", "owner", "job", "episode", "memory"])
def test_apply_checks_current_state(target, change):
    store, episode, evidence, _ = target
    prepared = freeze(target)
    with store._write_tx() as conn:
        if change == "evidence":
            conn.execute("UPDATE evidence SET content='changed' WHERE id=?", (evidence,))
        elif change == "owner":
            conn.execute("UPDATE episodes SET owner_id='other' WHERE id=?", (episode,))
        elif change == "episode":
            conn.execute("UPDATE episodes SET summary='Human edit' WHERE id=?", (episode,))
    if change == "job":
        store.enqueue("consolidate_episode", {"episode_id": episode}, owner_id="owner")
    if change == "memory":
        store.create_memory("owner", "fact", "New partial product",
                            sources=[{"source_type": "episode", "source_id": episode}])
    before = counts(store)
    with pytest.raises(RepairConflict):
        store.apply_memory_repair(prepared)
    assert counts(store) == before


def test_foreign_evidence_blocked(target):
    store, episode, _, _ = target
    foreign = store.add_evidence("other", "session", "user", "Foreign content")
    store.attach_evidence(episode, [foreign])
    with pytest.raises(RepairConflict, match="foreign_evidence"):
        freeze(target)


def test_failure_after_inserts_rolls_back_and_retry(target, monkeypatch):
    store, episode, _, _ = target
    prepared = freeze(target)
    before = counts(store)
    original = store._insert_job

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated interruption")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_insert_job", fail)
        with pytest.raises(RuntimeError, match="interruption"):
            store.apply_memory_repair(prepared)
    assert counts(store) == before
    assert not store.episode(episode)["summary"]
    MemoryStore(store.path).apply_memory_repair(prepared)
    assert counts(store)["memory_repair_receipts"] == 1


def test_receipt_failure_rolls_back_episode_and_all_products(target):
    import sqlite3

    store, episode, _, _ = target
    prepared = freeze(target)
    before = counts(store)
    with store._connect() as conn:
        conn.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON memory_repair_receipts "
                     "BEGIN SELECT RAISE(ABORT, 'receipt failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="receipt failure"):
        store.apply_memory_repair(prepared)
    assert counts(store) == before
    assert not store.episode(episode)["summary"]
    with store._connect() as conn:
        conn.execute("DROP TRIGGER fail_receipt")
    store.apply_memory_repair(prepared)
    assert counts(store)["memories"] == 1


def test_active_cross_episode_job_blocks_apply(target):
    store, episode, _, _ = target
    prepared = freeze(target)
    store.enqueue("cross_episode_dream", {"episode_ids": [episode]}, owner_id="owner")
    with pytest.raises(RepairConflict):
        store.apply_memory_repair(prepared)
    assert counts(store)["memories"] == 0


def test_existing_valid_summary_never_overwritten(target):
    store, episode, _, _ = target
    store.update_episode(episode, summary="Existing reviewed summary")
    with pytest.raises(RepairConflict, match="existing summary"):
        freeze(target, mode="summary-only", memories=False)


def test_concurrent_store_instances_one_receipt(target):
    store, _, _, _ = target
    prepared = freeze(target)
    stores = [MemoryStore(store.path), MemoryStore(store.path)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(lambda s: s.apply_memory_repair(prepared), stores))
    assert receipts[0] == receipts[1]
    assert counts(store)["memories"] == counts(store)["memory_repair_receipts"] == 1


def test_changed_frozen_result_and_alternate_preparation_refused(target):
    store, _, _, _ = target
    prepared = freeze(target)
    changed = copy.deepcopy(prepared)
    changed["result"]["episode"]["summary"] = "Changed"
    with pytest.raises(RepairConflict, match="fingerprint"):
        store.apply_memory_repair(changed)
    store.apply_memory_repair(prepared)
    changed["prepared_fingerprint"] = fingerprint(
        {k: v for k, v in changed.items() if k != "prepared_fingerprint"})
    with pytest.raises(RepairConflict, match="different prepared"):
        store.apply_memory_repair(changed)


def test_zero_facts_is_valid(target):
    store, _, _, _ = target
    store.apply_memory_repair(freeze(target, memories=False))
    assert counts(store)["memories"] == 0
    assert counts(store)["memory_repair_receipts"] == 1


def test_dryrun_and_stage_are_read_only(target, tmp_path, capsys):
    import json
    from scripts.repair_memory_once import main

    store, episode, _, job = target
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"owner_id": "owner", "items": [
        {"episode_id": episode, "job_ids": [job], "mode": "full"}]}))
    before = fingerprint(store.repair_snapshot("owner", episode))
    argv = ["--db", str(store.path), "--manifest", str(manifest)]
    assert main(argv) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["items"][0]["status"] == "eligible"
    results, staged = tmp_path / "results.json", tmp_path / "prepared.json"
    results.write_text(json.dumps({episode: {"source_fingerprint": before,
                                           "result": freeze(target)["result"]}}))
    assert main([*argv, "--action", "stage", "--results", str(results), "--output", str(staged)]) == 0
    assert staged.stat().st_mode & 0o777 == 0o600
    assert fingerprint(store.repair_snapshot("owner", episode)) == before
    assert counts(store)["memory_repair_receipts"] == 0
    results.write_text(json.dumps({episode: {"source_fingerprint": "stale",
                                           "result": freeze(target)["result"]}}))
    rejected = tmp_path / "rejected.json"
    assert main([*argv, "--action", "stage", "--results", str(results),
                 "--output", str(rejected)]) == 2
    assert not rejected.exists()


def reseal(prepared):
    prepared["prepared_fingerprint"] = fingerprint(
        {k: v for k, v in prepared.items() if k != "prepared_fingerprint"})
    return prepared


@pytest.mark.parametrize("different_verification", [False, True])
def test_duplicate_normalized_content_refused_without_writes(target, different_verification):
    store, episode, _, job = target
    prepared = freeze(target)
    duplicate = copy.deepcopy(prepared["result"]["memories"][0])
    duplicate["content"] = "  USE\n blue\twidgets for this PROJECT.  "
    if different_verification:
        duplicate["attributes"]["verification"] = {"supported": True, "reason": "another check"}
    prepared["result"]["memories"].append(duplicate)
    before, episode_before = counts(store), store.episode(episode)
    with pytest.raises(RepairConflict, match="duplicate prepared"):
        stage_repair(store.repair_snapshot("owner", episode), mode="full", job_ids=[job],
                     result=prepared["result"])
    with pytest.raises(RepairConflict, match="duplicate prepared"):
        store.apply_memory_repair(reseal(prepared))
    assert counts(store) == before
    assert store.episode(episode) == episode_before


def test_same_content_different_kind_not_duplicate(target):
    store, _, _, _ = target
    prepared = freeze(target)
    other = copy.deepcopy(prepared["result"]["memories"][0])
    other["kind"] = "preference"
    prepared["result"]["memories"].append(other)
    store.apply_memory_repair(reseal(prepared))
    assert counts(store)["memories"] == 2


def test_summary_only_preserves_nondefault_episode_metadata(target):
    store, episode, _, _ = target
    store.update_episode(episode, title="Reviewed title", outcome="failure",
                         entities_json='[ "human entity" ]',
                         attributes_json='{ "human": true, "nested": {"a": 1} }',
                         extractor_version="human-review-v1")
    before = store.repair_snapshot("owner", episode)["episode"]
    prepared = freeze(target, mode="summary-only", memories=False)
    prepared["result"]["episode"].update(
        outcome="success", entities_json=["generated entity"],
        attributes_json={"generated": True}, extractor_version="generated-v2")
    store.apply_memory_repair(reseal(prepared))
    after = store.repair_snapshot("owner", episode)["episode"]
    assert after["summary"] == "Blue widgets."
    for field in before.keys() - {"summary", "updated_at"}:
        assert after[field] == before[field]


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
def test_full_accepts_and_preserves_authoritative_outcome(target, outcome):
    store, episode, _, _ = target
    store.update_episode(episode, outcome=outcome)
    before = store.repair_snapshot("owner", episode)["episode"]
    prepared = freeze(target)
    prepared["result"]["episode"].update(
        outcome="unknown", entities_json=["widget"], attributes_json={"generated": True})
    store.apply_memory_repair(reseal(prepared))
    after = store.repair_snapshot("owner", episode)["episode"]
    assert after["outcome"] == outcome
    for field in ("status", "started_at", "ended_at", "turn_count", "primary_session_id"):
        assert after[field] == before[field]
    assert after["entities_json"] == '["widget"]'
    assert after["attributes_json"] == '{"generated":true}'


@pytest.mark.parametrize("metadata", [{"entities_json": ["reviewed"]},
                                      {"attributes_json": {"human": True}}])
def test_full_nonempty_metadata_requires_review(target, metadata):
    store, episode, _, _ = target
    store.update_episode(episode, **metadata)
    before = counts(store)
    with pytest.raises(RepairConflict, match="partial existing"):
        freeze(target)
    assert counts(store) == before


def schema_state(store):
    with store._connect() as conn:
        return (conn.execute("PRAGMA user_version").fetchone()[0],
                [tuple(row) for row in conn.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name")],
                [tuple(row) for row in conn.execute("SELECT * FROM memory_migrations ORDER BY name")])


def test_existing_repair_opener_and_apply_never_initialize(target, monkeypatch):
    import sqlite3

    store, _, _, _ = target
    prepared, before = freeze(target), schema_state(store)
    statements = []
    original = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    def forbidden(*args, **kwargs):
        raise AssertionError("repair must not initialize or migrate")

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    for method in ("_init", "_migrate_fts", "_migrate_recall_stats", "_migrate_repair_indexes",
                   "_migrate_repair_covering_indexes"):
        monkeypatch.setattr(MemoryStore, method, forbidden)
    reopened = MemoryStore.open_existing_for_repair(store.path)
    assert schema_state(reopened) == before
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
                   for sql in statements)
    receipt = reopened.apply_memory_repair(prepared)
    assert reopened.apply_memory_repair(prepared) == receipt
    assert schema_state(reopened) == before
    assert not any(sql.lstrip().upper().startswith(("CREATE", "ALTER", "DROP")) for sql in statements)
    assert counts(reopened)["memory_repair_receipts"] == 1


@pytest.mark.parametrize("damage", [
    "DROP INDEX idx_memory_sources_source", "DROP INDEX idx_jobs_episode",
    "DROP INDEX idx_artifacts_source_episodes_id", "DROP INDEX idx_memories_owner_kind_content",
    "DELETE FROM memory_migrations WHERE name='memory-repair-covering-indexes-v1'",
    "DROP TABLE memory_repair_receipts", "ALTER TABLE jobs RENAME COLUMN operation_key TO old_key",
    "DELETE FROM memory_migrations WHERE name='memory-recall-stats-v1'",
    "DELETE FROM memory_migrations WHERE name='memory-repair-indexes-v1'",
    "PRAGMA user_version=0",
])
def test_missing_repair_prerequisite_refused_without_migration(target, damage):
    store, episode, _, _ = target
    prepared = freeze(target)
    with store._connect() as conn:
        conn.execute(damage)
    before, episode_before = schema_state(store), store.episode(episode)
    with pytest.raises(RepairConflict, match="missing"):
        MemoryStore.open_existing_for_repair(store.path)
    with pytest.raises(RepairConflict, match="missing"):
        store.apply_memory_repair(prepared)
    assert schema_state(store) == before
    assert store.episode(episode) == episode_before
    with store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_existing_repair_opener_does_not_create_missing_db(tmp_path):
    import sqlite3

    path = tmp_path / "absent.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        MemoryStore.open_existing_for_repair(path)
    assert not path.exists()


@pytest.mark.parametrize("phase", ["read", "after_insert", "before_commit"])
def test_repair_deadline_rolls_back_everything(target, monkeypatch, phase):
    from backend import memory_repair, memory_store

    store, episode, _, _ = target
    prepared = freeze(target)
    before, episode_before, schema_before = counts(store), store.episode(episode), schema_state(store)
    clock = [100.0]
    monkeypatch.setattr(memory_store.time, "perf_counter", lambda: clock[0])

    def expire(conn):
        clock[0] = 101.0
        # Force SQLite to invoke the progress handler, independent of DB size.
        conn.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000) "
                     "SELECT sum(x) FROM n").fetchone()
        pytest.fail("SQL progress handler did not interrupt")

    with monkeypatch.context() as patch:
        if phase == "read":
            patch.setattr(memory_repair, "read_source", lambda conn, *args: expire(conn))
        elif phase == "after_insert":
            original = store._insert_memory

            def slow_insert(conn, *args, **kwargs):
                original(conn, *args, **kwargs)
                expire(conn)

            patch.setattr(store, "_insert_memory", slow_insert)
        else:
            original = memory_repair.apply_prepared

            def slow_python(*args):
                result = original(*args)
                clock[0] = 101.0
                return result

            patch.setattr(memory_repair, "apply_prepared", slow_python)
        with pytest.raises(TimeoutError, match="deadline"):
            store.apply_memory_repair(prepared)
    assert counts(store) == before
    assert store.episode(episode) == episode_before
    assert schema_state(store) == schema_before
    # A new item transaction gets a new budget; the expired handler is not reused.
    store.apply_memory_repair(prepared)
    assert counts(store)["memory_repair_receipts"] == 1


def test_repair_index_migration_is_separate_and_idempotent(target):
    store, _, _, _ = target
    with store._connect() as conn:
        conn.execute("DROP INDEX idx_memory_sources_source")
        conn.execute("DROP INDEX idx_jobs_episode")
        conn.execute("DELETE FROM memory_migrations WHERE name='memory-repair-indexes-v1'")
        statements = []
        conn.set_trace_callback(statements.append)
        store._migrate_repair_indexes(conn)
        assert sum(sql.startswith("CREATE INDEX") for sql in statements) == 2
        statements.clear()
        store._migrate_repair_indexes(conn)
        assert not any(sql.startswith(("CREATE", "INSERT", "BEGIN")) for sql in statements)
    MemoryStore.open_existing_for_repair(store.path)


@pytest.mark.parametrize("status", ["active", "pending_review", "superseded", "deleted"])
@pytest.mark.parametrize("orphan_owner", ["owner", "other"])
def test_owner_wide_orphans_match_legacy_fingerprint(target, status, orphan_owner):
    from backend.memory_repair import check_target, read_source

    store, episode, evidence, job = target
    assistant = store.add_evidence("owner", "unrelated", "assistant", "Other evidence")
    foreign = store.add_evidence("other", "unrelated", "user", "Foreign evidence")
    source_cases = [
        ("episode", episode, "derived_from"),
        ("episode", "missing-episode", "supports"),
        ("evidence", evidence, "supports"),
        ("evidence", assistant, "contradicts"),
        ("evidence", foreign, "custom-relation"),
        ("evidence", "missing-evidence", "derived_from"),
        ("custom-type", "missing-source", "custom-relation"),
    ]
    for source_type, source_id, relation in source_cases:
        source = {"source_type": source_type, "source_id": source_id, "relation": relation}
        # Repeated memory_id values on the RHS, including different relations;
        # even dangling or nonstandard provenance means the row is not orphaned.
        for sources in ([source], [source, source, {**source, "relation": "another-relation"}]):
            store.create_memory("owner", "fact", "Sourced memory", status=status, sources=sources)
    store.create_memory("other", "fact", "Foreign sourced memory", status=status,
                        sources=[{"source_type": "evidence", "source_id": "missing-evidence"}])
    orphan_ids = [store.create_memory(orphan_owner, kind, "Unattributed memory", status=status)["id"]
                  for kind in ("fact", "preference", "decision", "state", "episode")]
    with store._connect() as conn:
        conn.execute("PRAGMA query_only=ON")
        snapshot = read_source(conn, "owner", episode)
        legacy_orphans = [dict(row) for row in conn.execute(
            "SELECT m.id FROM memories m WHERE owner_id=? AND NOT EXISTS "
            "(SELECT 1 FROM memory_sources s WHERE s.memory_id=m.id) ORDER BY m.id", ("owner",))]
        assert read_source(conn, "owner", episode) == snapshot
    expected = [{"id": memory_id} for memory_id in sorted(orphan_ids)] if orphan_owner == "owner" else []
    assert snapshot["orphans"] == legacy_orphans == expected
    assert snapshot["blocked"] == (["unattributed_memories"] if expected else [])
    legacy_snapshot = {**snapshot, "orphans": legacy_orphans}
    assert fingerprint(snapshot) == fingerprint(legacy_snapshot)
    if expected:
        for mode in ("full", "summary-only"):
            with pytest.raises(RepairConflict, match="unattributed_memories"):
                check_target(snapshot, mode, [job])
    else:
        prepared = stage_repair(snapshot, mode="summary-only", job_ids=[job], result={
            "episode": {"title": "Widgets", "summary": "Blue widgets."}, "memories": []})
        assert prepared["source_fingerprint"] == fingerprint(legacy_snapshot)


def test_orphan_query_scans_sources_without_correlated_seeks(target):
    from backend.memory_repair import read_source

    store, episode, _, _ = target
    statements = []
    with store._connect() as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.set_trace_callback(statements.append)
        read_source(conn, "owner", episode)
        conn.set_trace_callback(None)
        orphan_sql = next(sql for sql in statements if sql.startswith(
            ("SELECT m.id FROM memories", "SELECT id FROM memories")))
        plan = " ".join(row[3].upper() for row in conn.execute("EXPLAIN QUERY PLAN " + orphan_sql))
    # Per-memory indexed seeks can exhaust the repair deadline on slow local
    # storage; assert the access pattern rather than a flaky wall-clock budget.
    assert "CORRELATED" not in plan
    assert "SCAN MEMORY_SOURCES" in plan


def test_repair_source_and_job_queries_use_migrated_indexes(target):
    store, episode, _, _ = target
    with store._connect() as conn:
        sources = " ".join(row[3] for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT memory_id FROM memory_sources "
            "WHERE source_type='episode' AND source_id=?", (episode,)))
        jobs = " ".join(row[3] for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM jobs "
            "WHERE json_extract(payload_json,'$.episode_id')=? ORDER BY id", (episode,)))
    assert "idx_memory_sources_source" in sources
    assert "idx_jobs_episode" in jobs


@pytest.mark.parametrize("status", ["pending_review", "active", "rejected", "deleted"])
@pytest.mark.parametrize("owner", ["owner", "other"])
def test_artifact_selection_preserves_all_rows_and_fingerprint(target, status, owner):
    from backend.memory_repair import read_source

    store, episode, _, job = target
    for kind in ("reflection", "skill_candidate", "custom-kind"):
        for episode_ids in ([episode], ["unrelated", episode, episode],
                            [episode, "unrelated"], [], ["unrelated"]):
            store.create_artifact(owner, kind, "Full title", {"body": "x" * 65536},
                                  episode_ids, status=status)
    nullable_id = store.create_artifact(owner, "reflection", "Legacy null ID",
                                        {"body": "y" * 65536}, [episode], status=status)["id"]
    with store._connect() as conn:
        # SQLite permits NULL in this legacy TEXT PRIMARY KEY schema.
        conn.execute("UPDATE artifacts SET id=NULL WHERE id=?", (nullable_id,))
        conn.execute("PRAGMA query_only=ON")
        snapshot = read_source(conn, "owner", episode)
        legacy = [dict(row) for row in conn.execute(
            "SELECT a.* FROM artifacts a WHERE EXISTS "
            "(SELECT 1 FROM json_each(a.source_episode_ids_json) WHERE value=?) ORDER BY id",
            (episode,))]
    assert len(legacy) == 10
    assert snapshot["artifacts"] == legacy
    assert all(len(row["payload_json"]) > 65536 for row in snapshot["artifacts"])
    assert fingerprint(snapshot) == fingerprint({**snapshot, "artifacts": legacy})
    for mode in ("full", "summary-only"):
        with pytest.raises(RepairConflict, match="existing"):
            stage_repair(snapshot, mode=mode, job_ids=[job], result={})


def test_repair_artifact_query_covers_selection_before_hydration(target):
    from backend.memory_repair import read_source

    store, episode, _, _ = target
    statements = []
    with store._connect() as conn:
        conn.set_trace_callback(statements.append)
        read_source(conn, "owner", episode)
        conn.set_trace_callback(None)
        sql = next(sql for sql in statements if sql.startswith("SELECT a.* FROM artifacts"))
        plan = " ".join(row[3].upper() for row in conn.execute("EXPLAIN QUERY PLAN " + sql))
    assert "SCAN ARTIFACTS USING COVERING INDEX IDX_ARTIFACTS_SOURCE_EPISODES_ID" in plan
    assert "SEARCH A USING INTEGER PRIMARY KEY" in plan
    assert "SCAN A " not in plan


def test_commit_duplicate_query_uses_covering_index(target, monkeypatch):
    import sqlite3

    store, _, _, _ = target
    prepared = freeze(target)
    # Exercise multiple kinds as well as the actual commit-time statement.
    other = copy.deepcopy(prepared["result"]["memories"][0])
    other["kind"] = "preference"
    prepared["result"]["memories"].append(other)
    statements = []
    original = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", tracked_connect)
        receipt = store.apply_memory_repair(reseal(prepared))
    sql = next(sql for sql in statements if sql.startswith("SELECT kind,content FROM memories"))
    with store._connect() as conn:
        plan = " ".join(row[3].upper() for row in conn.execute("EXPLAIN QUERY PLAN " + sql))
    assert "USING COVERING INDEX IDX_MEMORIES_OWNER_KIND_CONTENT" in plan
    assert "STATUS" not in sql.upper()
    assert len(receipt["memory_ids"]) == 2
    assert store.apply_memory_repair(prepared) == receipt


def remove_repair_covering_indexes(store):
    with store._connect() as conn:
        conn.execute("DROP INDEX idx_artifacts_source_episodes_id")
        conn.execute("DROP INDEX idx_memories_owner_kind_content")
        conn.execute("DELETE FROM memory_migrations WHERE name='memory-repair-covering-indexes-v1'")


def test_explicit_covering_migration_is_additive_and_idempotent(target, monkeypatch):
    import sqlite3

    store, episode, _, _ = target
    prepared = freeze(target)
    remove_repair_covering_indexes(store)
    before, source_before, counts_before = schema_state(store), store.repair_snapshot("owner", episode), counts(store)
    statements = []
    original = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    def forbidden(*args, **kwargs):
        raise AssertionError("explicit covering migration must not initialize or backfill")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", tracked_connect)
        for method in ("_init", "_migrate_columns", "_migrate_fts",
                       "_migrate_recall_stats", "_migrate_repair_indexes"):
            patch.setattr(MemoryStore, method, forbidden)
        MemoryStore.migrate_existing_repair_indexes(store.path)
        writes = [sql for sql in statements if sql.startswith(("CREATE", "INSERT", "UPDATE", "DELETE"))]
        assert len(writes) == 3
        assert sum(sql.startswith("CREATE INDEX") for sql in writes) == 2
        assert sum(sql.startswith("INSERT INTO memory_migrations") for sql in writes) == 1
        statements.clear()
        MemoryStore.migrate_existing_repair_indexes(store.path)
        assert not any(sql.startswith(("CREATE", "INSERT", "UPDATE", "DELETE", "BEGIN")) for sql in statements)
    after = schema_state(store)
    assert before[0] == after[0]
    assert set(before[1]) < set(after[1]) and len(after[1]) == len(before[1]) + 2
    assert set(before[2]) < set(after[2]) and len(after[2]) == len(before[2]) + 1
    assert counts(store) == counts_before
    assert store.repair_snapshot("owner", episode) == source_before
    assert fingerprint(source_before) == prepared["source_fingerprint"]
    reopened = MemoryStore.open_existing_for_repair(store.path)
    receipt = reopened.apply_memory_repair(prepared)
    assert MemoryStore.open_existing_for_repair(store.path).apply_memory_repair(prepared) == receipt


@pytest.mark.parametrize("statement", ["CREATE INDEX IF NOT EXISTS idx_artifacts_source_episodes_id",
                                        "CREATE INDEX IF NOT EXISTS idx_memories_owner_kind_content",
                                        "INSERT INTO memory_migrations"])
def test_covering_migration_deadline_rolls_back_ddl_and_marker(target, monkeypatch, statement):
    import sqlite3
    from backend import memory_store

    store, _, _, _ = target
    remove_repair_covering_indexes(store)
    before, counts_before = schema_state(store), counts(store)
    clock = [100.0]
    original = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        conn = original(*args, **kwargs)

        def expire(sql):
            if sql.startswith(statement):
                clock[0] = 101.0

        conn.set_trace_callback(expire)
        return conn

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", tracked_connect)
        patch.setattr(memory_store.time, "perf_counter", lambda: clock[0])
        with pytest.raises(TimeoutError, match="migration deadline"):
            MemoryStore.migrate_existing_repair_indexes(store.path, timeout_seconds=0.5)
    assert schema_state(store) == before
    assert counts(store) == counts_before
    MemoryStore.migrate_existing_repair_indexes(store.path)
    MemoryStore.open_existing_for_repair(store.path)


def test_covering_migration_refuses_missing_baseline_without_backfill(target):
    store, _, _, _ = target
    remove_repair_covering_indexes(store)
    with store._connect() as conn:
        conn.execute("DELETE FROM memory_migrations WHERE name='memory-recall-stats-v1'")
    before = schema_state(store)
    with pytest.raises(RepairConflict, match="missing"):
        MemoryStore.migrate_existing_repair_indexes(store.path)
    assert schema_state(store) == before


def test_covering_migration_lock_contention_rolls_back_and_retries(target):
    import sqlite3

    store, _, _, _ = target
    remove_repair_covering_indexes(store)
    before = schema_state(store)
    with store._connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises((sqlite3.OperationalError, TimeoutError)):
            MemoryStore.migrate_existing_repair_indexes(store.path, timeout_seconds=0.02)
        writer.execute("ROLLBACK")
    assert schema_state(store) == before
    MemoryStore.migrate_existing_repair_indexes(store.path)
    MemoryStore.open_existing_for_repair(store.path)


def test_covering_migration_never_creates_missing_db(tmp_path):
    import sqlite3

    path = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        MemoryStore.migrate_existing_repair_indexes(path)
    assert not path.exists()


@pytest.mark.parametrize("timeout", [0, -1, 31, float("nan"), float("inf")])
def test_covering_migration_requires_bounded_timeout(target, timeout):
    store, _, _, _ = target
    before = schema_state(store)
    with pytest.raises(ValueError, match="at most 30 seconds"):
        MemoryStore.migrate_existing_repair_indexes(store.path, timeout_seconds=timeout)
    assert schema_state(store) == before


@pytest.mark.parametrize("owner,kind,payload,blocks", [
    ("other", "consolidate_episode", "scalar", True),
    ("other", "cross_episode_dream", "array", True),
    ("owner", "cross_episode_dream", "unrelated", True),
    ("other", "cross_episode_dream", "unrelated", False),
    ("owner", "consolidate_episode", "unrelated", False),
])
def test_complete_repair_source_plans_and_active_fence(target, owner, kind, payload, blocks):
    from backend.memory_repair import read_source

    store, episode, _, _ = target
    payload = ({"episode_id": episode} if payload == "scalar" else
               {"episode_ids": [episode, episode]} if payload == "array" else
               {"episode_id": "unrelated", "episode_ids": ["unrelated"]})
    active_id = store.enqueue(kind, payload, owner_id=owner)
    statements = []
    with store._connect() as conn:
        conn.set_trace_callback(statements.append)
        source = read_source(conn, "owner", episode)
        conn.set_trace_callback(None)
        plans = {sql: " ".join(row[3].upper() for row in conn.execute("EXPLAIN QUERY PLAN " + sql))
                 for sql in statements if sql.startswith("SELECT")}
    assert [row["id"] for row in source["active_jobs"]] == ([active_id] if blocks else [])
    assert ("active_jobs" in source["blocked"]) == blocks
    active = next(plan for sql, plan in plans.items() if sql.startswith("SELECT id,kind,owner_id"))
    assert "SEARCH JOBS USING INDEX IDX_JOBS_CLAIM" in active
    memories = next(plan for sql, plan in plans.items() if sql.startswith("SELECT m.* FROM memories"))
    assert "SEARCH M USING INDEX SQLITE_AUTOINDEX_MEMORIES" in memories
    assert memories.count("USING COVERING INDEX IDX_MEMORY_SOURCES_SOURCE") == 2
    orphans = next(plan for sql, plan in plans.items() if sql.startswith("SELECT id FROM memories"))
    assert "USING COVERING INDEX IDX_MEMORIES_OWNER_ID" in orphans
    assert "SCAN MEMORY_SOURCES USING COVERING INDEX" in orphans
    historical = next(plan for sql, plan in plans.items() if sql.startswith("SELECT * FROM jobs"))
    assert "SEARCH JOBS USING INDEX IDX_JOBS_EPISODE" in historical


def test_replay_does_not_repeat_source_or_duplicate_scans(target, monkeypatch):
    from backend import memory_repair

    store, _, _, _ = target
    prepared = freeze(target)
    receipt = store.apply_memory_repair(prepared)

    def forbidden(*args, **kwargs):
        raise AssertionError("receipt replay must not scan source or duplicate memories")

    monkeypatch.setattr(memory_repair, "read_source", forbidden)
    assert MemoryStore.open_existing_for_repair(store.path).apply_memory_repair(prepared) == receipt
