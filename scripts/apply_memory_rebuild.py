#!/usr/bin/env python3
"""Safely apply a completed MuseLab Memory shadow rebuild.

The Registry is canonical. New Memories are staged as pending_review, indexed and
verified in Qdrant, then activated in one SQLite transaction while eligible old
inferred Memories are superseded. Old vectors are retained for logical rollback.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sqlite3
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backend.memory_config import database_path, load_config, memory_dir  # noqa: E402
from backend.memory_providers import (  # noqa: E402
    EmbeddingProvider,
    QdrantVectorStore,
    vector_store,
)
from backend.memory_store import MemoryStore, _fts_text  # noqa: E402
from scripts.rebuild_memory_once import source_fingerprint  # noqa: E402

SCHEMA_VERSION = 1
ID_NAMESPACE = uuid.UUID("f65c1fd5-508d-4ae3-a568-06060bbad6c2")
ALLOWED_KINDS = {"fact", "preference", "decision", "state", "episode"}
RUN_STATES = {
    "staged", "vectors_ready", "committed", "rolled_back",
    "committed_backup_pending",
}
DISALLOWED_SOURCE_TYPES = {"user_action", "message", "legacy_mem0"}
DISALLOWED_RELATIONS = {"confirmed_by", "confirmed_from"}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def normalize_content(value: Any) -> str:
    return " ".join(str(value or "").split())


def finite_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.5
    if not math.isfinite(number):
        number = 0.5
    return max(0.0, min(1.0, number))


def sources_hash(rows: list[dict[str, str]]) -> str:
    normalized = sorted(
        (row["source_type"], row["source_id"], row["relation"])
        for row in rows
    )
    return sha256_bytes(canonical_json(normalized).encode())


def memory_row_hash(row: sqlite3.Row | dict[str, Any]) -> str:
    keys = (
        "id", "owner_id", "kind", "content", "status", "authority",
        "confidence", "entities_json", "attributes_json", "tags_json",
        "valid_from", "valid_to", "version", "embedding_state",
        "created_at", "updated_at",
    )
    return sha256_bytes(canonical_json([row[key] for key in keys]).encode())


def deterministic_memory_id(
    owner_id: str,
    episode_id: str,
    kind: str,
    content: str,
    evidence_ids: list[str],
) -> str:
    payload = canonical_json([
        "muselab-memory-rebuild-v1", owner_id, episode_id, kind,
        normalize_content(content), sorted(evidence_ids),
    ])
    return f"mem_{uuid.uuid5(ID_NAMESPACE, payload).hex}"


def expected_point_id(store: QdrantVectorStore, memory_id: str) -> str:
    return store._point_id(memory_id)


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    else:
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def check_registry(conn: sqlite3.Connection) -> None:
    quick = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
    foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
    if quick != ["ok"] or foreign:
        raise RuntimeError(
            f"Registry integrity failed: quick={quick!r} foreign_keys={len(foreign)}")


def init_journal(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS memory_rebuild_runs (
      run_id TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL,
      manifest_sha256 TEXT NOT NULL,
      shadow_sha256 TEXT NOT NULL,
      manifest_json TEXT NOT NULL,
      state TEXT NOT NULL,
      candidate_count INTEGER NOT NULL,
      retire_count INTEGER NOT NULL,
      pre_backup_json TEXT NOT NULL DEFAULT '{}',
      post_backup_json TEXT NOT NULL DEFAULT '{}',
      created_at REAL NOT NULL,
      staged_at REAL,
      vectors_ready_at REAL,
      cutover_at REAL,
      rolled_back_at REAL,
      updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS memory_rebuild_new_items (
      run_id TEXT NOT NULL REFERENCES memory_rebuild_runs(run_id) ON DELETE CASCADE,
      memory_id TEXT NOT NULL,
      episode_id TEXT NOT NULL,
      content_sha256 TEXT NOT NULL,
      sources_sha256 TEXT NOT NULL,
      staged_updated_at REAL NOT NULL,
      cutover_updated_at REAL,
      PRIMARY KEY(run_id,memory_id)
    );
    CREATE TABLE IF NOT EXISTS memory_rebuild_retire_items (
      run_id TEXT NOT NULL REFERENCES memory_rebuild_runs(run_id) ON DELETE CASCADE,
      memory_id TEXT NOT NULL,
      prior_status TEXT NOT NULL,
      prior_valid_to REAL,
      prior_updated_at REAL NOT NULL,
      row_sha256 TEXT NOT NULL,
      sources_sha256 TEXT NOT NULL,
      cutover_updated_at REAL,
      PRIMARY KEY(run_id,memory_id)
    );
    CREATE TABLE IF NOT EXISTS memory_rebuild_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id TEXT NOT NULL,
      phase TEXT NOT NULL,
      outcome TEXT NOT NULL,
      details_json TEXT NOT NULL DEFAULT '{}',
      created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_memory_rebuild_events_run
      ON memory_rebuild_events(run_id,id);
    """)


def event(
    conn: sqlite3.Connection,
    run_id: str,
    phase: str,
    outcome: str,
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO memory_rebuild_events"
        "(run_id,phase,outcome,details_json,created_at) VALUES (?,?,?,?,?)",
        (run_id, phase, outcome, canonical_json(details or {}), time.time()),
    )


