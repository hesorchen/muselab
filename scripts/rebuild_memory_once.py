#!/usr/bin/env python3
"""One-off, resumable shadow rebuild for MuseLab Memory.

This script never mutates the source Registry. It verifies the latest Registry
backup, reads historical Episodes through a read-only SQLite connection, runs the
production Dreamer/Verifier prompts, and checkpoints accepted candidates in a
separate shadow SQLite database. A later explicit apply step can consume the
completed shadow run after review.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backend.memory_config import load_config  # noqa: E402
from backend.memory_engine import (  # noqa: E402
    MemoryEngine,
    _redact,
    _unwrap_schema_response,
)
from backend.memory_prompts import (  # noqa: E402
    DREAMER_PROMPT_VERSION,
    DREAMER_SYSTEM,
    VERIFIER_PROMPT_VERSION,
    VERIFIER_SYSTEM,
    dreamer_prompt,
    verifier_prompt,
)
from backend.memory_providers import (  # noqa: E402
    GenerationError,
    GenerationProvider,
)

ALLOWED_KINDS = {"fact", "preference", "decision", "state", "episode"}
UNTESTED_RE = re.compile(
    r"未实测|尚未验证|待验证|候选|untested|not\s+(?:yet\s+)?(?:tested|verified)",
    re.IGNORECASE,
)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(registry: Path, cutoff: float) -> dict[str, Any]:
    digest = hashlib.sha256()
    with sqlite3.connect(f"file:{registry}?mode=ro", uri=True) as conn:
        memory_rows = conn.execute(
            """SELECT id,kind,content,status,authority,confidence,valid_from,
                      valid_to,version,created_at,updated_at
               FROM memories WHERE created_at<=? ORDER BY id""",
            (cutoff,),
        ).fetchall()
        source_rows = conn.execute(
            """SELECT ms.memory_id,ms.source_type,ms.source_id,ms.relation
               FROM memory_sources ms JOIN memories m ON m.id=ms.memory_id
               WHERE m.created_at<=?
               ORDER BY ms.memory_id,ms.source_type,ms.source_id,ms.relation""",
            (cutoff,),
        ).fetchall()
    for row in (*memory_rows, *source_rows):
        digest.update(json_text(list(row)).encode())
        digest.update(b"\n")
    return {
        "cutoff": cutoff,
        "baseline_memories": len(memory_rows),
        "baseline_memory_sources": len(source_rows),
        "sha256": digest.hexdigest(),
    }


def verify_latest_backup(registry: Path) -> dict[str, Any]:
    with sqlite3.connect(f"file:{registry}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT id,filename,sha256,size_bytes,quick_check,counts_json,created_at
               FROM memory_backups ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
    if row is None:
        raise RuntimeError("no verified Memory backup receipt exists")
    receipt = dict(row)
    backup = registry.parent / "backups" / receipt["filename"]
    if not backup.is_file():
        raise RuntimeError(f"backup file is missing: {backup}")
    if stat.S_IMODE(backup.stat().st_mode) != 0o600:
        raise RuntimeError("backup file mode is not 0600")
    if backup.stat().st_size != int(receipt["size_bytes"]):
        raise RuntimeError("backup size does not match receipt")
    if sha256_file(backup) != receipt["sha256"]:
        raise RuntimeError("backup SHA-256 does not match receipt")
    with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as conn:
        quick = [item[0] for item in conn.execute("PRAGMA quick_check")]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if quick != ["ok"] or receipt["quick_check"] != "ok" or foreign_keys:
        raise RuntimeError("backup SQLite verification failed")
    return {
        "id": receipt["id"],
        "filename": receipt["filename"],
        "sha256": receipt["sha256"],
        "size_bytes": receipt["size_bytes"],
        "created_at": receipt["created_at"],
    }


def init_shadow(path: Path, metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS run_meta (
              key TEXT PRIMARY KEY,
              value_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS episode_results (
              episode_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '',
              old_memory_count INTEGER NOT NULL DEFAULT 0,
              dreamer_candidate_count INTEGER NOT NULL DEFAULT 0,
              accepted_count INTEGER NOT NULL DEFAULT 0,
              rejected_count INTEGER NOT NULL DEFAULT 0,
              result_json TEXT NOT NULL DEFAULT '{}',
              error_category TEXT NOT NULL DEFAULT '',
              attempts INTEGER NOT NULL DEFAULT 0,
              started_at REAL,
              finished_at REAL,
              updated_at REAL NOT NULL
            );
        """)
        for key, value in metadata.items():
            existing = conn.execute(
                "SELECT value_json FROM run_meta WHERE key=?", (key,)).fetchone()
            encoded = json_text(value)
            if existing is not None and existing[0] != encoded:
                raise RuntimeError(f"shadow run metadata mismatch: {key}")
            conn.execute(
                "INSERT OR IGNORE INTO run_meta(key,value_json) VALUES (?,?)",
                (key, encoded),
            )
    os.chmod(path, 0o600)


