"""Provider contracts without external network services."""
import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("value", ["high", "nan", "inf", "-inf"])
def test_model_numbers_reject_malformed_output_without_retry(value):
    from backend.memory_engine import _model_float
    from backend.memory_providers import GenerationError

    with pytest.raises(GenerationError) as exc_info:
        _model_float(value)

    assert exc_info.value.retryable is False
    assert exc_info.value.category == "malformed_response"


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
    from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationProvider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MUSELAB_MEMORY_DIR", str(tmp_path / "memory"))
    seen = {}

    async def fake_query(*, prompt, options):
        seen["prompt"] = prompt
        seen["options"] = options
        yield AssistantMessage(content=[TextBlock("ok")], model="claude")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                            is_error=False, num_turns=1, session_id="fixture")

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    provider = GenerationProvider(MemoryConfig(generation_model="claude-sonnet-4-6"))
    assert _run(provider.complete("system", "prompt")) == "ok"
    assert seen["options"].tools == []
    assert seen["options"].allowed_tools == []
    assert seen["options"].mcp_servers == {}
    assert seen["options"].setting_sources == []
    assert seen["options"].skills == []


def test_ducc_generation_model_never_routes_through_http(monkeypatch):
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationProvider

    # Even when a native Anthropic key is present, ducc:-prefixed models are a
    # CLI runtime and must never be sent to an HTTP /v1/messages endpoint.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    provider = GenerationProvider(MemoryConfig(
        generation_model="ducc:deepseek-v4-pro"))
    assert provider._route() is None
    assert provider.metadata() == ("ducc", "ducc:deepseek-v4-pro")


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


@pytest.mark.parametrize("intermediate,final", [
    (None, '{"memories": []}'),
    ('partial output', '{"memories": []}'),
    ('{"memories": []}', None),
])
def test_sdk_memory_uses_completed_final_answer_and_closes_iterator(
        tmp_path, monkeypatch, intermediate, final):
    import claude_agent_sdk
    from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationProvider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MUSELAB_MEMORY_DIR", str(tmp_path / "memory"))
    closed = []

    async def fake_query(*, prompt, options):
        try:
            if intermediate is not None:
                yield AssistantMessage(content=[TextBlock(intermediate)], model="fixture")
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id="fixture",
                                result=final)
            yield AssistantMessage(content=[TextBlock("ignored after terminal")], model="fixture")
        finally:
            closed.append(True)

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    provider = GenerationProvider(MemoryConfig(generation_model="claude-sonnet-4-6"))
    assert _run(provider.complete_json("system", "prompt")) == {"memories": []}
    assert closed == [True]


@pytest.mark.parametrize("terminal", ["missing", "error"])
def test_sdk_memory_rejects_partial_output_when_completion_fails(
        tmp_path, monkeypatch, terminal):
    import claude_agent_sdk
    from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationError, GenerationProvider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MUSELAB_MEMORY_DIR", str(tmp_path / "memory"))
    closed = []

    async def fake_query(*, prompt, options):
        try:
            yield AssistantMessage(content=[TextBlock('{"memories": []}')], model="fixture")
            if terminal == "error":
                yield ResultMessage(subtype="error", duration_ms=1, duration_api_ms=1,
                                    is_error=True, num_turns=1, session_id="fixture",
                                    result="sensitive detail", api_error_status=500)
        finally:
            closed.append(True)

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    provider = GenerationProvider(MemoryConfig(generation_model="claude-sonnet-4-6"))
    with pytest.raises(GenerationError) as caught:
        _run(provider.complete_json("system", "prompt"))
    assert caught.value.retryable
    assert caught.value.category == "transient_provider"
    assert "sensitive" not in str(caught.value)
    assert closed == [True]


def test_sdk_memory_drains_nested_query_cleanup_before_return(tmp_path, monkeypatch):
    import claude_agent_sdk
    from claude_agent_sdk.types import ResultMessage
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationProvider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MUSELAB_MEMORY_DIR", str(tmp_path / "memory"))
    closed = []

    async def inner():
        try:
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id="fixture", result="ok")
        finally:
            closed.append(True)

    # Mirrors the public SDK wrapper's nested async-for ownership.
    async def fake_query(*, prompt, options):
        async for message in inner():
            yield message

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    provider = GenerationProvider(MemoryConfig(generation_model="claude-sonnet-4-6"))

    async def exercise():
        assert await provider.complete("system", "prompt") == "ok"
        # Assert before asyncio.run's shutdown_asyncgens can hide a leaked query.
        assert closed == [True]

    _run(exercise())


def test_generation_diagnostics_correlate_timeout_without_private_details(monkeypatch, capsys):
    import httpx
    from backend import observability
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationError, GenerationProvider, generation_job_ref

    monkeypatch.setenv("MUSELAB_PERF_LOG", "1")
    monkeypatch.setattr(observability, "_perf_writer", None)
    provider = GenerationProvider(MemoryConfig(generation_model="private-model-detail"))
    monkeypatch.setattr(provider, "_route", lambda: None)

    async def fail(*args):
        raise httpx.ReadTimeout("private-request-detail")

    monkeypatch.setattr(provider, "_complete_with_sdk", fail)
    token = generation_job_ref.set("012345abcdef")
    try:
        with pytest.raises(GenerationError) as caught:
            _run(provider.complete("private-system-detail", "private-prompt-detail"))
        assert caught.value.reason == "timeout"
    finally:
        generation_job_ref.reset(token)
    output = capsys.readouterr().err
    assert '"event":"memory.generation"' in output
    assert '"job_ref":"012345abcdef"' in output
    assert '"cause_kind":"ReadTimeout"' in output
    assert '"reason":"timeout"' in output
    assert "private-" not in output


