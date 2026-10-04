"""Raw responses must serve the file validated by the scoped preview ticket."""
import asyncio

from fastapi import FastAPI
import httpx
import pytest


@pytest.mark.parametrize("mode,suffix,preview,content_type", [
    ("inline", ".png", False, "image/png"),
    ("sandbox", ".svg", True, "image/svg+xml"),
    ("attachment", ".bin", False, "application/octet-stream"),
])
@pytest.mark.parametrize("replacement", ["sensitive_symlink", "atomic_replace", "unchanged"])
def test_raw_stream_holds_checked_target(
    app_module, auth, temp_root, mode, suffix, preview, content_type, replacement,
):
    from backend import files

    target = temp_root / ("public" + suffix)
    approved = b"SYNTHETIC_RAW_APPROVED_TARGET".ljust(64, b".")
    blocked = b"SYNTHETIC_RAW_BLOCKED_TARGET".ljust(64, b".")
    target.write_bytes(approved)
    sensitive = temp_root / ".env"
    sensitive.write_bytes(blocked)
    app = FastAPI()
    app.include_router(files.router)
    started = []

    async def controlled(scope, receive, send):
        async def observe(message):
            if (scope["path"] == "/api/files/raw"
                    and message["type"] == "http.response.start"
                    and message["status"] == 200 and not started):
                # The real response has checked/stat'd A and committed its
                # headers. Change only the on-disk entry before its body read.
                started.append(True)
                if replacement == "sensitive_symlink":
                    target.unlink()
                    target.symlink_to(sensitive)
                elif replacement == "atomic_replace":
                    other = temp_root / ("candidate" + suffix)
                    other.write_bytes(blocked)
                    other.replace(target)
            await send(message)

        await app(scope, receive, observe)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=controlled), base_url="http://synthetic.local",
        ) as api:
            denied = await api.post(
                "/api/files/preview-ticket", headers=auth, json={"path": ".env"},
            )
            assert denied.status_code == 403
            minted = await api.post(
                "/api/files/preview-ticket", headers=auth, json={"path": target.name},
            )
            assert minted.status_code == 200
            # Exercise the real scoped-ticket entry point, without a service
            # token on the raw request or a mocked read/authorization result.
            response = await api.get("/api/files/raw", params={
                "path": target.name, "ticket": minted.json()["ticket"], "preview": preview,
            })
            assert response.status_code == 200
            assert started == [True]
            assert int(response.headers["content-length"]) == len(approved)
            assert len(response.content) == len(approved)
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers["content-type"].startswith(content_type)
            assert response.headers["content-disposition"].startswith(
                "attachment" if mode == "attachment" else "inline",
            )
            if mode == "sandbox":
                assert "sandbox allow-scripts" in response.headers["content-security-policy"]
            assert response.content == approved, {
                "mode": mode,
                "replacement": replacement,
                "sensitive_ticket_status": denied.status_code,
                "raw_status": response.status_code,
                "served_approved": response.content == approved,
                "served_blocked": response.content == blocked,
            }

    asyncio.run(exercise())


def test_raw_preview_ticket_consumes_the_dependency_verified_target(
    app_module, auth, temp_root, monkeypatch,
):
    from backend import files

    approved = temp_root / "approved.bin"
    sibling = temp_root / "sibling.bin"
    approved.write_bytes(b"SYNTHETIC_APPROVED_TICKET_TARGET")
    sibling.write_bytes(b"SYNTHETIC_DIFFERENT_TICKET_TARGET")
    logical = temp_root / "alias.bin"
    logical.symlink_to(approved)
    original_resolve = files.safe_read_resolve
    armed = []
    verified = []

    def retarget_after_resolve(path, root=None, *, external=False):
        target = original_resolve(path, root=root, external=external)
        if armed and path == logical.name:
            # Keep the actual safe-resolve result and ticket validation; only
            # retarget the alias after the dependency resolved approved A.
            armed.clear()
            verified.append(target)
            logical.unlink()
            logical.symlink_to(sibling)
        return target

    monkeypatch.setattr(files, "safe_read_resolve", retarget_after_resolve)
    app = FastAPI()
    app.include_router(files.router)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://synthetic.local",
        ) as api:
            minted = await api.post(
                "/api/files/preview-ticket", headers=auth, json={"path": logical.name},
            )
            assert minted.status_code == 200
            ticket = minted.json()["ticket"]
            armed.append(True)
            response = await api.get(
                "/api/files/raw", params={"path": logical.name, "ticket": ticket},
                follow_redirects=True,
            )
            assert verified == [approved]
            assert response.status_code == 200
            # The original ticket remains reusable for A, but cannot authorize
            # B just because a formerly valid alias now points there.
            assert (await api.get(
                "/api/files/raw", params={"path": logical.name, "ticket": ticket},
            )).status_code == 401
            reused = await api.get(
                "/api/files/raw", params={"path": approved.name, "ticket": ticket},
            )
            assert reused.status_code == 200
            assert reused.content == approved.read_bytes()
            assert (await api.get(
                "/api/files/raw", params={"path": logical.name},
            )).status_code == 401
            assert response.content == approved.read_bytes(), {
                "redirects": [item.status_code for item in response.history],
                "served_approved": response.content == approved.read_bytes(),
                "served_sibling": response.content == sibling.read_bytes(),
            }

    asyncio.run(exercise())
