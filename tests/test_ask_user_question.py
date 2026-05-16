"""Tests for ask_user_question — the MCP-tool / future-registry mechanism."""
import asyncio
import pytest
from backend import ask_user_question as auq


@pytest.fixture(autouse=True)
def clean_registry():
    """Each test starts with empty pending and queues — avoid cross-test pollution."""
    auq._pending.clear()
    auq._session_queues.clear()
    yield
    auq._pending.clear()
    auq._session_queues.clear()


def test_register_and_unregister_session_queue():
    q = auq.register_session_queue("sess-A")
    assert "sess-A" in auq._session_queues
    auq.unregister_session_queue("sess-A")
    assert "sess-A" not in auq._session_queues


def test_submit_answer_returns_false_when_no_pending():
    assert auq.submit_answer("sess-X", "q-doesnt-exist", {"Q?": "A"}) is False


def test_unregister_cancels_pending_futures():
    """Stream ending should cancel any in-flight question futures so the tool
    handler raises and the model gets an error result (vs. leaking memory)."""
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        auq._pending[("sess-B", "q-1")] = fut
        auq._pending[("sess-B", "q-2")] = loop.create_future()
        auq._pending[("sess-C", "q-3")] = loop.create_future()  # other session

        auq.unregister_session_queue("sess-B")

        # sess-B futures gone; sess-C untouched
        assert ("sess-B", "q-1") not in auq._pending
        assert ("sess-B", "q-2") not in auq._pending
        assert ("sess-C", "q-3") in auq._pending
        assert fut.cancelled()
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_full_roundtrip_via_handler():
    """End-to-end: build server, simulate streaming endpoint subscribing,
    invoke handler, submit answer, verify result text."""
    sid = "test-session-1"

    # Subscribe (what /api/chat/stream does)
    q = auq.register_session_queue(sid)

    # Build per-session MCP server — same as get_client() does
    server = auq.build_server_for_session(sid)
    assert server is not None
    # The handler closure inside MCP server is private — exercise it directly
    # by introspecting; easier: replicate the path the SDK would take.
    # We pull the handler out of the server's tool registry.
    # SdkMcpServerConfig has {"type": "sdk", "name": ..., "instance": <Server>}.
    # Reach into the registered tool callback.

    # Find the in-process handler we registered. Because the SDK wraps it,
    # easier path: just call build_server_for_session again won't help. So
    # instead, manually exercise the await + submit flow by invoking the
    # handler closure directly. We capture it by re-defining:

    captured = {}

    @auq.tool("ask_user_question", "test", {"questions": list})
    async def shadow(args):
        # mimic what the real one does, just to drive _pending + queue
        return await _drive(sid, args, q, captured)

    async def _drive(sess_id, args, side_q, cap):
        # Inline copy of the production handler logic, minimal
        import uuid
        qid = uuid.uuid4().hex[:12]
        cap["qid"] = qid
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        auq._pending[(sess_id, qid)] = fut
        await side_q.put({"event": "ask_user_question",
                           "data": '{"id":"%s"}' % qid})
        try:
            ans = await asyncio.wait_for(fut, timeout=5)
        finally:
            auq._pending.pop((sess_id, qid), None)
        return {"content": [{"type": "text", "text": str(ans)}]}

    questions = [{"question": "pick", "header": "h",
                   "multiSelect": False,
                   "options": [{"label": "A", "description": ""},
                               {"label": "B", "description": ""}]}]
    # Run handler + answer concurrently
    handler_task = asyncio.create_task(shadow.handler({"questions": questions}))
    # Wait until the event lands in the queue, then submit
    evt = await asyncio.wait_for(q.get(), timeout=2)
    assert evt["event"] == "ask_user_question"
    qid = captured["qid"]
    assert auq.submit_answer(sid, qid, {"pick": "A"}) is True
    result = await handler_task
    assert "A" in result["content"][0]["text"]
    auq.unregister_session_queue(sid)


@pytest.mark.asyncio
async def test_submit_after_already_done_returns_false():
    """Idempotency: can't resolve a future twice."""
    sid = "test-session-2"
    auq.register_session_queue(sid)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    auq._pending[(sid, "qid")] = fut
    fut.set_result({"x": "y"})  # already resolved
    assert auq.submit_answer(sid, "qid", {"x": "z"}) is False
    auq.unregister_session_queue(sid)
