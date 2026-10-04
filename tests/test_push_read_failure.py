"""A failed subscription reload must never overwrite durable device entries."""

import json
from pathlib import Path

import pytest

from tests.test_push_subscription_store import _sub_body


@pytest.fixture()
def push_mod(app_module, monkeypatch):
    from backend import push
    monkeypatch.setattr(push, "_subs", {})
    return push


@pytest.mark.parametrize("operation", ["subscribe", "unsubscribe"])
@pytest.mark.parametrize("failure", ["io", "invalid_json", "wrong_shape"])
def test_subscription_reload_failure_preserves_disk_and_recovers(
    push_mod, client, auth, temp_root, monkeypatch, operation, failure,
):
    old = _sub_body("https://push.example.com/cached-device")
    other = _sub_body("https://push.example.com/new-on-disk")
    incoming = _sub_body("https://push.example.com/incoming-device")
    push_mod.add_subscription(old)
    path = temp_root / ".muselab" / "push_subs.json"
    durable = {old["endpoint"]: old, other["endpoint"]: other}
    healthy_bytes = json.dumps(durable).encode("utf-8")
    path.write_bytes(healthy_bytes)
    if failure == "invalid_json":
        path.write_bytes(b'{"incomplete":')
    elif failure == "wrong_shape":
        path.write_bytes(b'[]')
    before = path.read_bytes()
    real_read = Path.read_text
    reads = []

    def failed_read(target, *args, **kwargs):
        if target == path:
            reads.append(target)
            if failure == "io":
                raise OSError("synthetic read failure")
        return real_read(target, *args, **kwargs)

    body = incoming if operation == "subscribe" else {"endpoint": old["endpoint"]}
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", failed_read)
        response = client.post(f"/api/push/{operation}", headers=auth, json=body)
    assert reads
    assert path.read_bytes() == before, "unreadable durable subscriptions must stay intact"
    assert response.status_code == 503
    assert response.json() == {"detail": "push subscription storage unavailable"}
    assert push_mod._subs == {old["endpoint"]: old}

    # Once storage is readable again, the actual endpoint reloads all devices.
    path.write_bytes(healthy_bytes)
    response = client.post(f"/api/push/{operation}", headers=auth, json=body)
    assert response.status_code == 200
    saved = json.loads(path.read_text(encoding="utf-8"))
    expected = set(durable)
    if operation == "subscribe":
        expected.add(incoming["endpoint"])
    else:
        expected.remove(old["endpoint"])
    assert set(saved) == expected
    assert saved[other["endpoint"]] == other


def test_manual_push_does_not_send_from_unreadable_snapshot(push_mod, client, auth, temp_root, monkeypatch):
    import pywebpush

    old = _sub_body("https://push.example.com/cached-device")
    push_mod.add_subscription(old)
    path = temp_root / ".muselab" / "push_subs.json"
    before = path.read_bytes()
    sent = []
    monkeypatch.setattr(pywebpush, "webpush", lambda **kwargs: sent.append(kwargs["subscription_info"]))
    real_read = Path.read_text

    def failed_read(target, *args, **kwargs):
        if target == path:
            raise OSError("synthetic read failure")
        return real_read(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", failed_read)
        response = client.post("/api/push/test", headers=auth)
    assert sent == [], "an unreadable snapshot must not send to cached devices"
    assert response.status_code == 503
    assert response.json() == {"detail": "push subscription storage unavailable"}
    assert path.read_bytes() == before
    response = client.post("/api/push/test", headers=auth)
    assert response.status_code == 200
    assert response.json() == {"sent": 1, "dropped": 0, "errors": []}
    assert sent == [old]
