"""Cost dashboard aggregation (GET /api/chat/cost-dashboard).

Stages a sidecar with cost annotations + a matching JSONL with
timestamps and verifies the bucketing math. Cleans up its own JSONL
under ~/.claude/projects after the test so it doesn't pollute the
developer's real CLI dir.
"""
import datetime as dt
import json
import uuid
from pathlib import Path

import pytest


def _projects_dir_for(root: Path) -> Path:
    return Path.home() / ".claude" / "projects" / str(root).replace("/", "-")


@pytest.fixture()
def _staged(temp_root, request, app_module):
    """Drop one sidecar (cost) + one JSONL (usage + ts) with 4 assistant
    turns under the per-cwd CLI dir + the monkeypatched test SESS_DIR.
    Includes a GLM turn with cost-side $0 to verify third-party tokens
    still aggregate via the JSONL path."""
    from backend import sessions as sess_mod
    sess_dir = sess_mod.SESS_DIR
    proj = _projects_dir_for(temp_root)
    sid = str(uuid.uuid4())
    uuids = ["u-today", "u-3d", "u-35d", "u-glm-today"]
    sidecar = sess_dir / f"{sid}.sidecar.json"
    # Sidecar only carries cost for the Claude turns — GLM has no cost
    # entry (mirrors real third-party behaviour).
    sidecar.write_text(json.dumps({
        "messages": {
            uuids[0]: {"cost": "$1.2345", "model": "claude-opus-4-7"},
            uuids[1]: {"cost": "$0.5000", "model": "claude-sonnet-4-6"},
            uuids[2]: {"cost": "$0.1000", "model": "claude-haiku-4-5-20251001"},
        }
    }))

    now = dt.datetime.now(dt.timezone.utc)
    jsonl_path = proj / f"{sid}.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    times = [now, now - dt.timedelta(days=3),
              now - dt.timedelta(days=35), now]
    models = ["claude-opus-4-7", "claude-sonnet-4-6",
               "claude-haiku-4-5-20251001", "glm-4.7"]
    # Per-turn usage shape mirrors what CLI writes for every vendor.
    usages = [
        {"input_tokens": 1000, "output_tokens": 200,
          "cache_read_input_tokens": 500, "cache_creation_input_tokens": 100},
        {"input_tokens": 800,  "output_tokens": 150,
          "cache_read_input_tokens": 0,    "cache_creation_input_tokens": 0},
        {"input_tokens": 600,  "output_tokens": 100,
          "cache_read_input_tokens": 0,    "cache_creation_input_tokens": 0},
        # GLM today — no sidecar cost, but tokens must still aggregate.
        {"input_tokens": 32240, "output_tokens": 35,
          "cache_read_input_tokens": 64,  "cache_creation_input_tokens": 0},
    ]
    with jsonl_path.open("w", encoding="utf-8") as f:
        for u, t, m, usage in zip(uuids, times, models, usages):
            f.write(json.dumps({
                "type": "assistant", "uuid": u,
                "timestamp": t.isoformat().replace("+00:00", "Z"),
                "message": {"model": m, "usage": usage, "content": [
                    {"type": "text", "text": "ok"}]},
            }) + "\n")

    def _cleanup():
        try:
            jsonl_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            proj.rmdir()
        except OSError:
            pass

    request.addfinalizer(_cleanup)
    return {"sid": sid, "uuids": uuids}


