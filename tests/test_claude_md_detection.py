"""Tests for the extended CLAUDE.md detection in context_info().

Covers:
  - Multi-source scanning (project / project_local / project_dot / user /
    subdir:<name>) — make sure the union surfaces, not just the canonical
    project-root file.
  - The filled-vs-template heuristic — install scripts seed a 100+ line
    bilingual template; that must NOT count as "Muse knows you".
  - Back-compat fields (claude_md_exists / lines / mtime) — anything that
    consumes the old shape must keep working.
"""
from __future__ import annotations
import os
from pathlib import Path

import pytest


TEMPLATE_FIXTURE = """# CLAUDE.md

> Muse's brief about you — fill what's true.

## Who I am
- Name / how you'd like Muse to address you:
- Birth year (an age range is fine):
- Where you currently live:
- Languages:

## What I'm mainly doing right now
- Main:
- How long:
- Big goal:
- One big decision this year:

## Money
- Income source:
- Rough scale:
- Current focus:

## Body
- General shape:
- Last checkup:
- Meds / supplements:

## People I care about
- Key relationships:
- Who needs attention most:
"""


FILLED_FIXTURE = """# CLAUDE.md

## Who I am
- Name: Test Persona
- Birth year: 1990
- Where I live: Atlantis
- Languages: English, Pirate

## What I'm doing
- Main: Building submarines for the Atlantean fleet
- How long: 4 years
- Big goal this year: Finish the deep-trench survey before monsoon
- One big decision: Whether to hire a co-engineer or stay solo
"""


def _seed(root: Path, files: dict[str, str]) -> None:
    """Write the (rel_path: content) files into root. Pre-existing fixture
    content in conftest's temp_root is benign — these tests only assert on
    CLAUDE.md sources."""
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _ctx(client) -> dict:
    """Hit /api/chat/context-info via the conftest-provided client."""
    r = client.get("/api/chat/context-info",
                   headers={"X-Auth-Token": os.environ["MUSELAB_TOKEN"]})
    assert r.status_code == 200, r.text
    return r.json()


# ---------- helper-level tests (no FastAPI) ----------

def test_filled_ratio_template_is_unfilled(tmp_path):
    """Install-script template should report 0 filled content lines —
    every bullet ends in ':' with no user value."""
    from backend.chat import _claude_md_filled_ratio
    p = tmp_path / "CLAUDE.md"
    p.write_text(TEMPLATE_FIXTURE)
    filled, ratio = _claude_md_filled_ratio(p)
    assert filled == 0, f"template should have 0 filled lines, got {filled}"
    assert ratio == 0.0


def test_filled_ratio_real_content_counts(tmp_path):
    """Real user content (label: value) lines should count as filled."""
    from backend.chat import _claude_md_filled_ratio
    p = tmp_path / "CLAUDE.md"
    p.write_text(FILLED_FIXTURE)
    filled, ratio = _claude_md_filled_ratio(p)
    assert filled >= 6, f"expected >=6 filled lines, got {filled}"
    assert ratio > 0.5


def test_filled_ratio_mixed_partial(tmp_path):
    """A half-filled CLAUDE.md should land somewhere in the middle —
    not 0, not 1."""
    from backend.chat import _claude_md_filled_ratio
    mixed = """# CLAUDE.md

## Who I am
- Name: Half Filled
- Birth year:
- Where I live: Somewhere
- Languages:

## Goals
- Goal A:
- Goal B:
"""
    p = tmp_path / "CLAUDE.md"
    p.write_text(mixed)
    filled, ratio = _claude_md_filled_ratio(p)
    assert 1 <= filled < 6, f"expected partial fill (1-5), got {filled}"


def test_filled_ratio_missing_file_returns_zero(tmp_path):
    """Non-existent path → (0, 0.0), no exception."""
    from backend.chat import _claude_md_filled_ratio
    filled, ratio = _claude_md_filled_ratio(tmp_path / "nope.md")
    assert filled == 0
    assert ratio == 0.0


