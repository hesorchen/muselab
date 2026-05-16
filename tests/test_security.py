"""Security boundaries that must not regress: path traversal,
sensitive-file blocking, MUSELAB_ROOT validation, short token rejection."""
import pytest


# ---- path traversal ----

def test_traversal_dotdot_blocked(client, auth):
    r = client.get("/api/files/read?path=../../../etc/passwd", headers=auth)
    assert r.status_code in (400, 403)


def test_traversal_absolute_blocked(client, auth):
    r = client.get("/api/files/read?path=/etc/passwd", headers=auth)
    # Path is stripped of leading slash then resolved relative to ROOT — should
    # land outside or 404, but never return /etc/passwd.
    assert r.status_code in (400, 403, 404)
    assert b"root:" not in r.content


# ---- sensitive file blocking ----

@pytest.mark.parametrize("name", [
    ".env", ".env.local", ".env.production",
    "id_rsa", "id_ed25519",
    "credentials.json",
    "secret.pem",
    "foo.key",
])
def test_sensitive_files_blocked_for_read(client, auth, temp_root, name):
    (temp_root / name).write_text("sensitive")
    r = client.get(f"/api/files/read?path={name}", headers=auth)
    assert r.status_code == 403


def test_sensitive_files_blocked_for_write(client, auth):
    r = client.put(
        "/api/files/write",
        headers=auth,
        json={"path": ".env.production", "content": "evil"},
    )
    assert r.status_code == 403


# ---- MUSELAB_ROOT validation ----

@pytest.mark.parametrize("bad", ["/", "/etc", "/root", "/home", "/var", "/usr"])
def test_portal_root_blocklist(monkeypatch, bad, tmp_path):
    """settings.py refuses dangerous MUSELAB_ROOT values at import time."""
    import sys
    monkeypatch.setenv("MUSELAB_TOKEN", "long-enough-test-token-1234567890abcdef")
    monkeypatch.setenv("MUSELAB_ROOT", bad)
    for n in [m for m in list(sys.modules) if m.startswith("backend")]:
        del sys.modules[n]
    with pytest.raises(RuntimeError, match="too broad|does not exist"):
        import backend.settings  # type: ignore[import]  # noqa: F401


def test_short_token_rejected(monkeypatch, tmp_path):
    import sys
    monkeypatch.setenv("MUSELAB_TOKEN", "short")
    monkeypatch.setenv("MUSELAB_ROOT", str(tmp_path))
    for n in [m for m in list(sys.modules) if m.startswith("backend")]:
        del sys.modules[n]
    with pytest.raises(RuntimeError, match="too short"):
        import backend.settings  # type: ignore[import]  # noqa: F401
