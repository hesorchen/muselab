"""Provider editor: edit built-ins, create custom providers, restore / delete.

The effective catalog = built-in defaults + a user override layer persisted in
provider_overrides.json. These tests pin the HTTP contract the Settings UI
relies on, and verify edits actually re-route models (lookup / normalize) and
surface masked keys without ever echoing the raw value.
"""
import pytest


@pytest.fixture()
def iso_overrides(monkeypatch, tmp_path):
    """Redirect the override store to a throwaway file so tests never touch
    the repo's real provider_overrides.json (and stay hermetic)."""
    from backend import endpoints as ep
    monkeypatch.setattr(ep, "OVERRIDES_PATH", tmp_path / "provider_overrides.json")
    return ep


def _provider(client, auth, pid):
    d = client.get("/api/settings", headers=auth).json()
    return next((p for p in d["providers"] if p.get("id") == pid), None)


def test_anthropic_row_is_non_editable(client, auth, iso_overrides):
    p = _provider(client, auth, "anthropic")
    assert p is not None
    assert p["editable"] is False
    assert p["kind"] == "anthropic"


def test_builtins_surface_with_editor_fields(client, auth, iso_overrides):
    ds = _provider(client, auth, "b:deepseek-")
    assert ds is not None
    assert ds["editable"] is True
    assert ds["is_builtin"] is True
    assert ds["is_overridden"] is False
    assert ds["prefix"] == "deepseek-"
    assert ds["base_url"].startswith("http")
    assert isinstance(ds["models"], list) and ds["models"]


def test_edit_builtin_endpoint_and_models(client, auth, iso_overrides):
    r = client.post("/api/settings/providers", headers=auth, json={
        "id": "b:deepseek-",
        "base_url": "https://proxy.internal/anthropic",
        "prefix": "deepseek-",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    })
    assert r.status_code == 200, r.text
    ds = _provider(client, auth, "b:deepseek-")
    assert ds["is_overridden"] is True
    assert ds["base_url"] == "https://proxy.internal/anthropic"
    assert ds["models"] == ["deepseek-chat", "deepseek-reasoner"]
    assert ds["supports_thinking"] is True
    # New model id routes back to this provider, endpoint override applies.
    assert iso_overrides.lookup("deepseek-reasoner").id == "b:deepseek-"


def test_restore_builtin_to_factory(client, auth, iso_overrides):
    client.post("/api/settings/providers", headers=auth, json={
        "id": "b:deepseek-", "base_url": "https://x/anthropic",
        "prefix": "deepseek-", "models": ["deepseek-chat"],
    })
    assert _provider(client, auth, "b:deepseek-")["is_overridden"] is True
    r = client.post("/api/settings/providers/restore", headers=auth,
                    json={"id": "b:deepseek-"})
    assert r.status_code == 200 and r.json()["changed"] is True
    ds = _provider(client, auth, "b:deepseek-")
    assert ds["is_overridden"] is False
    assert "deepseek.com" in ds["base_url"]


def test_codex_gateway_builtin_surfaces_as_local_sidecar(client, auth, iso_overrides):
    p = _provider(client, auth, "b:codex:")
    assert p is not None
    assert p["display"] == "Codex Gateway"
    assert p["env_key"] == "CODEX_GATEWAY_API_KEY"
    assert p["base_url"] == "http://127.0.0.1:8317"
    assert p["prefix"] == "codex:"
    assert p["models"] and all(m.startswith("codex:") for m in p["models"])
    assert "codex:gpt-5.5" in p["models"]
    assert p["supports_thinking"] is False
    assert p["supports_effort"] is True


def test_create_custom_provider_with_key(client, auth, iso_overrides, monkeypatch, tmp_path):
    from backend import api_settings as api_s
    fake_env = tmp_path / "custom.env"
    fake_env.write_text("MUSELAB_TOKEN=existing-test-token-1234567890\n")
    monkeypatch.setattr(api_s, "ENV_PATH", fake_env)

    r = client.post("/api/settings/providers", headers=auth, json={
        "id": None,
        "base_url": "https://api.acme.ai/anthropic",
        "prefix": "acme-",
        "display": "Acme",
        "models": ["acme-large", "acme-small"],
        "api_key": "sk-acme-secret-value-987654321",
    })
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    env_key = r.json()["env_key"]
    assert pid.startswith("c:")
    assert env_key.startswith("MUSELAB_PROVIDER_") and env_key.endswith("_API_KEY")
    # Key written to .env, never echoed back raw.
    assert f"{env_key}=sk-acme-secret-value-987654321" in fake_env.read_text()
    p = _provider(client, auth, pid)
    assert p["configured"] is True
    assert "sk-acme-secret" not in p["masked"]
    assert "•" in p["masked"]
    # Routing works for the new prefix.
    assert iso_overrides.lookup("acme-large").id == pid


