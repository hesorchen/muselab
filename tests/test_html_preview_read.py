"""HTML bridge reads retain the authorized file and a bounded byte budget."""
import asyncio
import os
from pathlib import Path

from fastapi import FastAPI
import httpx
import pytest


async def _preview(files, auth, target, *, check_blocked=False):
    app = FastAPI()
    app.include_router(files.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://synthetic.local",
    ) as api:
        if check_blocked:
            assert (await api.post(
                "/api/files/preview-ticket", headers=auth, json={"path": ".env"},
            )).status_code == 403
        minted = await api.post(
            "/api/files/preview-ticket", headers=auth, json={"path": target.name},
        )
        assert minted.status_code == 200
        return await api.get("/api/files/raw", params={
            "path": target.name, "ticket": minted.json()["ticket"], "preview": True,
        })


@pytest.mark.parametrize("replace_target", [False, True])
def test_html_bridge_uses_a_regular_authorized_file(
    app_module, auth, temp_root, monkeypatch, replace_target,
):
    from backend import files

    target = temp_root / "report.html"
    target.write_text("<body>APPROVED_HTML_FIXTURE</body>", encoding="utf-8")
    blocked = temp_root / ".env"
    blocked.write_text("BLOCKED_HTML_FIXTURE", encoding="utf-8")
    original_inject = files._inject_preview_html_bridge
    entered = []

    def before_injection(resolved):
        assert resolved == target
        entered.append(True)
        if replace_target:
            target.unlink()
            target.symlink_to(blocked)
        return original_inject(resolved)

    monkeypatch.setattr(files, "_inject_preview_html_bridge", before_injection)
    response = asyncio.run(_preview(files, auth, target, check_blocked=True))
    assert entered == [True]
    if replace_target:
        assert response.status_code in {403, 404}
        assert b"BLOCKED_HTML_FIXTURE" not in response.content
    else:
        assert response.status_code == 200
        assert "APPROVED_HTML_FIXTURE" in response.text
        assert files._PREVIEW_HTML_BRIDGE in response.text
        assert "sandbox allow-scripts" in response.headers["content-security-policy"]


def test_html_bridge_caps_read_when_file_grows_after_size_check(
    app_module, auth, temp_root, monkeypatch,
):
    from backend import files

    target = temp_root / "growing.html"
    target.write_bytes(b"<body>small</body>")
    identity = target.stat()
    grown = b"<body>" + b"GROWTH_FIXTURE" * 100 + b"</body>"
    monkeypatch.setattr(files, "_PREVIEW_INJECT_MAX_BYTES", 128)
    original_inject = files._inject_preview_html_bridge
    original_stat = Path.stat
    original_fstat = os.fstat
    active = []
    mutated = []

    def after_metadata(info):
        if (active and not mutated
                and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)):
            # Return the actual size snapshot; append/rewrite only after the
            # OS produced it, just as an independent writer can do.
            assert info.st_size < 128
            target.write_bytes(grown)
            mutated.append(True)
        return info

    def path_stat(path, *args, **kwargs):
        return after_metadata(original_stat(path, *args, **kwargs))

    def file_stat(fd):
        return after_metadata(original_fstat(fd))

    def during_injection(resolved):
        active.append(True)
        try:
            return original_inject(resolved)
        finally:
            active.clear()

    # Old path-stat and new descriptor-stat paths observe the same real
    # post-stat mutation. Unrelated files/descriptors are never modified.
    monkeypatch.setattr(Path, "stat", path_stat)
    monkeypatch.setattr(os, "fstat", file_stat)
    monkeypatch.setattr(files, "_inject_preview_html_bridge", during_injection)
    response = asyncio.run(_preview(files, auth, target))
    assert mutated == [True]
    assert response.status_code == 200
    assert response.content == grown
    assert files._PREVIEW_HTML_BRIDGE not in response.text


@pytest.mark.parametrize("payload,injected", [
    ("<body>caf\u00e9\r\nline\r</body>".encode(), True),
    (b"<body>invalid\xff</body>", False),
    (b"<body>" + b"x" * 128 + b"</body>", False),
])
def test_html_bridge_preserves_text_and_streaming_fallbacks(
    app_module, auth, temp_root, monkeypatch, payload, injected,
):
    from backend import files

    monkeypatch.setattr(files, "_PREVIEW_INJECT_MAX_BYTES", 64)
    target = temp_root / "control.html"
    target.write_bytes(payload)
    response = asyncio.run(_preview(files, auth, target))
    assert response.status_code == 200
    assert "sandbox allow-scripts" in response.headers["content-security-policy"]
    if injected:
        assert files._PREVIEW_HTML_BRIDGE in response.text
        assert "caf\u00e9\nline\n" in response.text
        assert "\r" not in response.text
    else:
        assert response.content == payload
