"""White-box Memory API and secret-handling tests."""
import os


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
        json={"useful": True, "recall_id": "recall-1"})
    assert feedback.status_code == 200
    assert feedback.json()["attributes"]["helpful_count"] == 1

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

    async def broken(_config=None):
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(api_memory.engine, "probe", broken)
    response = client.put("/api/memory/config", headers=auth, json=body)
    assert response.status_code == 400
    assert "配置未保存" in response.json()["detail"]
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
    assert exported["schema"] == "muselab-memory-export-v1"
    assert {row["content"] for row in exported["memories"]} == {"决策 A", "偏好 B"}
    # The neutral export is directly accepted by the import endpoint. Exact
    # content deduplication makes restoring into the same registry idempotent.
    restored = client.post("/api/memory/import", headers=auth, json=exported)
    assert restored.status_code == 200
    assert restored.json()["created"] == 0
