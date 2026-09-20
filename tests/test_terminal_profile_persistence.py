"""Failed terminal-profile changes must not alter the next terminal's command."""
import errno

import pytest


@pytest.mark.parametrize("operation", ["create_first", "create_default", "update", "delete"])
def test_failed_profile_write_preserves_live_and_durable_state(
    app_module, tmp_path, monkeypatch, operation,
):
    from backend import terminal

    registry = terminal.TerminalProfileRegistry(tmp_path)
    if operation == "create_first":
        original = None
    else:
        original = registry.create(terminal.TerminalProfileWrite(
            name="Original", command="printf original", is_default=True,
        ))
        registry.create(terminal.TerminalProfileWrite(
            name="Alternate", command="printf alternate",
        ))
    before = registry.list()
    before_bytes = registry.path.read_bytes() if registry.path.exists() else None
    before_default = registry.get(None, use_default=True)

    def change():
        request = terminal.TerminalProfileWrite(
            name="Replacement", command="printf replacement", is_default=True,
        )
        if operation.startswith("create"):
            registry.create(request)
        elif operation == "update":
            registry.update(original["id"], request)
        else:
            registry.delete(original["id"])

    def fail_write(*args, **kwargs):
        raise OSError(errno.ENOSPC, "synthetic disk full")

    with monkeypatch.context() as patch:
        patch.setattr(terminal, "atomic_write_text", fail_write)
        with pytest.raises(OSError, match="synthetic disk full"):
            change()

    assert registry.list() == before
    assert registry.get(None, use_default=True) == before_default
    assert (
        registry.path.read_bytes() if registry.path.exists() else None
    ) == before_bytes
    assert terminal.TerminalProfileRegistry(tmp_path).list() == before

    # A retry publishes one successful change and survives a registry reload.
    change()
    restored = terminal.TerminalProfileRegistry(tmp_path)
    assert registry.list() == restored.list()
    assert registry.get(None, use_default=True) == restored.get(None, use_default=True)
    if operation.startswith("create"):
        assert len(registry.list()) == len(before) + 1
    elif operation == "delete":
        assert len(registry.list()) == len(before) - 1
    else:
        assert registry.get(original["id"])["command"] == "printf replacement"
