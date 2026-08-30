"""Prompt ownership and default Skill payload boundaries."""
from __future__ import annotations

import inspect


def _prompts(app_module):
    from backend import prompts
    return prompts


def _chat(app_module):
    from backend import chat
    return chat


def test_curator_starter_is_self_contained_and_safe_in_both_locales(app_module):
    starter = _prompts(app_module).CURATOR_INITIAL_MESSAGE
    assert set(starter) == {"zh", "en"}
    assert any("一" <= c <= "鿿" for c in starter["zh"])
    assert not any("一" <= c <= "鿿" for c in starter["en"])

    for text in starter.values():
        lower = text.casefold()
        assert "skill" not in lower
        assert "read-only" in lower or "只读" in text
        assert "propos" in lower or "方案" in text
        assert "confirm" in lower or "确认" in text
        assert "hard boundary" in lower or "不可越过的边界" in text
        assert "symlink" in lower or "符号链接" in text
        assert ".git" in lower
        assert "second explicit confirmation" in lower or "再次取得明确确认" in text
        assert "recoverable" in lower or "可恢复" in text
        assert "claude.md" in lower
        assert "personal profile" in lower or "个人画像" in text
        assert "preset directory" in lower or "预设目录" in text


def test_chat_keeps_native_skill_discovery_without_ultra_skill_hook(app_module):
    chat = _chat(app_module)
    assert not hasattr(chat, "SYSTEM_PROMPT")
    assert not hasattr(chat, "_build_ultra_skill_hook")
    source = inspect.getsource(chat._build_and_connect_client)
    assert 'opts_kwargs["system_prompt"]' not in source
    assert "_build_ultra_skill_hook" not in source
    assert 'setting_sources=["user", "project", "local"]' in source
    assert '"type": "local"' in source
    assert '"path": str(Path(__file__).resolve().parent.parent)' in source
    assert '[] if skills_off or side_question_runtime else "all"' in source
