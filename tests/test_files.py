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


def test_delete_nonempty_dir_refused(client, auth):
    r = client.request(
        "DELETE",
        "/api/files/delete",
        headers=auth,
        json={"path": "notes"},
    )
    assert r.status_code == 400


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


def test_random_extension_blocked(client, auth, temp_root):
    """Binary-ish unknown extensions returned 415 by /read."""
    (temp_root / "x.weird").write_text("hi")
    r = client.get("/api/files/read?path=x.weird", headers=auth)
    assert r.status_code == 415
