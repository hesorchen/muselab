"""Tests for the native AskUserQuestion browser-state bridge."""
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
    auq.register_session_queue("sess-A")
    assert "sess-A" in auq._session_queues
    auq.unregister_session_queue("sess-A")
    assert "sess-A" not in auq._session_queues


def test_submit_answer_returns_false_when_no_pending():
    assert auq.submit_answer("sess-X", "q-doesnt-exist", {"Q?": "A"}) is False


def test_submit_answer_resumes_before_waking_model(monkeypatch):
    order = []

    class FakeFuture:
        def done(self):
            return False

        def set_result(self, value):
            order.append(("resolve", value))

    from backend import activity as activity_module
    monkeypatch.setattr(activity_module.activity, "resume",
                        lambda sid: order.append(("resume", sid)))
    auq._pending[("sess-X", "q-1")] = FakeFuture()

    assert auq.submit_answer("sess-X", "q-1", {"Q?": "A"}) is True
    assert order == [("resume", "sess-X"), ("resolve", {"Q?": "A"})]


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


# ============================================================================
# Normalization — the actual reason ask_user_question options sometimes
# rendered as dead buttons in the UI. Models occasionally hand us looser
# shapes than the schema docs request; backend coerces them into a single
# canonical shape so the frontend template can be naive.
# ============================================================================

def test_normalize_canonical_passthrough():
    """The happy case — already-correct shape comes through unchanged."""
    out = auq._normalize_questions([{
        "question": "Pick one",
        "header": "color",
        "multiSelect": False,
        "options": [{"label": "red", "description": ""},
                    {"label": "blue", "description": "the cooler one"}],
    }])
    assert len(out) == 1
    assert out[0]["question"] == "Pick one"
    assert out[0]["multiSelect"] is False
    assert out[0]["options"][1]["description"] == "the cooler one"


def test_normalize_bare_string_options():
    """Model handed us `options: ["yes", "no"]` instead of objects — wrap."""
    out = auq._normalize_questions([{
        "question": "Continue?",
        "options": ["Yes", "No"],
    }])
    assert out[0]["options"] == [
        {"label": "Yes", "description": ""},
        {"label": "No", "description": ""},
    ]
    # Missing header / multiSelect default sensibly.
    assert out[0]["header"] == ""
    assert out[0]["multiSelect"] is False


def test_normalize_alternative_keys():
    """Common synonyms for label/description/question across model styles."""
    out = auq._normalize_questions([{
        "text": "Which provider?",
        "header": "vendor",
        "multi_select": True,
        "choices": [
            {"text": "Anthropic", "detail": "default"},
            {"name": "DeepSeek"},
            {"value": "GLM", "desc": "Zhipu"},
        ],
    }])
    assert out[0]["question"] == "Which provider?"
    assert out[0]["multiSelect"] is True
    labels = [o["label"] for o in out[0]["options"]]
    assert labels == ["Anthropic", "DeepSeek", "GLM"]
    descs = [o["description"] for o in out[0]["options"]]
    assert descs == ["default", "", "Zhipu"]


def test_normalize_drops_unusable_entries():
    """Questions without question-text or without any valid options vanish."""
    out = auq._normalize_questions([
        {"question": "", "options": ["a"]},        # no text
        {"question": "real", "options": []},       # empty options
        {"question": "also real", "options": [{"description": "no label"}]},  # no label key
        {"question": "keeper", "options": ["A", "B"]},
    ])
    assert len(out) == 1
    assert out[0]["question"] == "keeper"


def test_normalize_empty_input_returns_empty():
    assert auq._normalize_questions([]) == []
    assert auq._normalize_questions([None, "not a dict", 42]) == []