def test_create_rejects_dup_prefix(client, auth, iso_overrides):
    r = client.post("/api/settings/providers", headers=auth, json={
        "id": None, "base_url": "https://x.ai/anthropic",
        "prefix": "deepseek-", "models": ["deepseek-foo"],
    })
    assert r.status_code == 422
    assert "prefix" in r.json()["detail"].lower()


def test_create_rejects_model_not_prefixed(client, auth, iso_overrides):
    r = client.post("/api/settings/providers", headers=auth, json={
        "id": None, "base_url": "https://y.ai/anthropic",
        "prefix": "zeta-", "models": ["wrong-id"],
    })
    assert r.status_code == 422


def test_create_rejects_bad_url(client, auth, iso_overrides):
    r = client.post("/api/settings/providers", headers=auth, json={
        "id": None, "base_url": "not-a-url",
        "prefix": "zeta-", "models": ["zeta-1"],
    })
    assert r.status_code == 422


def test_delete_custom_and_tombstone_builtin(client, auth, iso_overrides):
    # custom: gone outright
    pid = client.post("/api/settings/providers", headers=auth, json={
        "id": None, "base_url": "https://api.acme.ai/anthropic",
        "prefix": "acme-", "models": ["acme-x"],
    }).json()["id"]
    assert _provider(client, auth, pid) is not None
    client.post("/api/settings/providers/delete", headers=auth, json={"id": pid})
    assert _provider(client, auth, pid) is None

    # built-in: tombstoned (hidden) until restored
    client.post("/api/settings/providers/delete", headers=auth, json={"id": "b:glm-"})
    assert _provider(client, auth, "b:glm-") is None
    client.post("/api/settings/providers/restore", headers=auth, json={"id": "b:glm-"})
    assert _provider(client, auth, "b:glm-") is not None


def test_restore_noop_for_custom(client, auth, iso_overrides):
    pid = client.post("/api/settings/providers", headers=auth, json={
        "id": None, "base_url": "https://api.acme.ai/anthropic",
        "prefix": "acme-", "models": ["acme-x"],
    }).json()["id"]
    r = client.post("/api/settings/providers/restore", headers=auth, json={"id": pid})
    assert r.status_code == 200 and r.json()["changed"] is False


def test_namespaced_provider_model_routes_through_probe(
    client, auth, iso_overrides, monkeypatch,
):
    import httpx
    from urllib.parse import quote

    model = "account-gateway:secondary/gpt-test"
    response = client.post("/api/settings/providers", headers=auth, json={
        "base_url": "https://gateway.example.test",
        "prefix": "account-gateway:",
        "models": [model],
    })
    assert response.status_code == 200, response.text
    saved = response.json()
    assert _provider(client, auth, saved["id"])["models"] == [model]
    assert iso_overrides.lookup(model).id == saved["id"]
    assert iso_overrides.normalize_model_id(model) == "secondary/gpt-test"
    monkeypatch.setenv(saved["env_key"], "test-provider-credential")

    calls = []

    async def fake_post(self, url, *, json, headers):
        calls.append((url, json["model"]))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    probe = client.get("/api/chat/probe/" + quote(model, safe=""), headers=auth)
    assert probe.status_code == 200, probe.text
    assert probe.json()["ok"] is True
    assert calls == [("https://gateway.example.test/v1/messages", "secondary/gpt-test")]


@pytest.mark.parametrize("suffix", ["account//model", "account/../model", "account/", "a" * 100])
def test_namespaced_provider_rejects_invalid_paths(client, auth, iso_overrides, suffix):
    response = client.post("/api/settings/providers", headers=auth, json={
        "base_url": "https://gateway.example.test",
        "prefix": "account-gateway:",
        "models": ["account-gateway:" + suffix],
    })
    assert response.status_code == 422


