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
    assert all("archive-curator skill" in text for text in starter.values())
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


def test_archive_curator_skill_has_native_workflow(app_module):
    skill = (Path(__file__).parents[1] / "skills" / "archive-curator" / "SKILL.md")
    text = skill.read_text(encoding="utf-8")
    assert "name: archive-curator" in text
    assert "## Workflow" in text
    assert "CLAUDE.md" in text
    assert "Do not execute an archive mutation until the user confirms" in text


# ---------- is_chinese_locale drives the template pick ----------

def test_is_chinese_locale_zh_branch(app_module, monkeypatch):
    from backend.settings import is_chinese_locale
    for var in ("LANG", "LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    assert is_chinese_locale() is True


def test_is_chinese_locale_en_branch(app_module, monkeypatch):
    from backend.settings import is_chinese_locale
    for var in ("LANG", "LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert is_chinese_locale() is False


def test_seed_template_pick_follows_locale_zh(app_module, monkeypatch, temp_root):
    """_seed_claude_md_and_archive_skeleton seeds a CLAUDE.md from the
    zh template when the host locale is Chinese. We drive the real knob
    (LANG) and assert on the seeded file content, not internals."""
    chat = _chat(app_module)
    for var in ("LANG", "LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    (temp_root / "CLAUDE.md").unlink(missing_ok=True)
    chat._seed_claude_md_and_archive_skeleton()
    md = (temp_root / "CLAUDE.md")
    assert md.exists()
    # zh template carries CJK; golden substring guards against asset swap.
    text = md.read_text(encoding="utf-8")
    assert any("一" <= c <= "鿿" for c in text), text[:200]


def test_seed_template_pick_follows_locale_en(app_module, monkeypatch, temp_root):
    chat = _chat(app_module)
    for var in ("LANG", "LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    (temp_root / "CLAUDE.md").unlink(missing_ok=True)
    chat._seed_claude_md_and_archive_skeleton()
    md = (temp_root / "CLAUDE.md")
    assert md.exists()
    text = md.read_text(encoding="utf-8")
    # en template is ASCII-dominant — no CJK section headers.
    assert not any("一" <= c <= "鿿" for c in text), text[:200]


def test_seed_is_idempotent_on_existing_claude_md(app_module, monkeypatch, temp_root):
    """If a CLAUDE.md already exists, the seeder must NOT clobber it."""
    chat = _chat(app_module)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    md = temp_root / "CLAUDE.md"
    md.write_text("# my own profile\nName: keep me\n", encoding="utf-8")
    chat._seed_claude_md_and_archive_skeleton()
    assert md.read_text(encoding="utf-8") == "# my own profile\nName: keep me\n"
