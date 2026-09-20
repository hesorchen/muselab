"""Web Push subsystem: VAPID keypair gen/persist, subscribe/unsubscribe
endpoints, and graceful degradation when pywebpush is unavailable.

push.py resolves its on-disk paths (_VAPID_FILE / _SUBS_FILE) from ROOT at
module import; conftest reloads backend modules against temp_root, so each
test gets a clean <temp_root>/.muselab/ directory.
"""
import json

import pytest


@pytest.fixture()
def push_mod(app_module):
    """Freshly-reloaded backend.push with in-memory caches cleared so a
    prior test's keypair / subs don't leak across."""
    from backend import push as push_mod
    push_mod._vapid = None
    push_mod._subs = {}
    yield push_mod
    push_mod._vapid = None
    push_mod._subs = {}


# ====== VAPID key generation / persistence ======

def test_vapid_generated_and_persisted(push_mod, temp_root):
    """First call generates a P-256 keypair, writes vapid.json, and returns
    a urlsafe-base64 public key. The on-disk file holds the private PEM in
    SEC1 form (the format py_vapid accepts)."""
    pub = push_mod.get_vapid_public_key()
    assert isinstance(pub, str) and len(pub) > 50
    assert "=" not in pub   # base64 padding stripped

    vapid_file = temp_root / ".muselab" / "vapid.json"
    assert vapid_file.exists(), "vapid.json not persisted"
    data = json.loads(vapid_file.read_text(encoding="utf-8"))
    assert "private_pem" in data and "public_b64" in data
    assert "BEGIN EC PRIVATE KEY" in data["private_pem"], \
        "private key must be SEC1, not PKCS8 (py_vapid chokes on PKCS8 EC)"
    assert data["public_b64"] == pub


def test_vapid_stable_across_calls(push_mod):
    """Repeated calls return the SAME public key — regenerating would
    invalidate every existing browser subscription."""
    pub1 = push_mod.get_vapid_public_key()
    push_mod._vapid = None   # drop in-memory cache; force disk reload
    pub2 = push_mod.get_vapid_public_key()
    assert pub1 == pub2, "VAPID public key changed across calls — subs would break"


def test_vapid_reloaded_from_disk_not_regenerated(push_mod, temp_root):
    """A second process (simulated by clearing the in-memory cache) reads
    the persisted keypair instead of generating a new one."""
    pub1 = push_mod.get_vapid_public_key()
    mtime = (temp_root / ".muselab" / "vapid.json").stat().st_mtime
    push_mod._vapid = None
    pub2 = push_mod.get_vapid_public_key()
    assert pub1 == pub2
    # File untouched (no rewrite) on the reload path.
    assert (temp_root / ".muselab" / "vapid.json").stat().st_mtime == mtime


