"""Prompt ownership: SDK defaults + CLAUDE.md + an on-demand workflow Skill."""
from __future__ import annotations

import inspect
from pathlib import Path


def _prompts(app_module):
    from backend import prompts
    return prompts


def _chat(app_module):
    from backend import chat
    return chat


def test_curator_starter_invokes_skill_in_both_locales(app_module):
    p = _prompts(app_module)
    starter = p.CURATOR_INITIAL_MESSAGE
    assert set(starter) == {"zh", "en"}
    assert all("workspace-curator skill" in text for text in starter.values())
    assert all("archive" not in text.casefold() for text in starter.values())
    assert any("一" <= c <= "鿿" for c in starter["zh"])
    assert not any("一" <= c <= "鿿" for c in starter["en"])


def test_chat_does_not_define_or_pass_a_muselab_system_prompt(app_module):
    chat = _chat(app_module)
    assert not hasattr(chat, "SYSTEM_PROMPT")
    source = inspect.getsource(chat._build_and_connect_client)
    assert "system_prompt=" not in source
    assert '"system_prompt"' not in source
    assert 'setting_sources=["user", "project", "local"]' in source
    assert '"type": "local"' in source
    assert '"path": str(Path(__file__).resolve().parent.parent)' in source


def test_workspace_curator_skill_has_native_workflow(app_module):
    skill = (Path(__file__).parents[1] / "skills" / "workspace-curator" / "SKILL.md")
    text = skill.read_text(encoding="utf-8")
    assert "name: workspace-curator" in text
    assert "## Workflow" in text
    assert "Do not execute any workspace mutation until the user confirms" in text
    assert "Never collect personal-profile information" in text
    assert "Never create or edit `CLAUDE.md`" in text


def test_archive_curator_remains_a_safe_deprecated_alias(app_module):
    skill = (Path(__file__).parents[1] / "skills" / "archive-curator" / "SKILL.md")
    text = skill.read_text(encoding="utf-8")
    assert "name: archive-curator" in text
    assert "Deprecated compatibility alias" in text
    assert "Do not collect personal-profile information" in text
    assert "Do not create or edit `CLAUDE.md`" in text
