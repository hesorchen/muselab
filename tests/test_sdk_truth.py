"""Lock the SDK-as-truth delegations.

muselab used to hand-roll two things the SDK already exposes — the CLI
projects-dir path encoding and the effort literal set. Both silently drifted on
SDK changes. These tests fail if someone reverts to a hand-maintained copy.
"""
from typing import get_args

from claude_agent_sdk import EffortLevel, project_key_for_directory


def test_cli_encode_cwd_delegates_to_sdk():
    from backend.chat import _cli_encode_cwd
    for p in ("/home/alice",
              "/home/a_b.c/x",
              "/home/用户/笔记",   # unicode: hand-rolled drifted here
              "/home/alice/.local/state/muselab/vendor-cli/projects"):
        assert _cli_encode_cwd(p) == project_key_for_directory(p)


def test_valid_effort_sourced_from_sdk():
    from backend.chat import _SDK_EFFORT_LEVELS, _VALID_EFFORT
    assert tuple(_SDK_EFFORT_LEVELS) == tuple(get_args(EffortLevel))
    assert tuple(_VALID_EFFORT) == (
        "auto", *get_args(EffortLevel), "ultra",
    )
    # Empty is legacy input only; session/API output uses canonical `auto`.
    assert "" not in _VALID_EFFORT