def test_pkcs8_vapid_migrated_to_sec1(push_mod, temp_root):
    """An old PKCS8 vapid.json is migrated in place to SEC1 on load, with
    the SAME public key (so subscriptions survive)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
    import base64

    key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    pkcs8_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    nums = key.public_key().public_numbers()
    raw_pub = b"\x04" + nums.x.to_bytes(32, "big") + nums.y.to_bytes(32, "big")
    pub_b64 = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode("ascii")

    vdir = temp_root / ".muselab"
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "vapid.json").write_text(
        json.dumps({"private_pem": pkcs8_pem, "public_b64": pub_b64}),
        encoding="utf-8")

    returned_pub = push_mod.get_vapid_public_key()
    assert returned_pub == pub_b64, "migration changed the public key"
    migrated = json.loads((vdir / "vapid.json").read_text(encoding="utf-8"))
    assert "BEGIN EC PRIVATE KEY" in migrated["private_pem"], \
        "PKCS8 not migrated to SEC1 on load"


# ====== subscribe / unsubscribe endpoints ======

def _sub_body(endpoint="https://push.example.com/abc"):
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": "BFakeP256dhKeyValue", "auth": "FakeAuthValue"},
    }


def test_vapid_public_endpoint(push_mod, client, auth):
    r = client.get("/api/push/vapid-public", headers=auth)
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["public_key"], str)


def test_subscribe_persists_and_unsubscribe_removes(push_mod, client, auth, temp_root):
    """POST /subscribe writes push_subs.json; /unsubscribe removes the entry."""
    r = client.post("/api/push/subscribe",
                    headers={**auth, "Content-Type": "application/json"},
                    json=_sub_body())
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    subs_file = temp_root / ".muselab" / "push_subs.json"
    assert subs_file.exists()
    saved = json.loads(subs_file.read_text(encoding="utf-8"))
    assert "https://push.example.com/abc" in saved
    assert push_mod.list_subscriptions(), "subscription not loaded back"

    r = client.post("/api/push/unsubscribe",
                    headers={**auth, "Content-Type": "application/json"},
                    json={"endpoint": "https://push.example.com/abc"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    saved = json.loads(subs_file.read_text(encoding="utf-8"))
    assert "https://push.example.com/abc" not in saved


def test_subscribe_rejects_missing_keys(push_mod, client, auth):
    """Pydantic schema rejects a body without the required keys block (422),
    so junk can't accumulate in push_subs.json."""
    r = client.post("/api/push/subscribe",
                    headers={**auth, "Content-Type": "application/json"},
                    json={"endpoint": "https://push.example.com/x"})
    assert r.status_code == 422, r.text


def test_subscribe_cap_enforced(push_mod, client, auth, monkeypatch):
    """Once _MAX_SUBS distinct endpoints exist, a NEW endpoint is rejected
    with 429 (prevents unbounded push_subs.json growth)."""
    from backend import api_push
    monkeypatch.setattr(api_push, "_MAX_SUBS", 2)
    for i in range(2):
        r = client.post("/api/push/subscribe",
                        headers={**auth, "Content-Type": "application/json"},
                        json=_sub_body(endpoint=f"https://push.example.com/{i}"))
        assert r.status_code == 200, r.text
    # Third distinct endpoint → over cap.
    r = client.post("/api/push/subscribe",
                    headers={**auth, "Content-Type": "application/json"},
                    json=_sub_body(endpoint="https://push.example.com/overflow"))
    assert r.status_code == 429, r.text
    # But re-subscribing an EXISTING endpoint is still allowed (idempotent).
    r = client.post("/api/push/subscribe",
                    headers={**auth, "Content-Type": "application/json"},
                    json=_sub_body(endpoint="https://push.example.com/0"))
    assert r.status_code == 200, r.text


def test_push_endpoints_require_auth(push_mod, client):
    """No token → 401/403, never 200. Push surface is auth-gated."""
    for method, path, body in [
        ("get", "/api/push/vapid-public", None),
        ("post", "/api/push/subscribe", _sub_body()),
        ("post", "/api/push/unsubscribe", {"endpoint": "https://x"}),
    ]:
        if method == "get":
            r = client.get(path)
        else:
            r = client.post(path, json=body)
        assert r.status_code in (401, 403), f"{path} not auth-gated: {r.status_code}"


# ====== graceful degradation ======

def test_send_to_all_no_subscriptions(push_mod):
    """send_to_all with zero subs returns a clean zero-result, no crash even
    though it imports pywebpush at call time."""
    push_mod._subs = {}
    push_mod._save_subs()
    res = push_mod.send_to_all(title="t", body="b")
    assert res == {"sent": 0, "dropped": 0, "errors": []}


