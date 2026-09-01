from __future__ import annotations

import json
import math

import pytest
from claude_agent_sdk import ResultError

from backend.sdk_lifecycle import (
    normalize_model_usage,
    normalize_origin,
    normalize_terminal_reason,
    result_error_info,
    terminal_status,
)


def test_normalize_origin_keeps_only_bounded_attribution_fields():
    origin = {
        "kind": "task-notification",
        "subkind": "scheduled-trigger",
        "senderTaskId": "task-123",
        "body": "private peer message",
        "from": "peer-address",
        "server": "private-server",
        "name": "Private Name",
        "fromSession": "private-session",
        "verifiedPeerPid": 1234,
        "futureSecret": "must not cross the boundary",
    }

    assert normalize_origin(origin) == {
        "kind": "task-notification",
        "subkind": "scheduled-trigger",
        "task_id": "task-123",
        "source": "sdk",
    }


def test_normalize_origin_preserves_safe_future_kind_and_does_not_mutate_input():
    origin = {"kind": "future-delivery", "subkind": "future-subkind"}

    normalized = normalize_origin(origin)

    assert normalized == {
        "kind": "future-delivery",
        "subkind": "future-subkind",
        "task_id": None,
        "source": "sdk",
    }
    assert origin == {"kind": "future-delivery", "subkind": "future-subkind"}


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "human",
        {},
        {"kind": 1},
        {"kind": ""},
        {"kind": " human"},
        {"kind": "peer\nforged"},
        {"kind": "x" * 65},
    ],
)
def test_normalize_origin_rejects_invalid_or_overlong_kind(origin):
    assert normalize_origin(origin) is None


def test_normalize_origin_drops_invalid_optional_fields_without_dropping_kind():
    assert normalize_origin({
        "kind": "peer",
        "subkind": "x" * 65,
        "senderTaskId": "x" * 257,
    }) == {
        "kind": "peer",
        "subkind": None,
        "task_id": None,
        "source": "sdk",
    }


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("completed", "completed"),
        ("future_reason", "future_reason"),
        (None, ""),
        (123, ""),
        (" max_turns", ""),
        ("bad\nreason", ""),
        ("x" * 129, ""),
    ],
)
def test_normalize_terminal_reason_is_bounded_and_forward_compatible(reason, expected):
    assert normalize_terminal_reason(reason) == expected


@pytest.mark.parametrize(
    ("reason", "is_error", "cancelled", "expected"),
    [
        ("completed", False, False, "completed"),
        ("completed", True, False, "failed"),
        ("max_turns", False, False, "stopped"),
        ("max_turns", True, False, "stopped"),
        ("aborted_streaming", True, False, "cancelled"),
        ("aborted_tools", False, False, "cancelled"),
        ("completed", True, True, "cancelled"),
        ("future_reason", False, False, "completed"),
        ("future_reason", True, False, "failed"),
    ],
)
def test_terminal_status_preserves_product_cancel_and_existing_error_evidence(
    reason, is_error, cancelled, expected,
):
    assert terminal_status(
        reason, is_error=is_error, cancelled=cancelled) == expected


def test_normalize_model_usage_keeps_only_typed_sdk_fields():
    raw = {
        "claude-opus-alias": {
            "inputTokens": 100,
            "outputTokens": 20,
            "cacheReadInputTokens": 30,
            "cacheCreationInputTokens": 40,
            "webSearchRequests": 2,
            "costUSD": 1.25,
            "contextWindow": 200_000,
            "maxOutputTokens": 32_000,
            "canonicalModel": "claude-opus-canonical",
            "provider": "gateway",
            "rawPayload": {"secret": "do not copy"},
            "futureSecret": "do not copy",
        }
    }

    assert normalize_model_usage(raw) == {
        "claude-opus-alias": {
            "inputTokens": 100,
            "outputTokens": 20,
            "cacheReadInputTokens": 30,
            "cacheCreationInputTokens": 40,
            "webSearchRequests": 2,
            "costUSD": 1.25,
            "contextWindow": 200_000,
            "maxOutputTokens": 32_000,
            "canonicalModel": "claude-opus-canonical",
            "provider": "gateway",
        }
    }
    assert raw["claude-opus-alias"]["futureSecret"] == "do not copy"


