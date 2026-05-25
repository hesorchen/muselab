"""Cross-session full-text search (GET /api/chat/search).

The search endpoint reads CLI JSONL files under
~/.claude/projects/<encoded-cwd>/. These tests stage a few synthetic
JSONL lines there, hit the endpoint, and verify the hit shape +
ordering. Each test cleans up its own JSONL files to avoid polluting
the developer's real CLI projects dir.
"""
import json
import uuid
from pathlib import Path

import pytest


def _projects_dir_for(root: Path) -> Path:
    # Use the shared encoder — see backend.chat._cli_encode_cwd for why a
    # naive `str(root).replace("/", "-")` breaks on paths containing `_`.
    from backend.chat import _cli_encode_cwd
    return Path.home() / ".claude" / "projects" / _cli_encode_cwd(str(root))


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


@pytest.fixture()
def _staged_jsonls(temp_root, request):
    """Drop two SDK-shaped JSONL files into the per-cwd CLI dir and
    register a finalizer that removes them. Returns the project dir."""
    proj = _projects_dir_for(temp_root)
    sid_a = str(uuid.uuid4())
    sid_b = str(uuid.uuid4())
    _write_jsonl(proj / f"{sid_a}.jsonl", [
        {"type": "user", "uuid": "u1",
         "message": {"role": "user", "content": "tell me about FIRE planning"},
         "timestamp": "2026-05-19T10:00:00Z"},
        {"type": "assistant", "uuid": "a1",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "FIRE means financial independence retire early"},
         ]},
         "timestamp": "2026-05-19T10:00:05Z"},
    ])
    _write_jsonl(proj / f"{sid_b}.jsonl", [
        {"type": "user", "uuid": "u2",
         "message": {"role": "user", "content": "compile the cake recipe"},
         "timestamp": "2026-05-20T09:00:00Z"},
        # Tool-use blocks must NOT match (search ignores tool noise).
        {"type": "assistant", "uuid": "a2",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t1", "name": "FIRE_TOOL",
              "input": {"recipe": "FIRE marker should not match"}},
         ]},
         "timestamp": "2026-05-20T09:00:05Z"},
    ])

    def _cleanup():
        for p in proj.glob("*.jsonl"):
            p.unlink(missing_ok=True)
        try:
            proj.rmdir()
        except OSError:
            pass

    request.addfinalizer(_cleanup)
    return {"dir": proj, "sid_a": sid_a, "sid_b": sid_b}


def test_search_returns_matches_sorted_by_timestamp(client, auth, _staged_jsonls):
    r = client.get("/api/chat/search?q=fire", headers=auth)
    assert r.status_code == 200
    data = r.json()
    hits = data["hits"]
    # Two real text matches (u1, a1). tool_use block should be filtered out.
    assert len(hits) == 2
    assert {h["uuid"] for h in hits} == {"u1", "a1"}
    # Sorted by ts desc.
    assert hits[0]["ts"] >= hits[1]["ts"]
    # Snippet includes the matched substring (case-folded).
    for h in hits:
        assert "fire" in h["snippet"].lower()


def test_search_empty_query_returns_empty(client, auth):
    r = client.get("/api/chat/search?q=", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"hits": [], "total": 0}


def test_search_respects_limit(client, auth, _staged_jsonls):
    r = client.get("/api/chat/search?q=fire&limit=1", headers=auth)
    assert r.status_code == 200
    assert len(r.json()["hits"]) == 1


def test_search_requires_auth(client):
    r = client.get("/api/chat/search?q=anything")
    assert r.status_code in (401, 403)
