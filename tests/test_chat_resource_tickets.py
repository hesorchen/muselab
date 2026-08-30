"""Security contracts for header-less chat resource tickets."""

from __future__ import annotations

import base64
import time
from pathlib import Path

from backend.capability_tickets import CapabilityTicketStore

from .conftest import TEST_TOKEN


def _mint(client, auth, **body) -> dict:
    response = client.post(
        "/api/chat/resource-ticket",
        headers=auth,
        json=body,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ticket"].startswith("chat-resource.")
    assert TEST_TOKEN not in data["url"]
    return data


def test_capability_ticket_supports_bounded_reuse() -> None:
    store = CapabilityTicketStore()
    ticket = store.mint("resource", ("one",), ttl=60, max_uses=3)

    # A wrong scope neither authorizes nor consumes the ticket.
    assert store.validate(ticket, "resource", ("other",)) is False
    assert store.validate(ticket, "resource", ("one",)) is True
    assert store.validate(ticket, "resource", ("one",)) is True
    assert store.validate(ticket, "resource", ("one",)) is True
    assert store.validate(ticket, "resource", ("one",)) is False


def test_export_uses_session_bound_single_use_ticket(client, auth) -> None:
    first = client.post(
        "/api/chat/sessions", headers=auth, json={"name": "first-export"},
    ).json()["id"]
    second = client.post(
        "/api/chat/sessions", headers=auth, json={"name": "second-export"},
    ).json()["id"]
    resource = _mint(client, auth, resource="export", session_id=first)

    legacy = client.get(
        f"/api/chat/sessions/{first}/export", params={"token": TEST_TOKEN},
    )
    assert legacy.status_code == 401

    wrong_scope = client.get(
        f"/api/chat/sessions/{second}/export",
        params={"ticket": resource["ticket"]},
    )
    assert wrong_scope.status_code == 401

    exported = client.get(resource["url"])
    assert exported.status_code == 200
    assert "# first-export" in exported.text
    assert client.get(resource["url"]).status_code == 401


def test_queued_image_ticket_is_id_bound_and_bounded(
    client, auth, app_module,
) -> None:
    del app_module
    from backend import chat as chat_mod

    aid = "abcdef123456"
    other_aid = "fedcba654321"
    encoded = base64.b64encode(b"image-bytes").decode("ascii")
    with chat_mod._image_store_lock:
        chat_mod._image_store[aid] = {
            "kind": "image", "mime": "image/png",
            "b64": encoded, "name": "image.png", "ts": time.time(),
        }
        chat_mod._image_store[other_aid] = {
            "kind": "image", "mime": "image/png",
            "b64": encoded, "name": "other.png", "ts": time.time(),
        }
    try:
        resource = _mint(
            client, auth, resource="queued-image", attachment_id=aid,
        )
        assert resource["max_uses"] == 8
        assert client.get(
            f"/api/chat/queued-image/{other_aid}",
            params={"ticket": resource["ticket"]},
        ).status_code == 401
        assert client.get(
            f"/api/chat/queued-image/{aid}", params={"token": TEST_TOKEN},
        ).status_code == 401
        for _ in range(resource["max_uses"]):
            assert client.get(resource["url"]).status_code == 200
        assert client.get(resource["url"]).status_code == 401
    finally:
        with chat_mod._image_store_lock:
            chat_mod._image_store.pop(aid, None)
            chat_mod._image_store.pop(other_aid, None)


def test_attachment_ticket_is_exact_and_never_contains_global_token(
    client, auth, app_module,
) -> None:
    del app_module
    from backend import chat as chat_mod

    sid = client.post(
        "/api/chat/sessions", headers=auth, json={"name": "attachment"},
    ).json()["id"]
    saved = chat_mod._persist_attachment(
        sid, "abcdef123456", "report.png", b"image-bytes",
    )
    assert saved is not None
    filename = Path(saved[0]).name
    resource = _mint(
        client, auth, resource="attachment",
        session_id=sid, filename=filename,
    )
    assert resource["max_uses"] == 16
    assert client.get(
        f"/api/chat/attachments/{sid}/{filename}",
        params={"token": TEST_TOKEN},
    ).status_code == 401
    assert client.get(
        f"/api/chat/attachments/{sid}/different.png",
        params={"ticket": resource["ticket"]},
    ).status_code == 401
    for _ in range(resource["max_uses"]):
        assert client.get(resource["url"]).status_code == 200
    assert client.get(resource["url"]).status_code == 401


def test_frontend_never_places_global_token_in_chat_resource_urls() -> None:
    root = Path(__file__).resolve().parents[1]
    app = (root / "frontend" / "app.js").read_text(encoding="utf-8")
    html = (root / "frontend" / "index.html").read_text(encoding="utf-8")

    assert 'fetch("/api/chat/resource-ticket"' in app
    assert '_mintChatResourceUrl("queued-image"' in app
    assert '_mintChatResourceUrl(\n          "export"' in app
    assert '@click="openMessageImage(im)"' in html
    assert "/export?token=" not in app
    assert "/queued-image/${a.id}?token=" not in app
    assert "im.url + '?token='" not in html
