import importlib.util
import json
from pathlib import Path


def _quota_script_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "codex-quota-refresh.py"
    spec = importlib.util.spec_from_file_location("muselab_codex_quota_refresh", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_app_server_account_snapshot_normalizes_multiple_limit_buckets_and_usage():
    quota = _quota_script_module()
    result = quota._normalize_account_snapshot(
        {
            "rateLimits": {
                "limitId": "codex",
                "planType": "pro",
                "primary": {
                    "usedPercent": 46,
                    "windowDurationMins": 300,
                    "resetsAt": 1782565701,
                },
                "secondary": {
                    "usedPercent": 7,
                    "windowDurationMins": 10080,
                    "resetsAt": 1783152501,
                },
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "planType": "pro",
                    "primary": {
                        "usedPercent": 46,
                        "windowDurationMins": 300,
                        "resetsAt": 1782565701,
                    },
                    "secondary": {
                        "usedPercent": 7,
                        "windowDurationMins": 10080,
                        "resetsAt": 1783152501,
                    },
                },
                "codex_spark": {
                    "limitId": "codex_spark",
                    "limitName": "Codex Spark",
                    "planType": "pro",
                    "primary": {
                        "usedPercent": 2,
                        "windowDurationMins": 10080,
                        "resetsAt": 1783152501,
                    },
                },
            },
            "rateLimitResetCredits": {"availableCount": 2},
        },
        {
            "summary": {"lifetimeTokens": 123456, "peakDailyTokens": 9000},
            "dailyUsageBuckets": [
                {"startDate": "2026-07-15", "tokens": 321},
                {"startDate": "2026-07-14", "tokens": 123},
            ],
        },
    )

    assert result["ok"] is True
    assert result["source"] == "codex-app-server"
    assert result["provider_authoritative"] is False
    assert result["account_authoritative"] is True
    assert result["windows"]["primary"]["rate_limit_type"] == "five_hour"
    assert result["windows"]["secondary"]["rate_limit_type"] == "seven_day"
    assert result["windows"]["codex_spark:primary"]["limit_name"] == "Codex Spark"
    assert result["rate_limit_reset_credits"]["available_count"] == 2
    assert result["account_usage"]["summary"]["lifetime_tokens"] == 123456
    assert result["account_usage"]["daily_usage_buckets"][0]["tokens"] == 123
    assert result["account_usage"]["daily_usage_buckets"][-1]["tokens"] == 321


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
    assert d["provider_authoritative"] is False
    assert d["source_scope"] == "codex_cli_session_log"
    assert d["windows"]["primary"]["rate_limit_type"] == "five_hour"
    assert d["windows"]["primary"]["remaining_percent"] == 54.0
    assert d["windows"]["secondary"]["rate_limit_type"] == "monthly"
    assert d["windows"]["secondary"]["remaining_percent"] == 93.0


def test_codex_rate_limit_labels_weekly_window(client, auth, tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    sessions = codex_home / "sessions" / "2026" / "06" / "30"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    event = {
        "timestamp": "2026-06-30T06:24:59.760Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": "codex",
                "plan_type": "prolite",
                "primary": {
                    "used_percent": 1.0,
                    "window_minutes": 300,
                    "resets_at": 1782809110,
                },
                "secondary": {
                    "used_percent": 0.0,
                    "window_minutes": 10080,
                    "resets_at": 1783395910,
                },
                "rate_limit_reached_type": None,
            },
        },
    }
    (sessions / "rollout.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    r = client.get("/api/chat/codex-rate-limit", headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["windows"]["primary"]["rate_limit_type"] == "five_hour"
    assert d["windows"]["secondary"]["rate_limit_type"] == "seven_day"


def test_codex_rate_limit_refresh_uses_app_server_bridge(client, auth, monkeypatch):
    from backend import chat as chat_mod

    payload = {
        "ok": True,
        "source": "codex-app-server",
        "source_scope": "codex_cli_exec_rate_limits",
        "provider_authoritative": False,
        "updated_at": 1782802582.322,
        "windows": {
            "primary": {
                "rate_limit_type": "five_hour",
                "used_percent": 8.0,
                "remaining_percent": 92.0,
            },
        },
        "elapsed_s": 3.8,
    }
    monkeypatch.setattr(chat_mod, "_refresh_codex_rate_limits", lambda: payload)

    r = client.get("/api/chat/codex-rate-limit?refresh=1", headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["source"] == "codex-app-server"
    assert d["elapsed_s"] == 3.8
    assert d["windows"]["primary"]["remaining_percent"] == 92.0