def test_send_to_all_drops_dead_subscription(push_mod, temp_root):
    """A 410-Gone push response means the sub is dead → it's removed from
    the store and counted as dropped, not surfaced as an error."""
    push_mod.add_subscription(_sub_body(endpoint="https://dead.example.com/x"))

    import pywebpush

    class _FakeResp:
        status_code = 410

    class _FakeWebPushException(Exception):
        def __init__(self, msg, response=None):
            super().__init__(msg)
            self.response = response

    def _fake_webpush(**kwargs):
        raise _FakeWebPushException("gone", response=_FakeResp())

    # py_vapid.Vapid.from_pem must succeed on our generated key; it does,
    # but stub it too so the test doesn't depend on py_vapid internals.
    import py_vapid

    class _FakeVapid:
        @staticmethod
        def from_pem(pem):
            return object()

    import unittest.mock as mock
    with mock.patch.object(pywebpush, "webpush", _fake_webpush), \
         mock.patch.object(pywebpush, "WebPushException", _FakeWebPushException), \
         mock.patch.object(py_vapid, "Vapid", _FakeVapid):
        res = push_mod.send_to_all(title="t", body="b")

    assert res["sent"] == 0
    assert res["dropped"] == 1
    assert res["errors"] == []
    # Dead sub removed from disk too.
    subs_file = temp_root / ".muselab" / "push_subs.json"
    saved = json.loads(subs_file.read_text(encoding="utf-8"))
    assert "https://dead.example.com/x" not in saved


def test_send_to_all_records_non_fatal_error(push_mod):
    """A non-410 push failure is collected in `errors` and does NOT drop the
    sub — transient failures shouldn't lose the subscription."""
    push_mod.add_subscription(_sub_body(endpoint="https://flaky.example.com/x"))

    import unittest.mock as mock

    import py_vapid
    import pywebpush

    class _FakeResp:
        status_code = 500

    class _FakeWebPushException(Exception):
        def __init__(self, msg, response=None):
            super().__init__(msg)
            self.response = response

    seen = {}

    def _fake_webpush(**kwargs):
        seen.update(kwargs)
        raise _FakeWebPushException("server error", response=_FakeResp())

    class _FakeVapid:
        @staticmethod
        def from_pem(pem):
            return object()

    with mock.patch.object(pywebpush, "webpush", _fake_webpush), \
         mock.patch.object(pywebpush, "WebPushException", _FakeWebPushException), \
         mock.patch.object(py_vapid, "Vapid", _FakeVapid):
        res = push_mod.send_to_all(title="t", body="b")

    assert res["sent"] == 0
    assert res["dropped"] == 0
    assert len(res["errors"]) == 1
    assert "500" in res["errors"][0]
    assert seen["timeout"] == push_mod._PUSH_HTTP_TIMEOUT_S
    assert 0 < seen["timeout"] <= 15
    # Sub retained for the next attempt.
    assert any(s["endpoint"] == "https://flaky.example.com/x"
               for s in push_mod.list_subscriptions())


def test_send_to_all_redacts_endpoint_and_proxy_details(
        push_mod, capsys):
    """Push logs/results must not expose opaque subscription device tokens."""
    endpoint = "https://push.example.com/private-device-token"
    push_mod.add_subscription(_sub_body(endpoint=endpoint))

    import unittest.mock as mock

    import py_vapid
    import pywebpush

    class _FakeVapid:
        @staticmethod
        def from_pem(pem):
            return object()

    def _fake_webpush(**kwargs):
        raise ConnectionError(
            f"proxy failed while posting {endpoint}?auth=private-auth-token")

    with mock.patch.object(pywebpush, "webpush", _fake_webpush), \
         mock.patch.object(py_vapid, "Vapid", _FakeVapid):
        res = push_mod.send_to_all(
            title="t", body="b", context="privacy-regression")

    diagnostics = capsys.readouterr().err
    assert res["errors"] == ["ConnectionError"]
    assert endpoint not in diagnostics
    assert "private-auth-token" not in diagnostics
    assert "ConnectionError" in diagnostics


def test_vapid_failed_initial_write_never_publishes_unpersisted_key(push_mod, temp_root, monkeypatch):
    original_write = push_mod.atomic_write_text

    def fail_write(*args, **kwargs):
        raise OSError('simulated full disk')

    monkeypatch.setattr(push_mod, 'atomic_write_text', fail_write)
    for _ in range(2):
        with pytest.raises(OSError, match='simulated full disk'):
            push_mod.get_vapid_public_key()
    vapid_file = temp_root / '.muselab' / 'vapid.json'
    assert not vapid_file.exists()

    monkeypatch.setattr(push_mod, 'atomic_write_text', original_write)
    public_key = push_mod.get_vapid_public_key()
    assert vapid_file.exists()
    push_mod._vapid = None
    assert push_mod.get_vapid_public_key() == public_key


