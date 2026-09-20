"""Settings edits must replace or remove export-prefixed and quoted dotenv keys."""
import os

import pytest
from dotenv import dotenv_values, load_dotenv


@pytest.mark.parametrize("assignment", ["export {key}", "export\t{key}", "'{key}'", "export '{key}'"])
@pytest.mark.parametrize("replacement", ["synthetic-new-value", "_delete_"])
def test_dotenv_provider_key_is_updated_without_reviving_old_credentials(
    client, auth, monkeypatch, tmp_path, assignment, replacement,
):
    from backend import api_settings

    key = "DEEPSEEK_API_KEY"
    original = "synthetic-old-value"
    path = tmp_path / "exported.env"
    path.write_text(
        f"# retained comment\n{assignment.format(key=key)}={original}\nUNRELATED=keep\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(api_settings, "ENV_PATH", path)
    monkeypatch.setenv(key, original)
    assert dotenv_values(path)[key] == original

    response = client.put(
        "/api/settings", headers=auth,
        json={"deepseek_api_key": replacement},
    )
    assert response.status_code == 200
    text = path.read_text(encoding="utf-8")
    assert "# retained comment" in text
    assert dotenv_values(path)["UNRELATED"] == "keep"

    # Model startup reloads the saved dotenv file. An old assignment
    # must not restore a deleted credential when the process restarts.
    monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("UNRELATED", raising=False)
    load_dotenv(path, override=True)
    if replacement == "_delete_":
        assert key not in os.environ
        assert key not in dotenv_values(path)
    else:
        assert os.environ[key] == replacement
        assert text.count(key + "=") == 1
    assert original not in text
