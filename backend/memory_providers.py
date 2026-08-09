"""Replaceable embedding, vector, rerank and generation adapters."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import uuid
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlsplit

import httpx

from .memory_config import (
    EmbeddingConfig,
    MemoryConfig,
    RerankConfig,
    VectorConfig,
    memory_dir,
)

log = logging.getLogger("muselab.memory")


def _safe_http_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("endpoint must be an HTTP(S) URL without embedded credentials")
    return value


class EmbeddingProvider:
    def __init__(self, config: EmbeddingConfig):
        self.config = config

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        url = _safe_http_url(self.config.base_url)
        if not url.endswith("/embeddings"):
            url += "/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        vectors: list[list[float]] = []
        batch_size = max(1, int(self.config.batch_size))
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds)
        ) as client:
            for start in range(0, len(texts), batch_size):
                chunk = texts[start:start + batch_size]
                payload: dict[str, Any] = {
                    "model": self.config.model,
                    "input": chunk,
                }
                if self.config.dimensions:
                    payload["dimensions"] = self.config.dimensions
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                body = response.json()
                rows = body.get("data", []) if isinstance(body, dict) else []
                try:
                    if any(not isinstance(item["index"], int)
                           or isinstance(item["index"], bool) for item in rows):
                        raise ValueError
                    indexed_rows = {item["index"]: item for item in rows}
                except (KeyError, TypeError, ValueError):
                    raise ValueError(
                        "embedding provider returned an invalid response") from None
                if (len(indexed_rows) != len(rows)
                        or set(indexed_rows) != set(range(len(chunk)))):
                    raise ValueError("embedding provider returned an invalid response")
                chunk_vectors = [indexed_rows[index].get("embedding")
                                 for index in range(len(chunk))]
                vectors.extend(chunk_vectors)
        if len(vectors) != len(texts) or not all(isinstance(v, list) and v for v in vectors):
            raise ValueError("embedding provider returned an invalid response")
        dimensions = len(vectors[0])
        if any(len(vector) != dimensions for vector in vectors):
            raise ValueError("embedding provider returned inconsistent dimensions")
        if self.config.dimensions and dimensions != self.config.dimensions:
            raise ValueError(
                f"embedding dimension mismatch: expected {self.config.dimensions}, got {dimensions}")
        return [[float(number) for number in vector] for vector in vectors]

    async def probe(self) -> dict:
        vectors = await self.embed(["MuseLab memory health check"])
        return {"ok": True, "dimensions": len(vectors[0]), "model": self.config.model}


class VectorStore(ABC):
    @abstractmethod
    async def ensure(self, dimensions: int) -> None: ...

    @abstractmethod
    async def upsert(self, item_id: str, vector: list[float], payload: dict) -> None: ...

    async def upsert_many(
        self,
        items: list[tuple[str, list[float], dict]],
    ) -> None:
        for item_id, vector, payload in items:
            await self.upsert(item_id, vector, payload)

    @abstractmethod
    async def search(self, vector: list[float], *, owner_id: str,
                     limit: int) -> list[dict]: ...

    @abstractmethod
    async def delete(self, item_id: str) -> None: ...

    @abstractmethod
    async def probe(self, dimensions: int) -> dict: ...


class QdrantVectorStore(VectorStore):
    _NAMESPACE = uuid.UUID("203b261d-094f-4889-a216-a935821ccbf7")

    def __init__(self, config: VectorConfig):
        self.config = config
        self.base = _safe_http_url(config.url)
        self.headers = {"Content-Type": "application/json"}
        if config.api_key:
            self.headers["api-key"] = config.api_key

    def _point_id(self, item_id: str) -> str:
        return str(uuid.uuid5(self._NAMESPACE, item_id))

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds)
        ) as client:
            response = await client.request(
                method, f"{self.base}{path}", headers=self.headers, **kwargs)
            response.raise_for_status()
            return response

    async def ensure(self, dimensions: int) -> None:
        path = f"/collections/{self.config.collection}"
        try:
            response = await self._request("GET", path)
            result = response.json().get("result", {})
            vectors = (((result.get("config") or {}).get("params") or {})
                       .get("vectors") or {})
            existing_size = vectors.get("size") if isinstance(vectors, dict) else None
            if existing_size is not None and int(existing_size) != dimensions:
                raise ValueError(
                    f"Qdrant collection dimension mismatch: "
                    f"{existing_size} != {dimensions}")
            return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
        await self._request("PUT", path, json={
            "vectors": {"size": dimensions, "distance": "Cosine"},
            "on_disk_payload": True,
        })

    async def upsert(self, item_id: str, vector: list[float], payload: dict) -> None:
        await self.upsert_many([(item_id, vector, payload)])

    async def upsert_many(
        self,
        items: list[tuple[str, list[float], dict]],
    ) -> None:
        for start in range(0, len(items), 256):
            chunk = items[start:start + 256]
            await self._request(
                "PUT", f"/collections/{self.config.collection}/points?wait=true",
                json={"points": [
                    {
                        "id": self._point_id(item_id),
                        "vector": vector,
                        "payload": {**payload, "memory_id": item_id},
                    }
                    for item_id, vector, payload in chunk
                ]},
            )

    async def search(self, vector: list[float], *, owner_id: str,
                     limit: int) -> list[dict]:
        response = await self._request(
            "POST", f"/collections/{self.config.collection}/points/query",
            json={
                "query": vector,
                "filter": {"must": [{"key": "owner_id",
                                     "match": {"value": owner_id}},
                                    {"key": "status",
                                     "match": {"value": "active"}}]},
                "with_payload": True,
                "limit": limit,
            },
        )
        body = response.json()
        result = body.get("result", {})
        points = result.get("points", result if isinstance(result, list) else [])
        return [{
            "id": (point.get("payload") or {}).get("memory_id"),
            "score": float(point.get("score", 0)),
            "payload": point.get("payload") or {},
            "channel": "dense",
        } for point in points if (point.get("payload") or {}).get("memory_id")]

    async def delete(self, item_id: str) -> None:
        await self._request(
            "POST", f"/collections/{self.config.collection}/points/delete?wait=true",
            json={"points": [self._point_id(item_id)]},
        )

    async def probe(self, dimensions: int) -> dict:
        await self.ensure(dimensions)
        response = await self._request("GET", f"/collections/{self.config.collection}")
        return {"ok": True, "provider": "qdrant",
                "collection": self.config.collection,
                "status": response.json().get("result", {}).get("status", "ok")}


class PgVectorStore(VectorStore):
    """Postgres/pgvector adapter. psycopg is imported only when selected."""

    _TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

    def __init__(self, config: VectorConfig):
        self.config = config
        if not self._TABLE_RE.fullmatch(config.collection):
            raise ValueError("pgvector collection must be a safe SQL identifier")
        self.table = config.collection

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "pgvector requires the optional psycopg[binary] dependency") from exc
        return psycopg.connect(self.config.url, connect_timeout=int(self.config.timeout_seconds))

    async def ensure(self, dimensions: int) -> None:
        def run():
            with self._connect() as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                conn.execute(
                    f"""CREATE TABLE IF NOT EXISTS {self.table} (
                        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                        status TEXT NOT NULL, payload JSONB NOT NULL,
                        embedding vector({int(dimensions)}) NOT NULL
                    )""")
                row = conn.execute(
                    """SELECT format_type(atttypid,atttypmod)
                       FROM pg_attribute
                       WHERE attrelid=to_regclass(%s) AND attname='embedding'
                         AND NOT attisdropped""",
                    (self.table,),
                ).fetchone()
                actual = str(row[0]) if row else ""
                expected = f"vector({int(dimensions)})"
                if actual != expected:
                    raise ValueError(
                        f"pgvector dimension mismatch: {actual or 'unknown'} "
                        f"!= {expected}")
                conn.execute(
                    f"""CREATE INDEX IF NOT EXISTS {self.table}_owner_status_idx
                        ON {self.table}(owner_id,status)""")
                conn.execute(
                    f"""CREATE INDEX IF NOT EXISTS {self.table}_embedding_hnsw
                        ON {self.table} USING hnsw (embedding vector_cosine_ops)""")
        await asyncio.to_thread(run)

    async def upsert(self, item_id: str, vector: list[float], payload: dict) -> None:
        await self.upsert_many([(item_id, vector, payload)])

    async def upsert_many(
        self,
        items: list[tuple[str, list[float], dict]],
    ) -> None:
        def run():
            with self._connect() as conn:
                conn.executemany(
                    f"""INSERT INTO {self.table}
                        (id,owner_id,status,payload,embedding)
                        VALUES (%s,%s,%s,%s,%s::vector)
                        ON CONFLICT(id) DO UPDATE SET owner_id=excluded.owner_id,
                        status=excluded.status,payload=excluded.payload,
                        embedding=excluded.embedding""",
                    [
                        (
                            item_id,
                            payload["owner_id"],
                            payload.get("status", "active"),
                            json.dumps(payload),
                            json.dumps(vector),
                        )
                        for item_id, vector, payload in items
                    ],
                )
        await asyncio.to_thread(run)

    async def search(self, vector: list[float], *, owner_id: str,
                     limit: int) -> list[dict]:
        def run():
            with self._connect() as conn:
                rows = conn.execute(
                    f"""SELECT id,payload,1-(embedding <=> %s::vector) AS score
                        FROM {self.table} WHERE owner_id=%s AND status='active'
                        ORDER BY embedding <=> %s::vector LIMIT %s""",
                    (json.dumps(vector), owner_id, json.dumps(vector), limit),
                ).fetchall()
                return [{"id": row[0], "payload": row[1],
                         "score": float(row[2]), "channel": "dense"} for row in rows]
        return await asyncio.to_thread(run)

    async def delete(self, item_id: str) -> None:
        def run():
            with self._connect() as conn:
                conn.execute(f"DELETE FROM {self.table} WHERE id=%s", (item_id,))
        await asyncio.to_thread(run)

    async def probe(self, dimensions: int) -> dict:
        await self.ensure(dimensions)
        return {"ok": True, "provider": "pgvector", "collection": self.table}


def vector_store(config: VectorConfig) -> VectorStore:
    if config.provider == "qdrant":
        return QdrantVectorStore(config)
    return PgVectorStore(config)


class Reranker:
    def __init__(self, config: RerankConfig):
        self.config = config

    async def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        if not self.config.enabled or not documents:
            return [(index, 0.0) for index in range(len(documents))]
        url = _safe_http_url(self.config.base_url)
        if not url.endswith("/rerank"):
            url += "/rerank"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds)
        ) as client:
            response = await client.post(url, headers=headers, json={
                "model": self.config.model, "query": query,
                "documents": documents, "top_n": len(documents),
            })
            response.raise_for_status()
            body = response.json()
        rows = body.get("results", body.get("data", []))
        return [(int(row.get("index", 0)),
                 float(row.get("relevance_score", row.get("score", 0))))
                for row in rows]

    async def probe(self) -> dict:
        result = await self.rerank("memory", ["memory system", "weather"])
        return {"ok": True, "model": self.config.model, "results": len(result)}


_GENERATION_TIMEOUT_ENV = "MUSELAB_MEMORY_GENERATION_TIMEOUT_SECONDS"
_GENERATION_TIMEOUT_DEFAULT = 60.0
_GENERATION_TIMEOUT_MAX = 600.0
_RETRYABLE_GENERATION_STATUS_CODES = {408, 409, 429, 529}
_SDK_TERMINAL_ERROR_NAMES = {
    "AuthenticationError",
    "BadRequestError",
    "CLINotFoundError",
    "ConfigurationError",
    "InvalidConfigurationError",
    "PermissionDeniedError",
}
_SDK_RETRYABLE_ERROR_NAMES = {
    "CLIConnectionError",
    "CLIJSONDecodeError",
    "ConnectionError",
    "MessageParseError",
    "ProcessError",
    "SDKJSONDecodeError",
    "TransportError",
}


def generation_timeout_seconds() -> float:
    """Return the background generation deadline without affecting chat."""
    from .settings import env_float

    value = env_float(_GENERATION_TIMEOUT_ENV, _GENERATION_TIMEOUT_DEFAULT)
    if not math.isfinite(value) or value <= 0:
        return _GENERATION_TIMEOUT_DEFAULT
    return min(value, _GENERATION_TIMEOUT_MAX)


def _generation_error_status(exc: BaseException) -> int | None:
    for value in (
        getattr(exc, "api_error_status", None),
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _sdk_error_names(exc: BaseException) -> set[str]:
    if not any(cls.__module__.startswith("claude_agent_sdk")
               for cls in type(exc).__mro__):
        return set()
    return {cls.__name__ for cls in type(exc).__mro__}


def _retryable_status(status: int | None) -> bool:
    return bool(status is not None and (
        status in _RETRYABLE_GENERATION_STATUS_CODES or 500 <= status <= 599
    ))


def _status_category(status: int | None) -> str:
    if status in {401, 403, 407}:
        return "authentication" if status == 401 else "permission"
    if status == 400:
        return "bad_request"
    if _retryable_status(status):
        return "transient_provider"
    return "provider_error"


def _sdk_exception_category(exc: BaseException) -> str | None:
    names = _sdk_error_names(exc)
    if not names:
        return None
    if "AuthenticationError" in names:
        return "authentication"
    if "PermissionDeniedError" in names:
        return "permission"
    if "BadRequestError" in names:
        return "bad_request"
    if names & {"CLINotFoundError", "ConfigurationError",
                "InvalidConfigurationError"}:
        return "invalid_configuration"
    if names & _SDK_RETRYABLE_ERROR_NAMES:
        return "transient_provider"
    return "generation_failure"


def is_retryable_generation_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    status = _generation_error_status(exc)
    if status is not None:
        return _retryable_status(status)
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return False
    sdk_names = _sdk_error_names(exc)
    if sdk_names & _SDK_TERMINAL_ERROR_NAMES:
        return False
    return bool(sdk_names & _SDK_RETRYABLE_ERROR_NAMES)


def _classify_sdk_result_error(message: Any) -> tuple[bool, str, int | None]:
    status = getattr(message, "api_error_status", None)
    if not isinstance(status, int) or isinstance(status, bool):
        status = None
    if status is not None:
        return _retryable_status(status), _status_category(status), status

    # Inspect only in memory to choose a safe category. The raw result/error
    # strings may contain response bodies, paths, or credentials and are never
    # persisted or logged.
    detail = " ".join([
        str(getattr(message, "subtype", "") or ""),
        str(getattr(message, "result", "") or ""),
        *(str(value) for value in (getattr(message, "errors", None) or [])),
    ]).casefold()
    strong_terminal = (
        ("missing_credentials", ("not logged in", "login required")),
        ("authentication", ("authentication", "unauthorized", "invalid api key")),
        ("permission", ("permission", "forbidden", "access denied")),
        ("invalid_configuration", (
            "invalid configuration", "config error", "cli not found")),
        ("max_turns", ("max turns", "max_turns")),
        ("bad_request", ("bad request", "invalid request")),
    )
    for category, needles in strong_terminal:
        if any(needle in detail for needle in needles):
            return False, category, None
    if any(needle in detail for needle in (
        "connection", "transport", "timeout", "timed out", "process exited",
        "broken pipe", "eof", "rate limit", "quota", "overloaded",
        "service unavailable",
    )):
        return True, "transient_provider", None
    if "credential" in detail:
        return False, "missing_credentials", None
    if any(needle in detail for needle in (
        "configuration", "config error", "cli not found",
    )):
        return False, "invalid_configuration", None
    return False, "generation_failure", None


class GenerationError(RuntimeError):
    """Sanitized generation failure carrying only retry/log metadata."""

    def __init__(self, *, retryable: bool, provider: str = "", model: str = "",
                 api_error_status: int | None = None,
                 category: str | None = None):
        category = category or (
            "transient_provider" if retryable else "generation_failure")
        super().__init__(category)
        self.retryable = retryable
        self.provider = provider
        self.model = model
        self.api_error_status = api_error_status
        self.category = category


class GenerationProvider:
    """Small no-tool Anthropic-compatible client for consolidation jobs."""

    def __init__(self, config: MemoryConfig):
        self.config = config

    def _route(self) -> tuple[str, str, str] | None:
        from . import endpoints
        model = self.config.generation_model
        provider = endpoints.lookup(model)
        if provider is not None:
            key = os.environ.get(provider.env_key, "")
            base = endpoints._resolve_base_url(provider.env_key, provider)
            if not key:
                raise GenerationError(
                    retryable=False,
                    provider=provider.display,
                    model=model,
                    category="missing_credentials",
                )
            return base.rstrip("/") + "/v1/messages", key, endpoints.normalize_model_id(model)
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            # Native Claude may be configured through `claude login` rather
            # than an API key. complete() handles it through a fresh, no-tool
            # SDK query instead of borrowing a live chat client/session.
            return None
        base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        return base + "/v1/messages", key, model

    def metadata(self) -> tuple[str, str]:
        from . import endpoints
        provider = endpoints.lookup(self.config.generation_model)
        return ((provider.display if provider is not None else "anthropic"),
                self.config.generation_model)

    async def complete(self, system: str, prompt: str, *,
                       max_tokens: int = 3000) -> str:
        provider, configured_model = self.metadata()
        timeout = generation_timeout_seconds()
        try:
            async with asyncio.timeout(timeout):
                route = self._route()
                if route is None:
                    return await self._complete_with_sdk(system, prompt)
                url, key, model = route
                payload = {"model": model, "max_tokens": max_tokens, "temperature": 0,
                           "system": system,
                           "messages": [{"role": "user", "content": prompt}]}
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
                    response = await client.post(
                        url,
                        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                 "Content-Type": "application/json"},
                        json=payload,
                    )
                    if (response.status_code == 400
                            and "temperature" in response.text.lower()):
                        # Extended-thinking models reject deterministic temperature.
                        log.debug("generation endpoint rejected temperature, retrying without")
                        payload.pop("temperature", None)
                        response = await client.post(
                            url,
                            headers={"x-api-key": key,
                                     "anthropic-version": "2023-06-01",
                                     "Content-Type": "application/json"},
                            json=payload,
                        )
                    response.raise_for_status()
                    try:
                        body = response.json()
                    except json.JSONDecodeError as exc:
                        raise GenerationError(
                            retryable=False,
                            provider=provider,
                            model=configured_model,
                            api_error_status=response.status_code,
                            category="malformed_response",
                        ) from exc
                if not isinstance(body, dict) or not isinstance(body.get("content"), list):
                    raise GenerationError(
                        retryable=False,
                        provider=provider,
                        model=configured_model,
                        api_error_status=response.status_code,
                        category="malformed_response",
                    )
                blocks = body["content"]
                text_blocks = [block.get("text") for block in blocks
                               if isinstance(block, dict)
                               and block.get("type") == "text"]
                if (any(not isinstance(block, dict) for block in blocks)
                        or not text_blocks
                        or any(not isinstance(text, str) for text in text_blocks)):
                    raise GenerationError(
                        retryable=False,
                        provider=provider,
                        model=configured_model,
                        api_error_status=response.status_code,
                        category="malformed_response",
                    )
                return "".join(text_blocks)
        except asyncio.CancelledError:
            raise
        except GenerationError:
            raise
        except Exception as exc:
            status = _generation_error_status(exc)
            retryable = is_retryable_generation_error(exc)
            raise GenerationError(
                retryable=retryable,
                provider=provider,
                model=configured_model,
                api_error_status=status,
                category=(
                    _status_category(status) if status is not None
                    else _sdk_exception_category(exc)
                    or ("invalid_configuration" if isinstance(exc, ValueError)
                        else "transient_provider" if retryable
                        else "generation_failure")
                ),
            ) from exc

    async def _complete_with_sdk(self, system: str, prompt: str) -> str:
        from claude_agent_sdk import ClaudeAgentOptions, query
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

        workdir = memory_dir() / "generator"
        workdir.mkdir(parents=True, exist_ok=True)
        options = ClaudeAgentOptions(
            model=self.config.generation_model,
            system_prompt=system,
            tools=[],
            allowed_tools=[],
            disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep",
                              "Task", "Agent", "WebFetch", "WebSearch"],
            mcp_servers={},
            setting_sources=[],
            skills=[],
            max_turns=1,
            permission_mode="default",
            cwd=workdir,
            include_partial_messages=False,
        )
        parts: list[str] = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content or []:
                    if isinstance(block, TextBlock):
                        parts.append(block.text or "")
            elif isinstance(message, ResultMessage) and message.is_error:
                retryable, category, status = _classify_sdk_result_error(message)
                provider, model = self.metadata()
                raise GenerationError(
                    retryable=retryable,
                    provider=provider,
                    model=model,
                    api_error_status=status,
                    category=category,
                )
        text = "".join(parts)
        if not text.strip():
            provider, model = self.metadata()
            raise GenerationError(
                retryable=False, provider=provider, model=model,
                category="malformed_response")
        return text

    async def complete_json(self, system: str, prompt: str) -> dict:
        text = (await self.complete(system, prompt)).strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                start, end = text.find("{"), text.rfind("}")
                if start < 0 or end <= start:
                    raise
                value = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            provider, model = self.metadata()
            raise GenerationError(
                retryable=False, provider=provider, model=model,
                category="malformed_response") from exc
        if not isinstance(value, dict):
            provider, model = self.metadata()
            raise GenerationError(
                retryable=False, provider=provider, model=model,
                category="malformed_response")
        return value

    async def probe(self) -> dict:
        value = await self.complete_json(
            "Return JSON only.",
            'Return exactly {"ok":true,"purpose":"memory-probe"}.',
        )
        if value.get("ok") is not True:
            raise ValueError("generation probe returned an unexpected payload")
        return {"ok": True, "model": self.config.generation_model}
