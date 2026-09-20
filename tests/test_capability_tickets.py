"""Expiry must free ticket capacity before live capabilities are evicted."""

from types import SimpleNamespace


def test_expired_tickets_do_not_evict_unexpired_resource(monkeypatch):
    from backend import capability_tickets as module

    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    store = module.CapabilityTicketStore(max_entries=3)
    image = store.mint("image", ("example",), ttl=300)
    expired = [store.mint("events", (str(index),), ttl=45) for index in range(2)]

    clock[0] = 60.0
    fresh = store.mint("events", ("fresh",), ttl=45)
    assert store.validate(image, "image", ("example",)) is True
    assert store.validate(fresh, "events", ("fresh",)) is True
    for index, ticket in enumerate(expired):
        assert store.validate(ticket, "events", (str(index),)) is False


def test_live_ticket_capacity_still_evicts_least_recently_used():
    from backend.capability_tickets import CapabilityTicketStore

    store = CapabilityTicketStore(max_entries=2)
    reused = store.mint("image", ("reused",), ttl=300, max_uses=3)
    oldest = store.mint("events", ("oldest",), ttl=45)
    assert store.validate(reused, "image", ("reused",)) is True
    fresh = store.mint("events", ("fresh",), ttl=45)

    assert store.validate(oldest, "events", ("oldest",)) is False
    assert store.validate(reused, "image", ("reused",)) is True
    assert store.validate(fresh, "events", ("fresh",)) is True
