"""A completed turn does not release the SDK's transcript writer."""
import json
from datetime import datetime, timezone

import pytest
from claude_agent_sdk import (
    AssistantMessage, ResultMessage, TextBlock, ThinkingBlock,
    ToolUseBlock, ToolResultBlock, UserMessage,
)

from tests.conftest import TEST_TOKEN
from tests.test_chat_stream import (
    _FakeStreamClient, _make_session, _parse_sse,
    stream_env as stream_env,
)


@pytest.mark.parametrize("followup", [False, True])
def test_vendor_final_survives_late_sdk_flush_and_history_reload(
    stream_env, client, monkeypatch, tmp_path, followup,
):
    from backend import jsonl_cleanup

    chat = stream_env
    sid = _make_session(client)
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.touch()
    original_inode = transcript.stat().st_ino
    final_text = "The first turn's final answer must survive a reload."

    def record(uid, role, content, parent=None):
        return {"uuid": uid, "type": role, "parentUuid": parent,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": {"role": role, "content": content}}

    user = record("owner-user", "user", "inspect fixture")
    partial = record("owner-partial", "assistant", [
        {"type": "thinking", "thinking": "Synthetic reasoning", "signature": ""},
        {"type": "text", "text": "Inspecting the fixture."},
        {"type": "tool_use", "id": "owner-tool", "name": "Read", "input": {}},
    ], "owner-user")
    tool_result = record("owner-tool-result", "user", [
        {"type": "tool_result", "tool_use_id": "owner-tool", "content": "fixture result"},
    ], "owner-partial")
    final = record("owner-final", "assistant", [{"type": "text", "text": final_text}],
                   "owner-tool-result")

    wire = [
        AssistantMessage(content=[
            ThinkingBlock(thinking="Synthetic reasoning", signature=""),
            TextBlock(text="Inspecting the fixture."),
            ToolUseBlock(id="owner-tool", name="Read", input={}),
        ], model="claude-sonnet-4-6", usage={}, uuid="owner-partial"),
        UserMessage(content=[ToolResultBlock(tool_use_id="owner-tool", content="fixture result")]),
        AssistantMessage(content=[TextBlock(text=final_text)],
                         model="claude-sonnet-4-6", usage={}, uuid="owner-final"),
        ResultMessage(subtype="success", duration_ms=1000, duration_api_ms=900,
                      is_error=False, num_turns=1, session_id=sid, usage={}, result=final_text),
    ]

    with transcript.open("a", encoding="utf-8") as sdk_writer:
        def append(rows, handle=sdk_writer):
            for row in rows:
                handle.write(json.dumps(row) + "\n")
            handle.flush()

        class WriterClient(_FakeStreamClient):
            async def query(self, prompt_or_gen):
                append([user, partial, tool_result])
                await super().query(prompt_or_gen)

        async def get_client(*args, **kwargs):
            return WriterClient(wire)

        monkeypatch.setattr(chat, "get_client", get_client)
        monkeypatch.setattr(chat.endpoints, "is_third_party", lambda _model: True)
        monkeypatch.setattr(chat, "_find_session_jsonl", lambda _sid: transcript)
        monkeypatch.setattr(chat, "_turn_uuids_from_boundary",
                            chat._real_turn_uuids_from_boundary_for_test)
        # If automatic cleanup regresses, route it to this SDK-owned file so
        # the retained descriptor exposes data loss through the history endpoint.
        monkeypatch.setattr(jsonl_cleanup, "clean_session",
                            lambda _sid: jsonl_cleanup.clean_jsonl(transcript))

        response = client.get("/api/chat/stream", params={
            "token": TEST_TOKEN, "session_id": sid,
            "prompt": "inspect fixture", "model": "claude-sonnet-4-6",
        })
        assert response.status_code == 200
        events = _parse_sse(response.text)
        assert final_text in [json.loads(data)["text"] for kind, data in events if kind == "text"]
        done = next(json.loads(data) for kind, data in events if kind == "done")
        assert not done["is_error"]

        # SDK protocol delivery and disk flushing have independent lifetimes.
        # The same SDK-owned descriptor can flush after Result was delivered.
        append([final])

        if followup:
            # A later query opens the transcript path again. It must see both
            # turns even if the prior query retained an older write descriptor.
            with transcript.open("a", encoding="utf-8") as next_writer:
                append([
                    record("next-user", "user", "follow-up fixture", "owner-final"),
                    record("next-final", "assistant", "The second answer.", "next-user"),
                ], next_writer)

        history = client.get(f"/api/chat/sessions/{sid}", params={"tail": 80},
                             headers={"X-Auth-Token": TEST_TOKEN})
        assert history.status_code == 200
        text = [row.get("text") for row in history.json()["messages"]
                if row.get("role") == "assistant"]
        assert final_text in text
        if followup:
            assert text.index(final_text) < text.index("The second answer.")
        assert transcript.stat().st_ino == original_inode
        persisted_partial = json.loads(transcript.read_text().splitlines()[1])
        assert persisted_partial["message"]["content"] == partial["message"]["content"]
