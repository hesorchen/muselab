"""Streaming request-body boundaries for the browser error sink."""

from __future__ import annotations

import json

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
