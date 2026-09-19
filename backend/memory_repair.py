"""Narrow frozen-input contract for the September historical memory repair.

Generation lives outside this module. `stage_repair` accepts the engine's fully
verified create_memory kwargs, never raw model candidates. No provider calls or
writes are made while staging; apply uses exactly one caller-owned transaction.
"""
from __future__ import annotations

import hashlib
import json
import math

from .memory_store import _id, _json, _now


class RepairConflict(ValueError):
    """The target needs manual review; no repair writes were committed."""


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def read_source(conn, owner_id: str, episode_id: str) -> dict:
    def rows(sql, params=()):
        return [dict(row) for row in conn.execute(sql, params)]

    episode = rows("SELECT * FROM episodes WHERE id=?", (episode_id,))
    if not episode or episode[0]["owner_id"] != owner_id:
        raise RepairConflict("episode missing or owner mismatch")
    evidence = rows(
        "SELECT e.*,x.position FROM episode_evidence x LEFT JOIN evidence e "
        "ON e.id=x.evidence_id WHERE x.episode_id=? ORDER BY x.position,e.id",
        (episode_id,))
    memories = rows(
        "SELECT m.* FROM memories m WHERE m.id IN "
        "(SELECT memory_id FROM memory_sources WHERE source_type='episode' AND source_id=? "
        "UNION SELECT memory_id FROM memory_sources WHERE source_type='evidence' "
        "AND source_id IN (SELECT evidence_id FROM episode_evidence WHERE episode_id=?)) "
        "ORDER BY m.id",
        (episode_id, episode_id))
    sources = []
    for memory in memories:
        sources.extend(rows("SELECT * FROM memory_sources WHERE memory_id=? "
                            "ORDER BY source_type,source_id,relation", (memory["id"],)))
    jobs = rows(
        "SELECT * FROM jobs WHERE json_extract(payload_json,'$.episode_id')=? "
        "ORDER BY id", (episode_id,))
    # Owner-wide source-less rows cannot safely be attributed to this episode.
    # Scan sources once rather than seeking once per owner memory.
    orphans = rows(
        "SELECT id FROM memories WHERE owner_id=? EXCEPT "
        "SELECT memory_id FROM memory_sources ORDER BY 1",
        (owner_id,))
    artifacts = rows(
        "SELECT a.* FROM artifacts a WHERE EXISTS "
        "(SELECT 1 FROM json_each(a.source_episode_ids_json) WHERE value=?) ORDER BY id",
        (episode_id,))
    active = rows(
        "SELECT id,kind,owner_id FROM jobs WHERE status IN ('queued','running') AND "
        "(json_extract(payload_json,'$.episode_id')=? OR "
        "EXISTS (SELECT 1 FROM json_each(jobs.payload_json,'$.episode_ids') WHERE value=?) OR "
        "(owner_id=? AND kind='cross_episode_dream')) "
        "ORDER BY id", (episode_id, episode_id, owner_id))
    reasons = []
    if episode[0]["status"] == "open":
        reasons.append("episode_open")
    if not evidence or any(not e["id"] or e["owner_id"] != owner_id
                           or not e["content"].strip() for e in evidence):
        reasons.append("missing_or_foreign_evidence")
    if any(m["owner_id"] != owner_id for m in memories):
        reasons.append("foreign_memory")
    if any(j["owner_id"] != owner_id for j in jobs):
        reasons.append("foreign_job")
    if active:
        reasons.append("active_jobs")
    if orphans:
        reasons.append("unattributed_memories")
    return {"owner_id": owner_id, "episode_id": episode_id, "episode": episode[0],
            "evidence": evidence, "memories": memories, "sources": sources,
            "jobs": jobs, "artifacts": artifacts, "orphans": orphans,
            "active_jobs": active, "blocked": reasons}


