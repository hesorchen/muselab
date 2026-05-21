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
    with pytest.raises(RuntimeError, match="system / cross-user path|does not exist"):
        import backend.settings  # type: ignore[import]  # noqa: F401


def test_short_token_rejected(monkeypatch, tmp_path):
    import sys
    monkeypatch.setenv("MUSELAB_TOKEN", "short")
    monkeypatch.setenv("MUSELAB_ROOT", str(tmp_path))
    for n in [m for m in list(sys.modules) if m.startswith("backend")]:
        del sys.modules[n]
    with pytest.raises(RuntimeError, match="too short"):
        import backend.settings  # type: ignore[import]  # noqa: F401


# ---- symlink escape ----

def test_symlink_to_outside_root_blocked_for_read(client, auth, temp_root, tmp_path):
    """A symlink inside ROOT pointing at /etc/passwd must not let the reader
    out. safe_resolve calls .resolve() which follows symlinks → we then
    verify the resolved path is still under ROOT."""
    secret = tmp_path / "secret"
    secret.write_text("OUTSIDE-ROOT-CANARY")
    (temp_root / "trap").symlink_to(secret)
    r = client.get("/api/files/read?path=trap", headers=auth)
    assert r.status_code in (400, 403)
    assert b"CANARY" not in r.content


def test_symlink_to_outside_root_blocked_for_write(client, auth, temp_root, tmp_path):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (temp_root / "evil-link").symlink_to(outside_dir)
    r = client.put("/api/files/write",
                    headers=auth,
                    json={"path": "evil-link/poisoned.txt", "content": "x"})
    assert r.status_code in (400, 403)
    assert not (outside_dir / "poisoned.txt").exists()


# ---- upload caps ----

def test_upload_size_cap(client, auth, monkeypatch):
    """Large uploads abort mid-stream with 413, partial file is cleaned up."""
    import io
    from backend import files as f
    monkeypatch.setattr(f, "MAX_UPLOAD_BYTES", 100)   # 100 bytes for the test
    big = b"x" * 500
    r = client.post(
        "/api/files/upload",
        headers=auth,
        files={"file": ("big.txt", io.BytesIO(big), "text/plain")},
        data={"path": ""},
    )
    assert r.status_code == 413


def test_upload_blocks_executable_extension(client, auth):
    import io
    r = client.post(
        "/api/files/upload",
        headers=auth,
        files={"file": ("payload.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
        data={"path": ""},
    )
    assert r.status_code == 400


def test_upload_blocks_sensitive_filename(client, auth):
    import io
    r = client.post(
        "/api/files/upload",
        headers=auth,
        files={"file": (".env.production", io.BytesIO(b"SECRET=x"), "text/plain")},
        data={"path": ""},
    )
    assert r.status_code == 403


# ---- /api/log/client-error rate limit ----

def test_client_error_rate_limited(client):
    """The unauthenticated client-error sink must not be floodable.
    First N requests log to stderr, rest return rate_limited:true."""
    from backend import main as m
    # Reset bucket state so previous tests don't interfere.
    m._CLIENT_ERR_BUCKETS.clear()
    cap = m._CLIENT_ERR_PER_WINDOW
    payload = {"msg": "test error"}
    # First `cap` requests should pass through.
    for _ in range(cap):
        r = client.post("/api/log/client-error", json=payload)
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True
        assert not body.get("rate_limited")
    # The next one must be rate-limited.
    r = client.post("/api/log/client-error", json=payload)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "rate_limited": True}
