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


def test_search_keeps_latest_matches_per_session(client, auth, _staged_jsonls):
    staged = _staged_jsonls
    _write_jsonl(staged['dir'] / f"{staged['sid_a']}.jsonl", [
        {"type": "user", "uuid": f"newest-{i}",
         "message": {"content": "unique-recency-probe"},
         "timestamp": f"2026-09-06T00:00:{i:02d}Z"}
        for i in (8, 2, 3, 4, 5, 6, 7, 1)
    ])
    response = client.get('/api/chat/search?q=unique-recency-probe', headers=auth)
    assert response.status_code == 200
    assert [hit['uuid'] for hit in response.json()['hits']] == [
        'newest-8', 'newest-7', 'newest-6', 'newest-5', 'newest-4']


@pytest.mark.parametrize('text,query', [
    ('测试搜索', '测试'), ('quote "example"', '"example"'),
    ('path C:\\notes', 'C:\\notes'), ('first\nsecond', 'first\nsecond'),
])
def test_search_decodes_json_escapes(client, auth, _staged_jsonls, text, query):
    staged = _staged_jsonls
    path = staged['dir'] / f"{staged['sid_a']}.jsonl"
    path.write_text(json.dumps({
        'type': 'user', 'uuid': 'escaped-match',
        'message': {'content': text}, 'timestamp': '2026-09-06T00:00:00Z',
    }, ensure_ascii=True) + '\n', encoding='utf-8')
    response = client.get('/api/chat/search', params={'q': query}, headers=auth)
    assert response.status_code == 200
    assert [hit['uuid'] for hit in response.json()['hits']] == ['escaped-match']


def test_incremental_search_append_rewrite_delete_and_warm_reads(tmp_path):
    import threading
    from backend.chat_search import SearchIndex
    from backend.chat import _extract_searchable_text, _strip_cli_slash_wrapper, _make_snippet

    path = tmp_path / 'canonical.jsonl'
    index = SearchIndex(tmp_path / 'private' / 'search.sqlite3')

    def entry(text, uid):
        return {'type': 'assistant', 'uuid': uid, 'message': {'content': text},
                'timestamp': '2026-09-11T00:00:00Z'}

    def search(query, paths=None):
        metrics = dict(read_bytes=0, parsed_lines=0, updated_files=0)
        result = index.search([path] if paths is None else paths, query, 20, {},
                              threading.Event(), _extract_searchable_text,
                              _strip_cli_slash_wrapper, _make_snippet, metrics)
        return [hit['uuid'] for hit in result['hits']], metrics

    _write_jsonl(path, [entry('完整搜索 first body', 'first')])
    assert search('完整搜索')[0] == ['first']
    assert search('first')[1]['read_bytes'] == 0
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(entry('完整搜索 appended', 'second')) + '\n')
    hits, metrics = search('完整搜索')
    assert set(hits) == {'first', 'second'}
    assert metrics['parsed_lines'] == 1
    _write_jsonl(path, [entry('replacement different content', 'replaced')])
    assert search('完整搜索')[0] == []
    assert search('replacement')[0] == ['replaced']
    path.unlink()
    assert search('replacement', [])[0] == []


@pytest.mark.parametrize('query', ['测', '测试', '测试搜索', '"quoted"', 'line\nbreak', 'CAFÉ', '%_'])
def test_index_preserves_exact_substring_semantics(tmp_path, query):
    import threading
    from backend.chat_search import SearchIndex
    from backend.chat import _extract_searchable_text, _strip_cli_slash_wrapper, _make_snippet
    path = tmp_path / 'source.jsonl'
    _write_jsonl(path, [{'type': 'user', 'uuid': 'hit', 'message': {'content':
                         '测试搜索 "quoted" line\nbreak café %_'}}])
    result = SearchIndex(tmp_path / 'private' / 'search.sqlite3').search(
        [path], query, 20, {}, threading.Event(), _extract_searchable_text,
        _strip_cli_slash_wrapper, _make_snippet,
        dict(read_bytes=0, parsed_lines=0, updated_files=0))
    assert [row['uuid'] for row in result['hits']] == ['hit']


def test_index_partial_tail_and_cancelled_write_are_recoverable(tmp_path):
    import threading
    from backend.chat_search import SearchIndex, SearchCancelled
    from backend.chat import _extract_searchable_text, _strip_cli_slash_wrapper, _make_snippet
    path = tmp_path / 'source.jsonl'
    value = json.dumps({'type': 'user', 'uuid': 'full', 'message': {'content': 'tail probe'}})
    path.write_text(value[:30], encoding='utf-8')
    index = SearchIndex(tmp_path / 'private' / 'search.sqlite3')
    cancel = threading.Event()
    def search(extract=_extract_searchable_text):
        return index.search([path], 'probe', 20, {}, cancel, extract,
                            _strip_cli_slash_wrapper, _make_snippet,
                            dict(read_bytes=0, parsed_lines=0, updated_files=0))
    assert not search()['hits']
    path.write_text(value, encoding='utf-8')  # valid final line without newline
    def cancelled_extract(content):
        cancel.set()
        return _extract_searchable_text(content)
    with pytest.raises((SearchCancelled, __import__('sqlite3').OperationalError)):
        search(cancelled_extract)
    cancel.clear()
    assert search()['hits'][0]['uuid'] == 'full'
    with path.open('a', encoding='utf-8') as handle:
        handle.write('\n' + value.replace('full', 'next') + '\n')
    assert {row['uuid'] for row in search()['hits']} == {'full', 'next'}


