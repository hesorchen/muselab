from backend.task_summaries import (
    TASK_SUMMARY_PREVIEW_CAP,
    normalize_task_summary,
    normalize_task_summary_fields,
)


def test_task_summary_preview_is_bounded_and_repeat_safe():
    full = "x" * (TASK_SUMMARY_PREVIEW_CAP + 317)

    first = normalize_task_summary(full)
    second = normalize_task_summary_fields(first)

    assert len(first["summary"]) == TASK_SUMMARY_PREVIEW_CAP
    assert first["summary_length"] == len(full)
    assert first["summary_truncated"] is True
    assert second == first


def test_short_task_summary_stays_verbatim():
    result = normalize_task_summary("done", summary_truncated="false")

    assert result == {
        "summary": "done",
        "summary_length": 4,
        "summary_truncated": False,
    }


def test_summary_metadata_without_text_does_not_inject_empty_summary():
    assert normalize_task_summary_fields({
        "summary_length": 10,
        "summary_truncated": True,
    }) == {
        "summary_length": 10,
        "summary_truncated": True,
    }