def select_episode_ids(
    registry: Path,
    requested: list[str],
    limit: int | None,
) -> list[str]:
    with sqlite3.connect(f"file:{registry}?mode=ro", uri=True) as conn:
        if requested:
            placeholders = ",".join("?" for _ in requested)
            found = {
                row[0] for row in conn.execute(
                    f"SELECT id FROM episodes WHERE id IN ({placeholders})",
                    requested,
                )
            }
            missing = [episode_id for episode_id in requested if episode_id not in found]
            if missing:
                raise RuntimeError(f"unknown Episode ids: {missing}")
            return list(dict.fromkeys(requested))
        sql = """SELECT id FROM episodes
                 WHERE status='closed' AND outcome='success'
                   AND EXISTS (SELECT 1 FROM episode_evidence ee
                               WHERE ee.episode_id=episodes.id)
                 ORDER BY started_at,id"""
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        return [row[0] for row in conn.execute(sql, params)]


def load_episode(registry: Path, episode_id: str, owner_id: str) -> dict[str, Any]:
    with sqlite3.connect(f"file:{registry}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        episode = conn.execute(
            "SELECT * FROM episodes WHERE id=? AND owner_id=?",
            (episode_id, owner_id),
        ).fetchone()
        if episode is None:
            raise RuntimeError(f"Episode is missing or owner-fenced: {episode_id}")
        evidence = [dict(row) for row in conn.execute(
            """SELECT e.id,e.role,e.event_type,e.content,e.metadata_json
               FROM episode_evidence ee JOIN evidence e ON e.id=ee.evidence_id
               WHERE ee.episode_id=? AND e.owner_id=? ORDER BY ee.position""",
            (episode_id, owner_id),
        )]
        old_memories = [dict(row) for row in conn.execute(
            """SELECT DISTINCT m.id,m.kind,m.content,m.authority,m.confidence
               FROM memory_sources ms JOIN memories m ON m.id=ms.memory_id
               WHERE ms.source_type='episode' AND ms.source_id=?
                 AND m.owner_id=? AND m.status='active'
               ORDER BY m.created_at""",
            (episode_id, owner_id),
        )]
    clean_evidence = []
    for row in evidence:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        clean_evidence.append({
            "id": row["id"],
            "role": row["role"],
            "event_type": row["event_type"],
            "content": _redact(row["content"], 8000),
            "metadata": metadata,
        })
    return {
        "episode": dict(episode),
        "evidence": clean_evidence,
        "old_memories": old_memories,
    }


async def complete_json_retry(
    provider: GenerationProvider,
    system: str,
    prompt: str,
    attempts: int,
) -> tuple[dict[str, Any], int]:
    last: GenerationError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await provider.complete_json(system, prompt), attempt
        except GenerationError as exc:
            last = exc
            if attempt >= attempts or not (
                exc.retryable or exc.category == "malformed_response"
            ):
                raise
    assert last is not None
    raise last


