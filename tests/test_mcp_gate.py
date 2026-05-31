"""Tests for the wedge-readiness gate (backend/chat._await_mcp_ready) and the
claude.ai-connector detection that arms it.

Regression target — the wedge bug came back (2026-05-30) because claude.ai
proxy connectors (Gmail / Calendar / Drive / IBKR) enumerate a beat AFTER the
local stdio servers, and the old gate only checked "nothing pending right now".
A connector that hadn't appeared yet trivially passed that check, then connected
mid-first-turn and invalidated the in-flight thinking block →
`400 ... thinking blocks ... cannot be modified`.
"""
import asyncio
import json

from backend import chat


# ── status parsing ──────────────────────────────────────────────────────────

def _status(*pairs):
    """Build a CLI-shaped status: {'mcpServers': [{name, status}, ...]}."""
    return {"mcpServers": [{"name": n, "status": s} for n, s in pairs]}


def test_servers_from_status_list_shape():
    st = _status(("gmail", "failed"), ("muselab", "connected"))
    assert chat._mcp_servers_from_status(st) == [
        ("gmail", "failed"), ("muselab", "connected")]


def test_servers_from_status_dict_shape():
    st = {"servers": {"a": {"status": "Connected"}, "b": "PENDING"}}
    got = dict(chat._mcp_servers_from_status(st))
    assert got == {"a": "connected", "b": "pending"}


def test_servers_from_status_unknown_shape_is_empty():
    assert chat._mcp_servers_from_status(None) == []
    assert chat._mcp_servers_from_status({"nope": 1}) == []


def test_states_shim_still_works():
    st = _status(("x", "Connecting"), ("y", "connected"))
    assert chat._mcp_states_from_status(st) == ["connecting", "connected"]


# ── the gate ────────────────────────────────────────────────────────────────

class _FakeClient:
    """get_mcp_status returns the next scripted snapshot each call; the last
    snapshot repeats forever (simulates a settled tool-set)."""
    def __init__(self, snapshots):
        self._snaps = list(snapshots)
        self.calls = 0

    async def get_mcp_status(self):
        self.calls += 1
        idx = min(self.calls - 1, len(self._snaps) - 1)
        return self._snaps[idx]


def test_gate_waits_for_late_connector():
    """The set GROWS across polls (claude.ai proxy shows up late). The gate must
    NOT return on the first 'nothing pending' poll — it has to see the SAME set
    twice before proceeding."""
    snaps = [
        _status(("muselab", "connected")),                           # poll 1
        _status(("muselab", "connected"), ("gmail", "connecting")),  # poll 2 (pending)
        _status(("muselab", "connected"), ("gmail", "connected"),
                ("claude.ai IBKR", "connected")),                    # poll 3 (set grew)
        _status(("muselab", "connected"), ("gmail", "connected"),
                ("claude.ai IBKR", "connected")),                    # poll 4 == 3 → ready
    ]
    fake = _FakeClient(snaps)
    asyncio.run(chat._await_mcp_ready(fake, timeout=5.0, poll=0.0))
    assert fake.calls >= 4   # never bailed at poll 1


def test_gate_does_not_bail_on_first_empty_then_grows():
    """A connector that appears only on poll 2 must not be missed because poll 1
    looked settled."""
    snaps = [
        _status(("muselab", "connected")),                           # looks settled…
        _status(("muselab", "connected"), ("late", "connected")),    # but one appeared
        _status(("muselab", "connected"), ("late", "connected")),    # stable → ready
    ]
    fake = _FakeClient(snaps)
    asyncio.run(chat._await_mcp_ready(fake, timeout=5.0, poll=0.0))
    assert fake.calls >= 3


def test_gate_returns_once_stable():
    """Steady-state set, no pending: returns after the second identical poll."""
    snap = _status(("gmail", "failed"), ("drive", "needs-auth"),
                   ("muselab", "connected"))
    fake = _FakeClient([snap, snap, snap])
    asyncio.run(chat._await_mcp_ready(fake, timeout=5.0, poll=0.0))
    assert fake.calls == 2   # poll 1 baseline, poll 2 confirms stability


def test_gate_terminal_states_do_not_block():
    """needs-auth / failed are terminal — they must not keep the gate spinning."""
    snap = _status(("a", "needs-auth"), ("b", "failed"), ("muselab", "connected"))
    fake = _FakeClient([snap, snap])
    asyncio.run(chat._await_mcp_ready(fake, timeout=5.0, poll=0.0))
    assert fake.calls == 2


def test_gate_status_error_returns_immediately():
    """If get_mcp_status throws, don't hold the turn hostage."""
    class _Boom:
        async def get_mcp_status(self):
            raise RuntimeError("no control channel")
    asyncio.run(chat._await_mcp_ready(_Boom(), timeout=5.0, poll=0.0))  # no raise


def test_gate_timeout_backstops_flapping(monkeypatch):
    """A set that never stabilises (keeps flapping) must exit via timeout, not
    hang forever."""
    flap = [_status(("x", "connecting")), _status(("x", "connected"))]

    class _Flap:
        def __init__(self):
            self.calls = 0

        async def get_mcp_status(self):
            self.calls += 1
            return flap[self.calls % 2]

    fake = _Flap()
    t = {"v": 0.0}
    monkeypatch.setattr(chat.time, "monotonic", lambda: t["v"])

    async def _sleep(*_a, **_k):
        t["v"] += 1.0   # each poll advances 1s of fake time

    monkeypatch.setattr(chat.asyncio, "sleep", _sleep)
    asyncio.run(chat._await_mcp_ready(fake, timeout=3.0, poll=0.0))
    assert fake.calls >= 3   # polled a few times then gave up at the deadline


# ── claude.ai connector detection arms the gate ─────────────────────────────

def test_has_claude_ai_connectors_true(monkeypatch, tmp_path):
    from backend import api_settings
    p = tmp_path / "claude.json"
    p.write_text(json.dumps({
        "claudeAiMcpEverConnected": ["claude.ai Gmail", "claude.ai IBKR"]}))
    monkeypatch.setattr(api_settings, "_CLAUDE_USER_JSON", p)
    assert api_settings.has_claude_ai_connectors() is True


def test_has_claude_ai_connectors_false_when_empty(monkeypatch, tmp_path):
    from backend import api_settings
    p = tmp_path / "claude.json"
    p.write_text(json.dumps({"claudeAiMcpEverConnected": []}))
    monkeypatch.setattr(api_settings, "_CLAUDE_USER_JSON", p)
    assert api_settings.has_claude_ai_connectors() is False


def test_has_claude_ai_connectors_false_when_missing_file(monkeypatch, tmp_path):
    from backend import api_settings
    monkeypatch.setattr(api_settings, "_CLAUDE_USER_JSON", tmp_path / "nope.json")
    assert api_settings.has_claude_ai_connectors() is False


def test_enabled_external_mcp_armed_by_claude_ai(monkeypatch):
    """Even with ZERO mcpServers entries, the gate must arm when a claude.ai
    connector exists — the exact claude.ai-only install the old code skipped."""
    from backend import api_settings
    monkeypatch.setattr(api_settings, "_load_mcp_merged", lambda: {})
    monkeypatch.setattr(api_settings, "has_claude_ai_connectors", lambda: True)
    assert chat._has_enabled_external_mcp() is True


def test_enabled_external_mcp_false_when_nothing(monkeypatch):
    from backend import api_settings
    monkeypatch.setattr(api_settings, "_load_mcp_merged", lambda: {})
    monkeypatch.setattr(api_settings, "has_claude_ai_connectors", lambda: False)
    assert chat._has_enabled_external_mcp() is False
