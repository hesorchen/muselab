"""SQLite canonical registry for evidence, episodes and derived memories.

Vector databases are deliberately materialized indexes, not the source of
truth.  Every derived item remains reconstructable and traceable here.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


def _now() -> float:
    return time.time()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class MemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init(self) -> None:
        schema = """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS evidence (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, session_id TEXT NOT NULL,
          role TEXT NOT NULL, content TEXT NOT NULL, event_type TEXT NOT NULL,
          source_ref TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
          checksum TEXT NOT NULL UNIQUE, created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_evidence_session
          ON evidence(owner_id, session_id, created_at);
        CREATE TABLE IF NOT EXISTS episodes (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, primary_session_id TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open', title TEXT NOT NULL DEFAULT '',
          summary TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT 'unknown',
          entities_json TEXT NOT NULL DEFAULT '[]',
          attributes_json TEXT NOT NULL DEFAULT '{}',
          started_at REAL NOT NULL, ended_at REAL, updated_at REAL NOT NULL,
          turn_count INTEGER NOT NULL DEFAULT 0, extractor_version TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_episode_open
          ON episodes(owner_id, primary_session_id, status, updated_at);
        CREATE TABLE IF NOT EXISTS episode_evidence (
          episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
          evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
          position INTEGER NOT NULL, PRIMARY KEY(episode_id, evidence_id)
        );
        CREATE TABLE IF NOT EXISTS memories (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
          content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
          authority TEXT NOT NULL DEFAULT 'inferred', confidence REAL NOT NULL DEFAULT 0.5,
          entities_json TEXT NOT NULL DEFAULT '[]',
          attributes_json TEXT NOT NULL DEFAULT '{}', tags_json TEXT NOT NULL DEFAULT '[]',
          valid_from REAL, valid_to REAL, version INTEGER NOT NULL DEFAULT 1,
          embedding_state TEXT NOT NULL DEFAULT 'pending',
          created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memories_owner_status
          ON memories(owner_id, status, updated_at DESC);
        CREATE TABLE IF NOT EXISTS memory_sources (
          memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
          source_type TEXT NOT NULL, source_id TEXT NOT NULL, relation TEXT NOT NULL,
          PRIMARY KEY(memory_id, source_type, source_id, relation)
        );
        CREATE TABLE IF NOT EXISTS relations (
          id TEXT PRIMARY KEY, from_type TEXT NOT NULL, from_id TEXT NOT NULL,
          relation TEXT NOT NULL, to_type TEXT NOT NULL, to_id TEXT NOT NULL,
          metadata_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS artifacts (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending_review', title TEXT NOT NULL DEFAULT '',
          payload_json TEXT NOT NULL, source_episode_ids_json TEXT NOT NULL DEFAULT '[]',
          model TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
          created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_artifacts_status
          ON artifacts(owner_id, kind, status, updated_at DESC);
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          payload_json TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          run_after REAL NOT NULL, last_error TEXT NOT NULL DEFAULT '',
          created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_claim
          ON jobs(status, run_after, created_at);
        CREATE TABLE IF NOT EXISTS recall_logs (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, session_id TEXT NOT NULL,
          query TEXT NOT NULL, results_json TEXT NOT NULL, latency_ms REAL NOT NULL,
          status TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, action TEXT NOT NULL,
          target_type TEXT NOT NULL, target_id TEXT NOT NULL,
          details_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
          memory_id UNINDEXED, owner_id UNINDEXED, kind UNINDEXED, content,
          tokenize='unicode61'
        );
        """
        with self._lock, self._connect() as conn:
            conn.executescript(schema)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        for key in tuple(out):
            if key.endswith("_json"):
                try:
                    out[key[:-5]] = json.loads(out.pop(key))
                except Exception:
                    out[key[:-5]] = None
        return out

    def add_evidence(self, owner_id: str, session_id: str, role: str, content: str,
                     *, event_type: str = "message", source_ref: str | None = None,
                     metadata: dict | None = None, created_at: float | None = None) -> str:
        digest = hashlib.sha256(_json([
            owner_id, session_id, role, event_type, source_ref, content
        ]).encode()).hexdigest()
        eid = _id("ev")
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO evidence
                   (id,owner_id,session_id,role,content,event_type,source_ref,
                    metadata_json,checksum,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (eid, owner_id, session_id, role, content, event_type, source_ref,
                 _json(metadata or {}), digest, created_at or _now()),
            )
            row = conn.execute("SELECT id FROM evidence WHERE checksum=?", (digest,)).fetchone()
            return str(row["id"])

    def get_or_create_episode(self, owner_id: str, session_id: str,
                              *, idle_seconds: float) -> dict:
        now = _now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM episodes WHERE owner_id=? AND primary_session_id=?
                   AND status='open' ORDER BY updated_at DESC LIMIT 1""",
                (owner_id, session_id),
            ).fetchone()
            if row and now - float(row["updated_at"]) <= idle_seconds:
                return self._row(row) or {}
            if row:
                conn.execute(
                    "UPDATE episodes SET status='closed', ended_at=?, updated_at=? WHERE id=?",
                    (now, now, row["id"]),
                )
            episode_id = _id("ep")
            conn.execute(
                """INSERT INTO episodes
                   (id,owner_id,primary_session_id,started_at,updated_at)
                   VALUES (?,?,?,?,?)""",
                (episode_id, owner_id, session_id, now, now),
            )
            return self._row(conn.execute(
                "SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()) or {}

    def close_current_episode(self, owner_id: str, session_id: str) -> str | None:
        """Close a non-empty open Episode without changing its outcome."""
        now = _now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """SELECT id,turn_count FROM episodes
                   WHERE owner_id=? AND primary_session_id=? AND status='open'
                   ORDER BY updated_at DESC LIMIT 1""",
                (owner_id, session_id),
            ).fetchone()
            if row is None or int(row["turn_count"]) <= 0:
                return None
            conn.execute(
                """UPDATE episodes SET status='closed',ended_at=?,updated_at=?
                   WHERE id=? AND status='open'""",
                (now, now, row["id"]),
            )
            return str(row["id"])

    def evidence(self, evidence_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            return self._row(conn.execute(
                "SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone())

    def recent_evidence(self, owner_id: str, session_id: str, *,
                        role: str | None = None, limit: int = 4) -> list[dict]:
        sql = "SELECT * FROM evidence WHERE owner_id=? AND session_id=?"
        params: list[Any] = [owner_id, session_id]
        if role:
            sql += " AND role=?"
            params.append(role)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row(row) or {} for row in reversed(rows)]

    def attach_evidence(self, episode_id: str, evidence_ids: list[str],
                        *, increment_turn: bool = True) -> None:
        with self._lock, self._connect() as conn:
            start = conn.execute(
                "SELECT count(*) AS n FROM episode_evidence WHERE episode_id=?",
                (episode_id,),
            ).fetchone()["n"]
            for offset, evidence_id in enumerate(evidence_ids):
                conn.execute(
                    """INSERT OR IGNORE INTO episode_evidence
                       (episode_id,evidence_id,position) VALUES (?,?,?)""",
                    (episode_id, evidence_id, start + offset),
                )
            conn.execute(
                """UPDATE episodes SET updated_at=?,
                   turn_count=turn_count+? WHERE id=?""",
                (_now(), 1 if increment_turn else 0, episode_id),
            )

    def episode(self, episode_id: str, *, with_evidence: bool = True) -> dict | None:
        with self._lock, self._connect() as conn:
            value = self._row(conn.execute(
                "SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone())
            if value and with_evidence:
                rows = conn.execute(
                    """SELECT e.* FROM evidence e JOIN episode_evidence x
                       ON x.evidence_id=e.id WHERE x.episode_id=?
                       ORDER BY x.position""", (episode_id,),
                ).fetchall()
                value["evidence"] = [self._row(row) for row in rows]
            return value

    def list_episodes(self, owner_id: str, *, limit: int = 100,
                      status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM episodes WHERE owner_id=?"
        params: list[Any] = [owner_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as conn:
            return [self._row(row) or {} for row in conn.execute(sql, params)]

    def close_idle_episodes(self, owner_id: str, *, cutoff: float) -> list[str]:
        now = _now()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT id FROM episodes WHERE owner_id=? AND status='open'
                   AND updated_at<=? AND turn_count>0""", (owner_id, cutoff)
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            for episode_id in ids:
                conn.execute(
                    """UPDATE episodes SET status='closed',ended_at=?,updated_at=?
                       WHERE id=? AND status='open'""",
                    (now, now, episode_id))
            return ids

    def update_episode(self, episode_id: str, **values) -> None:
        allowed = {"status", "title", "summary", "outcome", "entities_json",
                   "attributes_json", "ended_at", "extractor_version"}
        fields = {key: value for key, value in values.items() if key in allowed}
        if not fields:
            return
        for key in ("entities_json", "attributes_json"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = _json(fields[key])
        fields["updated_at"] = _now()
        assignments = ",".join(f"{key}=?" for key in fields)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE episodes SET {assignments} WHERE id=?",
                         (*fields.values(), episode_id))

    def create_memory(self, owner_id: str, kind: str, content: str, *,
                      authority: str = "inferred", confidence: float = 0.5,
                      status: str = "active", entities: list | None = None,
                      attributes: dict | None = None, tags: list | None = None,
                      sources: list[dict] | None = None,
                      valid_from: float | None = None) -> dict:
        memory_id, now = _id("mem"), _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO memories
                   (id,owner_id,kind,content,status,authority,confidence,
                    entities_json,attributes_json,tags_json,valid_from,
                    created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (memory_id, owner_id, kind, content, status, authority,
                 max(0.0, min(1.0, confidence)), _json(entities or []),
                 _json(attributes or {}), _json(tags or []), valid_from or now, now, now),
            )
            conn.execute(
                "INSERT INTO memory_fts(memory_id,owner_id,kind,content) VALUES (?,?,?,?)",
                (memory_id, owner_id, kind, content),
            )
            for source in sources or []:
                conn.execute(
                    """INSERT OR IGNORE INTO memory_sources
                       (memory_id,source_type,source_id,relation) VALUES (?,?,?,?)""",
                    (memory_id, source["source_type"], source["source_id"],
                     source.get("relation", "supports")),
                )
        self.audit(owner_id, "create", "memory", memory_id,
                   {"authority": authority, "status": status})
        return self.memory(memory_id) or {}

    def memory(self, memory_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            value = self._row(conn.execute(
                "SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())
            if value:
                value["sources"] = [dict(row) for row in conn.execute(
                    "SELECT source_type,source_id,relation FROM memory_sources "
                    "WHERE memory_id=?", (memory_id,))]
                value["relations"] = [self._row(row) for row in conn.execute(
                    "SELECT * FROM relations WHERE from_id=? OR to_id=?",
                    (memory_id, memory_id))]
            return value

    def list_memories(self, owner_id: str, *, limit: int = 100, offset: int = 0,
                      status: str | None = None, kind: str | None = None,
                      query: str | None = None) -> list[dict]:
        if query:
            rows = [item["memory"] for item in self.lexical_search(
                owner_id, query, limit=limit + offset, include_status=status,
                kind=kind)]
            return rows[offset:offset + limit]
        sql, params = "SELECT * FROM memories WHERE owner_id=?", [owner_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend((limit, offset))
        with self._lock, self._connect() as conn:
            return [self._row(row) or {} for row in conn.execute(sql, params)]

    def memory_sources(self, memory_ids: list[str]) -> dict[str, list[dict]]:
        if not memory_ids:
            return {}
        unique_ids = list(dict.fromkeys(memory_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        grouped: dict[str, list[dict]] = {memory_id: [] for memory_id in unique_ids}
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""SELECT memory_id,source_type,source_id,relation
                    FROM memory_sources WHERE memory_id IN ({placeholders})
                    ORDER BY memory_id,source_type,source_id""",
                unique_ids,
            ).fetchall()
        for row in rows:
            grouped[str(row["memory_id"])].append({
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "relation": row["relation"],
            })
        return grouped

    def update_memory(self, memory_id: str, *, content: str | None = None,
                      status: str | None = None, kind: str | None = None,
                      confidence: float | None = None, authority: str | None = None,
                      attributes: dict | None = None, tags: list | None = None) -> dict | None:
        fields: dict[str, Any] = {"updated_at": _now()}
        for key, value in (("content", content), ("status", status), ("kind", kind),
                           ("confidence", confidence), ("authority", authority)):
            if value is not None:
                fields[key] = value
        if attributes is not None:
            fields["attributes_json"] = _json(attributes)
        if tags is not None:
            fields["tags_json"] = _json(tags)
        with self._lock, self._connect() as conn:
            if conn.execute("SELECT 1 FROM memories WHERE id=?", (memory_id,)).fetchone() is None:
                return None
            conn.execute(
                f"UPDATE memories SET {','.join(f'{key}=?' for key in fields)} WHERE id=?",
                (*fields.values(), memory_id),
            )
            if content is not None:
                conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
                row = conn.execute(
                    "SELECT owner_id,kind,content FROM memories WHERE id=?", (memory_id,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO memory_fts(memory_id,owner_id,kind,content) VALUES (?,?,?,?)",
                    (memory_id, row["owner_id"], row["kind"], row["content"]),
                )
        return self.memory(memory_id)

    def supersede_memory(self, memory_id: str, owner_id: str, content: str,
                         *, kind: str | None = None) -> dict:
        old = self.memory(memory_id)
        if not old or old["owner_id"] != owner_id:
            raise KeyError(memory_id)
        new = self.create_memory(
            owner_id, kind or old["kind"], content, authority="confirmed",
            confidence=1.0, attributes=old.get("attributes"), tags=old.get("tags"),
            sources=[{"source_type": "memory", "source_id": memory_id,
                      "relation": "supersedes"}],
        )
        self.update_memory(memory_id, status="superseded")
        self.add_relation("memory", new["id"], "supersedes", "memory", memory_id)
        return self.memory(new["id"]) or new

    def delete_memory(self, memory_id: str, owner_id: str) -> bool:
        item = self.memory(memory_id)
        if not item or item["owner_id"] != owner_id:
            return False
        self.update_memory(memory_id, status="deleted")
        self.audit(owner_id, "delete", "memory", memory_id)
        return True

    def lexical_search(self, owner_id: str, query: str, *, limit: int = 20,
                       include_status: str | None = "active",
                       kind: str | None = None) -> list[dict]:
        tokens = [token.replace('"', "") for token in query.split() if token.strip()]
        expression = " OR ".join(f'"{token}"' for token in tokens[:20])
        if not expression:
            return []
        status_clause = " AND m.status=?" if include_status else ""
        kind_clause = " AND m.kind=?" if kind else ""
        params: list[Any] = [expression, owner_id]
        if include_status:
            params.append(include_status)
        if kind:
            params.append(kind)
        params.append(limit)
        with self._lock, self._connect() as conn:
            try:
                rows = conn.execute(
                    f"""SELECT m.*, bm25(memory_fts) AS lexical_rank
                        FROM memory_fts JOIN memories m ON m.id=memory_fts.memory_id
                        WHERE memory_fts MATCH ? AND m.owner_id=?
                        {status_clause} {kind_clause}
                        ORDER BY lexical_rank LIMIT ?""", params,
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            return [{"memory": self._row(row) or {},
                     "score": 1.0 / (1.0 + abs(float(row["lexical_rank"]))),
                     "channel": "lexical"} for row in rows]

    def add_relation(self, from_type: str, from_id: str, relation: str,
                     to_type: str, to_id: str, metadata: dict | None = None) -> str:
        relation_id = _id("rel")
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO relations
                   (id,from_type,from_id,relation,to_type,to_id,metadata_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (relation_id, from_type, from_id, relation, to_type, to_id,
                 _json(metadata or {}), _now()),
            )
        return relation_id

    def create_artifact(self, owner_id: str, kind: str, title: str, payload: dict,
                        source_episode_ids: list[str], *, model: str = "",
                        status: str = "pending_review") -> dict:
        artifact_id, now = _id("art"), _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO artifacts
                   (id,owner_id,kind,status,title,payload_json,source_episode_ids_json,
                    model,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (artifact_id, owner_id, kind, status, title, _json(payload),
                 _json(source_episode_ids), model, now, now),
            )
        self.audit(owner_id, "create", "artifact", artifact_id, {"kind": kind})
        return self.artifact(artifact_id) or {}

    def artifact(self, artifact_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            return self._row(conn.execute(
                "SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone())

    def list_artifacts(self, owner_id: str, *, kind: str | None = None,
                       status: str | None = None, limit: int = 100) -> list[dict]:
        sql, params = "SELECT * FROM artifacts WHERE owner_id=?", [owner_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as conn:
            return [self._row(row) or {} for row in conn.execute(sql, params)]

    def update_artifact(self, artifact_id: str, *, status: str | None = None,
                        payload: dict | None = None) -> dict | None:
        fields: dict[str, Any] = {"updated_at": _now()}
        if status is not None:
            fields["status"] = status
        if payload is not None:
            fields["payload_json"] = _json(payload)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE artifacts SET {','.join(f'{key}=?' for key in fields)} WHERE id=?",
                (*fields.values(), artifact_id),
            )
        return self.artifact(artifact_id)

    def enqueue(self, kind: str, payload: dict, *, run_after: float | None = None) -> str:
        job_id, now = _id("job"), _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO jobs
                   (id,kind,payload_json,run_after,created_at,updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (job_id, kind, _json(payload), run_after or now, now, now),
            )
        return job_id

    def claim_job(self) -> dict | None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM jobs WHERE status='queued' AND run_after<=?
                   ORDER BY created_at LIMIT 1""", (_now(),),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE jobs SET status='running',attempts=attempts+1,updated_at=? "
                    "WHERE id=?", (_now(), row["id"]),
                )
            conn.execute("COMMIT")
            return self._row(row)

    def recover_running_jobs(self) -> int:
        """Requeue jobs orphaned by shutdown, crash, or worker cancellation."""
        now = _now()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """UPDATE jobs SET status='queued',run_after=?,updated_at=?,
                   last_error=CASE
                     WHEN last_error='' THEN 'recovered after interrupted worker'
                     ELSE last_error
                   END
                   WHERE status='running'""",
                (now, now),
            )
            return max(0, int(cursor.rowcount))

    def finish_job(self, job_id: str, *, error: str | None = None,
                   retry_seconds: float | None = None) -> None:
        with self._lock, self._connect() as conn:
            if error and retry_seconds is not None:
                conn.execute(
                    """UPDATE jobs SET status='queued',last_error=?,run_after=?,updated_at=?
                       WHERE id=?""",
                    (error[:1000], _now() + retry_seconds, _now(), job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status=?,last_error=?,updated_at=? WHERE id=?",
                    ("failed" if error else "done", (error or "")[:1000], _now(), job_id),
                )

    def list_jobs(self, *, limit: int = 100) -> list[dict]:
        with self._lock, self._connect() as conn:
            return [self._row(row) or {} for row in conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def log_recall(self, owner_id: str, session_id: str, query: str,
                   results: list[dict], latency_ms: float, status: str) -> str:
        recall_id = _id("recall")
        safe = [{"id": item.get("id"), "score": item.get("score"),
                 "channels": item.get("channels", [])} for item in results]
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO recall_logs
                   (id,owner_id,session_id,query,results_json,latency_ms,status,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (recall_id, owner_id, session_id, query[:2000], _json(safe),
                 latency_ms, status, _now()),
            )
        return recall_id

    def recent_recalls(self, owner_id: str, *, limit: int = 100) -> list[dict]:
        with self._lock, self._connect() as conn:
            return [self._row(row) or {} for row in conn.execute(
                "SELECT * FROM recall_logs WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                (owner_id, limit))]

    def audit(self, owner_id: str, action: str, target_type: str, target_id: str,
              details: dict | None = None) -> str:
        audit_id = _id("audit")
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO audit
                   (id,owner_id,action,target_type,target_id,details_json,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (audit_id, owner_id, action, target_type, target_id,
                 _json(details or {}), _now()),
            )
        return audit_id

    def audits(self, owner_id: str, *, limit: int = 100) -> list[dict]:
        with self._lock, self._connect() as conn:
            return [self._row(row) or {} for row in conn.execute(
                "SELECT * FROM audit WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                (owner_id, limit))]

    def stats(self, owner_id: str) -> dict:
        with self._lock, self._connect() as conn:
            def count(table: str, where: str = "owner_id=?") -> int:
                return int(conn.execute(
                    f"SELECT count(*) AS n FROM {table} WHERE {where}",
                    (owner_id,) if "?" in where else (),
                ).fetchone()["n"])
            queued = conn.execute(
                "SELECT count(*) AS n FROM jobs WHERE status IN ('queued','running')"
            ).fetchone()["n"]
            return {
                "memories": count("memories", "owner_id=? AND status='active'"),
                "episodes": count("episodes"),
                "pending_artifacts": count(
                    "artifacts", "owner_id=? AND status='pending_review'"),
                "queued_jobs": int(queued),
            }