def validate_verification(
    candidate: dict[str, Any],
    verification: dict[str, Any],
    visible_source_ids: set[str],
) -> tuple[bool, str, str]:
    kind = str(candidate.get("kind", ""))
    if kind not in ALLOWED_KINDS:
        return False, "invalid_kind", ""
    decision = verification.get("decision")
    final_content = " ".join(str(
        verification.get("final_content", "")).split())[:3000]
    supported_claims = verification.get("supported_claims")
    unsupported_claims = verification.get("unsupported_claims")
    removed_claims = verification.get("removed_claims")
    if (decision not in {"accept", "rewrite"}
            or not final_content or len(final_content) > 550
            or verification.get("supported") is not True
            or verification.get("conflict") is True
            or verification.get("self_contained") is not True
            or verification.get("specific") is not True
            or verification.get("durable") is not True
            or verification.get("generic") is True
            or not isinstance(supported_claims, list) or not supported_claims
            or unsupported_claims != []
            or not isinstance(removed_claims, list)):
        return False, "verifier_rejected", ""
    has_untested = False
    for claim in supported_claims:
        if not isinstance(claim, dict):
            return False, "invalid_claim_ledger", ""
        source_ids = claim.get("source_ids")
        runtime_status = claim.get("runtime_status")
        if (not str(claim.get("claim", "")).strip()
                or not isinstance(source_ids, list) or not source_ids
                or any(not isinstance(source_id, str)
                       or source_id not in visible_source_ids
                       for source_id in source_ids)
                or claim.get("evidence_type") not in {"direct", "derived"}
                or runtime_status not in {
                    "verified", "untested", "not_applicable"}):
            return False, "invalid_claim_ledger", ""
        has_untested = has_untested or runtime_status == "untested"
    if has_untested and not UNTESTED_RE.search(final_content):
        return False, "missing_untested_marker", ""
    candidate_content = " ".join(str(candidate.get("content", "")).split())
    if decision == "accept":
        if (verification.get("rewrite_required") is True
                or final_content != candidate_content):
            return False, "invalid_accept_contract", ""
    else:
        rewritten = " ".join(str(
            verification.get("rewritten_content", "")).split())[:3000]
        if verification.get("rewrite_required") is not True or rewritten != final_content:
            return False, "invalid_rewrite_contract", ""
    if MemoryEngine._memory_quality_issue(final_content, kind):
        return False, "deterministic_quality_gate", ""
    return True, "accepted", final_content


