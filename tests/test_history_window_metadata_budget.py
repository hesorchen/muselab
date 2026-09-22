"""Warm history pages do not recount every turn when metadata is current."""
import pytest


@pytest.fixture()
def warm_history(client, monkeypatch, tmp_path):
    from backend import chat

    count = 5000
    metrics = {"prompt_reads": 0, "shaped_records": 0}

    class Record(dict):
        def get(self, key, default=None):
            if key == "real_user_prompt":
                metrics["prompt_reads"] += 1
            return super().get(key, default)

    records = [Record(uuid=f"record-{i}", bubble_count=1,
                      real_user_prompt=i % 2 == 0) for i in range(count)]
    index = {
        "records": records,
        "orders": {"normal": list(range(count)), "full": list(range(count))},
        "bubble_prefix": {"normal": list(range(count + 1)),
                          "full": list(range(count + 1))},
        "history_generation": "fixture-generation",
    }
    meta = {"id": "metadata-budget", "model": "fixture",
            "message_count": count, "turn_count": count // 2}
    saved = []
    path = tmp_path / "history.jsonl"
    path.write_text("", encoding="utf-8")

    def shape(_path, _index, record_ids, _annotations):
        metrics["shaped_records"] += len(record_ids)
        return [{"uuid": records[i]["uuid"], "role": "user", "text": f"fixture {i}"}
                for i in record_ids]

    monkeypatch.setattr(chat.sess, "get_session_meta", lambda _sid: dict(meta))
    monkeypatch.setattr(chat.sess, "has_pending_attachments", lambda _sid: False)
    monkeypatch.setattr(chat.sess, "get_message_annotations", lambda _sid: {})
    monkeypatch.setattr(chat.sess, "get_runtime_task_overlays", lambda _sid: {})
    monkeypatch.setattr(chat.sess, "set_message_count",
                        lambda sid, total, **kwargs: saved.append((sid, total, kwargs)))
    monkeypatch.setattr(chat, "_load_cancelled_turn_snapshots", lambda _sid: ([], ""))
    monkeypatch.setattr(chat, "_ensure_transcript_index", lambda _sid: (path, index))
    monkeypatch.setattr(chat, "_indexed_ui_records", shape)
    return count, meta, metrics, saved


@pytest.mark.parametrize("query", [
    "tail=7", "offset=1500&limit=7", "full=1&tail=7",
    "around_uuid=record-2500&limit=7",
])
def test_current_metadata_keeps_history_page_work_bounded(client, auth, warm_history, query):
    count, _, metrics, saved = warm_history
    response = client.get(f"/api/chat/sessions/metadata-budget?{query}", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert len(body["messages"]) == 7
    assert body["total"] == body["message_count"] == count
    assert body["turn_count"] == count // 2
    assert metrics["shaped_records"] == 7
    assert metrics["prompt_reads"] == 0
    assert saved == []


@pytest.mark.parametrize("full", [False, True])
def test_metadata_recount_still_repairs_only_normal_history(client, auth, warm_history, full):
    count, meta, metrics, saved = warm_history
    meta.update(message_count=1, turn_count=0)
    response = client.get(
        f"/api/chat/sessions/metadata-budget?tail=7&full={int(full)}", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == count
    assert len(body["messages"]) == metrics["shaped_records"] == 7
    if full:
        assert (body["message_count"], body["turn_count"]) == (1, 0)
        assert saved == []
        assert metrics["prompt_reads"] == 0
    else:
        assert (body["message_count"], body["turn_count"]) == (count, count // 2)
        assert saved == [("metadata-budget", count, {"turn_count": count // 2})]
        assert metrics["prompt_reads"] == count
