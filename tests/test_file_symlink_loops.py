"""An unusable symlink must not fail unrelated workspace file operations."""

import pytest


@pytest.mark.parametrize("indirect", [False, True])
def test_content_search_skips_symlink_cycles(client, auth, temp_root, indirect):
    (temp_root / "cycle.md").symlink_to("other.md" if indirect else "cycle.md")
    if indirect:
        (temp_root / "other.md").symlink_to("cycle.md")
    (temp_root / "valid.md").write_text("unique-search-probe\n", encoding="utf-8")

    response = client.get("/api/files/grep", params={"q": "unique-search-probe"}, headers=auth)
    assert response.status_code == 200
    assert [hit["path"] for hit in response.json()["hits"]] == ["valid.md"]
    assert response.json()["truncated"] is False


def test_read_symlink_cycle_returns_client_error(client, auth, temp_root):
    (temp_root / "cycle.md").symlink_to("cycle.md")

    response = client.get("/api/files/read", params={"path": "cycle.md"}, headers=auth)
    # Python versions differ on whether non-strict resolve rejects the cycle
    # itself or leaves a path that the subsequent existence check rejects.
    assert response.status_code in (400, 404)
    assert (temp_root / "cycle.md").is_symlink()