@pytest.mark.parametrize("source", ["environment", "override"])
def test_custom_codex_account_inherits_gateway_capabilities(
    client, auth, iso_overrides, monkeypatch, source,
):
    gateway = "https://gateway.example.test"
    monkeypatch.delenv("CODEX_GATEWAY_BASE_URL", raising=False)
    if source == "environment":
        monkeypatch.setenv("CODEX_GATEWAY_BASE_URL", gateway + "/")
    else:
        iso_overrides.upsert_provider(
            pid="b:codex:", base_url=gateway, prefix="codex:",
            display="Codex Gateway", env_key="CODEX_GATEWAY_API_KEY",
            models=["codex:gpt-test"],
        )
    response = client.post("/api/settings/providers", headers=auth, json={
        "base_url": gateway, "prefix": "account-gateway:",
        "display": "Codex Secondary Gateway",
        "models": ["account-gateway:secondary/gpt-test"],
    })
    assert response.status_code == 200
    provider = iso_overrides.get_provider(response.json()["id"])
    assert provider.supports_effort is True
    assert provider.supports_thinking is False
    assert provider.max_output_tokens == 128000
    assert not iso_overrides._looks_like_codex_provider(
        "Codex Secondary Gateway", "account-gateway:", "OTHER_API_KEY",
        gateway + ".unrelated.test",
    )


def test_custom_codex_account_catalog_controls_stay_separate(
    client, auth, iso_overrides, monkeypatch,
):
    import httpx
    from backend import chat

    gateway = "https://gateway.example.test"
    monkeypatch.setenv("CODEX_GATEWAY_BASE_URL", gateway)
    monkeypatch.setenv("CODEX_GATEWAY_API_KEY", "test-shared-key")
    monkeypatch.setenv("MUSELAB_PROVIDER_SECONDARY_API_KEY", "test-shared-key")
    iso_overrides.upsert_provider(
        pid="b:codex:", base_url=gateway, prefix="codex:",
        display="Codex Gateway", env_key="CODEX_GATEWAY_API_KEY",
        models=["codex:gpt-test"],
    )
    iso_overrides.upsert_provider(
        pid=None, base_url=gateway, prefix="account-gateway:",
        display="Codex Secondary Gateway",
        env_key="MUSELAB_PROVIDER_SECONDARY_API_KEY",
        models=["account-gateway:secondary/gpt-test",
                "account-gateway:secondary/gpt-other"],
    )
    requests = []

    class CatalogClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            requests.append(url)
            assert url == gateway + "/v1/models?client_version"
            return httpx.Response(200, json={"models": [
                {"slug": "gpt-test", "context_window": 272000,
                 "supported_reasoning_levels": ["high", "ultra"]},
                {"slug": "secondary/gpt-test", "context_window": 921000,
                 "supported_reasoning_levels": ["low", "high", "max"]},
                {"slug": "secondary/gpt-other", "context_window": 272000,
                 "supported_reasoning_levels": ["medium"]},
            ]})

    monkeypatch.setattr(httpx, "AsyncClient", CatalogClient)
    # The first response schedules a refresh; the next one must use that cache
    # for every internal alias without mixing account-prefixed capabilities.
    client.get("/api/chat/providers", headers=auth)
    response = client.get("/api/chat/providers", headers=auth)
    rows = {row["model"]: row for row in response.json()["models"]}
    second_model = "account-gateway:secondary/gpt-test"
    assert rows[second_model]["supports_effort"] is True
    assert rows[second_model]["supports_thinking"] is False
    assert rows[second_model]["effort_levels"] == ["auto", "low", "high", "max"]
    assert rows["account-gateway:secondary/gpt-other"]["effort_levels"] == ["auto", "medium"]
    assert rows["codex:gpt-test"]["effort_levels"] == ["auto", "high", "ultra"]
    assert chat._cached_gateway_context_capability(second_model)["context_raw_limit"] == 921000
    assert len(requests) == 1
    sid = client.post("/api/chat/sessions", headers=auth, json={
        "name": "secondary account effort", "model": second_model,
    }).json()["id"]
    assert client.patch(f"/api/chat/sessions/{sid}", headers=auth,
                        json={"effort": "high"}).status_code == 200
    assert client.patch(f"/api/chat/sessions/{sid}", headers=auth,
                        json={"effort": "ultra"}).status_code == 400