def test_cost_dashboard_aggregates_tokens_by_day_and_model(client, auth, _staged):
    r = client.get("/api/chat/cost-dashboard?days=30&tz_offset_minutes=0",
                    headers=auth)
    assert r.status_code == 200
    data = r.json()

    # All-time captures all 4 turns regardless of window (incl. GLM
    # which has no sidecar cost).
    assert data["all_time"]["turns"] == 4
    assert data["all_time"]["cost"] == pytest.approx(1.8345, abs=1e-4)
    # Sum of all input tokens: 1000 + 800 + 600 + 32240 = 34640
    assert data["all_time"]["input_tokens"] == 34640
    assert data["all_time"]["output_tokens"] == 485
    assert data["all_time"]["cache_read_tokens"] == 564

    # 30d window excludes the 35-day-old haiku turn.
    assert data["last_30d"]["turns"] == 3
    # cost without haiku = 1.2345 + 0.5 = 1.7345 (GLM cost is 0)
    assert data["last_30d"]["cost"] == pytest.approx(1.7345, abs=1e-4)

    # 7d window catches today (opus) + 3d (sonnet) + today (glm).
    assert data["last_7d"]["turns"] == 3

    # Today catches the two same-day turns (opus + glm).
    assert data["today"]["turns"] == 2
    # opus cost only — glm contributes 0 cost.
    assert data["today"]["cost"] == pytest.approx(1.2345, abs=1e-4)

    # by_model: GLM must be present even without sidecar cost.
    models = {m["model"]: m for m in data["by_model"]}
    assert "glm-4.7" in models, "third-party vendor missing from by_model"
    assert models["glm-4.7"]["input_tokens"] == 32240
    assert models["glm-4.7"]["cost"] == 0.0   # vendor doesn't report cost
    assert models["claude-opus-4-7"]["cost"] == pytest.approx(1.2345, abs=1e-4)

    # by_day is densified to exactly window_days entries. Use UTC date
    # to match the tz_offset_minutes=0 query — `dt.date.today()` is local
    # tz and disagrees when local clock is ahead of / behind UTC across
    # midnight (caused a flaky failure at 00:xx UTC).
    assert len(data["by_day"]) == 30
    assert data["by_day"][-1]["date"] == \
        dt.datetime.now(dt.timezone.utc).date().isoformat()


def test_cost_dashboard_empty_when_no_sidecars(client, auth):
    r = client.get("/api/chat/cost-dashboard?days=7", headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert data["all_time"]["turns"] == 0
    assert data["all_time"]["cost"] == 0.0
    assert len(data["by_day"]) == 7


def test_cost_dashboard_requires_auth(client):
    r = client.get("/api/chat/cost-dashboard")
    assert r.status_code in (401, 403)


def test_cost_dashboard_validates_days(client, auth):
    r = client.get("/api/chat/cost-dashboard?days=0", headers=auth)
    assert r.status_code == 422
    r = client.get("/api/chat/cost-dashboard?days=999", headers=auth)
    assert r.status_code == 422


def test_cost_dashboard_discovers_vendor_config_dir_jsonl(
    client, auth, app_module, temp_root, tmp_path, monkeypatch, request,
):
    """Third-party providers (DeepSeek / GLM / MiniMax) run the CLI with
    CLAUDE_CONFIG_DIR=/tmp/muselab-vendor-cli-config so Pro OAuth never
    leaks out. The CLI then writes its JSONL to
    <CLAUDE_CONFIG_DIR>/projects/, NOT ~/.claude/projects/. Dashboard
    must scan both roots — otherwise GLM/MiniMax token usage is invisible
    even though we already build the env_for_model isolation path."""
    import tempfile
    from backend import sessions as sess_mod

    # Build a fake vendor projects dir like the real CLI would.
    vendor_root = (Path(tempfile.gettempdir())
                    / "muselab-vendor-cli-config" / "projects")
    proj = vendor_root / str(temp_root).replace("/", "-")
    proj.mkdir(parents=True, exist_ok=True)

    sid = str(uuid.uuid4())
    # Sidecar in SESS_DIR registers the session as muselab-known (one of
    # the two ways known_sids gets populated). Cost stays empty —
    # third-party vendors don't report cost.
    (sess_mod.SESS_DIR / f"{sid}.sidecar.json").write_text(
        json.dumps({"messages": {}}))

    jsonl = proj / f"{sid}.jsonl"
    now = dt.datetime.now(dt.timezone.utc)
    jsonl.write_text(json.dumps({
        "type": "assistant",
        "uuid": "u-glm-vendor",
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "message": {
            "model": "glm-4.7",
            "usage": {
                "input_tokens": 12345, "output_tokens": 678,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            },
            "content": [{"type": "text", "text": "ok"}],
        },
    }) + "\n")

    def _cleanup():
        try:
            jsonl.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            proj.rmdir()
        except OSError:
            pass
    request.addfinalizer(_cleanup)

    r = client.get("/api/chat/cost-dashboard?days=7&tz_offset_minutes=0",
                    headers=auth)
    assert r.status_code == 200
    data = r.json()
    by_model = {m["model"]: m for m in data["by_model"]}
    assert "glm-4.7" in by_model, (
        "vendor-config-dir JSONL was not picked up — dashboard only "
        "scanned ~/.claude/projects/ and missed third-party turns"
    )
    assert by_model["glm-4.7"]["input_tokens"] == 12345
    assert by_model["glm-4.7"]["output_tokens"] == 678
    # Vendor doesn't report cost — sidecar has no entry — must be $0.
    assert by_model["glm-4.7"]["cost"] == 0.0
