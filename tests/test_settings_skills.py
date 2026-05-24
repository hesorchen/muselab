"""Tests for /api/settings/skills — skill discovery."""
import pytest
from pathlib import Path


@pytest.fixture
def fake_skill_dirs(monkeypatch, tmp_path, app_module):
    """Redirect USER + PROJECT + PLUGIN skill dirs to tmp_path so the
    dev's real Claude Code skill collection doesn't bleed into the
    test assertions (real `~/.claude/skills` has 20+ entries, real
    `~/.claude/plugins/marketplaces/*/plugins/*/skills` has more)."""
    from backend import api_settings
    user = tmp_path / "user_skills"
    proj = tmp_path / "project_skills"
    plugin = tmp_path / "plugin_root"   # empty unless a test populates it
    user.mkdir()
    proj.mkdir()
    plugin.mkdir()
    monkeypatch.setattr(api_settings, "SKILL_USER_DIR", user)
    monkeypatch.setattr(api_settings, "SKILL_PROJECT_DIR", proj)
    monkeypatch.setattr(api_settings, "SKILL_PLUGIN_ROOT", plugin)
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


def test_discovers_plugin_skills_with_scope_and_source(
    fake_skill_dirs, monkeypatch, tmp_path, client, auth
):
    """Plugin skills live under
    ~/.claude/plugins/marketplaces/<mp>/plugins/<plugin>/skills/<skill>/.
    They should surface with scope='plugin' and a `source` field
    pointing at <marketplace>/<plugin> so the UI can show which plugin
    a skill came from."""
    from backend import api_settings
    plugin_root = tmp_path / "plugin_root"
    # Layout: plugin_root/mp1/plugins/myplugin/skills/cool-skill/SKILL.md
    skill_dir = (plugin_root / "mp1" / "plugins" / "myplugin"
                 / "skills" / "cool-skill")
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: cool-skill\ndescription: \"a cool plugin skill\"\n---\n"
    )
    # Make sure pointer is in place (fixture already does this, but be explicit).
    monkeypatch.setattr(api_settings, "SKILL_PLUGIN_ROOT", plugin_root)

    r = client.get("/api/settings/skills", headers=auth)
    assert r.status_code == 200
    skills = r.json()["skills"]
    by_name = {s["name"]: s for s in skills}
    assert "cool-skill" in by_name
    assert by_name["cool-skill"]["scope"] == "plugin"
    assert by_name["cool-skill"]["source"] == "mp1/myplugin"
    assert by_name["cool-skill"]["description"] == "a cool plugin skill"


def test_plugin_root_missing_doesnt_crash(fake_skill_dirs, monkeypatch, client, auth):
    """If ~/.claude/plugins/marketplaces doesn't exist (user hasn't
    installed any plugins), discovery should silently return [], not 500."""
    from backend import api_settings
    monkeypatch.setattr(api_settings, "SKILL_PLUGIN_ROOT",
                         api_settings.Path("/nonexistent/path/no-plugins"))
    r = client.get("/api/settings/skills", headers=auth)
    assert r.status_code == 200
    # Should still return whatever's in project + user (which is empty here).
    assert r.json() == {"skills": []}