def test_vapid_failed_migration_retries_without_changing_key(push_mod, temp_root, monkeypatch):
    from cryptography.hazmat.primitives import serialization

    public_key = push_mod.get_vapid_public_key()
    vapid_file = temp_root / '.muselab' / 'vapid.json'
    data = json.loads(vapid_file.read_text(encoding='utf-8'))
    key = serialization.load_pem_private_key(data['private_pem'].encode('ascii'), password=None)
    data['private_pem'] = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode('ascii')
    original_bytes = json.dumps(data).encode('utf-8')
    vapid_file.write_bytes(original_bytes)
    push_mod._vapid = None
    original_write = push_mod.atomic_write_text

    def fail_write(*args, **kwargs):
        raise OSError('simulated full disk')

    monkeypatch.setattr(push_mod, 'atomic_write_text', fail_write)
    for _ in range(2):
        with pytest.raises(RuntimeError, match='Refusing to regenerate'):
            push_mod.get_vapid_public_key()
    assert vapid_file.read_bytes() == original_bytes

    monkeypatch.setattr(push_mod, 'atomic_write_text', original_write)
    assert push_mod.get_vapid_public_key() == public_key
    migrated = json.loads(vapid_file.read_text(encoding='utf-8'))
    assert 'BEGIN EC PRIVATE KEY' in migrated['private_pem']
    push_mod._vapid = None
    assert push_mod.get_vapid_public_key() == public_key


def test_vapid_concurrent_read_waits_for_first_persistence(push_mod, temp_root, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    import threading

    write_started = threading.Event()
    release_write = threading.Event()
    reader_started = threading.Event()
    original_write = push_mod.atomic_write_text

    def blocked_write(*args, **kwargs):
        write_started.set()
        assert release_write.wait(5)
        return original_write(*args, **kwargs)

    def read_key():
        reader_started.set()
        return push_mod.get_vapid_public_key()

    monkeypatch.setattr(push_mod, 'atomic_write_text', blocked_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(push_mod.get_vapid_public_key)
        try:
            assert write_started.wait(5)
            second = pool.submit(read_key)
            assert reader_started.wait(5)
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
        finally:
            release_write.set()
        public_key = first.result(timeout=5)
        assert second.result(timeout=5) == public_key
    push_mod._vapid = None
    assert push_mod.get_vapid_public_key() == public_key


@pytest.mark.parametrize("refresh", ["keys", "metadata"])
def test_stale_push_failure_does_not_remove_renewed_subscription(
    push_mod, monkeypatch, refresh,
):
    from types import SimpleNamespace
    import pywebpush

    old = _sub_body()
    push_mod.add_subscription_capped(old, 64, ua="initial-test-agent")
    renewed = _sub_body()
    if refresh == "keys":
        renewed["keys"] = {"p256dh": "RenewedSyntheticKey", "auth": "RenewedSyntheticAuth"}

    class Gone(Exception):
        response = SimpleNamespace(status_code=410)

    def delayed_failure(**kwargs):
        assert kwargs["subscription_info"]["keys"] == old["keys"]
        # Resubscription finishes while delivery of the old snapshot is in
        # flight; the old failure must not delete the just-saved record.
        push_mod.add_subscription_capped(renewed, 64, ua="renewed-test-agent")
        raise Gone("gone")

    monkeypatch.setattr(pywebpush, "webpush", delayed_failure)
    monkeypatch.setattr(pywebpush, "WebPushException", Gone)
    result = push_mod.send_to_all(title="synthetic", body="synthetic")
    saved = push_mod.list_subscriptions()
    assert len(saved) == 1
    assert saved[0]["keys"] == renewed["keys"]
    assert saved[0]["ua"] == "renewed-test-agent"
    assert result == {"sent": 0, "dropped": 0, "errors": []}
