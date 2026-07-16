"""File CRUD + search + hidden-toggle endpoints."""
import io


# ---- list / read ----

def test_list_root(client, auth):
    r = client.get("/api/files/list?path=", headers=auth)
    assert r.status_code == 200
    names = {e["name"] for e in r.json()["entries"]}
    assert "README.md" in names
    assert "notes" in names
    assert ".secret" not in names   # hidden by default


def test_list_show_hidden(client, auth):
    r = client.get("/api/files/list?path=&show_hidden=true", headers=auth)
    names = {e["name"] for e in r.json()["entries"]}
    assert ".secret" in names
    # .env is sensitive AND hidden — still listed (the block only fires on
    # read/write of its content; listing is allowed so the UI can show it).
    assert ".env" in names


def test_list_subdir(client, auth):
    r = client.get("/api/files/list?path=notes", headers=auth)
    names = {e["name"] for e in r.json()["entries"]}
    assert names == {"a.md", "b.txt", "deep"}


def test_read_markdown(client, auth):
    r = client.get("/api/files/read?path=README.md", headers=auth)
    assert r.status_code == 200
    assert "Hello" in r.text


def test_read_inline_disposition(client, auth):
    r = client.get("/api/files/read?path=README.md", headers=auth)
    assert r.headers["content-disposition"] == "inline"


def test_read_nonexistent(client, auth):
    r = client.get("/api/files/read?path=nope.md", headers=auth)
    assert r.status_code == 404


# ---- write / delete / mkdir / rename ----

def test_write_then_read(client, auth, temp_root):
    r = client.put(
        "/api/files/write",
        headers=auth,
        json={"path": "notes/new.md", "content": "fresh\n"},
    )
    assert r.status_code == 200
    assert (temp_root / "notes" / "new.md").read_text() == "fresh\n"


def test_mkdir(client, auth, temp_root):
    r = client.post(
        "/api/files/mkdir",
        headers=auth,
        json={"path": "fresh/sub"},
    )
    assert r.status_code == 200
    assert (temp_root / "fresh" / "sub").is_dir()


def test_rename(client, auth, temp_root):
    r = client.post(
        "/api/files/rename",
        headers=auth,
        json={"src": "notes/a.md", "dst": "notes/renamed.md"},
    )
    assert r.status_code == 200
    assert (temp_root / "notes" / "renamed.md").exists()
    assert not (temp_root / "notes" / "a.md").exists()


def test_delete_file(client, auth, temp_root):
    r = client.request(
        "DELETE",
        "/api/files/delete",
        headers=auth,
        json={"path": "notes/b.txt"},
    )
    assert r.status_code == 200
    assert not (temp_root / "notes" / "b.txt").exists()


def test_delete_nonempty_dir_moves_to_trash(client, auth, temp_root):
    # Trash semantics (2026-05-25): /delete on a non-empty dir is no longer
    # refused — it does a soft-delete by moving the whole subtree to
    # `<ROOT>/.muselab-dustbin/`. Only `permanent=true` actually rmtrees.
    assert (temp_root / "notes").exists()
    r = client.request(
        "DELETE",
        "/api/files/delete",
        headers=auth,
        json={"path": "notes"},
    )
    assert r.status_code == 200, r.text
    assert not (temp_root / "notes").exists()
    dustbin = temp_root / ".muselab-dustbin"
    assert dustbin.is_dir()
    assert any(dustbin.iterdir()), "trash dir should hold the moved subtree"


def test_delete_nonempty_dir_permanent_still_works(client, auth, temp_root):
    # Sanity: permanent=true bypasses trash entirely (used by the
    # trash-purge / empty-trash flows).
    assert (temp_root / "notes").exists()
    r = client.request(
        "DELETE",
        "/api/files/delete?permanent=true",
        headers=auth,
        json={"path": "notes"},
    )
    assert r.status_code == 200, r.text
    assert not (temp_root / "notes").exists()
    assert not (temp_root / ".muselab-dustbin" / "notes").exists()


