"""Authenticated white-box API for memory configuration and governance."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .auth import require_token
from .memory_config import (
    MemoryConfig,
    load_config,
    public_config,
    save_config,
)
from .memory_engine import classify_memory_failure, engine

router = APIRouter(
    prefix="/api/memory", tags=["memory"], dependencies=[Depends(require_token)])

# Statuses a legacy v1 import may land in. Matches memory_engine._MEMORY_STATUSES;
# kept as a local name so the API layer states its own contract explicitly.
_IMPORTABLE_STATUSES = ("active", "pending_review")
# Authorities recall() knows how to weight (see memory_engine._rank); anything
# else imports as "confirmed" rather than silently landing in the 0.8 bucket.
_IMPORTABLE_AUTHORITIES = ("confirmed", "inferred", "legacy_import")


def _failure_detail(exc: BaseException) -> dict[str, object]:
    return classify_memory_failure(exc)[1]


class MemoryCreate(BaseModel):
    kind: str = "fact"
    content: str = Field(min_length=1, max_length=12_000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    source_session_id: str | None = Field(default=None, max_length=128)
    source_message_id: str | None = Field(default=None, max_length=128)
    source_role: str | None = Field(default=None, max_length=32)


class MemoryCorrection(BaseModel):
    content: str = Field(min_length=1, max_length=12_000)
    kind: str | None = None


class SkillApproval(BaseModel):
    markdown: str | None = Field(default=None, max_length=50_000)


class MemoryImportItem(MemoryCreate):
    """An export row. Governance fields are optional so a hand-written
    ``{kind, content}`` list still imports as a user-confirmed memory."""

    status: str | None = None
    authority: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class MemoryImport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    export_schema: str | None = Field(default=None, alias="schema")
    # ``items`` remains the compact/manual compatibility shape.
    items: list[MemoryImportItem] = Field(default_factory=list, max_length=10_000)
    memories: list[dict[str, Any]] = Field(default_factory=list, max_length=100_000)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=100_000)
    episodes: list[dict[str, Any]] = Field(default_factory=list, max_length=100_000)
    episode_evidence: list[dict[str, Any]] = Field(
        default_factory=list, max_length=100_000)
    memory_sources: list[dict[str, Any]] = Field(
        default_factory=list, max_length=100_000)
    relations: list[dict[str, Any]] = Field(default_factory=list, max_length=100_000)
    artifacts: list[dict[str, Any]] = Field(default_factory=list, max_length=100_000)
    audit: list[dict[str, Any]] = Field(default_factory=list, max_length=100_000)


class MemoryFeedback(BaseModel):
    useful: bool
    recall_id: str | None = Field(default=None, max_length=128)


@router.get("/config")
def get_config() -> dict:
    return public_config()


@router.put("/config")
async def put_config(config: dict, probe: bool = Query(default=True)) -> dict:
    current = load_config(fresh=True)
    merged = _resolve_config_input(config, current)
    if merged.enabled and probe:
        try:
            await engine.probe(merged)
        except Exception as exc:
            raise HTTPException(400, _failure_detail(exc)) from None
    save_config(merged)
    await engine.reconfigure()
    return {"config": public_config(merged), "status": engine.status()}


@router.post("/probe")
async def probe_memory(config: dict | None = None) -> dict:
    if config is not None:
        config = _resolve_config_input(config, load_config(fresh=True))
    try:
        return await engine.probe(config)
    except Exception as exc:
        raise HTTPException(400, _failure_detail(exc)) from None


def _resolve_config_input(raw: dict, current: MemoryConfig) -> MemoryConfig:
    """Restore masked credentials before strict capability validation."""
    data = dict(raw)
    for name in ("embedding", "vector", "rerank"):
        section = dict(data.get(name) or {})
        section.pop("has_api_key", None)
        key = section.get("api_key", "")
        if key == "":
            section["api_key"] = getattr(current, name).api_key
        elif key == "_delete_":
            section["api_key"] = ""
        data[name] = section
    vector = data["vector"]
    vector.pop("has_url", None)
    if vector.get("provider") == "pgvector":
        if vector.get("url", "") == "" and current.vector.provider == "pgvector":
            vector["url"] = current.vector.url
        elif vector.get("url") == "_delete_":
            vector["url"] = ""
    data.pop("enabled", None)
    try:
        return MemoryConfig.model_validate(data)
    except Exception as exc:
        raise HTTPException(422, _failure_detail(exc)) from None


@router.get("/status")
def get_status() -> dict:
    return engine.status()


@router.get("/items")
def list_items(
    q: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    cfg = load_config()
    rows = engine.store.list_memories(
        cfg.owner_id, query=q, kind=kind, status=status, limit=limit, offset=offset)
    sources = engine.store.memory_sources([row["id"] for row in rows])
    for row in rows:
        row["sources"] = sources.get(row["id"], [])
    return {"items": rows, "count": len(rows)}


@router.post("/items")
async def create_item(body: MemoryCreate) -> dict:
    try:
        source = None
        if body.source_session_id and body.source_message_id:
            source = {
                "source_type": "message",
                "source_id": f"{body.source_session_id}:{body.source_message_id}",
                "relation": "confirmed_from",
                "role": body.source_role,
            }
        return await engine.add_confirmed_memory(
            body.kind, body.content, tags=body.tags, source=source)
    except ValueError as exc:
        raise HTTPException(400, _failure_detail(exc)) from None


@router.get("/items/{memory_id}")
def get_item(memory_id: str) -> dict:
    cfg = load_config()
    item = engine.store.memory(memory_id)
    if not item or item.get("owner_id") != cfg.owner_id:
        raise HTTPException(404, "memory not found")
    return item


@router.post("/items/{memory_id}/correct")
async def correct_item(memory_id: str, body: MemoryCorrection) -> dict:
    try:
        return await engine.correct_memory(memory_id, body.content, kind=body.kind)
    except KeyError:
        raise HTTPException(404, "memory not found") from None
    except ValueError as exc:
        raise HTTPException(409, _failure_detail(exc)) from None


@router.post("/items/{memory_id}/approve")
async def approve_item(memory_id: str) -> dict:
    cfg = load_config()
    item = engine.store.memory(memory_id)
    if not item or item.get("owner_id") != cfg.owner_id:
        raise HTTPException(404, "memory not found")
    updated = engine.store.approve_memory(memory_id, cfg.owner_id)
    if updated is None:
        raise HTTPException(
            409, f"memory is {item.get('status')} and cannot be approved")
    if cfg.enabled:
        engine.store.enqueue("reindex_memory", {"memory_id": memory_id},
                             owner_id=cfg.owner_id)
        engine._wake.set()
    return updated


@router.delete("/items/{memory_id}")
async def delete_item(memory_id: str) -> dict:
    if not await engine.forget_memory(memory_id):
        raise HTTPException(404, "memory not found")
    return {"ok": True}


@router.post("/items/{memory_id}/feedback")
def feedback_item(memory_id: str, body: MemoryFeedback) -> dict:
    cfg = load_config()
    item = engine.store.memory(memory_id)
    if not item or item.get("owner_id") != cfg.owner_id:
        raise HTTPException(404, "memory not found")
    attributes = dict(item.get("attributes") or {})
    key = "helpful_count" if body.useful else "unhelpful_count"
    attributes[key] = int(attributes.get(key, 0)) + 1
    attributes["last_feedback_recall_id"] = body.recall_id
    updated = engine.store.update_memory(memory_id, attributes=attributes)
    engine.store.audit(
        cfg.owner_id, "feedback", "memory", memory_id,
        {"useful": body.useful, "recall_id": body.recall_id})
    return updated or item


@router.get("/episodes")
def list_episodes(
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    cfg = load_config()
    rows = engine.store.list_episodes(cfg.owner_id, status=status, limit=limit)
    return {"items": rows, "count": len(rows)}


@router.get("/episodes/{episode_id}")
def get_episode(episode_id: str) -> dict:
    cfg = load_config()
    item = engine.store.episode(episode_id)
    if not item or item.get("owner_id") != cfg.owner_id:
        raise HTTPException(404, "episode not found")
    return item


@router.get("/artifacts")
def list_artifacts(
    kind: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    cfg = load_config()
    rows = engine.store.list_artifacts(
        cfg.owner_id, kind=kind, status=status, limit=limit)
    return {"items": rows, "count": len(rows)}


@router.post("/skills/{artifact_id}/approve")
def approve_skill(artifact_id: str, body: SkillApproval) -> dict:
    try:
        return engine.approve_skill(artifact_id, body.markdown)
    except KeyError:
        raise HTTPException(404, "pending skill candidate not found") from None
    except ValueError as exc:
        raise HTTPException(400, _failure_detail(exc)) from None


@router.post("/skills/{artifact_id}/reject")
def reject_skill(artifact_id: str) -> dict:
    try:
        return engine.reject_skill(artifact_id)
    except KeyError:
        raise HTTPException(404, "skill candidate not found") from None


@router.post("/skills/{artifact_id}/disable")
def disable_skill(artifact_id: str) -> dict:
    try:
        return engine.disable_skill(artifact_id)
    except KeyError:
        raise HTTPException(404, "skill candidate not found") from None
    except ValueError as exc:
        raise HTTPException(400, _failure_detail(exc)) from None


@router.get("/jobs")
def list_jobs(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    rows = engine.store.list_jobs(limit=limit)
    return {"items": rows, "count": len(rows)}


@router.get("/recalls")
def list_recalls(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    cfg = load_config()
    rows = engine.store.recent_recalls(cfg.owner_id, limit=limit)
    return {"items": rows, "count": len(rows)}


@router.get("/audit")
def list_audit(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    cfg = load_config()
    rows = engine.store.audits(cfg.owner_id, limit=limit)
    return {"items": rows, "count": len(rows)}


@router.post("/dream")
def trigger_dream() -> dict:
    cfg = load_config()
    if not cfg.enabled:
        raise HTTPException(409, "memory is disabled")
    if not cfg.consolidation.dreamer_enabled:
        raise HTTPException(409, "Dreamer is disabled")
    engine.start()
    return {"ok": True, "job_id": engine.trigger_dream()}


@router.post("/reindex")
def trigger_reindex() -> dict:
    if not load_config().enabled:
        raise HTTPException(409, "memory is disabled")
    engine.start()
    return {"ok": True, "queued": engine.reindex_all()}


@router.get("/export")
def export_memory() -> dict:
    """Portable canonical export; vector embeddings are intentionally absent.

    The v2 snapshot carries every row verbatim, including retired ones and their
    governance state (status/authority/confidence), because import_snapshot
    restores by primary key: replaying an export cannot promote a superseded row
    back to user-confirmed the way a lossy `{kind, content}` round-trip would.
    Installed Skill paths are host-local and are therefore stripped on import;
    an imported active Skill candidate returns to pending review.
    """
    cfg = load_config()
    return {
        "schema": "muselab-memory-export-v2",
        **engine.store.export_snapshot(cfg.owner_id),
    }


@router.post("/import")
async def import_memory(body: MemoryImport) -> dict:
    schema = body.export_schema
    if schema not in (
        None,
        "muselab-memory-export-v1",
        "muselab-memory-export-v2",
    ):
        raise HTTPException(422, "unsupported memory export schema")

    cfg = load_config()
    if schema == "muselab-memory-export-v2":
        snapshot = body.model_dump(
            by_alias=True,
            exclude={"export_schema", "items"},
        )
        try:
            counts = await asyncio.to_thread(
                engine.store.import_snapshot, snapshot, cfg.owner_id)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, _failure_detail(exc)) from None
        queued = engine.reindex_all() if cfg.enabled and body.memories else 0
        return {
            "ok": True,
            "created": counts.get("memories", 0),
            "restored": counts,
            "queued_reindex": queued,
        }

    try:
        legacy_memories = [MemoryImportItem.model_validate(row)
                           for row in body.memories]
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, _failure_detail(exc)) from None
    incoming = [*body.items, *legacy_memories]
    if not incoming:
        raise HTTPException(422, "memory import contains no items")
    if len(incoming) > 10_000:
        raise HTTPException(422, "memory import exceeds 10000 items")
    existing = {
        " ".join(item["content"].casefold().split())
        for item in engine.store.list_memories(cfg.owner_id, limit=100_000)
    }
    created = 0
    for item in incoming:
        key = " ".join(item.content.casefold().split())
        if key in existing:
            continue
        status = item.status if item.status in _IMPORTABLE_STATUSES else "active"
        authority = (item.authority if item.authority in _IMPORTABLE_AUTHORITIES
                     else "confirmed")
        try:
            await engine.add_confirmed_memory(
                item.kind, item.content, tags=item.tags, status=status,
                authority=authority,
                confidence=1.0 if item.confidence is None else item.confidence)
        except ValueError as exc:
            raise HTTPException(422, _failure_detail(exc)) from None
        existing.add(key)
        created += 1
    return {"ok": True, "created": created}


@router.post("/import/mem0")
async def import_legacy_mem0() -> dict:
    from . import memory_client
    try:
        values = await memory_client.export_legacy_memories()
    except Exception as exc:
        raise HTTPException(400, _failure_detail(exc)) from None
    cfg = load_config()
    existing = {
        " ".join(item["content"].casefold().split())
        for item in engine.store.list_memories(cfg.owner_id, limit=100_000)
    }
    created = 0
    for value in values:
        key = " ".join(value.casefold().split())
        if key in existing:
            continue
        engine.store.create_memory(
            cfg.owner_id, "fact", value, authority="legacy_import",
            confidence=0.5, status="pending_review",
            sources=[{"source_type": "legacy_mem0", "source_id": "muselab",
                      "relation": "imported_from"}])
        existing.add(key)
        created += 1
    return {"ok": True, "found": len(values), "created": created,
            "status": "pending_review"}
