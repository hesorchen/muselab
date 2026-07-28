"""Provider contracts without external network services."""
import asyncio

import pytest

def _run(coro):
    return asyncio.run(coro)


def test_endpoint_rejects_credentials_query_and_non_http():
    from backend.memory_providers import _safe_http_url
    for value in (
        "ftp://embed/v1",
        "https://user:password@embed/v1",
        "https://embed/v1?token=secret",
        "https://embed/v1#fragment",
    ):
        with pytest.raises(ValueError):
            _safe_http_url(value)


def test_embedding_response_order_and_dimension_validation(monkeypatch):
    import backend.memory_providers as module
    from backend.memory_config import EmbeddingConfig
    from backend.memory_providers import EmbeddingProvider

    class Response:
        def raise_for_status(self): pass

        def json(self):
            return {"data": [
                {"index": 1, "embedding": [0, 1, 0]},
                {"index": 0, "embedding": [1, 0, 0]},
            ]}

    class Client:
        def __init__(self, **_kwargs): pass

        async def __aenter__(self): return self

        async def __aexit__(self, *_args): return False

        async def post(self, _url, **_kwargs): return Response()

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    provider = EmbeddingProvider(EmbeddingConfig(
        base_url="http://embed/v1", model="bge", dimensions=3))
    assert _run(provider.embed(["a", "b"])) == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]

    provider = EmbeddingProvider(EmbeddingConfig(
        base_url="http://embed/v1", model="bge", dimensions=4))
    with pytest.raises(ValueError, match="dimension mismatch"):
        _run(provider.embed(["a", "b"]))


def test_embedding_provider_honours_configured_batch_size(monkeypatch):
    import backend.memory_providers as module
    from backend.memory_config import EmbeddingConfig
    from backend.memory_providers import EmbeddingProvider

    batches: list[list[str]] = []

    class Response:
        def __init__(self, texts):
            self.texts = texts

        def raise_for_status(self): pass

        def json(self):
            return {"data": [
                {"index": index, "embedding": [float(index), 1.0]}
                for index, _text in enumerate(self.texts)
            ]}

    class Client:
        def __init__(self, **_kwargs): pass

        async def __aenter__(self): return self

        async def __aexit__(self, *_args): return False

        async def post(self, _url, **kwargs):
            texts = kwargs["json"]["input"]
            batches.append(texts)
            return Response(texts)

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    provider = EmbeddingProvider(EmbeddingConfig(
        base_url="http://embed/v1", model="bge", dimensions=2, batch_size=2))
    vectors = _run(provider.embed(["a", "b", "c", "d", "e"]))
    assert batches == [["a", "b"], ["c", "d"], ["e"]]
    assert len(vectors) == 5


def test_qdrant_refuses_existing_collection_with_wrong_dimension(monkeypatch):
    from backend.memory_config import VectorConfig
    from backend.memory_providers import QdrantVectorStore
    class Response:
        def raise_for_status(self): pass

        def json(self):
            return {"result": {"config": {"params": {"vectors": {"size": 768}}}}}

    store = QdrantVectorStore(VectorConfig(
        provider="qdrant", url="http://qdrant:6333", collection="memory"))

    async def request(_method, _path, **_kwargs):
        return Response()

    monkeypatch.setattr(store, "_request", request)
    with pytest.raises(ValueError, match="dimension mismatch"):
        _run(store.ensure(1024))


def test_pgvector_table_name_is_not_interpolatable():
    from backend.memory_config import VectorConfig
    from backend.memory_providers import PgVectorStore
    with pytest.raises(ValueError):
        PgVectorStore(VectorConfig(
            provider="pgvector", url="postgresql://db/memory",
            collection="memory; DROP TABLE users"))


def test_claude_oauth_generation_uses_fresh_no_tool_sdk_query(
        tmp_path, monkeypatch):
    import claude_agent_sdk
    from claude_agent_sdk.types import AssistantMessage, TextBlock
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationProvider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MUSELAB_MEMORY_DIR", str(tmp_path / "memory"))
    seen = {}

    async def fake_query(*, prompt, options):
        seen["prompt"] = prompt
        seen["options"] = options
        yield AssistantMessage(content=[TextBlock("ok")], model="claude")

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    provider = GenerationProvider(MemoryConfig(generation_model="claude-sonnet-4-6"))
    assert _run(provider.complete("system", "prompt")) == "ok"
    assert seen["options"].tools == []
    assert seen["options"].allowed_tools == []
    assert seen["options"].mcp_servers == {}
    assert seen["options"].setting_sources == []
    assert seen["options"].skills == []