def test_trash_purge_rejects_path_traversal_in_trash_id(client, auth, temp_root):
    # Without trash_id validation, `"../../tmp/x"` would resolve outside
    # the dustbin and trash_purge would happily rmtree() arbitrary dirs.
    # `_valid_trash_id` blocks any payload that isn't `\\d+_[0-9a-f]{8}$`.
    for evil in ["../../tmp/x", "../etc", "/etc/passwd", "abc.json", "x/y", ""]:
        r = client.request(
            "DELETE",
            "/api/files/trash/purge",
            headers=auth,
            json={"trash_id": evil},
        )
        assert r.status_code == 400, f"bad trash_id should 400: {evil!r}, got {r.status_code}"


def test_trash_restore_rejects_path_traversal_in_trash_id(client, auth):
    for evil in ["../../tmp/x", "../etc", "/etc/passwd", "abc.json", "x/y", ""]:
        r = client.post(
            "/api/files/trash/restore",
            headers=auth,
            json={"trash_id": evil},
        )
        assert r.status_code == 400, f"bad trash_id should 400: {evil!r}, got {r.status_code}"


def test_trash_purge_accepts_valid_trash_id_format(client, auth):
    # Well-formed id that doesn't exist → 404 (not 400). Confirms the
    # validator allows real ids through.
    r = client.request(
        "DELETE",
        "/api/files/trash/purge",
        headers=auth,
        json={"trash_id": "1234567890_deadbeef"},
    )
    assert r.status_code == 404


def test_env_int_handles_bad_input(monkeypatch):
    # Module-level `int(os.environ.get(...))` patterns crash backend
    # startup on typos. env_int must fall back to the default + log
    # instead. Verified against MUSELAB_TRASH_TTL_DAYS as a stand-in
    # for the 5+ call sites that share the helper.
    from backend.settings import env_int
    monkeypatch.setenv("MUSELAB_TRASH_TTL_DAYS", "abc")
    assert env_int("MUSELAB_TRASH_TTL_DAYS", 30, min_value=0) == 30
    monkeypatch.setenv("MUSELAB_TRASH_TTL_DAYS", "30 days")
    assert env_int("MUSELAB_TRASH_TTL_DAYS", 30, min_value=0) == 30
    monkeypatch.setenv("MUSELAB_TRASH_TTL_DAYS", "")
    assert env_int("MUSELAB_TRASH_TTL_DAYS", 30, min_value=0) == 30
    monkeypatch.delenv("MUSELAB_TRASH_TTL_DAYS", raising=False)
    assert env_int("MUSELAB_TRASH_TTL_DAYS", 30, min_value=0) == 30
    # Valid values pass through.
    monkeypatch.setenv("MUSELAB_TRASH_TTL_DAYS", "7")
    assert env_int("MUSELAB_TRASH_TTL_DAYS", 30, min_value=0) == 7
    monkeypatch.setenv("MUSELAB_TRASH_TTL_DAYS", "0")
    assert env_int("MUSELAB_TRASH_TTL_DAYS", 30, min_value=0) == 0
    # Negatives → clamped to min_value (disabled, in TTL semantics).
    monkeypatch.setenv("MUSELAB_TRASH_TTL_DAYS", "-5")
    assert env_int("MUSELAB_TRASH_TTL_DAYS", 30, min_value=0) == 0


def test_env_float_handles_bad_input(monkeypatch):
    from backend.settings import env_float
    monkeypatch.setenv("MUSELAB_BUDGET_USD", "abc")
    assert env_float("MUSELAB_BUDGET_USD", 0.0) == 0.0
    monkeypatch.setenv("MUSELAB_BUDGET_USD", "20.5")
    assert env_float("MUSELAB_BUDGET_USD", 0.0) == 20.5
    monkeypatch.setenv("MUSELAB_BUDGET_USD", "")
    assert env_float("MUSELAB_BUDGET_USD", 7.5) == 7.5


