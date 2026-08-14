"""Streaming request-body boundaries for the browser error sink."""

from __future__ import annotations

import json
import logging

import pytest
from starlette.requests import Request


@pytest.mark.asyncio
async def test_client_error_stream_without_content_length_still_returns_413(
    app_module,
):
    from backend import main

    main._CLIENT_ERR_BUCKETS.clear()
    chunks = [b"x" * main._CLIENT_ERR_BODY_LIMIT, b"y"]
    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        body = chunks[receive_calls]
        receive_calls += 1
        return {
            "type": "http.request",
            "body": body,
            "more_body": receive_calls < len(chunks),
        }

    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/log/client-error",
            "raw_path": b"/api/log/client-error",
            "query_string": b"",
            "headers": [(b"content-type", b"application/octet-stream")],
            "client": ("stream-test", 12345),
            "server": ("testserver", 80),
        },
        receive,
    )
    assert request.headers.get("content-length") is None

    response = await main.client_error_log(request)

    assert response.status_code == 413
    assert json.loads(response.body) == {
        "ok": False,
        "error": "body_too_large",
    }
    assert receive_calls == 2


def test_client_error_rejects_invalid_json_without_logging_raw_body(
    client, caplog,
):
    from backend import main

    main._CLIENT_ERR_BUCKETS.clear()
    private = "private-prompt-and-token"
    with caplog.at_level(logging.DEBUG, logger="muselab.client"):
        response = client.post(
            "/api/log/client-error",
            content=("{not-json:" + private).encode(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "invalid_json"}
    assert private not in caplog.text


def test_client_error_logs_only_strict_safe_projection(client, caplog):
    from backend import main

    main._CLIENT_ERR_BUCKETS.clear()
    private = "private-prompt-token-file-name"
    payload = {
        "kind": "error",
        "name": "TypeError",
        "message": private,
        "stack": f"stack includes {private}",
        "filename": f"/private/{private}.js",
        "url": f"https://example.invalid/?token={private}",
        "ua": private,
        "lineno": 42,
        "colno": 7,
        "lastFetch": {
            "url": f"/api/files/read?path={private}",
            "method": "POST",
        },
    }

    with caplog.at_level(logging.ERROR, logger="muselab.client"):
        response = client.post("/api/log/client-error", json=payload)

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert private not in caplog.text
    assert "example.invalid" not in caplog.text
    assert '"error_name":"TypeError"' in caplog.text
    assert '"last_method":"POST"' in caplog.text
    assert '"reason_fp":' in caplog.text
    assert '"trace_fp":' in caplog.text


def test_render_key_diagnostic_logs_aggregate_counts_only(client, caplog):
    from backend import main

    main._CLIENT_ERR_BUCKETS.clear()
    private = "private-session-and-pane"
    payload = {
        "kind": "message_render_key",
        "session": private,
        "pane": private,
        "issues": [
            {"issue": "duplicate", "count": 4},
            {"issue": "duplicate", "count": 3},
            {"issue": "missing", "count": 2},
            {"issue": "private-issue", "count": 999},
        ],
    }

    with caplog.at_level(logging.WARNING, logger="muselab.client"):
        response = client.post("/api/log/client-error", json=payload)

    assert response.status_code == 200
    assert private not in caplog.text
    assert "private-issue" not in caplog.text
    assert '"duplicate_count":7' in caplog.text
    assert '"missing_count":2' in caplog.text


def test_client_error_rejects_unknown_schema_without_logging_payload(
    client, caplog,
):
    from backend import main

    main._CLIENT_ERR_BUCKETS.clear()
    private = "private-unknown-payload"
    with caplog.at_level(logging.DEBUG, logger="muselab.client"):
        response = client.post(
            "/api/log/client-error",
            json={"kind": "unknown", "message": private},
        )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": "invalid_payload"}
    assert private not in caplog.text
