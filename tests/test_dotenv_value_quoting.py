"""Provider credentials must survive the same dotenv reload used at startup."""
import os

import pytest
from dotenv import dotenv_values, load_dotenv


@pytest.mark.parametrize("existing", [False, True], ids=["append", "replace"])
@pytest.mark.parametrize("value", [
    "synthetic # retained suffix",
    "'synthetic-quoted'",
    '"synthetic-quoted"',
    " synthetic-spaces ",
    "\tshared-secret\t",
    "synthetic # apostrophe's \\ path",
])
def test_saved_provider_value_survives_restart(
    client, auth, monkeypatch, tmp_path, existing, value,
):
    from backend import api_settings

    key = "DEEPSEEK_API_KEY"
    path = tmp_path / "settings.env"
    path.write_text(
        "# preserved comment\nUNRELATED=keep\n"
        + (f"{key}=previous-value\n" if existing else ""),
        encoding="utf-8",
    )
    monkeypatch.setattr(api_settings, "ENV_PATH", path)
    response = client.put(
        "/api/settings", headers=auth,
        json={"provider_keys": {key: value}},
    )
    assert response.status_code == 200
    assert os.environ[key] == value
    assert dotenv_values(path)[key] == value
    monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("UNRELATED", raising=False)
    load_dotenv(path, override=True)
    assert os.environ[key] == value
    assert os.environ["UNRELATED"] == "keep"
    assert "# preserved comment" in path.read_text(encoding="utf-8")
