"""Safety invariants for applying a completed Memory shadow rebuild."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from backend.memory_config import EmbeddingConfig, MemoryConfig, VectorConfig
from backend.memory_providers import QdrantVectorStore
from backend.memory_store import MemoryStore
from scripts import apply_memory_rebuild as apply


OWNER = "test-owner"


def _config() -> MemoryConfig:
    return MemoryConfig(
        mode="active",
        owner_id=OWNER,
        generation_model="test-generation",
        embedding=EmbeddingConfig(
            base_url="http://embedding.invalid",
            model="test-embedding",
            dimensions=3,
        ),
        vector=VectorConfig(
            url="http://qdrant.invalid",
            collection="test_memory_rebuild",
        ),
    )


def _item(memory_id: str = "mem_new") -> dict:
    content = "rebuilt memory content"
    sources = [
        {"source_type": "episode", "source_id": "ep-1", "relation": "derived_from"},
        {"source_type": "evidence", "source_id": "ev-1", "relation": "supports"},
    ]
    return {
        "memory_id": memory_id,
        "episode_id": "ep-1",
        "kind": "fact",
        "content": content,
        "content_sha256": apply.sha256_bytes(content.encode()),
        "confidence": 0.8,
        "attributes": {"shadow_episode_id": "ep-1"},
        "sources": sources,
        "sources_sha256": apply.sources_hash(sources),
        "expected_point_id": "unused-by-registry-tests",
    }


def _manifest(registry: Path, run_dir: Path, *, items: list[dict] | None = None,
              retire: list[dict] | None = None) -> dict:
    new_items = items if items is not None else [_item()]
    retired = retire or []
    body = {
        "schema_version": apply.SCHEMA_VERSION,
        "owner_id": OWNER,
        "registry": str(registry.resolve()),
        "run_dir": str(run_dir.resolve()),
        "shadow_sha256": "1" * 64,
        "source_cutoff": 100.0,
        "source_before": {"fingerprint": "test"},
        "model": "test-generation",
        "dreamer_prompt_version": "dreamer-v1",
        "verifier_prompt_version": "verifier-v1",
        "episode_ids": ["ep-1"],
        "vector": {
            "collection": "test_memory_rebuild",
            "provider": "qdrant",
            "embedding_model": "test-embedding",
            "embedding_dimensions": 3,
        },
        "expected": {
            "active_before": len(retired),
            "new_count": len(new_items),
            "retire_count": len(retired),
            "protected_count": 0,
            "active_after": len(new_items),
        },
        "new_memories": new_items,
        "retire_memories": retired,
        "protected_memories": [],
        "created_at": "2026-08-28T00:00:00.000000Z",
    }
    digest = apply.sha256_bytes(apply.canonical_json(body).encode())
    return {
        **body,
        "run_id": f"rebuild_{digest[:24]}",
        "manifest_sha256": digest,
    }


def _install_manifest(run_dir: Path, manifest: dict) -> None:
    run_dir.mkdir()
    apply.write_manifest(run_dir, manifest)


def _isolate_runtime(monkeypatch: pytest.MonkeyPatch, manifest: dict) -> None:
    monkeypatch.setattr(apply, "assert_manifest_matches_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        MemoryStore,
        "create_backup",
        lambda self, owner_id, backup_dir: {
            "id": "backup-test",
            "filename": "unused.sqlite3",
            "sha256": "2" * 64,
            "size_bytes": 0,
        },
    )
    monkeypatch.setattr(apply, "verify_backup", lambda *args, **kwargs: None)


class FakeQdrant(QdrantVectorStore):
    def __init__(self, points: dict[str, dict]):
        self.points = points
        self.payload_updates: list[tuple[tuple[str, ...], dict]] = []

    async def retrieve_many(self, item_ids: list[str]) -> list[dict]:
        return [self.points[item_id] for item_id in item_ids if item_id in self.points]

    async def set_payload_many(self, item_ids: list[str], payload: dict) -> None:
        self.payload_updates.append((tuple(item_ids), dict(payload)))
        for item_id in item_ids:
            if item_id in self.points:
                self.points[item_id]["payload"].update(payload)


def test_deterministic_memory_id_is_stable_and_input_sensitive():
    first = apply.deterministic_memory_id(
        OWNER, "ep-1", "fact", "  stable\n memory   content ", ["ev-2", "ev-1"])
    replay = apply.deterministic_memory_id(
        OWNER, "ep-1", "fact", "stable memory content", ["ev-1", "ev-2"])

    assert first == replay
    assert first.startswith("mem_")
    assert first != apply.deterministic_memory_id(
        OWNER, "ep-2", "fact", "stable memory content", ["ev-1", "ev-2"])
    assert first != apply.deterministic_memory_id(
        OWNER, "ep-1", "fact", "different content", ["ev-1", "ev-2"])


def test_manifest_hash_and_permissions_reject_tampering(tmp_path: Path):
    registry = tmp_path / "registry.sqlite3"
    MemoryStore(registry)
    run_dir = tmp_path / "run"
    manifest = _manifest(registry, run_dir)
    _install_manifest(run_dir, manifest)

    assert apply.load_manifest(run_dir) == manifest
    path = apply.manifest_path(run_dir)
    tampered = json.loads(path.read_text())
    tampered["new_memories"][0]["content"] = "tampered"
    path.write_text(json.dumps(tampered))
    path.chmod(0o600)
    with pytest.raises(RuntimeError, match="SHA-256 is invalid"):
        apply.load_manifest(run_dir)

    path.write_text(json.dumps(manifest))
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="not mode 0600"):
        apply.load_manifest(run_dir)


def test_build_manifest_strictly_protects_confirmed_new_and_cross_episode_memories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    registry = tmp_path / "registry.sqlite3"
    store = MemoryStore(registry)
    eligible = store.create_memory(
        OWNER, "fact", "eligible", sources=[
            {"source_type": "episode", "source_id": "ep-1", "relation": "derived_from"}])
    confirmed = store.create_memory(
        OWNER, "fact", "confirmed", authority="confirmed", sources=[
            {"source_type": "episode", "source_id": "ep-1", "relation": "derived_from"}])
    confirmed_relation = store.create_memory(
        OWNER, "fact", "confirmed relation", sources=[
            {"source_type": "episode", "source_id": "ep-1", "relation": "confirmed_from"}])
    after_cutoff = store.create_memory(
        OWNER, "fact", "newer", sources=[
            {"source_type": "episode", "source_id": "ep-1", "relation": "derived_from"}])
    cross_episode = store.create_memory(
        OWNER, "fact", "cross episode", sources=[
            {"source_type": "episode", "source_id": "ep-1", "relation": "derived_from"},
            {"source_type": "episode", "source_id": "ep-2", "relation": "derived_from"},
        ])
    cutoff = 100.0
    with sqlite3.connect(registry) as conn:
        conn.execute(
            "UPDATE memories SET created_at=90,updated_at=90 WHERE id!=?",
            (after_cutoff["id"],),
        )
        conn.execute(
            "UPDATE memories SET created_at=101,updated_at=101 WHERE id=?",
            (after_cutoff["id"],),
        )

    episode_ids = ["ep-1", "ep-2", *[f"ep-{index}" for index in range(3, 1125)]]
    baseline = {"fingerprint": "unchanged"}
    meta = {
        "owner_id": OWNER,
        "registry": str(registry),
        "source_before": baseline,
        "source_cutoff": cutoff,
        "episode_ids": episode_ids,
        "model": "test-generation",
    }
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "shadow.sqlite3").write_bytes(b"shadow")
    monkeypatch.setattr(apply, "load_config", lambda fresh=True: _config())
    monkeypatch.setattr(apply, "load_shadow", lambda path: (meta, []))
    monkeypatch.setattr(apply, "source_fingerprint", lambda path, value: baseline)
    monkeypatch.setattr(
        apply,
        "vector_store",
        lambda config: QdrantVectorStore(config),
    )

    manifest = apply.build_manifest(registry, run_dir)

    assert [row["memory_id"] for row in manifest["retire_memories"]] == [eligible["id"]]
    protected = {row["memory_id"]: row["reason"] for row in manifest["protected_memories"]}
    assert "authority_protected" in protected[confirmed["id"]]
    assert "confirmed_relation" in protected[confirmed_relation["id"]]
    assert "created_after_cutoff" in protected[after_cutoff["id"]]
    assert "cross_episode_source" in protected[cross_episode["id"]]


def test_stage_atomically_writes_registry_index_sources_and_journal_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    registry = tmp_path / "registry.sqlite3"
    MemoryStore(registry)
    run_dir = tmp_path / "run"
    manifest = _manifest(registry, run_dir)
    _install_manifest(run_dir, manifest)
    _isolate_runtime(monkeypatch, manifest)

    first = apply.stage(registry, run_dir)
    second = apply.stage(registry, run_dir)

    assert first["state"] == second["state"] == "staged"
    with apply.connect(registry, readonly=True) as conn:
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM memory_sources").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM memory_rebuild_runs").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM memory_rebuild_new_items").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM memory_rebuild_events").fetchone()[0] == 1


def test_stage_rolls_back_all_staged_rows_on_mid_transaction_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    registry = tmp_path / "registry.sqlite3"
    MemoryStore(registry)
    run_dir = tmp_path / "run"
    duplicate = _item("mem_duplicate")
    manifest = _manifest(registry, run_dir, items=[duplicate, duplicate])
    _install_manifest(run_dir, manifest)
    _isolate_runtime(monkeypatch, manifest)

    with pytest.raises(sqlite3.IntegrityError):
        apply.stage(registry, run_dir)

    with apply.connect(registry, readonly=True) as conn:
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM memory_sources").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM memory_rebuild_runs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM memory_rebuild_new_items").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM memory_rebuild_events").fetchone()[0] == 0


def test_cutover_and_rollback_use_registry_cas_without_external_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    registry = tmp_path / "registry.sqlite3"
    store = MemoryStore(registry)
    old = store.create_memory(
        OWNER, "fact", "old inferred memory", sources=[
            {"source_type": "episode", "source_id": "ep-1", "relation": "derived_from"}])
    with apply.connect(registry) as conn:
        old_row = conn.execute("SELECT * FROM memories WHERE id=?", (old["id"],)).fetchone()
        retire = [{
            "memory_id": old["id"],
            "prior_status": old_row["status"],
            "prior_valid_to": old_row["valid_to"],
            "prior_updated_at": old_row["updated_at"],
            "row_sha256": apply.memory_row_hash(old_row),
            "sources_sha256": apply.sources_hash(apply.current_sources(conn, old["id"])),
        }]
    run_dir = tmp_path / "run"
    manifest = _manifest(registry, run_dir, retire=retire)
    _install_manifest(run_dir, manifest)
    _isolate_runtime(monkeypatch, manifest)
    apply.stage(registry, run_dir)

    item = manifest["new_memories"][0]
    with apply.connect(registry) as conn:
        conn.execute(
            "UPDATE memories SET embedding_state='ready' WHERE id=?",
            (item["memory_id"],),
        )
        conn.execute(
            "UPDATE memory_rebuild_runs SET state='vectors_ready' WHERE run_id=?",
            (manifest["run_id"],),
        )
    fake = FakeQdrant({
        item["memory_id"]: {
            "payload": {
                "memory_id": item["memory_id"],
                "owner_id": OWNER,
                "status": "pending_review",
                "content_sha256": item["content_sha256"],
                "rebuild_run_id": manifest["run_id"],
                "manifest_sha256": manifest["manifest_sha256"],
                "embedding_model": "test-embedding",
                "embedding_dimensions": 3,
            },
            "vector": [0.1, 0.2, 0.3],
        },
        old["id"]: {"payload": {"memory_id": old["id"], "status": "active"}, "vector": [1.0]},
    })
    monkeypatch.setattr(apply, "load_config", lambda fresh=True: _config())
    monkeypatch.setattr(apply, "vector_store", lambda config: fake)

    committed = asyncio.run(apply.cutover(registry, run_dir))
    assert committed["state"] == "committed"
    assert store.memory(item["memory_id"])["status"] == "active"
    assert store.memory(old["id"])["status"] == "superseded"

    rolled_back = asyncio.run(apply.rollback(registry, run_dir))
    assert rolled_back["state"] == "rolled_back"
    assert store.memory(item["memory_id"])["status"] == "superseded"
    assert store.memory(old["id"])["status"] == "active"
    assert fake.points[item["memory_id"]]["payload"]["status"] == "superseded"
    assert fake.points[old["id"]]["payload"]["status"] == "active"


def test_rollback_cas_rejects_a_memory_changed_after_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    registry = tmp_path / "registry.sqlite3"
    MemoryStore(registry)
    run_dir = tmp_path / "run"
    manifest = _manifest(registry, run_dir)
    _install_manifest(run_dir, manifest)
    _isolate_runtime(monkeypatch, manifest)
    apply.stage(registry, run_dir)
    item = manifest["new_memories"][0]
    with apply.connect(registry) as conn:
        conn.execute(
            "UPDATE memories SET status='active',embedding_state='ready',updated_at=20 WHERE id=?",
            (item["memory_id"],),
        )
        conn.execute(
            "UPDATE memory_rebuild_new_items SET cutover_updated_at=10 WHERE run_id=?",
            (manifest["run_id"],),
        )
        conn.execute(
            "UPDATE memory_rebuild_runs SET state='committed' WHERE run_id=?",
            (manifest["run_id"],),
        )
    fake = FakeQdrant({item["memory_id"]: {
        "payload": {"memory_id": item["memory_id"], "status": "active"},
        "vector": [0.1, 0.2, 0.3],
    }})
    monkeypatch.setattr(apply, "load_config", lambda fresh=True: _config())
    monkeypatch.setattr(apply, "vector_store", lambda config: fake)

    with pytest.raises(RuntimeError, match="changed after cutover"):
        asyncio.run(apply.rollback(registry, run_dir))
    assert MemoryStore(registry).memory(item["memory_id"])["status"] == "active"
