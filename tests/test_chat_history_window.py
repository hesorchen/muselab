from __future__ import annotations

from backend import chat_history_window as history


def _index(metrics: list[dict]) -> dict:
    count = len(metrics)
    return {
        "records": [{
            "uuid": "record-1",
            "bubble_count": count,
            "estimated_height": sum(item["estimated_height"] for item in metrics),
            "serialized_bytes": sum(item["serialized_bytes"] for item in metrics),
            "bubble_metrics": metrics,
            "real_user_prompt": True,
        }],
        "orders": {"normal": [0], "full": [0]},
        "bubble_prefix": {"normal": [0, count], "full": [0, count]},
    }


def test_history_manifest_chunks_by_render_cost_not_turn_count(monkeypatch):
    monkeypatch.setattr(history, "CHUNK_TARGET_HEIGHT", 250)
    monkeypatch.setattr(history, "CHUNK_MAX_BYTES", 250)
    monkeypatch.setattr(history, "CHUNK_MAX_BLOCKS", 3)
    index = _index([
        {"estimated_height": 100, "serialized_bytes": 80},
        {"estimated_height": 100, "serialized_bytes": 80},
        {"estimated_height": 100, "serialized_bytes": 80},
        {"estimated_height": 50, "serialized_bytes": 200},
        {"estimated_height": 50, "serialized_bytes": 60},
    ])

    manifest = history.build_history_manifest(
        index, [], "normal", generation="generation-1")

    assert manifest["generation"] == "generation-1"
    assert manifest["total_blocks"] == 5
    assert [chunk["block_count"] for chunk in manifest["chunks"]] == [2, 1, 1, 1]
    assert [chunk["start"] for chunk in manifest["chunks"]] == [0, 2, 3, 4]
    assert [chunk["end"] for chunk in manifest["chunks"]] == [2, 3, 4, 5]
    assert [chunk["estimated_top"] for chunk in manifest["chunks"]] == [0, 216, 324, 396]
    assert manifest["estimated_height"] == 468


def test_history_manifest_keeps_oversized_single_block_reachable(monkeypatch):
    monkeypatch.setattr(history, "CHUNK_TARGET_HEIGHT", 100)
    monkeypatch.setattr(history, "CHUNK_MAX_BYTES", 100)
    monkeypatch.setattr(history, "CHUNK_MAX_BLOCKS", 1)
    index = _index([
        {"estimated_height": 500, "serialized_bytes": 1000},
        {"estimated_height": 50, "serialized_bytes": 20},
    ])

    manifest = history.build_history_manifest(index, [], "normal")

    assert [chunk["block_count"] for chunk in manifest["chunks"]] == [1, 1]
    assert manifest["chunks"][0]["estimated_height"] == 508
    assert manifest["chunks"][0]["serialized_bytes"] == 1000
    assert manifest["total_blocks"] == 2
