"""Configuration for MuseLab's optional, provider-neutral memory system.

The JSON file is the canonical runtime configuration.  Secrets are stored with
0600 permissions and are never returned by the API.  Memory is off by default,
so importing this module cannot add latency or external dependencies to chat.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .settings import ROOT


class EmbeddingConfig(BaseModel):
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    dimensions: int | None = Field(default=None, ge=1, le=65536)
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    batch_size: int = Field(default=32, ge=1, le=256)


class VectorConfig(BaseModel):
    provider: Literal["qdrant", "pgvector"] = "qdrant"
    url: str = ""
    api_key: str = ""
    collection: str = "muselab_memory_v1"
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)


class RerankConfig(BaseModel):
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: float = Field(default=3.0, ge=0.2, le=30.0)


class RetrievalConfig(BaseModel):
    dense_candidates: int = Field(default=20, ge=1, le=100)
    lexical_candidates: int = Field(default=20, ge=1, le=100)
    final_limit: int = Field(default=6, ge=1, le=20)
    max_context_chars: int = Field(default=3000, ge=500, le=12000)
    soft_timeout_ms: int = Field(default=250, ge=100, le=3000)


class ConsolidationConfig(BaseModel):
    episode_turns: int = Field(default=6, ge=2, le=50)
    episode_idle_minutes: int = Field(default=30, ge=2, le=10080)
    dreamer_enabled: bool = True
    verifier_enabled: bool = True
    skill_learning_enabled: bool = True
    min_reflection_episodes: int = Field(default=2, ge=2, le=20)
    min_skill_success_episodes: int = Field(default=3, ge=2, le=20)


class MemoryConfig(BaseModel):
    schema_version: int = 1
    mode: Literal["off", "shadow", "active"] = "off"
    owner_id: str = "default"
    generation_model: str = ""
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    vector: VectorConfig = Field(default_factory=VectorConfig)
    rerank: RerankConfig = Field(default_factory=RerankConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    consolidation: ConsolidationConfig = Field(default_factory=ConsolidationConfig)

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @model_validator(mode="after")
    def validate_enabled_capabilities(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",
                            self.vector.collection):
            raise ValueError("vector.collection contains unsupported characters")
        if (self.vector.provider == "pgvector"
                and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}",
                                     self.vector.collection)):
            raise ValueError(
                "pgvector collection must be a SQL identifier of at most 63 characters")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", self.owner_id):
            raise ValueError("owner_id contains unsupported characters")
        if not self.enabled:
            return self
        missing: list[str] = []
        if not self.generation_model.strip():
            missing.append("generation_model")
        if not self.embedding.base_url.strip():
            missing.append("embedding.base_url")
        if not self.embedding.model.strip():
            missing.append("embedding.model")
        if not self.vector.url.strip():
            missing.append("vector.url")
        if not self.vector.collection.strip():
            missing.append("vector.collection")
        if self.rerank.enabled and (
            not self.rerank.base_url.strip() or not self.rerank.model.strip()
        ):
            missing.append("rerank.base_url/model")
        if missing:
            raise ValueError("memory is enabled but required settings are missing: "
                             + ", ".join(missing))
        return self


_LOCK = threading.RLock()
_cached: tuple[int, MemoryConfig] | None = None


def memory_dir() -> Path:
    override = os.environ.get("MUSELAB_MEMORY_DIR", "").strip()
    return Path(override).expanduser() if override else Path(ROOT) / ".muselab" / "memory"


def config_path() -> Path:
    return memory_dir() / "config.json"


def database_path() -> Path:
    return memory_dir() / "registry.sqlite3"


def load_config(*, fresh: bool = False) -> MemoryConfig:
    global _cached
    path = config_path()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = -1
    with _LOCK:
        if not fresh and _cached and _cached[0] == stamp:
            return _cached[1].model_copy(deep=True)
        if stamp < 0:
            value = MemoryConfig()
        else:
            try:
                value = MemoryConfig.model_validate_json(path.read_text("utf-8"))
            except Exception:
                # A corrupt config must never stop MuseLab from starting.
                value = MemoryConfig()
        _cached = (stamp, value)
        return value.model_copy(deep=True)


def save_config(config: MemoryConfig) -> MemoryConfig:
    global _cached
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config.model_dump(mode="json"), ensure_ascii=False,
                         indent=2) + "\n"
    with _LOCK:
        fd, tmp = tempfile.mkstemp(prefix=".config.", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        stamp = path.stat().st_mtime_ns
        _cached = (stamp, config.model_copy(deep=True))
    return config.model_copy(deep=True)


def public_config(config: MemoryConfig | None = None) -> dict:
    cfg = config or load_config()
    data = cfg.model_dump(mode="json")
    for section in ("embedding", "vector", "rerank"):
        secret = data[section].get("api_key", "")
        data[section]["api_key"] = ""
        data[section]["has_api_key"] = bool(secret)
    if cfg.vector.provider == "pgvector":
        data["vector"]["has_url"] = bool(data["vector"]["url"])
        data["vector"]["url"] = ""
    data["enabled"] = cfg.enabled
    return data


def merge_secret_fields(incoming: MemoryConfig, current: MemoryConfig) -> MemoryConfig:
    """Blank secrets preserve the stored value; ``_delete_`` removes it."""
    for name in ("embedding", "vector", "rerank"):
        target = getattr(incoming, name)
        old = getattr(current, name)
        if target.api_key == "":
            target.api_key = old.api_key
        elif target.api_key == "_delete_":
            target.api_key = ""
    if incoming.vector.provider == "pgvector":
        if incoming.vector.url == "" and current.vector.provider == "pgvector":
            incoming.vector.url = current.vector.url
        elif incoming.vector.url == "_delete_":
            incoming.vector.url = ""
    return incoming
