"""The target validated by a download ticket is the target actually opened."""
import asyncio

from fastapi import FastAPI
import httpx
import pytest


@pytest.mark.parametrize("shape", ["file_link", "directory_link"])
@pytest.mark.parametrize("scope", ["workspace", "registered", "external"])
def test_download_ticket_keeps_verified_target_when_link_is_retargeted(
    app_module, client, auth, temp_root, tmp_path, monkeypatch, shape, scope,
):
    from backend import files

    selected = temp_root
    base = temp_root
    external = scope == "external"
    if external:
        base = tmp_path / "outside"
        base.mkdir()
    elif scope == "registered":
        selected = temp_root / "nested"
        selected.mkdir()
        assert client.post(
            "/api/chat/workspaces", headers=auth, json={"path": str(selected)},
        ).status_code == 200
        auth = {**auth, "X-Muselab-Workspace": str(selected)}

    approved_dir = base / "approved"
    sibling_dir = base / "sibling"
    approved_dir.mkdir()
    sibling_dir.mkdir()
    approved = approved_dir / "public.txt"
    sibling = sibling_dir / "public.txt"
    approved.write_bytes(b"SYNTHETIC_APPROVED_TARGET")
    sibling.write_bytes(b"SYNTHETIC_DIFFERENT_TARGET")
    logical_root = base if external else selected
    link = logical_root / "link"
    if shape == "file_link":
        link.symlink_to(approved)
        logical = link
        replacement = sibling
    else:
        link.symlink_to(approved_dir, target_is_directory=True)
        logical = link / "public.txt"
        replacement = sibling_dir
    path = str(logical) if external else str(logical.relative_to(selected))

    original_validate = files.tickets.validate
    verified = []

    def retarget_after_validation(ticket, kind, resource_scope):
        valid = original_validate(ticket, kind, resource_scope)
        if valid and kind == "download":
            verified.append(resource_scope)
            assert resource_scope == (str(approved.resolve()), str(selected.resolve()))
            link.unlink()
            link.symlink_to(replacement, target_is_directory=shape == "directory_link")
        return valid

    monkeypatch.setattr(files.tickets, "validate", retarget_after_validation)
    app = FastAPI()
    app.include_router(files.router)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://synthetic.local",
        ) as api:
            payload = {"path": path, "external": external}
            minted = await api.post("/api/files/download-ticket", headers=auth, json=payload)
            assert minted.status_code == 200
            params = {**payload, "ticket": minted.json()["ticket"]}
            workspace_headers = {key: value for key, value in auth.items() if key != "X-Auth-Token"}
            result = await api.get("/api/files/download", params=params, headers=workspace_headers)
            assert result.status_code == 200
            assert result.content == approved.read_bytes()
            assert result.content != sibling.read_bytes()
            assert len(verified) == 1
            assert (await api.get(
                "/api/files/download", params=params, headers=workspace_headers,
            )).status_code == 401
            # The full service token does not replace the scoped download ticket.
            assert (await api.get(
                "/api/files/download", params=payload, headers=auth,
            )).status_code == 401

    asyncio.run(exercise())


def test_wrong_download_target_does_not_consume_valid_ticket(app_module, auth, temp_root):
    from backend import files

    (temp_root / "approved.txt").write_bytes(b"SYNTHETIC_APPROVED_TARGET")
    (temp_root / "sibling.txt").write_bytes(b"SYNTHETIC_DIFFERENT_TARGET")
    app = FastAPI()
    app.include_router(files.router)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://synthetic.local",
        ) as api:
            minted = await api.post(
                "/api/files/download-ticket", headers=auth, json={"path": "approved.txt"},
            )
            assert minted.status_code == 200
            ticket = minted.json()["ticket"]
            wrong = await api.get(
                "/api/files/download", params={"path": "sibling.txt", "ticket": ticket},
            )
            assert wrong.status_code == 401
            valid = await api.get(
                "/api/files/download", params={"path": "approved.txt", "ticket": ticket},
            )
            assert valid.status_code == 200
            assert valid.content == b"SYNTHETIC_APPROVED_TARGET"
            assert (await api.get(
                "/api/files/download", params={"path": "approved.txt", "ticket": ticket},
            )).status_code == 401

    asyncio.run(exercise())
