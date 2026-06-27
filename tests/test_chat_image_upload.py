"""Tests for POST /api/chat/upload-image."""
import base64
import io
from pathlib import Path

from tests.conftest import TEST_TOKEN


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


def test_image_generate_posts_to_openai_and_stages_attachment(client, auth, monkeypatch):
    monkeypatch.setenv("OPENAI_IMAGE_API_KEY", "sk-test-image-key")
    monkeypatch.setenv("OPENAI_IMAGE_BASE_URL", "https://api.openai.test/v1")
    posted = {}

    class _FakeResp:
        status_code = 200
        text = '{"data":[{"b64_json":"ignored-by-json"}]}'

        def json(self):
            return {"data": [{"b64_json": base64.b64encode(PNG_1X1).decode("ascii")}]}

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            posted["timeout"] = k.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None, data=None, files=None):
            posted["url"] = url
            posted["headers"] = headers
            posted["json"] = json
            posted["data"] = data
            posted["files"] = files
            return _FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    r = client.post("/api/chat/image-generate", headers=auth, json={
        "prompt": "a small blue square",
        "model": "gpt-image-2",
        "size": "1024x1024",
        "quality": "low",
        "output_format": "png",
        "n": 1,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    img = body["images"][0]
    assert img["id"]
    assert img["data_url"].startswith("data:image/png;base64,")
    assert posted["url"] == "https://api.openai.test/v1/images/generations"
    assert posted["headers"]["Authorization"] == "Bearer sk-test-image-key"
    assert posted["json"]["model"] == "gpt-image-2"
    assert posted["json"]["prompt"] == "a small blue square"

    from backend import chat
    staged = chat._image_store[img["id"]]
    assert staged["kind"] == "image"
    assert staged["mime"] == "image/png"
    assert base64.b64decode(staged["b64"]) == PNG_1X1


def test_image_generate_can_use_pending_reference_image(client, auth, monkeypatch):
    monkeypatch.setenv("OPENAI_IMAGE_API_KEY", "sk-test-image-key")
    posted = {}
    from backend import chat
    chat._image_store["ref1"] = {
        "kind": "image",
        "mime": "image/png",
        "name": "ref.png",
        "b64": base64.b64encode(PNG_1X1).decode("ascii"),
        "ts": 9999999999,
    }

    class _FakeResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"data": [{"b64_json": base64.b64encode(PNG_1X1).decode("ascii")}]}

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None, data=None, files=None):
            posted["url"] = url
            posted["data"] = data
            posted["files"] = files
            return _FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    r = client.post("/api/chat/image-generate", headers=auth, json={
        "prompt": "make it brighter",
        "image_ids": ["ref1"],
    })
    assert r.status_code == 200, r.text
    assert posted["url"].endswith("/images/edits")
    assert posted["data"]["prompt"] == "make it brighter"
    assert posted["files"][0][0] == "image[]"
    assert posted["files"][0][1][0] == "ref.png"


def test_image_generate_requires_image_key(client, auth, monkeypatch):
    # Force the OpenAI provider so this test does not depend on whether the
    # developer running tests has a logged-in local codex CLI.
    monkeypatch.setenv("MUSELAB_IMAGE_PROVIDER", "openai")
    r = client.post("/api/chat/image-generate", headers=auth, json={
        "prompt": "hello",
    })
    assert r.status_code == 400
    assert "OPENAI_IMAGE_API_KEY" in r.json()["detail"]


def test_image_generate_rejects_lookalike_loopback_http_base_url(client, auth, monkeypatch):
    monkeypatch.setenv("MUSELAB_IMAGE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_IMAGE_API_KEY", "sk-test-image-key")
    monkeypatch.setenv("OPENAI_IMAGE_BASE_URL", "http://localhost.evil.test/v1")
    r = client.post("/api/chat/image-generate", headers=auth, json={
        "prompt": "hello",
    })
    assert r.status_code == 400
    assert "OPENAI_IMAGE_BASE_URL" in r.json()["detail"]


def test_image_generate_auto_does_not_use_codex_without_opt_in(client, auth, monkeypatch):
    monkeypatch.setenv("MUSELAB_IMAGE_PROVIDER", "auto")
    monkeypatch.setenv("CODEX_IMAGEGEN_ENABLED", "false")
    r = client.post("/api/chat/image-generate", headers=auth, json={
        "prompt": "hello",
    })
    assert r.status_code == 400
    assert "OPENAI_IMAGE_API_KEY" in r.json()["detail"]


