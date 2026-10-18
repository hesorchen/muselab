"""White-box Memory API and secret-handling tests."""
import os

import pytest


def test_memory_api_is_authenticated(client):
    assert client.get("/api/memory/status").status_code == 401
    assert client.get("/api/memory/config").status_code == 401


def test_confirm_correct_and_forget_via_api(client, auth):
    created = client.post("/api/memory/items", headers=auth, json={
        "kind": "fact", "content": "长期业务背景", "tags": ["business"],
    })
    assert created.status_code == 200
    item = created.json()
    assert item["authority"] == "confirmed"
    assert item["status"] == "active"

    listed = client.get("/api/memory/items", headers=auth).json()["items"]
    assert [row["id"] for row in listed] == [item["id"]]
    detail = client.get(f"/api/memory/items/{item['id']}", headers=auth).json()
    assert detail["sources"][0]["source_type"] == "user_action"
    feedback = client.post(
        f"/api/memory/items/{item['id']}/feedback", headers=auth,
        json={"useful": True})
    assert feedback.status_code == 200
    assert feedback.json()["recall_stats"]["helpful_count"] == 1
    invalid_feedback = client.post(
        f"/api/memory/items/{item['id']}/feedback", headers=auth,
        json={"useful": False, "recall_id": "missing-recall"})
    assert invalid_feedback.status_code == 409

    corrected = client.post(
        f"/api/memory/items/{item['id']}/correct", headers=auth,
        json={"content": "更新后的长期业务背景", "kind": "fact"})
    assert corrected.status_code == 200
    assert corrected.json()["content"].startswith("更新后")
    assert client.get(
        f"/api/memory/items/{item['id']}", headers=auth).json()["status"] == "superseded"

    new_id = corrected.json()["id"]
    assert client.delete(f"/api/memory/items/{new_id}", headers=auth).status_code == 200
    assert client.get(
        f"/api/memory/items/{new_id}", headers=auth).json()["status"] == "deleted"


def test_memory_traceback_stats_and_verified_backup_api(client, auth):
    session_id = "4b1cb19f-d124-4f94-95eb-f1fc31c69b23"
    message_id = "fb20ecb0-a200-422c-9705-fe780cfc0921"
    created = client.post("/api/memory/items", headers=auth, json={
        "kind": "fact", "content": "带现场的记忆", "tags": [],
        "source_session_id": session_id,
        "source_message_id": message_id,
        "source_role": "user",
    }).json()
    listed = client.get("/api/memory/items", headers=auth).json()["items"]
    item = next(row for row in listed if row["id"] == created["id"])
    assert item["recall_stats"]["recall_count"] == 0
    traceback = client.get(
        f"/api/memory/items/{created['id']}/traceback", headers=auth)
    assert traceback.status_code == 200
    site = traceback.json()["sites"][0]
    assert site["session_id"] == session_id
    assert site["message_id"] == message_id
    assert "path" not in site

    backed_up = client.post("/api/memory/backup", headers=auth)
    assert backed_up.status_code == 200
    receipt = backed_up.json()["backup"]
    assert receipt["quick_check"] == "ok"
    assert receipt["size_bytes"] > 0
    listed_backups = client.get("/api/memory/backups", headers=auth).json()
    assert listed_backups["items"][0]["id"] == receipt["id"]
    assert listed_backups["items"][0]["exists"] is True


