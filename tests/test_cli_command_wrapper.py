"""CLI local-command wrappers are metadata and must not surface as chat text."""
from __future__ import annotations

import pytest

from backend import context_recovery
from backend.chat_presentation import strip_cli_slash_wrapper

CAVEAT = (
    "<local-command-caveat>The command below was run directly in Claude Code, "
    "not sent to you as a request, and its output goes straight to the user. "
    "It's recorded here as context for later messages.</local-command-caveat>"
)


@pytest.mark.parametrize("tag", [
    "command-name", "command-message", "command-args",
    "local-command-stdout", "local-command-stderr", "local-command-caveat",
])
def test_every_cli_wrapper_tag_is_stripped_to_nothing(tag):
    assert strip_cli_slash_wrapper(f"<{tag}>anything\nmultiline</{tag}>") == ""


def test_compact_caveat_message_is_dropped_but_real_text_survives():
    assert strip_cli_slash_wrapper(CAVEAT) == ""
    assert strip_cli_slash_wrapper(CAVEAT + "\n\nplease continue") == "please continue"


def test_recovery_summary_treats_the_caveat_as_a_command_wrapper():
    assert context_recovery._is_command_wrapper(CAVEAT)
    assert context_recovery._is_command_wrapper("  " + CAVEAT)
    assert not context_recovery._is_command_wrapper("a normal question")
