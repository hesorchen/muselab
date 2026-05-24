"""Tests for the Claude Auth (Pro/Max OAuth) settings endpoints.

Covers:
  - /api/settings/claude-auth/status — returns deterministic shape in all
    three states (CLI missing / CLI present but not logged in / logged in)
  - /api/settings/claude-auth/disconnect — moves credentials file to a
    timestamped .bak; idempotent when file already absent
  - Cross-platform: shutil.which finds the CLI on both POSIX and Windows
    (via PATHEXT) — we don't actually mock that, just assert the field is
    a string-or-None.
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path

import pytest


def _hdr():
    return {"X-Auth-Token": os.environ["MUSELAB_TOKEN"]}


def test_status_endpoint_returns_deterministic_shape(client):
    """No matter what state the host is in, /status returns ALL fields."""
    r = client.get("/api/settings/claude-auth/status", headers=_hdr())
    assert r.status_code == 200
    d = r.json()
    # Every field must exist (UI relies on this for null-safe rendering)
    for field in ("cli_installed", "cli_path", "credentials_file_present",
                  "logged_in", "email", "org_name", "subscription_type",
                  "expires_at", "reason"):
        assert field in d, f"missing field: {field}"
    # Types
    assert isinstance(d["cli_installed"], bool)
    assert isinstance(d["credentials_file_present"], bool)
    assert isinstance(d["logged_in"], bool)
    assert d["cli_path"] is None or isinstance(d["cli_path"], str)


def test_status_endpoint_requires_auth(client):
    """No token → 401, not 200 with empty body."""
    r = client.get("/api/settings/claude-auth/status")
    assert r.status_code in (401, 403)


def test_status_endpoint_when_cli_missing(client, monkeypatch):
    """If `claude` is not on PATH, cli_installed=False and logged_in=False
    with a 'cli-not-installed' reason."""
    import backend.api_settings as mod
    monkeypatch.setattr(mod, "_claude_cli_path", lambda: None)
    r = client.get("/api/settings/claude-auth/status", headers=_hdr())
    d = r.json()
    assert d["cli_installed"] is False
    assert d["cli_path"] is None
    assert d["logged_in"] is False
    assert d["reason"] == "cli-not-installed"


def test_status_endpoint_when_cli_present_but_not_logged_in(client, monkeypatch):
    """CLI installed but returns non-zero (no credentials) → logged_in=False
    with stderr captured in reason for diagnostic."""
    import subprocess as _sp
    import backend.api_settings as mod
    monkeypatch.setattr(mod, "_claude_cli_path", lambda: "/usr/bin/claude")
    # Fake out subprocess.run to return non-zero
    def fake_run(*args, **kwargs):
        return _sp.CompletedProcess(args=args[0], returncode=1,
                                     stdout="", stderr="Not logged in")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    r = client.get("/api/settings/claude-auth/status", headers=_hdr())
    d = r.json()
    assert d["cli_installed"] is True
    assert d["logged_in"] is False
    assert d["reason"] == "not-logged-in"


def test_status_endpoint_parses_logged_in_state(client, monkeypatch):
    """When `claude auth status --json` returns a valid auth payload,
    fields surface correctly."""
    import subprocess as _sp
    import backend.api_settings as mod
    monkeypatch.setattr(mod, "_claude_cli_path", lambda: "/usr/bin/claude")
    payload = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "email": "test@example.com",
        "orgId": "org-abc",
        "orgName": "Test Org",
        "subscriptionType": "max",
    }
    def fake_run(*args, **kwargs):
        return _sp.CompletedProcess(args=args[0], returncode=0,
                                     stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    r = client.get("/api/settings/claude-auth/status", headers=_hdr())
    d = r.json()
    assert d["logged_in"] is True
    assert d["email"] == "test@example.com"
    assert d["org_name"] == "Test Org"
    assert d["subscription_type"] == "max"
    assert d["reason"] is None


def test_status_endpoint_handles_cli_timeout(client, monkeypatch):
    """If `claude auth status` hangs, we shouldn't 500 — return a clean
    deterministic 'cli-timeout' reason."""
    import subprocess as _sp
    import backend.api_settings as mod
    monkeypatch.setattr(mod, "_claude_cli_path", lambda: "/usr/bin/claude")
    def fake_run(*args, **kwargs):
        raise _sp.TimeoutExpired(cmd=args[0], timeout=1.0)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    r = client.get("/api/settings/claude-auth/status", headers=_hdr())
    assert r.status_code == 200
    d = r.json()
    assert d["logged_in"] is False
    assert d["reason"] == "cli-timeout"


def test_status_endpoint_handles_bad_json(client, monkeypatch):
    """If CLI returns 0 but non-JSON output (rare), don't crash — report
    'cli-bad-json' so the UI shows a useful error."""
    import subprocess as _sp
    import backend.api_settings as mod
    monkeypatch.setattr(mod, "_claude_cli_path", lambda: "/usr/bin/claude")
    def fake_run(*args, **kwargs):
        return _sp.CompletedProcess(args=args[0], returncode=0,
                                     stdout="this is not json", stderr="")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    r = client.get("/api/settings/claude-auth/status", headers=_hdr())
    d = r.json()
    assert d["logged_in"] is False
    assert d["reason"] == "cli-bad-json"


def test_disconnect_when_no_credentials(client, monkeypatch, tmp_path):
    """Disconnect on a clean state → ok + already_disconnected flag."""
    import backend.api_settings as mod
    monkeypatch.setattr(mod, "_CLAUDE_CRED", tmp_path / ".credentials.json")
    r = client.post("/api/settings/claude-auth/disconnect", headers=_hdr())
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d.get("already_disconnected") is True


def test_disconnect_moves_credentials_to_timestamped_bak(client, monkeypatch, tmp_path):
    """Disconnect when credentials exist → moves the file to .json.<stamp>.bak."""
    import backend.api_settings as mod
    cred = tmp_path / ".credentials.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "fake"}}))
    monkeypatch.setattr(mod, "_CLAUDE_CRED", cred)
    r = client.post("/api/settings/claude-auth/disconnect", headers=_hdr())
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    backup = Path(d["backup_path"])
    assert backup.exists()
    assert ".bak" in backup.name
    assert ".credentials.json" in backup.name
    # Original is gone
    assert not cred.exists()


def test_disconnect_preserves_earlier_backup(client, monkeypatch, tmp_path):
    """Two disconnect cycles must produce TWO distinct .bak files —
    timestamp suffix prevents clobbering."""
    import backend.api_settings as mod
    cred = tmp_path / ".credentials.json"
    cred.write_text(json.dumps({"v": 1}))
    monkeypatch.setattr(mod, "_CLAUDE_CRED", cred)
    r1 = client.post("/api/settings/claude-auth/disconnect", headers=_hdr())
    backup1 = Path(r1.json()["backup_path"])
    assert backup1.exists()
    # Simulate re-connect: write a new credentials file
    cred.write_text(json.dumps({"v": 2}))
    # Sleep > 1s so the next timestamp is distinct (format is YYYYMMDD-HHMMSS)
    time.sleep(1.1)
    r2 = client.post("/api/settings/claude-auth/disconnect", headers=_hdr())
    backup2 = Path(r2.json()["backup_path"])
    assert backup2.exists()
    assert backup1.exists(), "earlier backup must not be clobbered"
    assert backup1 != backup2


def test_disconnect_requires_auth(client):
    """No token → 401/403, not 200."""
    r = client.post("/api/settings/claude-auth/disconnect")
    assert r.status_code in (401, 403)


def test_credentials_expiry_read_returns_none_for_missing_file(monkeypatch, tmp_path):
    """Helper returns None gracefully — not raising — when the file
    isn't there. UI shows '—' for the expiry field."""
    import backend.api_settings as mod
    monkeypatch.setattr(mod, "_CLAUDE_CRED", tmp_path / "nope.json")
    assert mod._read_credentials_expiry() is None


def test_credentials_expiry_read_parses_field(monkeypatch, tmp_path):
    """Helper extracts claudeAiOauth.expiresAt when present."""
    import backend.api_settings as mod
    cred = tmp_path / ".credentials.json"
    cred.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "x", "expiresAt": 1234567890123}
    }))
    monkeypatch.setattr(mod, "_CLAUDE_CRED", cred)
    assert mod._read_credentials_expiry() == 1234567890123


def test_credentials_expiry_read_handles_corrupt_file(monkeypatch, tmp_path):
    """Corrupt JSON should return None, not raise."""
    import backend.api_settings as mod
    cred = tmp_path / ".credentials.json"
    cred.write_text("{not valid json")
    monkeypatch.setattr(mod, "_CLAUDE_CRED", cred)
    assert mod._read_credentials_expiry() is None
