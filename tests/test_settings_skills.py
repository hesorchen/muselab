"""Tests for /api/settings/skills — skill discovery."""
import pytest
from pathlib import Path


@pytest.fixture
def fake_skill_dirs(monkeypatch, tmp_path, app_module):
    """Redirect USER + PROJECT skill dirs to tmp_path."""
    from backend import api_settings
    user = tmp_path / "user_skills"
    proj = tmp_path / "project_skills"
    user.mkdir()
    proj.mkdir()
    monkeypatch.setattr(api_settings, "SKILL_USER_DIR", user)
    monkeypatch.setattr(api_settings, "SKILL_PROJECT_DIR", proj)
    return user, proj


def _write_skill(d: Path, name: str, desc: str, file="SKILL.md"):
    sd = d / name
    sd.mkdir()
    (sd / file).write_text(
        f"---\nname: {name}\ndescription: \"{desc}\"\n---\n\n# {name}\nbody")


def test_no_skills_returns_empty_list(fake_skill_dirs, client, auth):
    r = client.get("/api/settings/skills", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"skills": []}


def test_lists_project_and_user_with_scope(fake_skill_dirs, client, auth):
    user, proj = fake_skill_dirs
    _write_skill(proj, "alpha", "project skill")
    _write_skill(user, "beta", "user skill")
    r = client.get("/api/settings/skills", headers=auth)
    assert r.status_code == 200
    skills = r.json()["skills"]
    by_name = {s["name"]: s for s in skills}
    assert by_name["alpha"]["scope"] == "project"
    assert by_name["alpha"]["description"] == "project skill"
    assert by_name["beta"]["scope"] == "user"
    assert by_name["beta"]["description"] == "user skill"


def test_handles_lowercase_skill_md(fake_skill_dirs, client, auth):
    user, _ = fake_skill_dirs
    _write_skill(user, "lower", "lowercase file", file="skill.md")
    r = client.get("/api/settings/skills", headers=auth)
    names = [s["name"] for s in r.json()["skills"]]
    assert "lower" in names


def test_ignores_skill_dir_without_md(fake_skill_dirs, client, auth):
    _, proj = fake_skill_dirs
    (proj / "empty").mkdir()
    (proj / "empty" / "config.yaml").write_text("foo: bar")
    r = client.get("/api/settings/skills", headers=auth)
    assert r.json() == {"skills": []}


def test_handles_skill_md_without_frontmatter(fake_skill_dirs, client, auth):
    _, proj = fake_skill_dirs
    sd = proj / "minimal"
    sd.mkdir()
    (sd / "SKILL.md").write_text("# Just a heading\n\nbody")
    r = client.get("/api/settings/skills", headers=auth)
    skills = r.json()["skills"]
    assert len(skills) == 1
    assert skills[0]["name"] == "minimal"
    assert skills[0]["description"] == ""


def test_unauthorized_returns_401(fake_skill_dirs, client):
    r = client.get("/api/settings/skills")
    assert r.status_code == 401
