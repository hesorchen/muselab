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


@pytest.mark.parametrize("rows", [
    [
        {"index": 0, "embedding": [1, 0]},
        {"index": 0, "embedding": [0, 1]},
    ],
    [
        {"index": 0, "embedding": [1, 0]},
        {"index": 2, "embedding": [0, 1]},
    ],
    [
        {"index": False, "embedding": [1, 0]},
        {"index": 1, "embedding": [0, 1]},
    ],
    [
        {"index": 0.9, "embedding": [1, 0]},
        {"index": 1, "embedding": [0, 1]},
    ],
])
def test_embedding_response_requires_exact_unique_indices(monkeypatch, rows):
    import backend.memory_providers as module
    from backend.memory_config import EmbeddingConfig
    from backend.memory_providers import EmbeddingProvider

    class Response:
        def raise_for_status(self): pass

        def json(self): return {"data": rows}

    class Client:
        def __init__(self, **_kwargs): pass

        async def __aenter__(self): return self

        async def __aexit__(self, *_args): return False

        async def post(self, _url, **_kwargs): return Response()

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    provider = EmbeddingProvider(EmbeddingConfig(
        base_url="http://embed/v1", model="bge", dimensions=2))
    with pytest.raises(ValueError, match="invalid response"):
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


def test_generation_timeout_has_compatible_default_and_env_override(monkeypatch):
    from backend.memory_providers import generation_timeout_seconds

    monkeypatch.delenv("MUSELAB_MEMORY_GENERATION_TIMEOUT_SECONDS", raising=False)
    assert generation_timeout_seconds() == 60.0
    monkeypatch.setenv("MUSELAB_MEMORY_GENERATION_TIMEOUT_SECONDS", "17.5")
    assert generation_timeout_seconds() == 17.5
    monkeypatch.setenv("MUSELAB_MEMORY_GENERATION_TIMEOUT_SECONDS", "9999")
    assert generation_timeout_seconds() == 600.0
    monkeypatch.setenv("MUSELAB_MEMORY_GENERATION_TIMEOUT_SECONDS", "nan")
    assert generation_timeout_seconds() == 60.0


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504, 529, 599])
def test_generation_http_retry_classification(status):
    import httpx
    from backend.memory_providers import is_retryable_generation_error

    request = httpx.Request("POST", "https://provider.test/v1/messages")
    response = httpx.Response(status, request=request)
    error = httpx.HTTPStatusError("sensitive response", request=request, response=response)
    assert is_retryable_generation_error(error) is True


def test_generation_network_and_timeout_errors_are_retryable():
    import httpx
    from backend.memory_providers import is_retryable_generation_error

    request = httpx.Request("POST", "https://provider.test/v1/messages")
    assert is_retryable_generation_error(httpx.ReadTimeout("timed out", request=request))
    assert is_retryable_generation_error(httpx.ConnectError("network", request=request))
    assert is_retryable_generation_error(httpx.RemoteProtocolError(
        "incomplete response", request=request))
    assert is_retryable_generation_error(TimeoutError())
    response = httpx.Response(400, request=request)
    assert not is_retryable_generation_error(httpx.HTTPStatusError(
        "bad request", request=request, response=response))
    assert not is_retryable_generation_error(ValueError("invalid JSON"))


def test_direct_http_generation_preserves_status_without_response_body(
        monkeypatch):
    import httpx
    import backend.memory_providers as module
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationError, GenerationProvider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://provider.test")
    secret = "sensitive upstream body"

    class Client:
        def __init__(self, **_kwargs): pass

        async def __aenter__(self): return self

        async def __aexit__(self, *_args): return False

        async def post(self, url, **_kwargs):
            request = httpx.Request("POST", url)
            return httpx.Response(503, text=secret, request=request)

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    provider = GenerationProvider(MemoryConfig(
        generation_model="claude-sonnet-4-6"))
    with pytest.raises(GenerationError) as caught:
        _run(provider.complete("system", "prompt"))

    assert caught.value.retryable is True
    assert caught.value.api_error_status == 503
    assert secret not in str(caught.value)