def test_scan_source_descriptor_shape(tmp_path):
    """_scan_claude_md_source should return all expected fields, or None
    if the path doesn't exist."""
    from backend.chat import _scan_claude_md_source
    p = tmp_path / "CLAUDE.md"
    p.write_text(FILLED_FIXTURE)
    desc = _scan_claude_md_source("project", p)
    assert desc is not None
    assert desc["scope"] == "project"
    assert desc["path"] == str(p)
    assert desc["lines"] > 0
    assert desc["filled_lines"] >= 6
    assert 0.0 <= desc["fill_ratio"] <= 1.0
    assert desc["meaningfully_filled"] is True
    assert isinstance(desc["mtime"], float)
    # Missing path → None
    assert _scan_claude_md_source("project", tmp_path / "missing.md") is None


# ---------- integration tests via /api/chat/context-info ----------

def test_context_info_template_only_is_not_meaningfully_filled(temp_root, client):
    """A freshly-installed CLAUDE.md (template) should report exists=True
    but meaningfully_filled=False — UI must NOT pretend Muse knows you."""
    _seed(temp_root, {"CLAUDE.md": TEMPLATE_FIXTURE})
    info = _ctx(client)
    assert info["claude_md_exists"] is True
    proj = next((s for s in info["claude_md_sources"] if s["scope"] == "project"), None)
    assert proj is not None, info
    assert proj["meaningfully_filled"] is False
    assert proj["filled_lines"] == 0


def test_context_info_filled_claude_md_is_meaningfully_filled(temp_root, client):
    _seed(temp_root, {"CLAUDE.md": FILLED_FIXTURE})
    info = _ctx(client)
    proj = next((s for s in info["claude_md_sources"] if s["scope"] == "project"), None)
    assert proj is not None
    assert proj["meaningfully_filled"] is True
    # And the union flag is true
    assert info["claude_md_meaningfully_filled"] is True


def test_context_info_picks_up_dot_claude_md(temp_root, client):
    """ROOT/.claude/CLAUDE.md (local scope) was previously missed."""
    _seed(temp_root, {".claude/CLAUDE.md": FILLED_FIXTURE})
    info = _ctx(client)
    scopes = {s["scope"] for s in info["claude_md_sources"]}
    assert "project_dot" in scopes, scopes


def test_context_info_picks_up_local_override(temp_root, client):
    """ROOT/CLAUDE.local.md (gitignored personal override) was previously missed."""
    _seed(temp_root, {"CLAUDE.local.md": FILLED_FIXTURE})
    info = _ctx(client)
    scopes = {s["scope"] for s in info["claude_md_sources"]}
    assert "project_local" in scopes, scopes


def test_context_info_picks_up_subdir_claude_md(temp_root, client):
    """ROOT/<subdir>/CLAUDE.md (per-domain rules) was previously missed."""
    _seed(temp_root, {
        "CLAUDE.md": TEMPLATE_FIXTURE,           # main template (unfilled)
        "health/CLAUDE.md": FILLED_FIXTURE,      # subdir override (filled)
    })
    info = _ctx(client)
    scopes = {s["scope"] for s in info["claude_md_sources"]}
    assert "subdir:health" in scopes, scopes
    # Union meaningfully_filled = True because health's CLAUDE.md is filled
    assert info["claude_md_meaningfully_filled"] is True


def test_context_info_skips_archives_subdir(temp_root, client):
    """archives/ is cold storage — its CLAUDE.md (if any) shouldn't show
    up as a configured profile source."""
    _seed(temp_root, {"archives/CLAUDE.md": FILLED_FIXTURE})
    info = _ctx(client)
    scopes = {s["scope"] for s in info["claude_md_sources"]}
    assert "subdir:archives" not in scopes, scopes


def test_context_info_back_compat_fields(temp_root, client):
    """claude_md_exists / claude_md_lines / claude_md_mtime must still be
    populated for downstream consumers that haven't migrated."""
    _seed(temp_root, {"CLAUDE.md": FILLED_FIXTURE})
    info = _ctx(client)
    assert info["claude_md_exists"] is True
    assert info["claude_md_lines"] > 0
    assert info["claude_md_mtime"] > 0
    # New field exists too
    assert "claude_md_meaningfully_filled" in info