async def rebuild_episode(
    registry: Path,
    shadow: Path,
    episode_id: str,
    provider: GenerationProvider,
    owner_id: str,
    attempts: int,
    semaphore: asyncio.Semaphore,
) -> None:
    now = time.time()
    with sqlite3.connect(shadow, timeout=30) as conn:
        row = conn.execute(
            "SELECT status FROM episode_results WHERE episode_id=?", (episode_id,)
        ).fetchone()
        if row is not None and row[0] == "completed":
            return
        conn.execute(
            """INSERT INTO episode_results(episode_id,status,attempts,started_at,updated_at)
               VALUES (?,'running',1,?,?)
               ON CONFLICT(episode_id) DO UPDATE SET status='running',
                 error_category='',attempts=episode_results.attempts+1,
                 started_at=excluded.started_at,updated_at=excluded.updated_at""",
            (episode_id, now, now),
        )
    try:
        sample = load_episode(registry, episode_id, owner_id)
        evidence = sample["evidence"]
        evidence_ids = {row["id"] for row in evidence}
        async with semaphore:
            dream_result, dream_attempts = await complete_json_retry(
                provider,
                DREAMER_SYSTEM,
                dreamer_prompt(sample["episode"], evidence),
                attempts,
            )
        dream_result = _unwrap_schema_response(dream_result, "memories")
        raw_candidates = [row for row in dream_result.get("memories", [])
                          if isinstance(row, dict)]
        candidates = raw_candidates[:3]
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = [
            {"kind": row.get("kind"), "reason": "dreamer_candidate_overflow"}
            for row in raw_candidates[3:]
        ]
        by_id = {row["id"]: row for row in evidence}
        provider_attempts = dream_attempts
        for candidate in candidates:
            try:
                future_use = float(candidate.get("future_use", 0) or 0)
            except (TypeError, ValueError):
                future_use = 0.0
            if future_use < 0.35:
                rejected.append({"kind": candidate.get("kind"),
                                 "reason": "low_future_use"})
                continue
            source_ids = list(dict.fromkeys(
                source_id for source_id in candidate.get("source_ids", [])
                if source_id in evidence_ids
            ))
            if not source_ids:
                rejected.append({"reason": "no_valid_sources"})
                continue
            sources = [{
                "id": source_id,
                "role": by_id[source_id]["role"],
                "content": _redact(by_id[source_id]["content"], 4000),
            } for source_id in source_ids]
            async with semaphore:
                verification, used_attempts = await complete_json_retry(
                    provider,
                    VERIFIER_SYSTEM,
                    verifier_prompt(candidate, sources, sample["old_memories"]),
                    attempts,
                )
            provider_attempts += used_attempts
            verification = _unwrap_schema_response(verification, "supported")
            ok, reason, final_content = validate_verification(
                candidate, verification, set(source_ids))
            if not ok:
                rejected.append({
                    "kind": candidate.get("kind"),
                    "reason": reason,
                    "verifier_decision": verification.get("decision"),
                })
                continue
            duplicate = next((row for row in accepted
                              if difflib.SequenceMatcher(
                                  None, final_content.casefold(),
                                  row["content"].casefold()).ratio() >= 0.92), None)
            if duplicate is not None:
                rejected.append({"kind": candidate.get("kind"),
                                 "reason": "duplicate_in_episode"})
                continue
            accepted.append({
                "kind": candidate["kind"],
                "content": final_content,
                "source_ids": source_ids,
                "confidence": candidate.get("confidence"),
                "future_use": candidate.get("future_use"),
                "reuse_conditions": candidate.get("reuse_conditions", []),
                "attributed_to": candidate.get("attributed_to", "derived"),
                "verification": verification,
                "dreamer_prompt_version": DREAMER_PROMPT_VERSION,
                "verifier_prompt_version": VERIFIER_PROMPT_VERSION,
            })
        result = {
            "episode": {
                "id": episode_id,
                "title": sample["episode"]["title"],
                "summary": dream_result.get("episode", {}).get("summary", ""),
            },
            "old_memories": sample["old_memories"],
            "accepted": accepted,
            "rejected": rejected,
            "provider_attempts": provider_attempts,
        }
        finished = time.time()
        with sqlite3.connect(shadow, timeout=30) as conn:
            conn.execute(
                """UPDATE episode_results SET status='completed',title=?,
                   old_memory_count=?,dreamer_candidate_count=?,accepted_count=?,
                   rejected_count=?,result_json=?,finished_at=?,updated_at=?
                   WHERE episode_id=?""",
                (
                    sample["episode"]["title"], len(sample["old_memories"]),
                    len(raw_candidates), len(accepted), len(rejected),
                    json_text(result), finished, finished, episode_id,
                ),
            )
    except Exception as exc:
        category = exc.category if isinstance(exc, GenerationError) else type(exc).__name__
        with sqlite3.connect(shadow, timeout=30) as conn:
            conn.execute(
                """UPDATE episode_results SET status='failed',error_category=?,
                   finished_at=?,updated_at=? WHERE episode_id=?""",
                (str(category)[:120], time.time(), time.time(), episode_id),
            )


