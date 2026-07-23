"""Documentation consistency checks for the public, maintained surface."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).parents[1]
DOCS = ROOT / "docs"
PUBLIC_ROOT_DOCS = (
    ROOT / "README.md",
    ROOT / "README_en.md",
    ROOT / "constitution.md",
    ROOT / "constitution_zh.md",
    ROOT / "THIRD_PARTY_LICENSES.md",
)
LINK_RE = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
SOURCE_TARGET_RE = re.compile(
    r"^(?:\.\./)?(?:backend|frontend|scripts|tests)/", re.IGNORECASE)


def _public_markdown() -> list[Path]:
    docs = [
        path for path in DOCS.glob("*.md")
        if not path.name.startswith("research-")
    ]
    return [*PUBLIC_ROOT_DOCS, *docs, ROOT / "skills" / "README.md"]


def _link_target(raw: str) -> str:
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    # Markdown permits an optional quoted title after the URL.
    return target.split(maxsplit=1)[0]


def test_public_docs_do_not_link_to_source_files_or_line_numbers():
    violations: list[str] = []
    for path in _public_markdown():
        text = path.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            target = _link_target(match.group(1))
            if SOURCE_TARGET_RE.match(target) or re.search(r"#L\d+", target):
                violations.append(f"{path.relative_to(ROOT)}: {target}")
    assert not violations, "public docs contain source links:\n" + "\n".join(violations)


def test_local_markdown_links_resolve():
    broken: list[str] = []
    for path in _public_markdown():
        text = path.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            target = _link_target(match.group(1))
            if (
                not target
                or target.startswith(("#", "http://", "https://", "mailto:"))
                or "://" in target
            ):
                continue
            rel = unquote(target.split("#", 1)[0])
            if rel and not (path.parent / rel).resolve().exists():
                broken.append(f"{path.relative_to(ROOT)}: {target}")
    assert not broken, "broken local documentation links:\n" + "\n".join(broken)


def test_documentation_has_chinese_and_english_pairs():
    missing: list[str] = []
    for english in DOCS.glob("*.md"):
        if english.name.endswith("_zh.md") or english.name.startswith("research-"):
            continue
        chinese = english.with_name(f"{english.stem}_zh.md")
        if not chinese.exists():
            missing.append(str(english.relative_to(ROOT)))
    assert not missing, "documentation missing _zh counterpart:\n" + "\n".join(missing)


def test_bundled_skills_are_listed_in_both_skill_docs():
    names = sorted(
        path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md")
    )
    for doc in (DOCS / "skills.md", DOCS / "skills_zh.md"):
        text = doc.read_text(encoding="utf-8")
        missing = [name for name in names if f"`{name}`" not in text]
        assert not missing, f"{doc.name} missing bundled skills: {missing}"


def test_example_environment_variables_are_in_configuration_reference():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    names = set(re.findall(r"^\s*#?\s*(MUSELAB_[A-Z0-9_]+)=", example, re.MULTILINE))
    for doc in (DOCS / "configuration.md", DOCS / "configuration_zh.md"):
        text = doc.read_text(encoding="utf-8")
        missing = sorted(name for name in names if f"`{name}`" not in text)
        assert not missing, f"{doc.name} missing .env.example variables: {missing}"


def test_documented_files_endpoint_count_matches_router():
    source = (ROOT / "backend" / "files.py").read_text(encoding="utf-8")
    count = len(re.findall(
        r"^@router\.(?:get|post|put|patch|delete|websocket)\(",
        source,
        re.MULTILINE,
    ))
    assert f"currently {count} Files endpoints" in (
        DOCS / "backend-files.md").read_text(encoding="utf-8")
    assert f"当前共 {count} 个文件端点" in (
        DOCS / "backend-files_zh.md").read_text(encoding="utf-8")


def test_docker_runtime_contains_bundled_skills_and_templates():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY skills ./skills" in dockerfile
    assert "COPY scripts/templates ./scripts/templates" in dockerfile
