"""Dreamer shape gates run before episode or candidate writes."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock, Mock

import pytest


def _valid():
    return {
        "episode": {"title": "Useful title", "summary": "Complete summary"},
        "memories": [{
            "kind": "fact", "content": "A complete synthetic fact",
            "source_ids": ["evidence-1"], "confidence": 0.8, "future_use": 0.9,
        }],
    }


def _invalid_cases():
    yield {}
    for key in ("episode", "memories"):
        for value in (None, "wrong", 1, {}):
            result = _valid()
            result[key] = value
            yield result
        result = _valid()
        del result[key]
        yield result
    for key in ("title", "summary"):
        for value in (None, "", " \n\t", 1, [], {}):
            result = _valid()
            result["episode"][key] = value
            yield result
        result = _valid()
        del result["episode"][key]
        yield result
    for value in (None, "wrong", [], 1):
        result = _valid()
        result["memories"].append(value)
        yield result
    for key, values in {
        "kind": [None, [], "unknown"],
        "content": [None, {}, " "],
        "source_ids": [None, "evidence-1", [], [{}], ["foreign-evidence"]],
        "confidence": [None, True, "0.8", "secret-token", -0.1, 1.1,
                       float("nan"), float("inf"), 10 ** 400],
        "future_use": [None, False, "0.8", [], -1, float("-inf")],
        "episode_ids": [None, "episode-1", [{}]],
        "reuse_conditions": [None, "wrong", [1]],
        "attributed_to": [[], "unknown"],
    }.items():
        for value in values:
            result = _valid()
            # Put the invalid item last: earlier valid candidates must not write.
            candidate = copy.deepcopy(result["memories"][0])
            candidate[key] = value
            result["memories"].append(candidate)
            yield result
    for key in ("kind", "content", "source_ids", "confidence", "future_use"):
        result = _valid()
        del result["memories"][0][key]
        yield result
    for key, value in (("outcome", []), ("entities", {}), ("attributes", [])):
        result = _valid()
        result["episode"][key] = value
        yield result


@pytest.mark.parametrize("value", list(_invalid_cases()))
def test_dreamer_rejects_invalid_shapes(value):
    from backend.memory_engine import _validate_dreamer_response

    with pytest.raises(ValueError, match="^invalid_schema$"):
        _validate_dreamer_response(value, {"evidence-1"})


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_dreamer_accepts_valid_batch_and_schema_wrapper(wrapped, empty):
    from backend.memory_engine import _validate_dreamer_response

    value = _valid()
    if empty:
        value["memories"] = []
    response = {"schema": value} if wrapped else value
    assert _validate_dreamer_response(response, {"evidence-1"}) == value


def _engine(monkeypatch):
    from backend.memory_config import MemoryConfig
    from backend.memory_engine import MemoryEngine

    # Exercise real orchestration/provider parsing with only I/O boundaries mocked.
    instance = object.__new__(MemoryEngine)
    instance._generation_lock = asyncio.Lock()
    instance._config_async = AsyncMock(return_value=MemoryConfig())
    store = Mock()
    store.episode.return_value = {
        "id": "episode-1", "outcome": "success", "title": "Old title",
        "summary": "Old valid summary", "evidence": [{
            "id": "evidence-1", "role": "user", "event_type": "message",
            "content": "Synthetic evidence",
        }],
    }
    store.list_episodes.return_value = []

    async def store_call(operation):
        return operation(store)

    monkeypatch.setattr(instance, "_store_call", store_call)
    instance._prepare_verified_memory = AsyncMock(return_value=None)
    return instance, store


def test_exhausted_schema_budget_has_zero_business_writes(monkeypatch):
    from backend.memory_providers import GenerationError, GenerationProvider

    instance, store = _engine(monkeypatch)
    value = _valid()
    value["memories"].append({"content": "secret-token"})
    complete = AsyncMock(return_value=json.dumps(value))
    monkeypatch.setattr(GenerationProvider, "complete", complete)
    before = copy.deepcopy(store.episode.return_value)
    with pytest.raises(GenerationError) as caught:
        asyncio.run(instance._consolidate_episode("episode-1"))
    assert caught.value.reason == "invalid_schema"
    assert caught.value.retryable is False
    assert complete.await_count == 2
    assert store.episode.return_value == before
    assert [call[0] for call in store.method_calls] == ["episode"]
    instance._prepare_verified_memory.assert_not_awaited()


@pytest.mark.parametrize("first", ["not JSON", "[]", "{}"])
def test_repaired_zero_fact_summary_is_written_in_full(monkeypatch, first):
    from backend.memory_providers import GenerationProvider

    instance, store = _engine(monkeypatch)
    value = _valid()
    value["memories"] = []
    value["episode"]["summary"] = "Complete summary " * 400
    complete = AsyncMock(side_effect=[first, json.dumps({"schema": value})])
    monkeypatch.setattr(GenerationProvider, "complete", complete)
    asyncio.run(instance._consolidate_episode("episode-1"))
    assert complete.await_count == 2
    store.commit_consolidation.assert_called_once()
    written = store.commit_consolidation.call_args.args[2]
    assert store.commit_consolidation.call_args.args[3] == []
    assert written["title"] == value["episode"]["title"]
    assert written["summary"] == value["episode"]["summary"].strip()
    instance._prepare_verified_memory.assert_not_awaited()


def test_valid_candidates_reach_verifier_only_after_batch_validation(monkeypatch):
    from backend.memory_providers import GenerationProvider

    instance, store = _engine(monkeypatch)
    value = _valid()
    monkeypatch.setattr(GenerationProvider, "complete", AsyncMock(
        return_value=json.dumps(value)))
    asyncio.run(instance._consolidate_episode("episode-1"))
    store.commit_consolidation.assert_called_once()
    instance._prepare_verified_memory.assert_awaited_once_with(
        value["memories"][0], "episode-1", ["evidence-1"],
        cfg=instance.config(), source_episode=store.episode.return_value)