def test_trash_list_returns_total_size_and_ttl(client, auth, temp_root):
    # After dropping one file into trash, list endpoint should report
    # both total_size and ttl_days top-level — used by the frontend to
    # render "Trash · 142 KB · auto-purged after 30 days".
    # Seed: create + soft-delete a file.
    (temp_root / "notes" / "trash_me.md").write_text(
        "x" * 1234, encoding="utf-8")
    r = client.request("DELETE", "/api/files/delete", headers=auth,
                       json={"path": "notes/trash_me.md"})
    assert r.status_code == 200
    r = client.get("/api/files/trash/list", headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert "items" in data and "total_size" in data and "ttl_days" in data
    assert data["total_size"] >= 1234, data
    assert isinstance(data["ttl_days"], int)


def test_upload(client, auth, temp_root):
    r = client.post(
        "/api/files/upload",
        headers=auth,
        data={"path": "notes"},
        files={"file": ("up.md", io.BytesIO(b"uploaded body"), "text/markdown")},
    )
    assert r.status_code == 200
    assert (temp_root / "notes" / "up.md").read_bytes() == b"uploaded body"


# ---- search / grep ----

def test_search_by_filename(client, auth):
    r = client.get("/api/files/search?q=read", headers=auth)
    names = [e["name"] for e in r.json()["entries"]]
    assert "README.md" in names


def test_grep_content(client, auth):
    r = client.get("/api/files/grep?q=first paragraph", headers=auth)
    hits = r.json()["hits"]
    assert any(h["path"] == "README.md" for h in hits)


def test_grep_skips_hidden_by_default(client, auth, temp_root):
    (temp_root / ".secret").write_text("UNIQUE_GREP_TOKEN_xyz\n")
    r = client.get("/api/files/grep?q=UNIQUE_GREP_TOKEN_xyz", headers=auth)
    assert r.json()["hits"] == []


# ---- raw / download ----

def test_raw_image_inline(client, temp_root):
    from .conftest import TEST_TOKEN
    (temp_root / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    r = client.get(f"/api/files/raw?path=x.png&token={TEST_TOKEN}")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("inline")


# ---- new endpoints / edge cases ----

def test_rename_endpoint(client, auth, temp_root):
    r = client.post("/api/files/rename", headers=auth,
                    json={"src": "README.md", "dst": "RENAMED.md"})
    assert r.status_code == 200
    assert (temp_root / "RENAMED.md").exists()
    assert not (temp_root / "README.md").exists()


def test_rename_to_existing_refused(client, auth):
    r = client.post("/api/files/rename", headers=auth,
                    json={"src": "README.md", "dst": "notes/a.md"})
    assert r.status_code == 409


# ---- copy-bak ----

def test_copy_bak_basic(client, auth, temp_root):
    r = client.post("/api/files/copy-bak", headers=auth,
                    json={"src": "README.md"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "README.md.bak"
    assert (temp_root / "README.md.bak").exists()
    # Original untouched.
    assert (temp_root / "README.md").exists()
    # Content copied verbatim.
    assert (temp_root / "README.md.bak").read_text() == \
           (temp_root / "README.md").read_text()


def test_copy_bak_increments_on_conflict(client, auth, temp_root):
    # Three rounds: .bak, .bak.2, .bak.3
    for expected in ["README.md.bak", "README.md.bak.2", "README.md.bak.3"]:
        r = client.post("/api/files/copy-bak", headers=auth,
                        json={"src": "README.md"})
        assert r.status_code == 200, r.text
        assert r.json()["path"] == expected
        assert (temp_root / expected).exists()


def test_copy_bak_cross_dir(client, auth, temp_root):
    r = client.post("/api/files/copy-bak", headers=auth,
                    json={"src": "README.md", "dst_dir": "notes"})
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "notes/README.md.bak"
    assert (temp_root / "notes" / "README.md.bak").exists()


def test_copy_bak_refuses_directory(client, auth):
    r = client.post("/api/files/copy-bak", headers=auth,
                    json={"src": "notes"})
    assert r.status_code == 400


def test_copy_bak_missing_source(client, auth):
    r = client.post("/api/files/copy-bak", headers=auth,
                    json={"src": "no-such-file.md"})
    assert r.status_code == 404


def test_copy_bak_missing_dst_dir(client, auth):
    r = client.post("/api/files/copy-bak", headers=auth,
                    json={"src": "README.md", "dst_dir": "no/such/dir"})
    assert r.status_code == 404


def test_search_with_show_hidden(client, auth):
    """show_hidden lets search/grep see .* files."""
    r = client.get("/api/files/grep?q=hidden&show_hidden=true", headers=auth)
    hits = r.json()["hits"]
    assert any(".secret" in h["path"] for h in hits)


def test_mkdir_nested(client, auth, temp_root):
    r = client.post("/api/files/mkdir", headers=auth,
                    json={"path": "a/b/c/d"})
    assert r.status_code == 200
    assert (temp_root / "a" / "b" / "c" / "d").is_dir()


def test_list_truncated_flag(client, auth, temp_root):
    """When list_dir hits MAX_LIST_ENTRIES, truncated=true."""
    big = temp_root / "big"
    big.mkdir()
    for i in range(550):
        (big / f"f{i:04d}.txt").write_text("x")
    r = client.get("/api/files/list?path=big", headers=auth)
    d = r.json()
    assert d["truncated"] is True
    assert len(d["entries"]) == 500   # MAX_LIST_ENTRIES


def test_no_extension_text_file(client, auth, temp_root):
    """Files like Dockerfile / Makefile (no ext) should be readable."""
    (temp_root / "Dockerfile").write_text("FROM python:3.12\n")
    r = client.get("/api/files/read?path=Dockerfile", headers=auth)
    assert r.status_code == 200
    assert "FROM python" in r.text


def test_unknown_extension_allowed_when_text(client, auth, temp_root):
    """Unknown extensions (.weird, .tmpl, .j2 …) preview as text if the content
    is actually text. Whitelist→blacklist+sniff inversion."""
    (temp_root / "x.weird").write_text("hi")
    r = client.get("/api/files/read?path=x.weird", headers=auth)
    assert r.status_code == 200
    assert r.text == "hi"


def test_known_binary_extension_rejected(client, auth, temp_root):
    """Fast-path 415 for known-binary extensions before reading content."""
    (temp_root / "blob.zip").write_bytes(b"PK\x03\x04not really a zip")
    r = client.get("/api/files/read?path=blob.zip", headers=auth)
    assert r.status_code == 415


def test_binary_content_rejected_by_sniff(client, auth, temp_root):
    """File with no known-binary extension but NUL bytes in content → 415."""
    (temp_root / "weird.dat").write_bytes(b"some\x00binary\x00data")
    r = client.get("/api/files/read?path=weird.dat", headers=auth)
    assert r.status_code == 415


def test_tmpl_file_previewable(client, auth, temp_root):
    """Regression: .tmpl was blocked by the old whitelist; should preview now."""
    (temp_root / "muselab.service.tmpl").write_text(
        "[Unit]\nDescription={{NAME}}\n")
    r = client.get("/api/files/read?path=muselab.service.tmpl", headers=auth)
    assert r.status_code == 200
    assert "{{NAME}}" in r.text


def test_empty_file_previewable(client, auth, temp_root):
    """Empty file is text by definition."""
    (temp_root / "empty.foo").write_text("")
    r = client.get("/api/files/read?path=empty.foo", headers=auth)
    assert r.status_code == 200
    assert r.text == ""


def test_chinese_markdown_at_sniff_boundary_previewable(client, auth, temp_root):
    """Regression: CJK markdown files exceeding the 4 KB sniff window were
    wrongly tagged binary because the chunk boundary split a 3-byte UTF-8
    char and plain decode() raised UnicodeDecodeError, then the fallback
    high-bit ratio test rejected as binary (CJK is ~100% high-bit)."""
    chinese = "今天天气真好，我去公园散步。" * 400   # ~10 KB of pure CJK
    (temp_root / "cn.md").write_text(chinese, encoding="utf-8")
    r = client.get("/api/files/read?path=cn.md", headers=auth)
    assert r.status_code == 200
    assert "今天天气真好" in r.text


# ---- csv preview ----

def test_csv_preview_basic(client, auth, temp_root):
    """Comma-separated file: sniffed header + paginated rows."""
    (temp_root / "small.csv").write_text(
        "name,age,city\nAlice,30,Beijing\nBob,25,Shanghai\nCarol,40,Guangzhou\n",
        encoding="utf-8",
    )
    r = client.get("/api/files/csv?path=small.csv", headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert data["has_header"]
    assert data["header"] == ["name", "age", "city"]
    assert data["rows"][0] == ["Alice", "30", "Beijing"]
    assert data["total_rows"] == 3
    assert data["delimiter"] == ","


def test_csv_preview_tsv(client, auth, temp_root):
    """Tab-separated — Sniffer can mis-guess on tabby files; .tsv extension
    forces the correct dialect."""
    (temp_root / "t.tsv").write_text("a\tb\nv1\tv2\n", encoding="utf-8")
    r = client.get("/api/files/csv?path=t.tsv", headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert data["delimiter"] == "\t"
    assert data["rows"][0] == ["v1", "v2"]


def test_csv_preview_pagination(client, auth, temp_root):
    """Page-window slicing: offset skips data rows; limit caps page size.

    Uses an obvious numeric/string header column so csv.Sniffer reliably
    detects has_header=True; the pagination math is then unambiguous
    (offset N means skip N data rows after the header)."""
    lines = ["id,name\n"] + [f"{i},row-{i}\n" for i in range(50)]
    (temp_root / "big.csv").write_text("".join(lines), encoding="utf-8")
    r = client.get("/api/files/csv?path=big.csv&offset=10&limit=5", headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert data["offset"] == 10
    assert data["limit"] == 5
    assert len(data["rows"]) == 5
    # Sniffer may or may not flag the header on small samples — accept both
    # branches: with-header skips header before counting, without-header
    # treats line 0 as data so offset=10 lands one row earlier.
    first_id = int(data["rows"][0][0])
    assert first_id in (9, 10)
    assert int(data["rows"][4][0]) == first_id + 4
    assert data["total_rows"] in (50, 51)


def test_csv_preview_rejects_non_csv(client, auth):
    """README.md is text but not CSV — endpoint rejects with 415."""
    r = client.get("/api/files/csv?path=README.md", headers=auth)
    assert r.status_code == 415


def test_csv_total_cache_reuses_signature_and_invalidates_on_write(
        client, auth, temp_root):
    """Pagination caches only the row count and refreshes it after a write."""
    from backend import files as files_module

    path = temp_root / "cached.csv"
    path.write_text(
        "id,name\n" + "".join(f"{i},row-{i}\n" for i in range(40)),
        encoding="utf-8",
    )
    with files_module._CSV_TOTAL_CACHE_LOCK:
        files_module._CSV_TOTAL_CACHE.clear()

    first = client.get(
        "/api/files/csv?path=cached.csv&offset=0&limit=3", headers=auth)
    assert first.status_code == 200
    first_total = first.json()["total_rows"]
    cache_key = str(path.resolve())
    with files_module._CSV_TOTAL_CACHE_LOCK:
        cached = files_module._CSV_TOTAL_CACHE[cache_key]
    assert cached[2] == first_total

    second = client.get(
        "/api/files/csv?path=cached.csv&offset=10&limit=3", headers=auth)
    assert second.status_code == 200
    assert second.json()["total_rows"] == first_total
    with files_module._CSV_TOTAL_CACHE_LOCK:
        assert files_module._CSV_TOTAL_CACHE[cache_key] == cached

    with path.open("a", encoding="utf-8") as handle:
        handle.write("40,row-40\n")
    changed = client.get(
        "/api/files/csv?path=cached.csv&offset=0&limit=3", headers=auth)
    assert changed.status_code == 200
    assert changed.json()["total_rows"] == first_total + 1
    with files_module._CSV_TOTAL_CACHE_LOCK:
        assert files_module._CSV_TOTAL_CACHE[cache_key][1] > cached[1]
