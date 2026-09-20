"""Reject directory moves into their own tree before creating destinations."""
import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("destination", [
    "notes/moved",
    "notes/new/deep/moved",
    "notes-alias/new/moved",
])
def test_self_descendant_move_is_rejected_without_creating_directories(
    app_module, auth, temp_root, destination,
):
    (temp_root / "notes-alias").symlink_to(
        temp_root / "notes", target_is_directory=True,
    )
    source = temp_root / "notes"
    before = sorted(p.relative_to(source).as_posix() for p in source.rglob("*"))
    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/files/rename", headers=auth,
            json={"src": "notes", "dst": destination},
        )
    after = sorted(p.relative_to(source).as_posix() for p in source.rglob("*"))
    assert after == before
    assert response.status_code == 409
    assert (source / "a.md").read_text(encoding="utf-8") == "# A\nbody of a\n"


def test_directory_move_to_similarly_named_sibling_still_succeeds(
    client, auth, temp_root,
):
    response = client.post(
        "/api/files/rename", headers=auth,
        json={"src": "notes", "dst": "notes-archive/moved"},
    )
    assert response.status_code == 200
    assert not (temp_root / "notes").exists()
    assert (temp_root / "notes-archive/moved/a.md").read_text(
        encoding="utf-8",
    ) == "# A\nbody of a\n"