def test_normalize_model_usage_drops_negative_nonfinite_boolean_and_huge_values():
    raw = {
        "model": {
            "inputTokens": -1,
            "outputTokens": True,
            "cacheReadInputTokens": 1 << 63,
            "cacheCreationInputTokens": 9,
            "webSearchRequests": 1.5,
            "costUSD": math.nan,
            "contextWindow": math.inf,
            "maxOutputTokens": 12,
        },
        "negative-cost": {"costUSD": -0.01},
        "infinite-cost": {"costUSD": math.inf},
        "huge-cost": {"costUSD": 1_000_000_001},
    }

    assert normalize_model_usage(raw) == {
        "model": {
            "cacheCreationInputTokens": 9,
            "maxOutputTokens": 12,
        }
    }


def test_normalize_model_usage_rejects_overlong_ids_and_caps_inspected_entries():
    raw = {
        **{f"model-{index}": {"inputTokens": index} for index in range(40)},
        "x" * 257: {"inputTokens": 1},
    }

    normalized = normalize_model_usage(raw)

    assert len(normalized) == 32
    assert list(normalized) == [f"model-{index}" for index in range(32)]
    assert "x" * 257 not in normalized


def test_normalize_model_usage_drops_overlong_optional_identifiers():
    assert normalize_model_usage({
        "model": {
            "inputTokens": 1,
            "canonicalModel": "x" * 257,
            "provider": "x" * 65,
        }
    }) == {"model": {"inputTokens": 1}}


@pytest.mark.parametrize("value", [None, [], "usage", {"model": None}])
def test_normalize_model_usage_rejects_non_mapping_shapes(value):
    assert normalize_model_usage(value) == {}


def test_result_error_info_is_bounded_and_never_exposes_raw_data():
    error = ResultError(
        "terminal failure",
        data={
            "subtype": "error_during_execution",
            "errors": ["first error", "second\x00error"],
            "result": "API Error: overloaded",
            "api_error_status": 529,
            "terminal_reason": "api_error",
            "session_id": "11111111-1111-4111-8111-111111111111",
            "secret": "must-never-leak",
            "body": "private raw protocol body",
        },
        exit_code=1,
    )

    info = result_error_info(error)

    assert info == {
        "subtype": "error_during_execution",
        "errors": ["first error", "second�error"],
        "result": "API Error: overloaded",
        "api_error_status": 529,
        "terminal_reason": "api_error",
        "session_id": "11111111-1111-4111-8111-111111111111",
        "exit_code": 1,
    }
    encoded = json.dumps(info, ensure_ascii=False)
    assert "must-never-leak" not in encoded
    assert "private raw protocol body" not in encoded
    assert "data" not in info


def test_result_error_info_caps_error_count_and_text_lengths():
    error = ResultError(
        "large terminal failure",
        data={
            "subtype": "x" * 129,
            "errors": [f"{index}:" + "x" * 3_000 for index in range(20)],
            "result": "y" * 10_000,
            "terminal_reason": "z" * 129,
            "session_id": "s" * 257,
        },
        exit_code=1 << 31,
    )

    info = result_error_info(error)

    assert info is not None
    assert info["subtype"] is None
    assert len(info["errors"]) == 16
    assert all(len(item) <= 2_048 for item in info["errors"])
    assert info["result"] is not None and len(info["result"]) == 8_192
    assert info["result"].endswith("…")
    assert info["terminal_reason"] == ""
    assert info["session_id"] is None
    assert info["exit_code"] is None


def test_result_error_info_rejects_invalid_status_and_non_result_error():
    error = ResultError(
        "bad status",
        data={"api_error_status": True},
        exit_code=True,
    )

    info = result_error_info(error)

    assert info is not None
    assert info["api_error_status"] is None
    assert info["exit_code"] is None
    assert result_error_info(RuntimeError("not a result error")) is None
