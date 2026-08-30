"""Tests for POST /api/chat/upload-image."""
import asyncio
import base64
import io
import threading
import zipfile

import pytest

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


def test_upload_large_text_accepted_and_kept_raw(client, auth, monkeypatch):
    """Text attachments are persisted to disk and referenced by path, never
    pasted into the prompt — so the old 200 KB 413 (which existed purely to
    protect the context window) is gone. Only _IMAGE_MAX_BYTES still applies.

    The raw bytes must be retained on the store entry: that's what gets
    written to disk at send-time, and re-encoding the decoded str would
    silently normalise line endings / BOM."""
    from backend import chat
    monkeypatch.setattr(chat, "_TEXT_MAX_BYTES", 50)
    big = b"x" * 200
    files = {"file": ("big.txt", io.BytesIO(big), "text/plain")}
    r = client.post("/api/chat/upload-image", files=files, headers=auth)
    assert r.status_code == 200, r.text
    entry = chat._image_store[r.json()["id"]]
    assert entry["kind"] == "text"
    assert entry["raw"] == big


def test_upload_xlsx_keeps_original_bytes_and_kind(client, auth):
    """xlsx must NOT be flipped to kind=text any more. The original workbook
    is persisted alongside a plain-text transcription — Read can't open the
    zip container, and the transcription loses formulas / sheet structure, so
    the pair is what makes the attachment actually usable."""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.active["A1"] = "hello"
    buf = io.BytesIO()
    wb.save(buf)
    raw = buf.getvalue()
    files = {"file": ("book.xlsx", io.BytesIO(raw),
                      "application/vnd.openxmlformats-officedocument."
                      "spreadsheetml.sheet")}
    r = client.post("/api/chat/upload-image", files=files, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "xlsx"
    from backend import chat
    entry = chat._image_store[r.json()["id"]]
    assert entry["raw"] == raw
    assert "hello" in entry["text"]


@pytest.mark.parametrize("raw,expected", [
    # POSIX traversal: Path.name already reduces this to the basename.
    ("../../etc/passwd", "passwd"),
    # Windows-style separators arriving on Linux survive Path.name, so they
    # have to be replaced explicitly.
    ("..\\..\\windows\\system32", "windows_system32"),
    ("  spaced name.md ", "spaced_name.md"),
    (".hidden", "hidden"),
    # Shell metacharacters: everything before the last `/` is dropped as a
    # directory part, the rest is neutralised.
    ("rm -rf /; echo pwned.txt", "echo_pwned.txt"),
    ("季度报表.xlsx", "季度报表.xlsx"),
    ("", "file"),
])
def test_safe_attach_name(raw, expected):
    """Filenames come straight from the client and become a path component."""
    from backend import chat
    assert chat._safe_attach_name(raw) == expected


def test_safe_attach_name_truncates_on_bytes_not_chars():
    """ext4/APFS cap each path component at 255 BYTES. A CJK name is 3
    bytes/char, so a char-based truncation would still blow the limit."""
    from backend import chat
    out = chat._safe_attach_name("报" * 200 + ".txt")
    assert len(out.encode("utf-8")) <= chat._ATTACH_NAME_MAX
    assert out.endswith(".txt")


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
    r = client.post("/api/chat/image-generate", headers=auth, json={
        "prompt": "hello",
    })
    assert r.status_code == 400
    assert "OPENAI_IMAGE_API_KEY" in r.json()["detail"]


def test_image_generate_rejects_lookalike_loopback_http_base_url(client, auth, monkeypatch):
    monkeypatch.setenv("OPENAI_IMAGE_API_KEY", "sk-test-image-key")
    monkeypatch.setenv("OPENAI_IMAGE_BASE_URL", "http://localhost.evil.test/v1")
    r = client.post("/api/chat/image-generate", headers=auth, json={
        "prompt": "hello",
    })
    assert r.status_code == 400
    assert "OPENAI_IMAGE_BASE_URL" in r.json()["detail"]


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
        "model": "gpt-image-2",
        "provider": "openai",
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


@pytest.mark.asyncio
async def test_upload_reader_stops_at_limit_plus_one(app_module, monkeypatch):
    del app_module
    from backend import chat

    monkeypatch.setattr(chat, "_IMAGE_MAX_BYTES", 7)
    monkeypatch.setattr(chat, "_UPLOAD_READ_CHUNK_BYTES", 3)

    class SizedReader:
        def __init__(self):
            self.offset = 0
            self.calls = []
            self.body = b"0123456789"

        async def read(self, size):
            self.calls.append(size)
            chunk = self.body[self.offset:self.offset + size]
            self.offset += len(chunk)
            return chunk

    reader = SizedReader()
    with pytest.raises(chat.HTTPException) as exc_info:
        await chat._read_upload_limited(reader)
    assert exc_info.value.status_code == 413
    assert reader.offset == 8
    assert reader.calls == [3, 3, 2]


@pytest.mark.parametrize("budget", ["entries", "bytes"])
def test_generated_batch_capacity_rejection_is_atomic(
    app_module,
    monkeypatch,
    budget,
):
    del app_module
    from backend import chat

    with chat._image_store_lock:
        chat._image_store.clear()
        chat._staged_attachment_claims.clear()
        count = 47 if budget == "entries" else 1
        for index in range(count):
            aid = f"leased-{index:02d}"
            entry = {
                "kind": "image",
                "mime": "image/png",
                "name": f"leased-{index}.png",
                "b64": base64.b64encode(b"x").decode(),
                "ts": chat.time.time(),
            }
            chat._image_store[aid] = entry
            chat._staged_attachment_claims[aid] = f"token-{index}"
        snapshot = dict(chat._image_store)
        protected_bytes = sum(
            chat._image_entry_bytes(entry)
            for entry in chat._image_store.values()
        )

    monkeypatch.setattr(chat, "_IMAGE_STORE_MAX_ENTRIES", 48)
    monkeypatch.setattr(
        chat,
        "_IMAGE_STORE_MAX_BYTES",
        chat._IMAGE_STORE_MAX_BYTES
        if budget == "entries" else protected_bytes + 1,
    )
    encoded = base64.b64encode(b"generated").decode()
    with pytest.raises(chat.HTTPException) as exc_info:
        chat._stage_generated_images([encoded, encoded], "image/png")
    assert exc_info.value.status_code == 503
    with chat._image_store_lock:
        assert chat._image_store == snapshot
        assert set(chat._staged_attachment_claims) == set(snapshot)


@pytest.mark.asyncio
async def test_response_owned_generated_batch_reclaimed_at_cancel_checkpoint(
    app_module,
    monkeypatch,
):
    del app_module
    from backend import chat

    encoded = base64.b64encode(PNG_1X1).decode()

    async def stage_then_queue_cancel(b64s, mime):
        items = chat._stage_generated_images(b64s, mime)
        task = asyncio.current_task()
        asyncio.get_running_loop().call_soon(task.cancel)
        return items

    monkeypatch.setattr(
        chat, "_stage_generated_images_owned", stage_then_queue_cancel)
    with pytest.raises(asyncio.CancelledError):
        await chat._stage_generated_images_for_response(
            [encoded, encoded], "image/png")
    with chat._image_store_lock:
        assert chat._image_store == {}
        assert chat._staged_attachment_claims == {}

@pytest.mark.asyncio
async def test_owned_stage_preserves_owner_cancel_when_worker_fails(
    app_module,
    monkeypatch,
):
    del app_module
    from backend import chat

    entered = threading.Event()
    release = threading.Event()

    def fail_after_cancel(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        raise chat.HTTPException(503, "injected worker failure")

    monkeypatch.setattr(chat, "_stage_generated_images", fail_after_cancel)
    owner = asyncio.create_task(chat._stage_generated_images_owned(
        [base64.b64encode(PNG_1X1).decode()],
        "image/png",
    ))
    assert await asyncio.to_thread(entered.wait, 5)
    owner.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    with chat._image_store_lock:
        assert chat._image_store == {}


@pytest.mark.asyncio
async def test_job_attach_reclaims_batch_at_owner_cancel_checkpoint(
    app_module,
    monkeypatch,
):
    del app_module
    from backend import chat

    job_id = "job-attach-cancel"
    image_id = "image-attach-cancel"
    job_dir = chat._IMAGEGEN_FILES / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "image-1.png").write_bytes(PNG_1X1)
    chat._imagegen_put_job({
        "id": job_id,
        "status": "succeeded",
        "images": [{
            "image_id": image_id,
            "file": "image-1.png",
            "name": "generated.png",
            "mime": "image/png",
        }],
        "created_at": 1.0,
        "updated_at": 1.0,
    })

    async def stage_then_queue_cancel(b64s, mime):
        items = chat._stage_generated_images(b64s, mime)
        task = asyncio.current_task()
        assert task is not None
        asyncio.get_running_loop().call_soon(task.cancel)
        return items

    monkeypatch.setattr(
        chat, "_stage_generated_images_owned", stage_then_queue_cancel)
    with pytest.raises(asyncio.CancelledError):
        await chat.attach_image_generate_job_image(job_id, image_id)
    with chat._image_store_lock:
        assert chat._image_store == {}
        assert chat._staged_attachment_claims == {}

@pytest.mark.parametrize("budget", ["entries", "member", "total", "ratio"])
def test_xlsx_archive_budgets_reject_before_openpyxl(
    app_module,
    monkeypatch,
    budget,
):
    del app_module
    from backend import chat

    payload = io.BytesIO()
    with zipfile.ZipFile(
        payload, "w", compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("a.xml", b"A" * 4096)
        archive.writestr("b.xml", b"B" * 4096)

    if budget == "entries":
        monkeypatch.setattr(chat, "_XLSX_ARCHIVE_MAX_ENTRIES", 1)
    elif budget == "member":
        monkeypatch.setattr(chat, "_XLSX_ARCHIVE_MAX_MEMBER_BYTES", 1024)
    elif budget == "total":
        monkeypatch.setattr(
            chat, "_XLSX_ARCHIVE_MAX_UNCOMPRESSED_BYTES", 6000)
    else:
        monkeypatch.setattr(chat, "_XLSX_ARCHIVE_MAX_COMPRESSION_RATIO", 1)

    with pytest.raises(chat.HTTPException) as exc_info:
        chat._validate_xlsx_archive(payload.getvalue())
    assert exc_info.value.status_code == 422


def test_xlsx_transcription_bounds_rows_and_columns(
    app_module,
    monkeypatch,
):
    del app_module
    from backend import chat
    openpyxl = pytest.importorskip("openpyxl")

    monkeypatch.setattr(chat, "_XLSX_ATTACH_MAX_ROWS", 2)
    monkeypatch.setattr(chat, "_XLSX_ATTACH_MAX_COLS", 2)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet["A1"] = "visible"
    sheet["C1"] = "hidden-column"
    sheet["A3"] = "hidden-row"
    payload = io.BytesIO()
    workbook.save(payload)
    workbook.close()

    text = chat._xlsx_to_text(payload.getvalue(), "safe.xlsx")
    assert "visible" in text
    assert "hidden-column" not in text
    assert "hidden-row" not in text
    assert "rows truncated at 2" in text
    assert "cols truncated at 2" in text


@pytest.mark.asyncio
async def test_background_image_job_reclaims_temporary_staged_results(
    app_module,
    monkeypatch,
):
    del app_module
    from backend import chat

    encoded = base64.b64encode(PNG_1X1).decode()
    staged = chat._stage_generated_images([encoded], "image/png")
    job_id = "job-reclaim"
    chat._imagegen_put_job({
        "id": job_id,
        "status": "queued",
        "prompt": "prompt",
        "model": "gpt-image-2",
        "provider": None,
        "size": "1024x1024",
        "quality": "low",
        "output_format": "png",
        "n": 1,
        "error": "",
        "images": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })

    async def fake_generate(**_kwargs):
        return {
            "provider": "openai",
            "model": "gpt-image-2",
            "images": staged,
        }

    monkeypatch.setattr(chat, "_generate_openai_image_api", fake_generate)
    request = chat.ImageGenerateReq(prompt="prompt")
    await chat._run_imagegen_job(job_id, request)
    with chat._image_store_lock:
        assert staged[0]["id"] not in chat._image_store
    assert chat._imagegen_load_jobs()[job_id]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_image_job_detail_base64_runs_in_worker(app_module, monkeypatch):
    del app_module
    from backend import chat

    job_id = "job-worker"
    image_id = "image-worker"
    job_dir = chat._IMAGEGEN_FILES / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "image-1.png").write_bytes(PNG_1X1)
    chat._imagegen_put_job({
        "id": job_id,
        "status": "succeeded",
        "images": [{
            "image_id": image_id,
            "file": "image-1.png",
            "mime": "image/png",
        }],
    })
    main_thread = threading.get_ident()
    worker_threads = []
    original = chat._imagegen_public_job

    def tracked(job, *, include_data=True):
        worker_threads.append(threading.get_ident())
        return original(job, include_data=include_data)

    monkeypatch.setattr(chat, "_imagegen_public_job", tracked)
    result = await chat.get_image_generate_job(job_id)
    assert result["job"]["images"][0]["data_url"].startswith(
        "data:image/png;base64,")
    assert worker_threads and worker_threads[0] != main_thread
