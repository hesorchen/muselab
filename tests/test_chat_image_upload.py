"""Tests for POST /api/chat/upload-image."""
import base64
import io


# 1x1 PNG (8-byte signature + minimal chunks) — small valid PNG
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8A"
    "AAAASUVORK5CYII="
)


def test_upload_png_returns_id(client, auth):
    files = {"file": ("a.png", io.BytesIO(PNG_1X1), "image/png")}
    r = client.post("/api/chat/upload-image", files=files, headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["id"]
    assert d["mime"] == "image/png"
    assert d["bytes"] == len(PNG_1X1)


def test_upload_rejects_bad_mime(client, auth):
    """A truly unsupported mime (binary blob, no recognized extension)."""
    files = {"file": ("a.weirdext", io.BytesIO(b"\x00\x01\x02"),
                       "application/octet-stream")}
    r = client.post("/api/chat/upload-image", files=files, headers=auth)
    assert r.status_code == 400
    assert "unsupported" in r.json()["detail"].lower()


def test_upload_accepts_text_doc(client, auth):
    """Text docs (md/txt/json/etc) are accepted, stored as utf-8 text."""
    files = {"file": ("notes.md", io.BytesIO("# Hello\nbody".encode("utf-8")),
                       "text/markdown")}
    r = client.post("/api/chat/upload-image", files=files, headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["kind"] == "text"
    assert d["name"] == "notes.md"
    from backend import chat
    assert chat._image_store[d["id"]]["text"].startswith("# Hello")


def test_upload_accepts_pdf(client, auth):
    """PDFs go down the document-block path, stored as base64."""
    files = {"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4\n..."),
                       "application/pdf")}
    r = client.post("/api/chat/upload-image", files=files, headers=auth)
    assert r.status_code == 200
    assert r.json()["kind"] == "pdf"


def test_upload_text_too_large_returns_413(client, auth, monkeypatch):
    from backend import chat
    monkeypatch.setattr(chat, "_TEXT_MAX_BYTES", 50)
    big = b"x" * 200
    files = {"file": ("big.txt", io.BytesIO(big), "text/plain")}
    r = client.post("/api/chat/upload-image", files=files, headers=auth)
    assert r.status_code == 413


def test_upload_text_rejects_non_utf8(client, auth):
    files = {"file": ("bad.txt", io.BytesIO(b"\xff\xfe\x00garbage"),
                       "text/plain")}
    r = client.post("/api/chat/upload-image", files=files, headers=auth)
    assert r.status_code == 400
    assert "utf-8" in r.json()["detail"].lower()


def test_upload_rejects_too_large(client, auth, monkeypatch):
    from backend import chat
    monkeypatch.setattr(chat, "_IMAGE_MAX_BYTES", 100)
    big = b"x" * 500
    files = {"file": ("a.png", io.BytesIO(big), "image/png")}
    r = client.post("/api/chat/upload-image", files=files, headers=auth)
    assert r.status_code == 413


def test_upload_requires_token(client):
    files = {"file": ("a.png", io.BytesIO(PNG_1X1), "image/png")}
    r = client.post("/api/chat/upload-image", files=files)
    assert r.status_code == 401


def test_upload_stores_in_memory_with_b64(client, auth):
    from backend import chat
    files = {"file": ("a.png", io.BytesIO(PNG_1X1), "image/png")}
    r = client.post("/api/chat/upload-image", files=files, headers=auth)
    img_id = r.json()["id"]
    entry = chat._image_store[img_id]
    assert entry["mime"] == "image/png"
    assert base64.b64decode(entry["b64"]) == PNG_1X1


def test_image_store_gc_drops_expired(client, auth, monkeypatch):
    from backend import chat
    import time
    # Insert a fake old entry, run gc, expect it gone
    chat._image_store["old"] = {"mime": "image/png", "b64": "",
                                 "ts": time.time() - 1000}
    monkeypatch.setattr(chat, "_IMAGE_TTL_S", 100)
    chat._gc_images()
    assert "old" not in chat._image_store