def check_target(source: dict, mode: str, job_ids: list[str]) -> None:
    if mode not in {"summary-only", "full"}:
        raise RepairConflict("explicit mode must be summary-only or full")
    if source["blocked"]:
        raise RepairConflict(",".join(source["blocked"]))
    if not job_ids or len(job_ids) != len(set(job_ids)):
        raise RepairConflict("explicit unique historical job IDs required")
    jobs = {j["id"]: j for j in source["jobs"]}
    if any(j not in jobs or jobs[j]["status"] not in {"failed", "done"}
           or jobs[j]["kind"] != "consolidate_episode" for j in job_ids):
        raise RepairConflict("historical consolidation job missing or no longer terminal")
    if source["episode"]["summary"].strip():
        raise RepairConflict("existing summary requires manual review")
    if mode == "full" and (source["memories"] or source["artifacts"]
                           or source["episode"]["title"].strip()
                           or source["episode"]["extractor_version"].strip()
                           or json.loads(source["episode"]["entities_json"])
                           or json.loads(source["episode"]["attributes_json"])):
        raise RepairConflict("partial existing products require manual review")
    if mode == "summary-only" and source["artifacts"]:
        raise RepairConflict("existing artifacts require manual review")


def validate_result(source: dict, mode: str, result: dict) -> None:
    """Validate persistence shape, not duplicate the engine's semantic verifier."""
    if set(result) != {"episode", "memories"}:
        raise RepairConflict("expected prepared episode and memories")
    episode = result["episode"]
    allowed = {"title", "summary", "outcome", "entities_json", "attributes_json",
               "extractor_version"}
    if not isinstance(episode, dict) or not set(episode) <= allowed:
        raise RepairConflict("invalid episode fields")
    if any(not isinstance(episode.get(k), str) or not episode[k].strip()
           for k in ("title", "summary")):
        raise RepairConflict("nonempty title and summary required")
    for key in ("outcome", "extractor_version"):
        if key in episode and not isinstance(episode[key], str):
            raise RepairConflict("invalid episode text")
    for key, kind in (("entities_json", list), ("attributes_json", dict)):
        if key in episode and not isinstance(episode[key], kind):
            raise RepairConflict("invalid episode metadata")
    memories = result["memories"]
    if not isinstance(memories, list) or (mode == "summary-only" and memories):
        raise RepairConflict("summary-only must not contain memory writes")
    evidence_ids = {e["id"] for e in source["evidence"]}
    seen = set()
    for memory in memories:
        allowed = {"kind", "content", "authority", "confidence", "status", "entities",
                   "attributes", "tags", "sources", "valid_from"}
        if not isinstance(memory, dict) or not set(memory) <= allowed:
            raise RepairConflict("invalid prepared memory fields")
        if memory.get("kind") not in {"fact", "preference", "decision", "state", "episode"}:
            raise RepairConflict("invalid prepared memory kind")
        if not isinstance(memory.get("content"), str) or not memory["content"].strip():
            raise RepairConflict("empty prepared memory")
        identity = (memory["kind"], " ".join(memory["content"].casefold().split()))
        if identity in seen:
            raise RepairConflict("duplicate prepared memory")
        seen.add(identity)
        if memory.get("authority") != "inferred" or memory.get("status") not in {
                "active", "pending_review"}:
            raise RepairConflict("invalid generated authority/status")
        confidence = memory.get("confidence")
        if type(confidence) not in {int, float} or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise RepairConflict("invalid confidence")
        for key, kind in (("attributes", dict), ("entities", list), ("tags", list)):
            if key in memory and not isinstance(memory[key], kind):
                raise RepairConflict("invalid prepared memory metadata")
        if not isinstance(memory.get("attributes", {}).get("verification"), dict):
            raise RepairConflict("verified preparation required")
        if "valid_from" in memory and (type(memory["valid_from"]) not in {int, float}
                                       or not math.isfinite(memory["valid_from"])):
            raise RepairConflict("invalid valid_from")
        sources = memory.get("sources")
        if not isinstance(sources, list) or not sources:
            raise RepairConflict("missing sources")
        has_episode = has_evidence = False
        for item in sources:
            if not isinstance(item, dict) or set(item) != {"source_type", "source_id", "relation"}:
                raise RepairConflict("invalid source")
            if item["source_type"] == "episode":
                if item["source_id"] != source["episode_id"] or item["relation"] != "derived_from":
                    raise RepairConflict("foreign episode source")
                has_episode = True
            elif item["source_type"] == "evidence":
                if item["source_id"] not in evidence_ids or item["relation"] != "supports":
                    raise RepairConflict("foreign evidence source")
                has_evidence = True
            else:
                raise RepairConflict("unsupported source type")
        if not has_episode or not has_evidence:
            raise RepairConflict("episode and evidence provenance required")


