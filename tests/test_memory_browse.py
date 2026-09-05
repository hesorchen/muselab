"""Browser sorting must operate on the whole registry, not the first page."""
import pytest

from backend.memory_store import MemoryStore


@pytest.fixture
def registry(tmp_path):
    return MemoryStore(tmp_path / "memory.sqlite3")


def test_sort_before_page_and_count_with_owner_and_search(registry):
    first = registry.create_memory("u", "fact", "searchable target")
    for _ in range(205):
        registry.create_memory("u", "fact", "searchable entry")
    foreign = registry.create_memory("other", "fact", "searchable foreign")
    for _ in range(3):
        registry.log_recall("u", "session", "query", [first], 1, "ok")
    for _ in range(5):
        registry.log_recall("other", "session", "query", [foreign], 1, "ok")
    rows, total = registry.browse_memories(
        "u", query="searchable", sort="recall_count", limit=1)
    assert total == 206
    assert [r["id"] for r in rows] == [first["id"]]
    next_page, _ = registry.browse_memories(
        "u", query="searchable", sort="recall_count", limit=1, offset=1)
    assert next_page[0]["id"] not in (first["id"], foreign["id"])
    ascending, _ = registry.browse_memories(
        "u", sort="recall_count", direction="asc", limit=500)
    assert ascending[-1]["id"] == first["id"]
    assert len({r["id"] for r in ascending}) == total


def test_filter_empty_relevance_and_stable_ties(registry):
    for content in ("中文记忆检索", "中文记忆测试", "other"):
        registry.create_memory("u", "fact", content)
    registry.create_memory("u", "preference", "中文记忆偏好")
    rows, total = registry.browse_memories("u", query="记忆", kind="fact")
    assert total == len(rows) == 2
    assert registry.browse_memories("u", query="!!!") == ([], 0)
    one, total = registry.browse_memories("u", sort="recall_count", limit=2)
    two, _ = registry.browse_memories("u", sort="recall_count", limit=2, offset=2)
    assert len({r["id"] for r in one + two}) == total == 4
    with pytest.raises(ValueError):
        registry.browse_memories("u", sort="updated_at; DROP TABLE memories")


def test_api_sort_validation_and_total(client, auth):
    for index in range(3):
        response = client.post("/api/memory/items", headers=auth, json={
            "kind": "fact", "content": f"fixture fact {index}",
        })
        assert response.status_code == 200
    data = client.get(
        "/api/memory/items?sort=recall_count&limit=2", headers=auth).json()
    assert data["count"] == 2
    assert data["total"] == 3
    assert data["has_more"] is True
    assert client.get("/api/memory/items?sort=unknown", headers=auth).status_code == 422