def test_config_defaults_off_and_secrets_are_never_returned(client, auth, app_module):
    body = {
        "schema_version": 1, "mode": "off", "owner_id": "default",
        "generation_model": "",
        "embedding": {
            "provider": "openai_compatible", "base_url": "http://embed/v1",
            "api_key": "embedding-secret", "model": "bge", "dimensions": 1024,
            "timeout_seconds": 10, "batch_size": 32,
        },
        "vector": {
            "provider": "pgvector",
            "url": "postgresql://user:password@db/memory",
            "api_key": "", "collection": "muselab_memory_v1", "timeout_seconds": 10,
        },
        "rerank": {
            "enabled": False, "base_url": "", "api_key": "rerank-secret",
            "model": "", "timeout_seconds": 3,
        },
        "retrieval": {
            "dense_candidates": 20, "lexical_candidates": 20, "final_limit": 6,
            "max_context_chars": 3000, "soft_timeout_ms": 250,
        },
        "consolidation": {
            "episode_turns": 6, "episode_idle_minutes": 30,
            "dreamer_enabled": True, "verifier_enabled": True,
            "skill_learning_enabled": True, "min_reflection_episodes": 2,
            "min_skill_success_episodes": 3,
        },
    }
    response = client.put("/api/memory/config?probe=true", headers=auth, json=body)
    assert response.status_code == 200
    public = client.get("/api/memory/config", headers=auth).json()
    assert public["mode"] == "off"
    assert public["embedding"]["api_key"] == ""
    assert public["embedding"]["has_api_key"] is True
    assert public["vector"]["url"] == ""
    assert public["vector"]["has_url"] is True
    raw = (app_module.ROOT / ".muselab" / "memory" / "config.json").read_text()
    assert "embedding-secret" in raw
    assert "postgresql://user:password" in raw
    assert oct(os.stat(
        app_module.ROOT / ".muselab" / "memory" / "config.json").st_mode & 0o777) == "0o600"


def test_enabled_config_must_pass_probe(client, auth, monkeypatch):
    from backend import api_memory

    body = {
        "mode": "active", "generation_model": "test-model",
        "embedding": {"base_url": "http://embed/v1", "model": "bge"},
        "vector": {"provider": "qdrant", "url": "http://qdrant:6333",
                   "collection": "memory"},
    }

    secret = "https://user:password@example.test/private?token=secret"

    async def broken(_config=None):
        raise RuntimeError(secret)

    monkeypatch.setattr(api_memory.engine, "probe", broken)
    response = client.put("/api/memory/config", headers=auth, json=body)
    assert response.status_code == 400
    assert response.json()["detail"] == {
        "category": "unclassified",
        "exception_class": "RuntimeError",
    }
    assert secret not in response.text
    assert "example.test" not in response.text
    assert client.get("/api/memory/config", headers=auth).json()["mode"] == "off"


def test_neutral_export_and_import(client, auth):
    imported = client.post("/api/memory/import", headers=auth, json={
        "items": [
            {"kind": "decision", "content": "决策 A", "tags": []},
            {"kind": "preference", "content": "偏好 B", "tags": []},
        ]
    })
    assert imported.status_code == 200
    assert imported.json()["created"] == 2
    exported = client.get("/api/memory/export", headers=auth).json()
    assert exported["schema"] == "muselab-memory-export-v2"
    assert {row["content"] for row in exported["memories"]} == {"决策 A", "偏好 B"}
    # The neutral export is directly accepted by the import endpoint. Exact
    # content deduplication makes restoring into the same registry idempotent.
    restored = client.post("/api/memory/import", headers=auth, json=exported)
    assert restored.status_code == 200
    assert restored.json()["created"] == 0
    assert "restored" in restored.json()


def test_export_preserves_governance_state_through_round_trip(client, auth):
    created = client.post("/api/memory/items", headers=auth, json={
        "kind": "fact", "content": "会被更正的事实", "tags": []}).json()
    client.post(f"/api/memory/items/{created['id']}/correct", headers=auth,
                json={"content": "更正后的事实"})
    exported = client.get("/api/memory/export", headers=auth).json()
    # The v2 snapshot is a verbatim canonical dump, so the superseded original
    # rides along WITH its terminal status. That is what makes replay safe:
    # import_snapshot restores by primary key, so a tombstone comes back as a
    # tombstone instead of being promoted to user-confirmed content.
    by_content = {row["content"]: row for row in exported["memories"]}
    assert by_content["会被更正的事实"]["status"] == "superseded"
    assert by_content["更正后的事实"]["status"] == "active"

    # The compact v1 shape also keeps declared governance instead of defaulting
    # everything to active/confirmed.
    pending = client.post("/api/memory/import", headers=auth, json={
        "items": [{"kind": "fact", "content": "待复核的导入项",
                   "status": "pending_review", "authority": "legacy_import",
                   "confidence": 0.4}]})
    assert pending.status_code == 200
    row = next(item for item in client.get(
        "/api/memory/items", headers=auth,
        params={"status": "pending_review"}).json()["items"]
        if item["content"] == "待复核的导入项")
    assert row["authority"] == "legacy_import"
    assert row["confidence"] == 0.4