def stage_repair(source: dict, *, mode: str, job_ids: list[str], result: dict) -> dict:
    """Freeze engine-prepared output on train-A; never call a model on apply.

    `result.memories` must be the accepted verifier output expressed as
    MemoryStore.create_memory kwargs (without owner_id), including verification
    attributes and sources. The engine adapter is deliberately a separate step.
    """
    check_target(source, mode, job_ids)
    validate_result(source, mode, result)
    source_hash = fingerprint(source)
    value = {"version": 1, "owner_id": source["owner_id"],
             "episode_id": source["episode_id"], "mode": mode,
             "job_ids": sorted(job_ids), "source_fingerprint": source_hash,
             "repair_key": fingerprint([source["owner_id"], source["episode_id"], source_hash]),
             "result": result}
    value["prepared_fingerprint"] = fingerprint(value)
    # Detach from mutable generator output before it is persisted/reused.
    return json.loads(_json(value))


def apply_prepared(store, conn, prepared: dict) -> dict:
    value = dict(prepared)
    digest = value.pop("prepared_fingerprint", None)
    if value.get("version") != 1 or fingerprint(value) != digest:
        raise RepairConflict("frozen result fingerprint mismatch")
    owner, episode_id = value["owner_id"], value["episode_id"]
    key = fingerprint([owner, episode_id, value["source_fingerprint"]])
    if value["repair_key"] != key:
        raise RepairConflict("repair key mismatch")
    receipt = conn.execute("SELECT * FROM memory_repair_receipts WHERE repair_key=?", (key,)).fetchone()
    if receipt:
        if receipt["prepared_fingerprint"] != digest:
            raise RepairConflict("different prepared result already applied")
        return json.loads(receipt["result_json"])
    source = read_source(conn, owner, episode_id)
    if fingerprint(source) != value["source_fingerprint"]:
        raise RepairConflict("source snapshot changed")
    check_target(source, value["mode"], value["job_ids"])
    validate_result(source, value["mode"], value["result"])
    # Commit-time exact guard, not a replacement for generation-side fuzzy dedup.
    # Other episodes are outside the source fingerprint; check every status before
    # any write, including deleted facts that must never be silently recreated.
    candidates = {(m["kind"], " ".join(m["content"].casefold().split()))
                  for m in value["result"]["memories"]}
    kinds = sorted({kind for kind, _ in candidates})
    if kinds:
        rows = conn.execute(
            "SELECT kind,content FROM memories WHERE owner_id=? AND kind IN ("
            + ",".join("?" for _ in kinds) + ")", (owner, *kinds))
        for row in rows:
            if (row["kind"], " ".join(row["content"].casefold().split())) in candidates:
                raise RepairConflict("duplicate existing memory; owner dependencies changed")
    now, memory_ids, index_job_ids = _now(), [], []
    for memory in value["result"]["memories"]:
        memory_id = store._insert_memory(conn, owner, **memory)
        memory_ids.append(memory_id)
        if memory["status"] == "active":
            job_id = _id("job")
            store._insert_job(conn, job_id, "reindex_memory", {"memory_id": memory_id}, owner, now, now)
            index_job_ids.append(job_id)
    episode = dict(value["result"]["episode"])
    # Outcome is recorded during normal turns/closure, before generation. It is
    # authoritative, not evidence of a partial extraction. Summary-only may also
    # encounter reviewed metadata; never replace those non-default fields.
    existing = source["episode"]
    for field in ("title", "outcome", "extractor_version"):
        if existing[field].strip() and (field != "outcome" or existing[field] != "unknown"):
            episode.pop(field, None)
    for field in ("entities_json", "attributes_json"):
        if json.loads(existing[field]):
            episode.pop(field, None)
    for field in ("entities_json", "attributes_json"):
        if field in episode:
            episode[field] = _json(episode[field])
    episode["updated_at"] = now
    conn.execute("UPDATE episodes SET " + ",".join(f"{k}=?" for k in episode) + " WHERE id=?",
                 (*episode.values(), episode_id))
    result = {"repair_key": key, "owner_id": owner, "episode_id": episode_id,
              "mode": value["mode"], "memory_ids": memory_ids, "index_job_ids": index_job_ids}
    conn.execute("INSERT INTO memory_repair_receipts VALUES (?,?,?,?,?,?,?)",
                 (key, owner, episode_id, value["source_fingerprint"], digest, _json(result), now))
    return result
