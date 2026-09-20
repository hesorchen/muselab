"""Subscription cache follows durable storage, including an absent file."""
import json

import pytest


@pytest.fixture()
def push_mod(app_module, monkeypatch):
    from backend import push
    monkeypatch.setattr(push, '_subs', {})
    return push


def _sub_body(endpoint):
    return {'endpoint': endpoint, 'keys': {'p256dh': 'test-public-key', 'auth': 'test-auth-value'}}


def test_removed_subscription_store_does_not_restore_stale_devices(push_mod, temp_root):
    old = _sub_body('https://push.example.com/old-device')
    new = _sub_body('https://push.example.com/new-device')
    push_mod.add_subscription(old)
    subs_file = temp_root / '.muselab' / 'push_subs.json'
    subs_file.unlink()
    assert push_mod.list_subscriptions() == []

    push_mod.add_subscription(new)
    saved = json.loads(subs_file.read_text(encoding='utf-8'))
    assert set(saved) == {new['endpoint']}
    assert [sub['endpoint'] for sub in push_mod.list_subscriptions()] == [new['endpoint']]


def test_failed_first_subscription_does_not_consume_capacity(push_mod, temp_root, monkeypatch):
    original_write = push_mod.atomic_write_text

    def fail_write(*args, **kwargs):
        raise OSError('simulated full disk')

    monkeypatch.setattr(push_mod, 'atomic_write_text', fail_write)
    with pytest.raises(OSError, match='simulated full disk'):
        push_mod.add_subscription_capped(_sub_body('https://push.example.com/failed-device'), 1)
    monkeypatch.setattr(push_mod, 'atomic_write_text', original_write)

    new = _sub_body('https://push.example.com/saved-device')
    push_mod.add_subscription_capped(new, 1)
    saved = json.loads((temp_root / '.muselab' / 'push_subs.json').read_text(encoding='utf-8'))
    assert set(saved) == {new['endpoint']}