def test_approve_and_correct_reject_retired_rows(client, auth):
    item = client.post("/api/memory/items", headers=auth, json={
        "kind": "fact", "content": "先确认再更正", "tags": []}).json()
    assert client.post(
        f"/api/memory/items/{item['id']}/approve", headers=auth).status_code == 200
    client.post(f"/api/memory/items/{item['id']}/correct", headers=auth,
                json={"content": "更正内容"})
    # Superseded is terminal: neither approving nor correcting may resurrect it.
    assert client.post(
        f"/api/memory/items/{item['id']}/approve", headers=auth).status_code == 409
    assert client.post(
        f"/api/memory/items/{item['id']}/correct", headers=auth,
        json={"content": "再更正一次"}).status_code == 409


def test_memory_reads_bypass_busy_writer_and_recall(client, auth, app_module):
    """A background write and slow recall cannot hold the Memory Center."""
    import threading
    import time
    from backend.memory_engine import engine

    created = client.post("/api/memory/items", headers=auth, json={
        "kind": "fact", "content": "只读查询测试",
    }).json()
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()

    def hold(index):
        def operation(store):
            entered[index].set()
            assert release.wait(5)
        return operation

    jobs = [engine._store_actor._submit(hold(0)),
            engine._recall_store_actor._submit(hold(1))]
    try:
        assert all(event.wait(1) for event in entered)
        started = time.perf_counter()
        for route in [
            "/items", f"/items/{created['id']}",
            f"/items/{created['id']}/traceback",
            "/status", "/episodes", "/artifacts", "/jobs", "/recalls",
            "/audit", "/backups",
        ]:
            response = client.get("/api/memory" + route, headers=auth)
            assert response.status_code == 200, (route, response.text)
        elapsed = time.perf_counter() - started
        assert elapsed < 2
        assert not any(job.done() for job in jobs)
    finally:
        release.set()
        for job in jobs:
            job.result(timeout=2)


def test_memory_read_timeout_is_retryable_and_queue_is_cancelled(
        client, auth, app_module, monkeypatch):
    import gc
    import threading
    import time
    from backend.memory_engine import engine

    assert client.get("/api/memory/items", headers=auth).status_code == 200
    monkeypatch.setattr(engine, "UI_READ_TIMEOUT_S", 0.05)
    entered, release = threading.Event(), threading.Event()
    executions = []
    original = engine._ui_store.browse_memories

    def browse(*args, **kwargs):
        executions.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine._ui_store, "browse_memories", browse)

    def hold(store):
        entered.set()
        release.wait(2)

    # The assertions below are wall-clock: a 50 ms read budget against a 2 s
    # blocker. A full collection over a large suite-wide heap stops every
    # thread while holding the GIL (observed: 3.9 s on a loaded macOS
    # runner), outlasting the blocker so the read completes with 200. Pay the
    # collection up front and keep the collector out of the timed window.
    gc.collect()
    gc.disable()
    blocker = engine._ui_store_actor._submit(hold)
    try:
        assert entered.wait(1)
        started = time.perf_counter()
        response = client.get("/api/memory/items", headers=auth)
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert time.perf_counter() - started < 0.5
    finally:
        gc.enable()
        release.set()
        blocker.result(timeout=2)
    engine._ui_store_actor._submit(lambda store: None).result(timeout=2)
    assert executions == []
    monkeypatch.setattr(engine, "UI_READ_TIMEOUT_S", 1)
    assert client.get("/api/memory/items", headers=auth).status_code == 200
    assert executions == [True]