@pytest.mark.parametrize("body", [
    '{"content":[{"type":"text","text":"truncated',
    '{"error":"missing content","secret":"provider-secret"}',
    '[{"type":"text","text":"wrong envelope"}]',
])
def test_direct_http_malformed_envelope_is_sanitized_and_terminal(
        monkeypatch, body):
    import httpx
    import backend.memory_providers as module
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationError, GenerationProvider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://provider.test")

    class Client:
        def __init__(self, **_kwargs): pass

        async def __aenter__(self): return self

        async def __aexit__(self, *_args): return False

        async def post(self, url, **_kwargs):
            return httpx.Response(
                200, text=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    provider = GenerationProvider(MemoryConfig(
        generation_model="claude-sonnet-4-6"))
    with pytest.raises(GenerationError) as caught:
        _run(provider.complete("system", "prompt"))

    assert caught.value.retryable is False
    assert caught.value.category == "malformed_response"
    assert caught.value.api_error_status == 200
    assert "provider-secret" not in str(caught.value)
    assert body not in str(caught.value)


def test_direct_http_local_value_error_remains_terminal(monkeypatch):
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationError, GenerationProvider

    provider = GenerationProvider(MemoryConfig(
        generation_model="claude-sonnet-4-6"))

    def fail_route():
        raise ValueError("local validation failure with secret-token")

    monkeypatch.setattr(provider, "_route", fail_route)
    with pytest.raises(GenerationError) as caught:
        _run(provider.complete("system", "prompt"))

    assert caught.value.retryable is False
    assert "secret-token" not in str(caught.value)


def test_malformed_generation_json_is_sanitized_and_terminal(monkeypatch):
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationError, GenerationProvider

    provider = GenerationProvider(MemoryConfig(
        generation_model="claude-sonnet-4-6"))

    async def malformed(_system, _prompt, *, max_tokens=3000):
        return '{"memories": ['

    monkeypatch.setattr(provider, "complete", malformed)
    with pytest.raises(GenerationError) as caught:
        _run(provider.complete_json("system", "prompt"))

    assert caught.value.retryable is False
    assert caught.value.category == "malformed_response"
    assert caught.value.provider == "anthropic"
    assert caught.value.model == "claude-sonnet-4-6"
    assert "memories" not in str(caught.value)


@pytest.mark.parametrize("status,retryable,category", [
    (429, True, "transient_provider"),
    (500, True, "transient_provider"),
    (529, True, "transient_provider"),
    (400, False, "bad_request"),
    (401, False, "authentication"),
    (403, False, "permission"),
])
def test_sdk_result_error_preserves_status_without_provider_detail(
        tmp_path, monkeypatch, status, retryable, category):
    import claude_agent_sdk
    from claude_agent_sdk.types import ResultMessage
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationError, GenerationProvider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MUSELAB_MEMORY_DIR", str(tmp_path / "memory"))
    secret = "raw provider body with secret-token"

    async def fake_query(*, prompt, options):
        yield ResultMessage(
            subtype="error",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id="sid",
            result=secret,
            errors=[secret],
            api_error_status=status,
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    provider = GenerationProvider(MemoryConfig(
        generation_model="claude-sonnet-4-6"))
    with pytest.raises(GenerationError) as caught:
        _run(provider.complete("system", "prompt"))

    error = caught.value
    assert error.api_error_status == status
    assert error.retryable is retryable
    assert error.category == category
    assert error.provider == "anthropic"
    assert error.model == "claude-sonnet-4-6"
    assert secret not in str(error)


@pytest.mark.parametrize("detail,retryable,category", [
    ("Not logged in. Run claude login.", False, "missing_credentials"),
    ("Authentication failed: invalid API key", False, "authentication"),
    ("Authentication failed: invalid API key; connection closed", False, "authentication"),
    ("Permission denied", False, "permission"),
    ("Invalid configuration", False, "invalid_configuration"),
    ("Process exited with code 1: invalid configuration", False,
     "invalid_configuration"),
    ("Maximum max_turns reached", False, "max_turns"),
    ("Bad request", False, "bad_request"),
    ("Transport connection interrupted", True, "transient_provider"),
    ("Credential helper connection timed out", True, "transient_provider"),
    ("Rate limit exceeded", True, "transient_provider"),
    ("Provider overloaded", True, "transient_provider"),
    ("Unclassified provider failure", False, "generation_failure"),
])
def test_statusless_sdk_result_is_safely_classified(
        tmp_path, monkeypatch, detail, retryable, category):
    import claude_agent_sdk
    from claude_agent_sdk.types import ResultMessage
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationError, GenerationProvider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MUSELAB_MEMORY_DIR", str(tmp_path / "memory"))

    async def fake_query(*, prompt, options):
        yield ResultMessage(
            subtype="error", duration_ms=1, duration_api_ms=1,
            is_error=True, num_turns=1, session_id="sid",
            result=detail, errors=[detail], api_error_status=None)

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    provider = GenerationProvider(MemoryConfig(
        generation_model="claude-sonnet-4-6"))
    with pytest.raises(GenerationError) as caught:
        _run(provider.complete("system", "prompt"))

    assert caught.value.retryable is retryable
    assert caught.value.category == category
    assert detail not in str(caught.value)


@pytest.mark.parametrize("error_name,retryable", [
    ("CLIConnectionError", True),
    ("CLIJSONDecodeError", True),
    ("ProcessError", True),
    ("CLINotFoundError", False),
    ("ConfigurationError", False),
    ("AuthenticationError", False),
    ("BadRequestError", False),
])
def test_sdk_exception_classification_is_sanitized(
        tmp_path, monkeypatch, error_name, retryable):
    import claude_agent_sdk
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationError, GenerationProvider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MUSELAB_MEMORY_DIR", str(tmp_path / "memory"))
    error_type = type(
        error_name,
        (RuntimeError,),
        {"__module__": "claude_agent_sdk._errors"},
    )
    secret = "sdk transport detail with secret-token"

    async def fake_query(*, prompt, options):
        if False:
            yield None
        raise error_type(secret)

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    provider = GenerationProvider(MemoryConfig(
        generation_model="claude-sonnet-4-6"))
    with pytest.raises(GenerationError) as caught:
        _run(provider.complete("system", "prompt"))

    assert caught.value.retryable is retryable
    assert caught.value.api_error_status is None
    assert secret not in str(caught.value)
