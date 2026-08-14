"""Privacy and shape contracts for compact performance events."""

from __future__ import annotations

import json

import pytest


def _payload(stderr: str) -> dict:
    line = stderr.strip()
    assert line.startswith("[perf] ")
    return json.loads(line.removeprefix("[perf] "))


def test_perf_event_is_one_bounded_structured_line(monkeypatch, capsys):
    from backend import observability as obs

    monkeypatch.setenv("MUSELAB_PERF_LOG", "1")
    obs.perf_event(
        "chat.turn",
        sid=obs.short_id("12345678-rest-is-private"),
        turn=obs.short_id("abcdef12-rest-is-private"),
        status="completed",
        total_ms=123,
    )

    payload = _payload(capsys.readouterr().err)
    assert payload == {
        "event": "chat.turn",
        "sid": "12345678",
        "turn": "abcdef12",
        "status": "completed",
        "total_ms": 123,
    }


@pytest.mark.parametrize(
    "field",
    ["prompt", "file_path", "request_url", "tool_content", "auth_token"],
)
def test_perf_event_rejects_sensitive_field_names(monkeypatch, field):
    from backend import observability as obs

    monkeypatch.setenv("MUSELAB_PERF_LOG", "1")
    with pytest.raises(ValueError, match="sensitive or invalid"):
        obs.perf_event("privacy.test", **{field: "must-not-be-written"})


def test_perf_event_can_be_disabled(monkeypatch, capsys):
    from backend import observability as obs

    monkeypatch.setenv("MUSELAB_PERF_LOG", "0")
    obs.perf_event("chat.turn", status="completed")
    assert capsys.readouterr().err == ""


def test_slow_threshold_is_bounded_and_elapsed_never_negative(monkeypatch):
    from backend import observability as obs

    monkeypatch.setenv("MUSELAB_SLOW_REQUEST_MS", "not-a-number")
    assert obs.slow_request_ms() == 500
    monkeypatch.setenv("MUSELAB_SLOW_REQUEST_MS", "1")
    assert obs.slow_request_ms() == 25
    monkeypatch.setenv("MUSELAB_SLOW_REQUEST_MS", "999999")
    assert obs.slow_request_ms() == 60_000
    assert obs.elapsed_ms(10.0, 9.0) == 0
