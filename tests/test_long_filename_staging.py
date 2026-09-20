"""Valid workspace filenames must not overflow temporary staging names."""
import uuid

import pytest


@pytest.mark.parametrize("filename", ["a" * 247 + ".txt", "文" * 80 + ".txt"], ids=["ascii", "unicode"])
@pytest.mark.parametrize("operation", ["write", "copy", "upload", "staged-upload"])
def test_file_operations_support_long_valid_names(
    client, auth, temp_root, filename, operation,
):
    content = b"long filename content"
    target = temp_root / filename
    if operation == "write":
        target.write_text("previous content", encoding="utf-8")
        response = client.put(
            "/api/files/write", headers=auth,
            json={"path": filename, "content": content.decode("utf-8")},
        )
    elif operation == "copy":
        target.write_bytes(content)
        response = client.post(
            "/api/files/copy-bak", headers=auth, json={"src": filename},
        )
    else:
        upload_id = uuid.uuid4().hex if operation == "staged-upload" else ""
        response = client.post(
            "/api/files/upload", headers=auth,
            data={"path": "", "upload_id": upload_id},
            files={"file": (filename, content, "text/plain")},
        )
        assert response.status_code == 200
        if upload_id:
            assert not target.exists()
            response = client.post(
                "/api/files/upload/commit", headers=auth,
                json={"path": "", "upload_id": upload_id},
            )
    assert response.status_code == 200
    assert target.read_bytes() == content
    if operation == "copy":
        assert (temp_root / response.json()["path"]).read_bytes() == content
    assert not list(temp_root.glob(".~*.uploading"))
    assert not list(temp_root.glob(".~*.copying"))
    assert not list(temp_root.glob("*.tmp.*"))
