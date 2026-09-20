"""Provider edits must commit whole transactions across concurrent requests."""
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest


def _provider(prefix):
    return {
        "base_url": f"https://{prefix}.example.test/anthropic",
        "prefix": prefix + ":",
        "display": prefix,
        "models": [prefix + ":model"],
    }


def _overlap_saves(monkeypatch, endpoints, first, second):
    """Hold the first write while an independent API request tries to finish."""
    entered = threading.Event()
    release = threading.Event()
    call_lock = threading.Lock()
    first_call = True
    save = endpoints._save_overrides

    def delayed_save(store):
        nonlocal first_call
        with call_lock:
            hold = first_call
            first_call = False
        if hold:
            entered.set()
            assert release.wait(10)
        return save(store)

    monkeypatch.setattr(endpoints, "_save_overrides", delayed_save)
    with ThreadPoolExecutor(max_workers=2) as workers:
        a = workers.submit(first)
        try:
            assert entered.wait(5)
            b = workers.submit(second)
            try:
                b.result(timeout=1)
            except TimeoutError:
                # Correct serialization waits for the first transaction.
                pass
        finally:
            release.set()
        return a.result(timeout=5), b.result(timeout=5)


@pytest.mark.parametrize("operation", [
    "create", "delete", "restore", "anthropic-models", "restore-anthropic",
])
def test_provider_edit_preserves_another_successful_edit(
    client, auth, monkeypatch, tmp_path, operation,
):
    from backend import endpoints

    monkeypatch.setattr(endpoints, "OVERRIDES_PATH", tmp_path / "providers.json")
    builtin = endpoints._builtin_id(endpoints.CATALOG[0])
    if operation == "restore":
        endpoints.delete_provider(builtin)
    if operation == "restore-anthropic":
        endpoints.set_anthropic_models(["claude-synthetic-model"])

    requests = {
        "create": ("/api/settings/providers", _provider("first")),
        "delete": ("/api/settings/providers/delete", {"id": builtin}),
        "restore": ("/api/settings/providers/restore", {"id": builtin}),
        "anthropic-models": (
            "/api/settings/providers/anthropic-models",
            {"models": ["claude-synthetic-model"]},
        ),
        "restore-anthropic": (
            "/api/settings/providers/restore", {"id": "anthropic"},
        ),
    }
    route, payload = requests[operation]
    first, second = _overlap_saves(
        monkeypatch, endpoints,
        lambda: client.post(route, headers=auth, json=payload),
        lambda: client.post(
            "/api/settings/providers", headers=auth, json=_provider("second"),
        ),
    )
    assert first.status_code == 200
    assert second.status_code == 200
    saved = endpoints._load_overrides()
    assert second.json()["id"] in saved["providers"]
    if operation == "create":
        assert first.json()["id"] in saved["providers"]
    elif operation == "delete":
        assert builtin in saved["deleted"]
    elif operation == "restore":
        assert builtin not in saved["deleted"]
    elif operation == "anthropic-models":
        assert saved["anthropic_models"] == ["claude-synthetic-model"]
    else:
        assert saved["anthropic_models"] is None


def test_concurrent_provider_creation_revalidates_unique_prefix(
    client, auth, monkeypatch, tmp_path,
):
    from backend import endpoints

    monkeypatch.setattr(endpoints, "OVERRIDES_PATH", tmp_path / "providers.json")
    payload = _provider("shared")
    other = dict(payload, base_url="https://other.example.test/anthropic")
    first, second = _overlap_saves(
        monkeypatch, endpoints,
        lambda: client.post("/api/settings/providers", headers=auth, json=payload),
        lambda: client.post("/api/settings/providers", headers=auth, json=other),
    )
    assert first.status_code == 200
    assert second.status_code == 422
    matches = [p for p in endpoints.catalog() if p.prefix == "shared:"]
    assert [p.id for p in matches] == [first.json()["id"]]