def build_report(shadow: Path, before: dict[str, Any], after: dict[str, Any]) -> dict:
    with sqlite3.connect(shadow, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM episode_results ORDER BY episode_id")]
    accepted_contents = []
    for row in rows:
        try:
            result = json.loads(row["result_json"])
        except json.JSONDecodeError:
            result = {}
        accepted_contents.extend(
            item.get("content", "") for item in result.get("accepted", []))
    lengths = [len(item) for item in accepted_contents]
    return {
        "episodes": len(rows),
        "completed": sum(row["status"] == "completed" for row in rows),
        "failed": sum(row["status"] == "failed" for row in rows),
        "dreamer_candidates": sum(row["dreamer_candidate_count"] for row in rows),
        "accepted_memories": sum(row["accepted_count"] for row in rows),
        "rejected_candidates": sum(row["rejected_count"] for row in rows),
        "accepted_length": {
            "min": min(lengths) if lengths else 0,
            "max": max(lengths) if lengths else 0,
            "average": round(sum(lengths) / len(lengths), 1) if lengths else 0,
        },
        "source_registry_unchanged": before == after,
        "source_before": before,
        "source_after": after,
        "rows": [{
            "episode_id": row["episode_id"], "title": row["title"],
            "status": row["status"], "old_memory_count": row["old_memory_count"],
            "candidates": row["dreamer_candidate_count"],
            "accepted": row["accepted_count"], "rejected": row["rejected_count"],
            "error_category": row["error_category"],
        } for row in rows],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path(os.environ.get(
            "MUSELAB_MEMORY_DIR",
            REPO / ".muselab" / "memory")) / "registry.sqlite3",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--episode-id", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--attempts", type=int, default=3)
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    os.umask(0o077)
    registry = args.registry.expanduser().resolve()
    if not registry.is_file():
        raise RuntimeError(f"Registry does not exist: {registry}")
    output_dir = (args.output_dir or (
        registry.parent.parent / "rebuild-runs" / f"run-{utc_stamp()}"
    )).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    shadow = output_dir / "shadow.sqlite3"
    config = load_config(fresh=True)
    if config.generation_model != "ducc:deepseek-v4-pro":
        raise RuntimeError(
            f"unexpected generation model: {config.generation_model}")
    if DREAMER_PROMPT_VERSION != "dreamer-v3" or VERIFIER_PROMPT_VERSION != "verifier-v3":
        raise RuntimeError("Memory Prompt v3 is not loaded")
    existing_meta: dict[str, Any] = {}
    if shadow.exists():
        with sqlite3.connect(shadow, timeout=30) as conn:
            try:
                existing_meta = {
                    key: json.loads(value)
                    for key, value in conn.execute(
                        "SELECT key,value_json FROM run_meta")
                }
            except sqlite3.OperationalError:
                existing_meta = {}

    backup = existing_meta.get("backup") or verify_latest_backup(registry)
    episode_ids = existing_meta.get("episode_ids") or select_episode_ids(
        registry, args.episode_id, args.limit)
    source_cutoff = existing_meta.get("source_cutoff")
    if source_cutoff is None:
        source_cutoff = time.time()
        if shadow.exists():
            with sqlite3.connect(shadow, timeout=30) as conn:
                try:
                    first_started = conn.execute(
                        "SELECT MIN(started_at) FROM episode_results"
                    ).fetchone()[0]
                except sqlite3.OperationalError:
                    first_started = None
            if first_started is not None:
                source_cutoff = first_started
    before = existing_meta.get("source_before") or source_fingerprint(
        registry, source_cutoff)
    metadata = {
        "registry": str(registry),
        "owner_id": config.owner_id,
        "model": config.generation_model,
        "dreamer_prompt_version": DREAMER_PROMPT_VERSION,
        "verifier_prompt_version": VERIFIER_PROMPT_VERSION,
        "backup": backup,
        "episode_ids": episode_ids,
        "source_cutoff": source_cutoff,
        "source_before": before,
    }
    init_shadow(shadow, metadata)
    provider = GenerationProvider(config)
    concurrency = max(1, args.concurrency)
    semaphore = asyncio.Semaphore(concurrency)
    queue: asyncio.Queue[str] = asyncio.Queue()
    for episode_id in episode_ids:
        queue.put_nowait(episode_id)

    async def worker() -> None:
        while True:
            try:
                episode_id = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await rebuild_episode(
                    registry, shadow, episode_id, provider, config.owner_id,
                    max(1, args.attempts), semaphore,
                )
            finally:
                queue.task_done()

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    with sqlite3.connect(shadow, timeout=30) as conn:
        shadow_quick_check = [row[0] for row in conn.execute("PRAGMA quick_check")]
    if shadow_quick_check != ["ok"]:
        raise RuntimeError("shadow SQLite quick_check failed")
    for candidate_path in (shadow, Path(f"{shadow}-wal"), Path(f"{shadow}-shm")):
        if candidate_path.exists():
            os.chmod(candidate_path, 0o600)
    after = source_fingerprint(registry, source_cutoff)
    report = build_report(shadow, before, after)
    report["shadow_quick_check"] = "ok"
    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    os.chmod(report_path, 0o600)
    print(json.dumps({
        "output_dir": str(output_dir),
        "shadow": str(shadow),
        "report": str(report_path),
        **{key: report[key] for key in (
            "episodes", "completed", "failed", "dreamer_candidates",
            "accepted_memories", "rejected_candidates",
            "accepted_length", "source_registry_unchanged")},
    }, ensure_ascii=False, indent=2))
    return 0 if report["failed"] == 0 and report["source_registry_unchanged"] else 1


def main() -> int:
    args = parse_args()
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be positive")
    if args.concurrency < 1 or args.attempts < 1:
        raise SystemExit("--concurrency and --attempts must be positive")
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