def load_shadow(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    shadow = run_dir / "shadow.sqlite3"
    report_path = run_dir / "report.json"
    if not shadow.is_file() or not report_path.is_file():
        raise RuntimeError("run directory must contain shadow.sqlite3 and report.json")
    report = json.loads(report_path.read_text())
    if (
        report.get("episodes") != 1124
        or report.get("completed") != 1124
        or report.get("failed") != 0
        or report.get("accepted_memories") != 1704
        or report.get("shadow_quick_check") != "ok"
        or report.get("source_registry_unchanged") is not True
    ):
        raise RuntimeError("shadow report is not the accepted completed full-v3 run")
    with connect(shadow, readonly=True) as conn:
        if [row[0] for row in conn.execute("PRAGMA quick_check")] != ["ok"]:
            raise RuntimeError("shadow quick_check failed")
        meta = {
            row["key"]: json.loads(row["value_json"])
            for row in conn.execute("SELECT key,value_json FROM run_meta")
        }
        rows = conn.execute(
            "SELECT episode_id,result_json FROM episode_results "
            "WHERE status='completed' ORDER BY episode_id"
        ).fetchall()
    if len(rows) != 1124:
        raise RuntimeError("shadow completed Episode count changed")
    accepted: list[dict[str, Any]] = []
    for row in rows:
        result = json.loads(row["result_json"])
        episode_id = str(result.get("episode", {}).get("id") or row["episode_id"])
        for index, candidate in enumerate(result.get("accepted", [])):
            if not isinstance(candidate, dict):
                raise RuntimeError(f"invalid accepted candidate in {episode_id}")
            accepted.append({
                "episode_id": episode_id,
                "candidate_index": index,
                **candidate,
            })
    if len(accepted) != 1704:
        raise RuntimeError("shadow accepted count changed")
    return meta, accepted


def current_sources(
    conn: sqlite3.Connection, memory_id: str,
) -> list[dict[str, str]]:
    return [dict(row) for row in conn.execute(
        "SELECT source_type,source_id,relation FROM memory_sources "
        "WHERE memory_id=? ORDER BY source_type,source_id,relation",
        (memory_id,),
    )]


def build_manifest(
    registry: Path,
    run_dir: Path,
) -> dict[str, Any]:
    cfg = load_config(fresh=True)
    if cfg.mode != "active" or cfg.vector.provider != "qdrant":
        raise RuntimeError("cutover requires active Memory with Qdrant")
    meta, accepted = load_shadow(run_dir)
    if meta.get("owner_id") != cfg.owner_id:
        raise RuntimeError("shadow owner does not match active config")
    if str(Path(meta.get("registry", "")).resolve()) != str(registry.resolve()):
        raise RuntimeError("shadow Registry path does not match")
    source_before = meta.get("source_before")
    if not isinstance(source_before, dict):
        raise RuntimeError("shadow source baseline is missing")
    current_fingerprint = source_fingerprint(registry, float(meta["source_cutoff"]))
    if current_fingerprint != source_before:
        raise RuntimeError("source Registry baseline changed since shadow rebuild")

    target = vector_store(cfg.vector)
    if not isinstance(target, QdrantVectorStore):
        raise RuntimeError("cutover currently supports Qdrant only")
    episode_ids = set(str(value) for value in meta.get("episode_ids", []))
    if len(episode_ids) != 1124:
        raise RuntimeError("shadow Episode ID set changed")

    new_memories: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    with connect(registry, readonly=True) as conn:
        check_registry(conn)
        for candidate in accepted:
            episode_id = str(candidate["episode_id"])
            kind = str(candidate.get("kind", ""))
            content = normalize_content(candidate.get("content"))
            evidence_ids = sorted(set(str(value) for value in candidate.get("source_ids", [])))
            if kind not in ALLOWED_KINDS or not content or not evidence_ids:
                raise RuntimeError(f"invalid candidate in Episode {episode_id}")
            episode = conn.execute(
                "SELECT owner_id FROM episodes WHERE id=?", (episode_id,)
            ).fetchone()
            if episode is None or episode["owner_id"] != cfg.owner_id:
                raise RuntimeError(f"owner-fenced Episode: {episode_id}")
            placeholders = ",".join("?" for _ in evidence_ids)
            found = conn.execute(
                f"SELECT count(*) FROM evidence WHERE owner_id=? "
                f"AND id IN ({placeholders})",
                (cfg.owner_id, *evidence_ids),
            ).fetchone()[0]
            if found != len(evidence_ids):
                raise RuntimeError(f"owner-fenced Evidence in Episode {episode_id}")
            memory_id = deterministic_memory_id(
                cfg.owner_id, episode_id, kind, content, evidence_ids)
            if memory_id in seen_ids:
                raise RuntimeError(f"deterministic Memory ID collision: {memory_id}")
            seen_ids.add(memory_id)
            if conn.execute("SELECT 1 FROM memories WHERE id=?", (memory_id,)).fetchone():
                raise RuntimeError(f"deterministic Memory ID already exists: {memory_id}")
            source_rows = [
                {"source_type": "episode", "source_id": episode_id,
                 "relation": "derived_from"},
                *[
                    {"source_type": "evidence", "source_id": evidence_id,
                     "relation": "supports"}
                    for evidence_id in evidence_ids
                ],
            ]
            attributes = {
                "attributed_to": candidate.get("attributed_to", "derived"),
                "reuse_conditions": candidate.get("reuse_conditions", []),
                "future_use": candidate.get("future_use"),
                "verification": candidate.get("verification", {}),
                "dreamer_prompt_version": candidate.get("dreamer_prompt_version"),
                "verifier_prompt_version": candidate.get("verifier_prompt_version"),
                "extractor_model": meta.get("model"),
                "shadow_episode_id": episode_id,
                "shadow_candidate_index": int(candidate["candidate_index"]),
            }
            new_memories.append({
                "memory_id": memory_id,
                "episode_id": episode_id,
                "kind": kind,
                "content": content,
                "content_sha256": sha256_bytes(content.encode()),
                "confidence": finite_confidence(candidate.get("confidence")),
                "attributes": attributes,
                "sources": source_rows,
                "sources_sha256": sources_hash(source_rows),
                "expected_point_id": expected_point_id(target, memory_id),
            })

        retire: list[dict[str, Any]] = []
        protected: list[dict[str, str]] = []
        active_rows = conn.execute(
            "SELECT * FROM memories WHERE owner_id=? AND status='active' ORDER BY id",
            (cfg.owner_id,),
        ).fetchall()
        cutoff = float(meta["source_cutoff"])
        for row in active_rows:
            sources = current_sources(conn, row["id"])
            episode_sources = {
                source["source_id"] for source in sources
                if source["source_type"] == "episode"
            }
            reasons: list[str] = []
            if row["authority"] != "inferred":
                reasons.append("authority_protected")
            if float(row["created_at"]) > cutoff:
                reasons.append("created_after_cutoff")
            if not episode_sources:
                reasons.append("no_episode_source")
            elif len(episode_sources) != 1:
                reasons.append("cross_episode_source")
            elif not episode_sources.issubset(episode_ids):
                reasons.append("cross_boundary_episode_source")
            if any(source["source_type"] in DISALLOWED_SOURCE_TYPES for source in sources):
                reasons.append("protected_source_type")
            if any(source["relation"] in DISALLOWED_RELATIONS
                   or source["relation"].startswith("confirmed") for source in sources):
                reasons.append("confirmed_relation")
            if reasons:
                protected.append({"memory_id": row["id"], "reason": ",".join(reasons)})
                continue
            retire.append({
                "memory_id": row["id"],
                "prior_status": row["status"],
                "prior_valid_to": row["valid_to"],
                "prior_updated_at": row["updated_at"],
                "row_sha256": memory_row_hash(row),
                "sources_sha256": sources_hash(sources),
            })
        active_count = len(active_rows)

    manifest_body = {
        "schema_version": SCHEMA_VERSION,
        "owner_id": cfg.owner_id,
        "registry": str(registry.resolve()),
        "run_dir": str(run_dir.resolve()),
        "shadow_sha256": sha256_file(run_dir / "shadow.sqlite3"),
        "source_cutoff": float(meta["source_cutoff"]),
        "source_before": source_before,
        "model": meta.get("model"),
        "dreamer_prompt_version": meta.get("dreamer_prompt_version"),
        "verifier_prompt_version": meta.get("verifier_prompt_version"),
        "episode_ids": sorted(episode_ids),
        "vector": {
            "collection": cfg.vector.collection,
            "provider": cfg.vector.provider,
            "embedding_model": cfg.embedding.model,
            "embedding_dimensions": cfg.embedding.dimensions,
        },
        "expected": {
            "active_before": active_count,
            "new_count": len(new_memories),
            "retire_count": len(retire),
            "protected_count": len(protected),
            "active_after": active_count - len(retire) + len(new_memories),
        },
        "new_memories": new_memories,
        "retire_memories": retire,
        "protected_memories": protected,
        "created_at": utc_stamp(),
    }
    basis = canonical_json(manifest_body)
    manifest_sha = sha256_bytes(basis.encode())
    manifest = {
        **manifest_body,
        "run_id": f"rebuild_{manifest_sha[:24]}",
        "manifest_sha256": manifest_sha,
    }
    return manifest


def manifest_path(run_dir: Path) -> Path:
    return run_dir / "apply-manifest.json"


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> Path:
    path = manifest_path(run_dir)
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path.exists():
        existing = json.loads(path.read_text())
        if existing.get("manifest_sha256") != manifest.get("manifest_sha256"):
            raise RuntimeError("existing apply manifest differs from current plan")
        return path
    temporary = run_dir / ".apply-manifest.tmp"
    with temporary.open("w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    directory_fd = os.open(run_dir, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return path


def load_manifest(run_dir: Path) -> dict[str, Any]:
    path = manifest_path(run_dir)
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError("apply-manifest.json is missing or not mode 0600")
    manifest = json.loads(path.read_text())
    body = {
        key: value for key, value in manifest.items()
        if key not in {"run_id", "manifest_sha256"}
    }
    digest = sha256_bytes(canonical_json(body).encode())
    if digest != manifest.get("manifest_sha256"):
        raise RuntimeError("manifest SHA-256 is invalid")
    if manifest.get("run_id") != f"rebuild_{digest[:24]}":
        raise RuntimeError("manifest run_id is invalid")
    return manifest


def verify_backup(registry: Path, receipt: dict[str, Any]) -> None:
    path = registry.parent / "backups" / receipt["filename"]
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError("backup file missing or permissions invalid")
    if path.stat().st_size != int(receipt["size_bytes"]):
        raise RuntimeError("backup size mismatch")
    if sha256_file(path) != receipt["sha256"]:
        raise RuntimeError("backup hash mismatch")
    with connect(path, readonly=True) as conn:
        check_registry(conn)


def source_rows_for_manifest(item: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"source_type": str(row["source_type"]),
         "source_id": str(row["source_id"]),
         "relation": str(row["relation"])}
        for row in item["sources"]
    ]


def assert_manifest_matches_runtime(
    registry: Path, manifest: dict[str, Any], *, require_source_baseline: bool = True,
) -> None:
    cfg = load_config(fresh=True)
    if str(registry.resolve()) != manifest["registry"]:
        raise RuntimeError("manifest Registry mismatch")
    if cfg.owner_id != manifest["owner_id"]:
        raise RuntimeError("manifest owner mismatch")
    if cfg.vector.provider != "qdrant":
        raise RuntimeError("cutover requires Qdrant")
    vector = manifest["vector"]
    if (
        cfg.vector.collection != vector["collection"]
        or cfg.embedding.model != vector["embedding_model"]
        or cfg.embedding.dimensions != vector["embedding_dimensions"]
    ):
        raise RuntimeError("embedding/vector config drifted since manifest creation")
    if require_source_baseline:
        current = source_fingerprint(registry, float(manifest["source_cutoff"]))
        if current != manifest["source_before"]:
            raise RuntimeError("source Registry baseline drifted")


def stage(registry: Path, run_dir: Path) -> dict[str, Any]:
    manifest = load_manifest(run_dir)
    assert_manifest_matches_runtime(registry, manifest)
    store = MemoryStore(registry)
    with store._lock, store._connect() as conn:
        init_journal(conn)
        existing_run = conn.execute(
            "SELECT manifest_sha256,state FROM memory_rebuild_runs WHERE run_id=?",
            (manifest["run_id"],),
        ).fetchone()
    if existing_run is not None:
        if existing_run["manifest_sha256"] != manifest["manifest_sha256"]:
            raise RuntimeError("run_id exists with different manifest")
        if existing_run["state"] in RUN_STATES:
            return status(registry, run_dir)
    backup = store.create_backup(manifest["owner_id"], registry.parent / "backups")
    verify_backup(registry, backup)
    now = time.time()
    with store._write_tx() as conn:
        conn.execute(
            """INSERT INTO memory_rebuild_runs
               (run_id,owner_id,manifest_sha256,shadow_sha256,manifest_json,state,
                candidate_count,retire_count,pre_backup_json,created_at,staged_at,updated_at)
               VALUES (?,?,?,?,?,'staged',?,?,?,?,?,?)""",
            (
                manifest["run_id"], manifest["owner_id"],
                manifest["manifest_sha256"], manifest["shadow_sha256"],
                canonical_json(manifest), len(manifest["new_memories"]),
                len(manifest["retire_memories"]), canonical_json(backup),
                now, now, now,
            ),
        )
        for item in manifest["new_memories"]:
            attributes = {
                **item["attributes"],
                "rebuild_run_id": manifest["run_id"],
                "manifest_sha256": manifest["manifest_sha256"],
                "shadow_sha256": manifest["shadow_sha256"],
                "content_sha256": item["content_sha256"],
            }
            conn.execute(
                """INSERT INTO memories
                   (id,owner_id,kind,content,status,authority,confidence,
                    entities_json,attributes_json,tags_json,valid_from,valid_to,
                    version,embedding_state,created_at,updated_at)
                   VALUES (?,?,?,?,'pending_review','inferred',?,'[]',?,? ,?,NULL,1,
                           'pending',?,?)""",
                (
                    item["memory_id"], manifest["owner_id"], item["kind"],
                    item["content"], item["confidence"], canonical_json(attributes),
                    canonical_json(["memory-rebuild-v3"]), now, now, now,
                ),
            )
            conn.execute(
                "INSERT INTO memory_fts(memory_id,owner_id,kind,content) VALUES (?,?,?,?)",
                (item["memory_id"], manifest["owner_id"], item["kind"],
                 _fts_text(item["content"])),
            )
            for source in source_rows_for_manifest(item):
                conn.execute(
                    "INSERT INTO memory_sources(memory_id,source_type,source_id,relation) "
                    "VALUES (?,?,?,?)",
                    (item["memory_id"], source["source_type"],
                     source["source_id"], source["relation"]),
                )
            conn.execute(
                """INSERT INTO memory_rebuild_new_items
                   (run_id,memory_id,episode_id,content_sha256,sources_sha256,
                    staged_updated_at) VALUES (?,?,?,?,?,?)""",
                (manifest["run_id"], item["memory_id"], item["episode_id"],
                 item["content_sha256"], item["sources_sha256"], now),
            )
        for item in manifest["retire_memories"]:
            row = conn.execute(
                "SELECT * FROM memories WHERE id=?", (item["memory_id"],)
            ).fetchone()
            if row is None or memory_row_hash(row) != item["row_sha256"]:
                raise RuntimeError(f"retire row drifted before stage: {item['memory_id']}")
            if sources_hash(current_sources(conn, item["memory_id"])) != item["sources_sha256"]:
                raise RuntimeError(f"retire sources drifted: {item['memory_id']}")
            conn.execute(
                """INSERT INTO memory_rebuild_retire_items
                   (run_id,memory_id,prior_status,prior_valid_to,prior_updated_at,
                    row_sha256,sources_sha256) VALUES (?,?,?,?,?,?,?)""",
                (manifest["run_id"], item["memory_id"], item["prior_status"],
                 item["prior_valid_to"], item["prior_updated_at"],
                 item["row_sha256"], item["sources_sha256"]),
            )
        event(conn, manifest["run_id"], "stage", "completed", {
            "new": len(manifest["new_memories"]),
            "retire": len(manifest["retire_memories"]),
            "pre_backup_id": backup["id"],
        })
    return status(registry, run_dir)


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM memory_rebuild_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("rebuild run has not been staged")
    return row


async def qdrant_verify_points(
    target: QdrantVectorStore,
    manifest: dict[str, Any],
    *, expected_status: str,
) -> dict[str, dict[str, Any]]:
    memory_ids = [item["memory_id"] for item in manifest["new_memories"]]
    points = await target.retrieve_many(memory_ids)
    by_memory = {
        str((point.get("payload") or {}).get("memory_id")): point
        for point in points
        if (point.get("payload") or {}).get("memory_id")
    }
    if set(by_memory) != set(memory_ids):
        missing = sorted(set(memory_ids) - set(by_memory))[:10]
        extra = sorted(set(by_memory) - set(memory_ids))[:10]
        raise RuntimeError(f"Qdrant point mismatch missing={missing} extra={extra}")
    item_by_id = {item["memory_id"]: item for item in manifest["new_memories"]}
    for memory_id, point in by_memory.items():
        payload = point.get("payload") or {}
        item = item_by_id[memory_id]
        expected = {
            "owner_id": manifest["owner_id"],
            "status": expected_status,
            "content_sha256": item["content_sha256"],
            "rebuild_run_id": manifest["run_id"],
            "manifest_sha256": manifest["manifest_sha256"],
            "embedding_model": manifest["vector"]["embedding_model"],
            "embedding_dimensions": manifest["vector"]["embedding_dimensions"],
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise RuntimeError(f"Qdrant payload mismatch {memory_id} {key}")
        vector = point.get("vector")
        if not isinstance(vector, list) or not vector:
            raise RuntimeError(f"Qdrant vector missing: {memory_id}")
        if len(vector) != int(manifest["vector"]["embedding_dimensions"]):
            raise RuntimeError(f"Qdrant vector dimension mismatch: {memory_id}")
        if not all(math.isfinite(float(number)) for number in vector):
            raise RuntimeError(f"Qdrant vector is non-finite: {memory_id}")
        if not any(float(number) != 0.0 for number in vector):
            raise RuntimeError(f"Qdrant vector is all zero: {memory_id}")
    return by_memory


async def preindex(registry: Path, run_dir: Path) -> dict[str, Any]:
    manifest = load_manifest(run_dir)
    assert_manifest_matches_runtime(registry, manifest)
    cfg = load_config(fresh=True)
    target = vector_store(cfg.vector)
    if not isinstance(target, QdrantVectorStore):
        raise RuntimeError("cutover preindex requires Qdrant")
    with connect(registry, readonly=True) as conn:
        run = get_run(conn, manifest["run_id"])
        if run["state"] not in {"staged", "vectors_ready"}:
            raise RuntimeError(f"cannot preindex run in state {run['state']}")
        rows = {
            row["id"]: row for row in conn.execute(
                "SELECT * FROM memories WHERE id IN "
                f"({','.join('?' for _ in manifest['new_memories'])})",
                tuple(item["memory_id"] for item in manifest["new_memories"]),
            )
        }
    embedding_config = cfg.embedding.model_copy(update={"timeout_seconds": 60.0})
    provider = EmbeddingProvider(embedding_config)
    dimensions = int(cfg.embedding.dimensions or 0)
    if dimensions <= 0:
        probe = await provider.probe()
        dimensions = int(probe["dimensions"])
    if manifest["vector"]["embedding_dimensions"] not in {None, dimensions}:
        raise RuntimeError("embedding dimension differs from manifest")
    manifest["vector"]["embedding_dimensions"] = dimensions
    await target.ensure(dimensions)
    batch_size = max(1, min(64, int(cfg.embedding.batch_size)))
    for start in range(0, len(manifest["new_memories"]), batch_size):
        chunk = manifest["new_memories"][start:start + batch_size]
        texts = []
        for item in chunk:
            row = rows.get(item["memory_id"])
            if row is None or row["status"] != "pending_review":
                raise RuntimeError(f"staged Memory missing: {item['memory_id']}")
            texts.append(row["content"])
        for attempt in range(3):
            try:
                vectors = await provider.embed(texts)
                break
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
        await target.upsert_many([
            (
                item["memory_id"], vector, {
                    "owner_id": manifest["owner_id"],
                    "status": "pending_review",
                    "kind": item["kind"],
                    "authority": "inferred",
                    "confidence": item["confidence"],
                    "content_sha256": item["content_sha256"],
                    "rebuild_run_id": manifest["run_id"],
                    "manifest_sha256": manifest["manifest_sha256"],
                    "embedding_model": cfg.embedding.model,
                    "embedding_dimensions": dimensions,
                },
            )
            for item, vector in zip(chunk, vectors, strict=True)
        ])
    await qdrant_verify_points(target, manifest, expected_status="pending_review")
    now = time.time()
    store = MemoryStore(registry)
    with store._write_tx() as conn:
        run = get_run(conn, manifest["run_id"])
        if run["state"] not in {"staged", "vectors_ready"}:
            raise RuntimeError(f"cannot mark vectors in state {run['state']}")
        for item in manifest["new_memories"]:
            row = conn.execute(
                "SELECT attributes_json,status FROM memories WHERE id=?",
                (item["memory_id"],),
            ).fetchone()
            if row is None or row["status"] != "pending_review":
                raise RuntimeError(f"staged Memory drifted: {item['memory_id']}")
            attributes = json.loads(row["attributes_json"] or "{}")
            attributes.update({
                "embedding_model": cfg.embedding.model,
                "embedding_dimensions": dimensions,
            })
            conn.execute(
                "UPDATE memories SET attributes_json=?,embedding_state='ready',updated_at=? "
                "WHERE id=? AND status='pending_review'",
                (canonical_json(attributes), now, item["memory_id"]),
            )
        conn.execute(
            "UPDATE memory_rebuild_runs SET state='vectors_ready',vectors_ready_at=?,"
            "updated_at=? WHERE run_id=?",
            (now, now, manifest["run_id"]),
        )
        event(conn, manifest["run_id"], "preindex", "completed", {
            "points": len(manifest["new_memories"]), "dimensions": dimensions,
        })
    return status(registry, run_dir)


async def verify(registry: Path, run_dir: Path) -> dict[str, Any]:
    manifest = load_manifest(run_dir)
    with connect(registry, readonly=True) as conn:
        check_registry(conn)
        run = get_run(conn, manifest["run_id"])
    assert_manifest_matches_runtime(
        registry, manifest,
        require_source_baseline=run["state"] not in {
            "committed", "committed_backup_pending", "rolled_back"},
    )
    cfg = load_config(fresh=True)
    target = vector_store(cfg.vector)
    if not isinstance(target, QdrantVectorStore):
        raise RuntimeError("cutover verify requires Qdrant")
    with connect(registry, readonly=True) as conn:
        check_registry(conn)
        run = get_run(conn, manifest["run_id"])
        if run["state"] in {"committed", "committed_backup_pending"}:
            expected_status = "active"
        elif run["state"] == "rolled_back":
            expected_status = "superseded"
        else:
            expected_status = "pending_review"
        for item in manifest["new_memories"]:
            row = conn.execute("SELECT * FROM memories WHERE id=?", (item["memory_id"],)).fetchone()
            if row is None or row["status"] != expected_status:
                raise RuntimeError(f"new Memory state mismatch: {item['memory_id']}")
            if sha256_bytes(row["content"].encode()) != item["content_sha256"]:
                raise RuntimeError(f"new Memory content drifted: {item['memory_id']}")
            if sources_hash(current_sources(conn, item["memory_id"])) != item["sources_sha256"]:
                raise RuntimeError(f"new Memory sources drifted: {item['memory_id']}")
            if run["state"] in {"vectors_ready", "committed", "committed_backup_pending"} \
                    and row["embedding_state"] != "ready":
                raise RuntimeError(f"new Memory embedding is not ready: {item['memory_id']}")
    await qdrant_verify_points(target, manifest, expected_status=expected_status)
    return status(registry, run_dir)


def verify_retire_cas(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
    row = conn.execute("SELECT * FROM memories WHERE id=?", (item["memory_id"],)).fetchone()
    if row is None or memory_row_hash(row) != item["row_sha256"]:
        raise RuntimeError(f"retire Memory drifted: {item['memory_id']}")
    if sources_hash(current_sources(conn, item["memory_id"])) != item["sources_sha256"]:
        raise RuntimeError(f"retire Memory sources drifted: {item['memory_id']}")


def commit_registry_cutover(
    store: MemoryStore, manifest: dict[str, Any], now: float,
) -> None:
    with store._write_tx() as conn:
        run = get_run(conn, manifest["run_id"])
        if run["state"] != "vectors_ready":
            raise RuntimeError(f"cutover state changed: {run['state']}")
        for item in manifest["retire_memories"]:
            verify_retire_cas(conn, item)
        new_changed = 0
        for item in manifest["new_memories"]:
            changed = conn.execute(
                "UPDATE memories SET status='active',updated_at=? "
                "WHERE id=? AND owner_id=? AND status='pending_review' "
                "AND embedding_state='ready'",
                (now, item["memory_id"], manifest["owner_id"]),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"new Memory CAS failed: {item['memory_id']}")
            conn.execute(
                "UPDATE memory_rebuild_new_items SET cutover_updated_at=? "
                "WHERE run_id=? AND memory_id=?",
                (now, manifest["run_id"], item["memory_id"]),
            )
            new_changed += changed
        retire_changed = 0
        for item in manifest["retire_memories"]:
            changed = conn.execute(
                "UPDATE memories SET status='superseded',valid_to=?,updated_at=? "
                "WHERE id=? AND owner_id=? AND status='active' AND updated_at=?",
                (now, now, item["memory_id"], manifest["owner_id"],
                 item["prior_updated_at"]),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"retire Memory CAS failed: {item['memory_id']}")
            conn.execute(
                "UPDATE memory_rebuild_retire_items SET cutover_updated_at=? "
                "WHERE run_id=? AND memory_id=?",
                (now, manifest["run_id"], item["memory_id"]),
            )
            retire_changed += changed
        if new_changed != len(manifest["new_memories"]) \
                or retire_changed != len(manifest["retire_memories"]):
            raise RuntimeError("cutover row counts do not match manifest")
        conn.execute(
            "UPDATE memory_rebuild_runs SET state='committed_backup_pending',"
            "cutover_at=?,updated_at=? WHERE run_id=?",
            (now, now, manifest["run_id"]),
        )
        event(conn, manifest["run_id"], "cutover", "committed", {
            "new": new_changed, "retired": retire_changed,
        })


def complete_post_backup(
    store: MemoryStore, registry: Path, manifest: dict[str, Any],
) -> dict[str, Any]:
    backup = store.create_backup(manifest["owner_id"], registry.parent / "backups")
    verify_backup(registry, backup)
    with store._write_tx() as conn:
        run = get_run(conn, manifest["run_id"])
        if run["state"] == "committed":
            return json.loads(run["post_backup_json"] or "{}")
        if run["state"] != "committed_backup_pending":
            raise RuntimeError("cutover state changed before post backup receipt")
        conn.execute(
            "UPDATE memory_rebuild_runs SET state='committed',post_backup_json=?,"
            "updated_at=? WHERE run_id=?",
            (canonical_json(backup), time.time(), manifest["run_id"]),
        )
        event(conn, manifest["run_id"], "post_backup", "completed", {
            "backup_id": backup["id"], "sha256": backup["sha256"],
        })
    return backup


async def cutover(registry: Path, run_dir: Path) -> dict[str, Any]:
    manifest = load_manifest(run_dir)
    with connect(registry, readonly=True) as conn:
        run = get_run(conn, manifest["run_id"])
    assert_manifest_matches_runtime(
        registry, manifest,
        require_source_baseline=run["state"] not in {
            "committed", "committed_backup_pending"},
    )
    cfg = load_config(fresh=True)
    target = vector_store(cfg.vector)
    if not isinstance(target, QdrantVectorStore):
        raise RuntimeError("cutover requires Qdrant")
    with connect(registry, readonly=True) as conn:
        run = get_run(conn, manifest["run_id"])
        if run["state"] in {"committed", "committed_backup_pending"}:
            await target.set_payload_many(
                [item["memory_id"] for item in manifest["new_memories"]],
                {"status": "active"},
            )
            await target.set_payload_many(
                [item["memory_id"] for item in manifest["retire_memories"]],
                {"status": "superseded"},
            )
            if run["state"] == "committed_backup_pending":
                complete_post_backup(MemoryStore(registry), registry, manifest)
            return status(registry, run_dir)
        if run["state"] != "vectors_ready":
            raise RuntimeError(f"cannot cut over run in state {run['state']}")
    await qdrant_verify_points(target, manifest, expected_status="pending_review")
    await target.set_payload_many(
        [item["memory_id"] for item in manifest["new_memories"]],
        {"status": "active"},
    )
    now = time.time()
    store = MemoryStore(registry)
    try:
        commit_registry_cutover(store, manifest, now)
    except BaseException:
        await target.set_payload_many(
            [item["memory_id"] for item in manifest["new_memories"]],
            {"status": "pending_review"},
        )
        raise
    await target.set_payload_many(
        [item["memory_id"] for item in manifest["retire_memories"]],
        {"status": "superseded"},
    )
    await qdrant_verify_points(target, manifest, expected_status="active")
    complete_post_backup(store, registry, manifest)
    return status(registry, run_dir)


async def rollback(registry: Path, run_dir: Path) -> dict[str, Any]:
    manifest = load_manifest(run_dir)
    assert_manifest_matches_runtime(registry, manifest, require_source_baseline=False)
    cfg = load_config(fresh=True)
    target = vector_store(cfg.vector)
    if not isinstance(target, QdrantVectorStore):
        raise RuntimeError("rollback requires Qdrant")
    with connect(registry, readonly=True) as conn:
        run = get_run(conn, manifest["run_id"])
        if run["state"] == "rolled_back":
            return status(registry, run_dir)
        if run["state"] not in {"committed", "committed_backup_pending"}:
            raise RuntimeError(f"cannot rollback run in state {run['state']}")
    await target.set_payload_many(
        [item["memory_id"] for item in manifest["retire_memories"]],
        {"status": "active"},
    )
    now = time.time()
    store = MemoryStore(registry)
    with store._write_tx() as conn:
        run = get_run(conn, manifest["run_id"])
        if run["state"] not in {"committed", "committed_backup_pending"}:
            raise RuntimeError(f"rollback state changed: {run['state']}")
        for item in manifest["new_memories"]:
            tracked = conn.execute(
                "SELECT cutover_updated_at FROM memory_rebuild_new_items "
                "WHERE run_id=? AND memory_id=?",
                (manifest["run_id"], item["memory_id"]),
            ).fetchone()
            row = conn.execute(
                "SELECT status,authority,updated_at,attributes_json FROM memories WHERE id=?",
                (item["memory_id"],),
            ).fetchone()
            if (
                row is None or row["status"] != "active"
                or row["authority"] != "inferred"
                or tracked is None or row["updated_at"] != tracked["cutover_updated_at"]
            ):
                raise RuntimeError(f"new Memory changed after cutover: {item['memory_id']}")
            attributes = json.loads(row["attributes_json"] or "{}")
            if attributes.get("rebuild_run_id") != manifest["run_id"]:
                raise RuntimeError(f"new Memory provenance changed: {item['memory_id']}")
        for item in manifest["retire_memories"]:
            tracked = conn.execute(
                "SELECT cutover_updated_at FROM memory_rebuild_retire_items "
                "WHERE run_id=? AND memory_id=?",
                (manifest["run_id"], item["memory_id"]),
            ).fetchone()
            row = conn.execute(
                "SELECT status,updated_at FROM memories WHERE id=?",
                (item["memory_id"],),
            ).fetchone()
            if (
                row is None or row["status"] != "superseded" or tracked is None
                or row["updated_at"] != tracked["cutover_updated_at"]
            ):
                raise RuntimeError(f"retired Memory changed after cutover: {item['memory_id']}")
        for item in manifest["retire_memories"]:
            conn.execute(
                "UPDATE memories SET status=?,valid_to=?,updated_at=? WHERE id=?",
                (item["prior_status"], item["prior_valid_to"], now,
                 item["memory_id"]),
            )
        for item in manifest["new_memories"]:
            conn.execute(
                "UPDATE memories SET status='superseded',valid_to=?,updated_at=? WHERE id=?",
                (now, now, item["memory_id"]),
            )
        conn.execute(
            "UPDATE memory_rebuild_runs SET state='rolled_back',rolled_back_at=?,"
            "updated_at=? WHERE run_id=?",
            (now, now, manifest["run_id"]),
        )
        event(conn, manifest["run_id"], "rollback", "completed", {
            "restored": len(manifest["retire_memories"]),
            "disabled_new": len(manifest["new_memories"]),
        })
    await target.set_payload_many(
        [item["memory_id"] for item in manifest["new_memories"]],
        {"status": "superseded"},
    )
    return status(registry, run_dir)


def status(registry: Path, run_dir: Path) -> dict[str, Any]:
    manifest = load_manifest(run_dir)
    with connect(registry, readonly=True) as conn:
        check_registry(conn)
        has_journal = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='memory_rebuild_runs'"
        ).fetchone() is not None
        run = conn.execute(
            "SELECT * FROM memory_rebuild_runs WHERE run_id=?",
            (manifest["run_id"],),
        ).fetchone() if has_journal else None
        state = run["state"] if run else "planned"
        counts = {
            row["status"]: int(row["n"])
            for row in conn.execute(
                "SELECT status,count(*) n FROM memories WHERE owner_id=? GROUP BY status",
                (manifest["owner_id"],),
            )
        }
        new_counts = {
            row["status"]: int(row["n"])
            for row in conn.execute(
                "SELECT m.status,count(*) n FROM memories m "
                "JOIN memory_rebuild_new_items i ON i.memory_id=m.id "
                "WHERE i.run_id=? GROUP BY m.status",
                (manifest["run_id"],),
            )
        } if run else {}
        retire_counts = {
            row["status"]: int(row["n"])
            for row in conn.execute(
                "SELECT m.status,count(*) n FROM memories m "
                "JOIN memory_rebuild_retire_items i ON i.memory_id=m.id "
                "WHERE i.run_id=? GROUP BY m.status",
                (manifest["run_id"],),
            )
        } if run else {}
        result = {
            "run_id": manifest["run_id"],
            "state": state,
            "manifest_sha256": manifest["manifest_sha256"],
            "expected": manifest["expected"],
            "memory_status_counts": counts,
            "new_status_counts": new_counts,
            "retire_status_counts": retire_counts,
            "pre_backup": json.loads(run["pre_backup_json"]) if run else {},
            "post_backup": json.loads(run["post_backup_json"]) if run else {},
        }
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(
        "plan", "stage", "preindex", "verify", "cutover", "status", "rollback"))
    parser.add_argument("--registry", type=Path, default=database_path())
    parser.add_argument("--run-dir", type=Path, required=True)
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> dict[str, Any]:
    os.umask(0o077)
    registry = args.registry.expanduser().resolve()
    run_dir = args.run_dir.expanduser().resolve()
    if not registry.is_file() or not run_dir.is_dir():
        raise RuntimeError("Registry or run directory does not exist")
    if args.command == "plan":
        existing_path = manifest_path(run_dir)
        manifest = (load_manifest(run_dir) if existing_path.exists()
                    else build_manifest(registry, run_dir))
        path = write_manifest(run_dir, manifest)
        return {
            "manifest": str(path), "run_id": manifest["run_id"],
            "manifest_sha256": manifest["manifest_sha256"],
            "expected": manifest["expected"],
        }
    if args.command == "stage":
        return stage(registry, run_dir)
    if args.command == "preindex":
        return await preindex(registry, run_dir)
    if args.command == "verify":
        return await verify(registry, run_dir)
    if args.command == "cutover":
        return await cutover(registry, run_dir)
    if args.command == "rollback":
        return await rollback(registry, run_dir)
    return status(registry, run_dir)


def main() -> int:
    args = parse_args()
    result = asyncio.run(async_main(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
