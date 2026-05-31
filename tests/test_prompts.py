"""Tests for the system-prompt surface.

backend/prompts.py turns out to hold only STATIC string constants (the
curator / profile-intake system prompts + bilingual initial-message dicts) —
no locale branching, no CLAUDE.md injection. The general default prompt and
the per-session prompt-composition logic live in backend/chat.py
(SYSTEM_PROMPT + _build_and_connect_client's `sp` build). The locale knob
itself is backend.settings.is_chinese_locale().

So this file covers what's genuinely there:
  - the static prompt constants' structural invariants + golden substrings
  - the bilingual initial-message dicts (zh vs en branches)
  - chat.SYSTEM_PROMPT interpolating the archive root + memory dir
  - the locale selector (is_chinese_locale) driving the zh/en template pick
    used by _seed_claude_md_and_archive_skeleton

We assert stable substrings / invariants, not full-string equality, but keep
at least one golden substring per branch so a silent rewrite is caught.
"""
from __future__ import annotations


def _prompts(app_module):
    """Pull the reloaded prompts module out of the backend.* tree."""
    from backend import prompts
    return prompts


def _chat(app_module):
    from backend import chat
    return chat


# ---------- static prompt constants ----------

def test_curator_prompt_structural_invariants(app_module):
    p = _prompts(app_module)
    sp = p.CURATOR_SYSTEM_PROMPT
    # Golden substring — a silent rewrite of the persona line trips this.
    assert "You are Muse acting as an archive curator" in sp
    # Key section headers of the 5-step workflow.
    assert "# 5-step workflow" in sp
    assert "## 1. Scan" in sp
    assert "# Hard rules" in sp
    # CLAUDE.md is the one pre-authorized write target.
    assert "CLAUDE.md" in sp


def test_profile_intake_prompt_structural_invariants(app_module):
    p = _prompts(app_module)
    sp = p.PROFILE_INTAKE_SYSTEM_PROMPT
    assert "You are Muse helping the user fill out their CLAUDE.md profile" in sp
    assert "# Workflow" in sp
    assert "# Hard rules" in sp
    # Surgical-edit invariant the prompt repeatedly insists on.
    assert "surgical Edits only" in sp


def test_initial_messages_have_both_locales(app_module):
    p = _prompts(app_module)
    for d in (p.CURATOR_INITIAL_MESSAGE, p.PROFILE_INTAKE_INITIAL_MESSAGE):
        assert set(d) == {"zh", "en"}
        # zh value carries CJK, en value has none — cheap branch check.
        # (en may include typographic punctuation like an em-dash, so we
        # test "no CJK" rather than strict ASCII.)
        assert any("一" <= c <= "鿿" for c in d["zh"])
        assert not any("一" <= c <= "鿿" for c in d["en"])


def test_curator_initial_message_golden(app_module):
    p = _prompts(app_module)
    assert "archive" in p.CURATOR_INITIAL_MESSAGE["en"]
    assert "archive" in p.CURATOR_INITIAL_MESSAGE["zh"]


# ---------- chat.SYSTEM_PROMPT (the general default) ----------

def test_system_prompt_injects_archive_root(app_module, temp_root):
    """The default prompt interpolates the live ROOT path so the model knows
    where the archive lives. Per conftest, ROOT == temp_root."""
    chat = _chat(app_module)
    assert str(temp_root) in chat.SYSTEM_PROMPT
    # Golden persona substring.
    assert "You are Muse, a personal assistant running inside muselab" in chat.SYSTEM_PROMPT
    # The CLAUDE.md-precedence section is present in the default prompt.
    assert "# When the user has a CLAUDE.md" in chat.SYSTEM_PROMPT


def test_system_prompt_references_memory_dir(app_module):
    """The memory-dir path is derived from ROOT (cli-encoded) and surfaced in
    the prompt so the model writes long-term memory to the right place."""
    chat = _chat(app_module)
    assert chat._MEMORY_DIR_PATH in chat.SYSTEM_PROMPT
    # ROOT is set in conftest → cli-encoded projects path, not the fallback.
    assert chat._MEMORY_DIR_PATH.endswith("/memory/")
    assert "~/.claude/projects/" in chat._MEMORY_DIR_PATH


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
