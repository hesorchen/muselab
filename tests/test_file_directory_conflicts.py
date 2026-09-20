"""Ordinary file/directory name collisions are recoverable client conflicts."""

import pytest


@pytest.mark.parametrize('path', ['README.md', 'README.md/child'])
def test_mkdir_conflicting_file_returns_conflict(client, auth, temp_root, path):
    original = (temp_root / 'README.md').read_bytes()
    response = client.post('/api/files/mkdir', headers=auth, json={'path': path})
    assert response.status_code == 409
    assert (temp_root / 'README.md').read_bytes() == original


def test_rename_into_file_parent_returns_conflict(client, auth, temp_root):
    original = (temp_root / 'README.md').read_bytes()
    source = (temp_root / 'notes' / 'a.md').read_bytes()
    response = client.post('/api/files/rename', headers=auth,
                           json={'src': 'notes/a.md', 'dst': 'README.md/renamed.md'})
    assert response.status_code == 409
    assert (temp_root / 'README.md').read_bytes() == original
    assert (temp_root / 'notes' / 'a.md').read_bytes() == source