def test_image_generate_can_use_codex_imagegen(client, auth, monkeypatch):
    monkeypatch.setenv("MUSELAB_IMAGE_PROVIDER", "codex_imagegen")
    monkeypatch.setenv("CODEX_IMAGEGEN_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-reach-codex-env")
    from backend import chat
    monkeypatch.setattr(chat, "locate_executable", lambda name: "/usr/bin/codex")

    calls = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self, payload):
            prompt = payload.decode("utf-8")
            calls["prompt"] = prompt
            final_path = calls["cmd"][calls["cmd"].index("--output-last-message") + 1]
            out_dir = Path(final_path).parent / "out"
            image_path = out_dir / "image-1.png"
            image_path.write_bytes(PNG_1X1)
            Path(final_path).write_text(
                f'{{"images":[{{"path":"{image_path}"}}]}}',
                encoding="utf-8",
            )
            return b"", b""

    async def _fake_create_subprocess_exec(*cmd, **kwargs):
        calls["cmd"] = list(cmd)
        calls["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(chat.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    r = client.post("/api/chat/image-generate", headers=auth, json={
        "prompt": "a minimal tomato timer app icon",
        "size": "1024x1024",
        "quality": "low",
        "output_format": "png",
        "n": 1,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "codex_imagegen"
    assert body["model"] == "codex-imagegen"
    img = body["images"][0]
    assert img["data_url"].startswith("data:image/png;base64,")
    assert calls["cmd"][:2] == ["/usr/bin/codex", "exec"]
    final_path = Path(calls["cmd"][calls["cmd"].index("--output-last-message") + 1])
    assert calls["cmd"][calls["cmd"].index("--cd") + 1] == str(final_path.parent)
    assert "--add-dir" not in calls["cmd"]
    assert "OPENAI_API_KEY" not in calls["kwargs"]["env"]
    assert "$imagegen" in calls["prompt"]
    assert "a minimal tomato timer app icon" in calls["prompt"]

    staged = chat._image_store[img["id"]]
    assert staged["kind"] == "image"
    assert base64.b64decode(staged["b64"]) == PNG_1X1


def test_image_generate_codex_imagegen_falls_back_to_codex_generated_dir(
        client, auth, monkeypatch, tmp_path):
    monkeypatch.setenv("MUSELAB_IMAGE_PROVIDER", "codex_imagegen")
    monkeypatch.setenv("CODEX_IMAGEGEN_ENABLED", "true")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    from backend import chat
    monkeypatch.setattr(chat, "locate_executable", lambda name: "/usr/bin/codex")

    calls = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self, payload):
            calls["prompt"] = payload.decode("utf-8")
            gen_dir = Path(calls["kwargs"]["env"]["CODEX_HOME"]) / "generated_images" / "run1"
            gen_dir.mkdir(parents=True)
            (gen_dir / "image.png").write_bytes(PNG_1X1)
            return b"", b""

    async def _fake_create_subprocess_exec(*cmd, **kwargs):
        calls["cmd"] = list(cmd)
        calls["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(chat.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    r = client.post("/api/chat/image-generate", headers=auth, json={
        "prompt": "a minimal muselab github icon",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "codex_imagegen"
    img = body["images"][0]
    staged = chat._image_store[img["id"]]
    assert staged["mime"] == "image/png"
    assert base64.b64decode(staged["b64"]) == PNG_1X1


def test_image_generate_history_lists_and_attaches(client, auth):
    from backend import chat

    job_id = "jobhist1"
    image_id = "imghist1"
    job_dir = chat._IMAGEGEN_FILES / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "image-1.png").write_bytes(PNG_1X1)
    chat._imagegen_put_job({
        "id": job_id,
        "status": "succeeded",
        "prompt": "muselab github icon",
        "model": "codex-imagegen",
        "provider": "codex_imagegen",
        "size": "1024x1024",
        "quality": "low",
        "output_format": "png",
        "n": 1,
        "error": "",
        "images": [{
            "image_id": image_id,
            "file": "image-1.png",
            "name": "image-1.png",
            "mime": "image/png",
            "bytes": len(PNG_1X1),
            "attach_ext": "png",
        }],
        "created_at": 123.0,
        "updated_at": 124.0,
    })

    r = client.get("/api/chat/image-generate/jobs", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    job = next(j for j in body["jobs"] if j["id"] == job_id)
    assert job["images"][0]["url"].endswith(f"/image-generate/jobs/{job_id}/images/{image_id}")
    assert "data_url" not in job["images"][0]

    r = client.get(job["images"][0]["url"], headers=auth)
    assert r.status_code == 200, r.text
    assert r.content == PNG_1X1
    r = client.get(f"{job['images'][0]['url']}?token={TEST_TOKEN}")
    assert r.status_code == 401

    r = client.post(
        f"/api/chat/image-generate/jobs/{job_id}/attach/{image_id}",
        headers=auth,
    )
    assert r.status_code == 200, r.text
    img = r.json()["image"]
    assert img["id"]
    assert base64.b64decode(chat._image_store[img["id"]]["b64"]) == PNG_1X1