@pytest.mark.parametrize("value,reason", [("not JSON", "invalid_json"), ("[]", "non_object_json")])
def test_generation_json_diagnostics_have_safe_specific_reason(monkeypatch, value, reason):
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationError, GenerationProvider
    from backend.memory_engine import classify_memory_failure
    provider = GenerationProvider(MemoryConfig())

    async def complete(*args):
        return value

    monkeypatch.setattr(provider, "complete", complete)
    with pytest.raises(GenerationError) as caught:
        _run(provider.complete_json("system", "prompt"))
    assert classify_memory_failure(caught.value)[1]["reason"] == reason
    assert GenerationError(retryable=False, reason="private detail").reason == "unknown"


@pytest.mark.parametrize("budget", ["unlimited", "positive", "background"])
def test_recall_providers_use_single_budget_without_changing_background(monkeypatch, budget):
    import time
    from contextlib import nullcontext
    from backend import memory_providers as module
    from backend.memory_config import EmbeddingConfig, RerankConfig, VectorConfig
    seen = []

    class Response:
        def raise_for_status(self): pass

        def json(self):
            return {"data": [{"index": 0, "embedding": [1., 0.]}],
                    "result": [], "results": [{"index": 0, "relevance_score": 1.}]}

    class Client:
        def __init__(self, *, timeout): seen.append(timeout)

        async def __aenter__(self): return self

        async def __aexit__(self, *args): return False

        async def post(self, *args, **kwargs): return Response()

        async def request(self, *args, **kwargs): return Response()

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)

    async def scenario():
        context = (nullcontext() if budget == "background" else
                   module.recall_request_budget(None if budget == "unlimited" else time.perf_counter() + 30))
        with context:
            await module.EmbeddingProvider(EmbeddingConfig(
                base_url="http://example.test", model="test", timeout_seconds=1)).embed(["synthetic"])
            await module.QdrantVectorStore(VectorConfig(
                url="http://example.test", timeout_seconds=1)).search([1., 0.], owner_id="default", limit=1)
            await module.Reranker(RerankConfig(
                enabled=True, base_url="http://example.test", model="test", timeout_seconds=1)).rerank("synthetic", ["fact"])
        assert module._request_timeout(1) == 1
        assert len(seen) == 3
        for timeout in seen:
            values = timeout.as_dict().values()
            if budget == "unlimited":
                assert all(value is None for value in values)
            elif budget == "positive":
                assert all(20 < value <= 30 for value in values)
            else:
                assert all(value == 1 for value in values)

    _run(scenario())


@pytest.mark.parametrize("budget", [None, .02])
def test_pgvector_search_cancels_actual_async_query(monkeypatch, budget):
    import time
    import psycopg
    from backend import memory_providers as module
    from backend.memory_config import VectorConfig
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Connection:
        @classmethod
        async def connect(cls, url, **kwargs):
            assert kwargs["connect_timeout"] == 0
            return cls()

        async def __aenter__(self): return self

        async def __aexit__(self, *args): return False

        async def execute(self, sql, params):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(psycopg, "AsyncConnection", Connection)

    async def scenario():
        store = module.PgVectorStore(VectorConfig(
            provider="pgvector", url="postgresql://example.test/memory", collection="memory"))
        with module.recall_request_budget(None if budget is None else time.perf_counter() + budget):
            task = asyncio.create_task(store.search([1., 0.], owner_id="default", limit=1))
        await entered.wait()
        if budget is None:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, .5)
        assert cancelled.is_set()

    _run(scenario())


@pytest.mark.parametrize("wrapped", [True, False])
def test_qdrant_search_accepts_object_and_legacy_list_results(monkeypatch, wrapped):
    from backend.memory_config import VectorConfig
    from backend.memory_providers import QdrantVectorStore
    points = [{"id": "point", "payload": {"memory_id": "memory"}, "score": .9}]

    class Response:
        def json(self): return {"result": {"points": points} if wrapped else points}

    async def request(*args, **kwargs): return Response()

    store = QdrantVectorStore(VectorConfig(url="http://example.test"))
    monkeypatch.setattr(store, "_request", request)
    result = _run(store.search([1., 0.], owner_id="default", limit=1))
    assert result[0]["id"] == "memory"
    assert result[0]["score"] == .9


def test_generation_repairs_non_object_once_and_preserves_schema(monkeypatch):
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationProvider
    provider = GenerationProvider(MemoryConfig())
    calls = []
    async def complete(system, prompt):
        calls.append((system, prompt))
        return '[]' if len(calls) == 1 else '{"memories":[{"content":"complete synthetic fact"}]}'
    monkeypatch.setattr(provider, 'complete', complete)
    assert _run(provider.complete_json('Object schema', 'synthetic input')) == {
        'memories': [{'content': 'complete synthetic fact'}]}
    assert len(calls) == 2
    assert calls[0][1] == calls[1][1]
    assert 'wrong format' in calls[1][0]


def test_sdk_memory_applies_native_output_and_reasoning_budget(tmp_path, monkeypatch):
    import claude_agent_sdk
    from claude_agent_sdk.types import ResultMessage
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationProvider

    monkeypatch.setenv("MUSELAB_MEMORY_DIR", str(tmp_path / "memory"))
    provider = GenerationProvider(MemoryConfig(generation_model="claude-sonnet-4-6"))
    monkeypatch.setattr(provider, "_route", lambda: None)

    async def fake_query(*, prompt, options):
        assert options.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "1500"
        assert options.thinking == {"type": "disabled"}
        assert options.effort == "low"
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                            is_error=False, num_turns=1, session_id="fixture", result="bounded output")

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    assert _run(provider.complete("system", "prompt", max_tokens=1500)) == "bounded output"
