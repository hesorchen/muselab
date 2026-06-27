import json


def test_codex_rate_limit_reads_local_session_log(client, auth, tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    sessions = codex_home / "sessions" / "2026" / "06" / "27"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    event = {
        "timestamp": "2026-06-27T11:08:30.585Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": None,
            "rate_limits": {
                "limit_id": "codex",
                "plan_type": "plus",
                "primary": {
                    "used_percent": 46.0,
                    "window_minutes": 300,
                    "resets_at": 1782565701,
                },
                "secondary": {
                    "used_percent": 7.0,
                    "window_minutes": 43200,
                    "resets_at": 1783152501,
                },
                "credits": None,
                "individual_limit": None,
                "rate_limit_reached_type": None,
            },
        },
    }
    (sessions / "rollout.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    r = client.get("/api/chat/codex-rate-limit", headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["plan_type"] == "plus"
    assert d["windows"]["primary"]["rate_limit_type"] == "five_hour"
    assert d["windows"]["primary"]["remaining_percent"] == 54.0
    assert d["windows"]["secondary"]["rate_limit_type"] == "monthly"
    assert d["windows"]["secondary"]["remaining_percent"] == 93.0