@pytest.mark.asyncio
async def test_disconnected_search_stops_worker_and_releases_next_query():
    import asyncio
    import threading
    from backend import chat_search
    entered, stopped = threading.Event(), threading.Event()
    class Request:
        async def is_disconnected(self):
            return entered.is_set()
    def slow(cancel, metrics):
        entered.set()
        try:
            assert cancel.wait(2)
            raise chat_search.SearchCancelled
        finally:
            stopped.set()
    try:
        with pytest.raises(chat_search.SearchCancelled):
            await chat_search.run_search(Request(), slow)
        assert await asyncio.to_thread(stopped.wait, 1)
        result = await chat_search.run_search(Request(), lambda cancel, metrics: {'hits': []})
        assert result == {'hits': []}
    finally:
        chat_search.shutdown()


def test_search_remains_complete_without_optional_trigram(tmp_path, monkeypatch):
    import sqlite3
    import threading
    from backend import chat_search
    from backend.chat import _extract_searchable_text, _strip_cli_slash_wrapper, _make_snippet
    connect = sqlite3.connect
    class NoTrigram(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith('CREATE VIRTUAL TABLE'):
                raise sqlite3.OperationalError('no such tokenizer: trigram')
            return super().execute(sql, *args, **kwargs)
    monkeypatch.setattr(chat_search.sqlite3, 'connect',
                        lambda *args, **kwargs: connect(*args, factory=NoTrigram, **kwargs))
    path = tmp_path / 'source.jsonl'
    _write_jsonl(path, [{'type': 'user', 'uuid': 'complete', 'message': {'content': '中文完整搜索'}}])
    metrics = dict(read_bytes=0, parsed_lines=0, updated_files=0)
    result = chat_search.SearchIndex(tmp_path / 'private' / 'search.sqlite3').search(
        [path], '完整搜索', 10, {}, threading.Event(), _extract_searchable_text,
        _strip_cli_slash_wrapper, _make_snippet, metrics)
    assert [row['uuid'] for row in result['hits']] == ['complete']
    assert metrics['fts'] is False


def test_search_snippet_uses_original_text_offsets_after_case_expansion(client, auth, _staged_jsonls):
    staged = _staged_jsonls
    _write_jsonl(staged['dir'] / f"{staged['sid_a']}.jsonl", [{
        'type': 'user', 'uuid': 'unicode-offset',
        'message': {'content': 'İ' * 100 + ' find-this-marker ' + 'suffix ' * 30},
        'timestamp': '2026-09-01T00:00:00Z',
    }])

    response = client.get('/api/chat/search', params={'q': 'FIND-THIS-MARKER'}, headers=auth)
    assert response.status_code == 200
    hits = response.json()['hits']
    assert [hit['uuid'] for hit in hits] == ['unicode-offset']
    assert 'find-this-marker' in hits[0]['snippet']
    assert len(hits[0]['snippet']) <= 200


@pytest.mark.parametrize('source_uuid', ['user-steering-uuid', ''])
def test_search_finds_native_steering_without_task_notifications(
    client, auth, _staged_jsonls, source_uuid,
):
    staged = _staged_jsonls
    _write_jsonl(staged['dir'] / f"{staged['sid_a']}.jsonl", [
        {'type': 'attachment', 'uuid': 'steering-record',
         'timestamp': '2026-09-21T00:00:00Z',
         'attachment': {'type': 'queued_command', 'commandMode': 'prompt',
                        'source_uuid': source_uuid,
                        'prompt': 'steering-search-probe change the output format'}},
        {'type': 'attachment', 'uuid': 'task-record',
         'attachment': {'type': 'queued_command', 'commandMode': 'task-notification',
                        'prompt': 'steering-search-probe internal task finished'}},
        {'type': 'attachment', 'uuid': 'task-xml-record',
         'attachment': {'type': 'queued_command',
                        'prompt': '<task-notification>steering-search-probe</task-notification>'}},
    ])
    for _ in range(2):
        response = client.get('/api/chat/search', params={'q': 'steering-search-probe'}, headers=auth)
        assert response.status_code == 200
        hits = response.json()['hits']
        assert len(hits) == 1
        assert hits[0]['uuid'] == (source_uuid or 'steering-record')
        assert hits[0]['role'] == 'user'
        assert 'change the output format' in hits[0]['snippet']


def test_search_rebuilds_legacy_cache_that_omitted_steering(tmp_path):
    import sqlite3
    import threading
    from backend.chat_search import SearchIndex
    from backend.chat import _extract_searchable_text, _strip_cli_slash_wrapper, _make_snippet

    path = tmp_path / 'canonical.jsonl'
    _write_jsonl(path, [{
        'type': 'attachment', 'uuid': 'record',
        'attachment': {'type': 'queued_command', 'commandMode': 'prompt',
                       'source_uuid': 'user-id', 'prompt': 'legacy-steering-marker'},
    }])
    canonical_bytes = path.read_bytes()
    index = SearchIndex(tmp_path / 'private' / 'search.sqlite3')

    def search():
        metrics = dict(read_bytes=0, parsed_lines=0, updated_files=0)
        result = index.search([path], 'legacy-steering-marker', 20, {}, threading.Event(),
                              _extract_searchable_text, _strip_cli_slash_wrapper,
                              _make_snippet, metrics)
        return result, metrics

    search()
    # Old caches checkpointed the unchanged file without indexing its attachment.
    with sqlite3.connect(index.path) as db:
        db.execute('DELETE FROM documents')
        db.execute('PRAGMA user_version=0')
    result, metrics = search()
    assert [hit['uuid'] for hit in result['hits']] == ['user-id']
    assert metrics['read_bytes'] > 0
    assert search()[1]['read_bytes'] == 0
    assert path.read_bytes() == canonical_bytes