@pytest.mark.parametrize("timeout_ms", [0, 30_000])
def test_recall_timeout_roundtrips_and_negative_is_rejected(client, auth, timeout_ms):
    config = client.get("/api/memory/config", headers=auth).json()
    config["mode"] = "off"
    config["retrieval"]["soft_timeout_ms"] = timeout_ms
    response = client.put("/api/memory/config?probe=false", headers=auth, json=config)
    assert response.status_code == 200
    assert client.get("/api/memory/config", headers=auth).json()["retrieval"]["soft_timeout_ms"] == timeout_ms
    config["retrieval"]["soft_timeout_ms"] = -1
    assert client.put("/api/memory/config?probe=false", headers=auth, json=config).status_code == 422


@pytest.mark.asyncio
async def test_slow_config_save_keeps_other_requests_and_cached_reads_responsive(app_module, monkeypatch):
    import asyncio
    import threading
    import time
    import httpx
    from backend import api_memory, memory_config
    from tests.conftest import TEST_TOKEN

    current = memory_config.MemoryConfig()
    memory_config.save_config(current)
    real_fsync = memory_config.os.fsync
    real_save = api_memory.save_config
    saving = threading.local()
    writing = threading.Event()
    release = threading.Event()

    def slow_fsync(fd):
        if getattr(saving, "active", False):
            writing.set()
            release.wait(0.6)
        real_fsync(fd)

    def save(config):
        saving.active = True
        try:
            return real_save(config)
        finally:
            saving.active = False

    async def status():
        return {"enabled": False}

    async def reconfigure():
        pass

    monkeypatch.setattr(memory_config.os, "fsync", slow_fsync)
    monkeypatch.setattr(api_memory, "save_config", save)
    monkeypatch.setattr(api_memory.engine, "status", status)
    monkeypatch.setattr(api_memory.engine, "reconfigure", reconfigure)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app),
                                base_url="http://fixture") as client:
        assert (await client.get("/api/meta", headers={"X-Auth-Token": TEST_TOKEN})).status_code == 200
        started = time.perf_counter()
        task = asyncio.create_task(client.put("/api/memory/config?probe=false",
            headers={"X-Auth-Token": TEST_TOKEN}, json=current.model_dump()))
        try:
            for _ in range(100):
                if writing.is_set():
                    break
                await asyncio.sleep(.005)
            assert writing.is_set()
            # This cache lookup is used by active chat/memory code on the loop.
            assert memory_config.load_config().mode == "off"
            response = await client.get("/api/meta", headers={"X-Auth-Token": TEST_TOKEN})
            assert response.status_code == 200
            assert time.perf_counter() - started < .3
            assert not task.done()
        finally:
            release.set()
            response = await task
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_retrieval_settings_save_does_not_probe_or_restart_running_worker(app_module, monkeypatch):
    from backend import api_memory, memory_config

    cfg = memory_config.MemoryConfig.model_validate({
        "mode": "active", "generation_model": "fixture:model",
        "embedding": {"base_url": "http://fixture", "model": "fixture"},
        "vector": {"url": "http://fixture"},
    })
    memory_config.save_config(cfg)
    calls = []

    async def probe(*args):
        calls.append("probe")

    async def reconfigure():
        calls.append("restart")

    async def status():
        return {"enabled": True}

    monkeypatch.setattr(api_memory.engine, "probe", probe)
    monkeypatch.setattr(api_memory.engine, "reconfigure", reconfigure)
    monkeypatch.setattr(api_memory.engine, "status", status)
    changed = cfg.model_copy(deep=True)
    changed.retrieval.final_limit = 15
    changed.retrieval.soft_timeout_ms = 0
    result = await api_memory.put_config(changed.model_dump(), probe=True)
    assert result["config"]["retrieval"]["final_limit"] == 15
    assert calls == []
    changed.embedding.model = "new-embedding-model"
    await api_memory.put_config(changed.model_dump(), probe=True)
    assert calls == ["probe", "restart"]
