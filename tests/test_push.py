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

    def _fake_webpush(**kwargs):
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
    # Sub retained for the next attempt.
    assert any(s["endpoint"] == "https://flaky.example.com/x"
               for s in push_mod.list_subscriptions())
