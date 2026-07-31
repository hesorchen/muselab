"""Static contract tests for per-session Claude SDK permission controls."""
from pathlib import Path
import re
from typing import get_args

from claude_agent_sdk.types import PermissionMode


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def test_session_permission_is_separate_from_new_session_default():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert 'defaultPermission: "bypassPermissions"' in app
    assert 'permission: this.defaultPermission || "bypassPermissions"' in app
    assert 'this.permission = s.permission || "default"' not in app
    assert 'this.defaultPermission = newDefaultPerm' in app
    assert 'this.permission = newDefaultPerm' not in app
    assert 'this.model = newDefaultModel' not in app
    assert '["_permissionExpected", "_permissionPatchTail", "permission"' in app


def test_permission_selector_matches_installed_sdk_modes():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    start = html.index("<!-- Permission mode -->")
    end = html.index("<!-- Reasoning effort.", start)
    toolbar = html[start:end]
    rendered = set(re.findall(r'<option value="([^"]+)">', toolbar))

    assert rendered == set(get_args(PermissionMode))


def test_permission_is_mirrored_per_tab_on_every_activation_path():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    blank = app[app.index("_blankTabState()"):app.index("_ensureTabState(id)")]
    activate = app[
        app.index("_activateTabState(id)"):
        app.index("// P1 (chat-perf-redesign)", app.index("_activateTabState(id)"))
    ]
    new_session = app[
        app.index("newSession(options = {})"):
        app.index("// ===== tabs =====", app.index("newSession(options = {})"))
    ]
    switch = app[
        app.index("async switchSession()"):
        app.index("_afterPaint(fn)", app.index("async switchSession()"))
    ]
    load = app[
        app.index("async loadSession("):
        app.index("loadEarlierMessages(", app.index("async loadSession("))
    ]

    assert 'permission: "",' in blank
    assert "_permissionChangePending: false" in blank
    assert "st.permission = mode;" in activate
    assert "this.permission = mode;" in activate
    assert "st.permission = meta.permission;" in new_session
    assert "this.permission = curPermission;" in switch
    assert "this.permission = st.permission;" in load
    assert "permissionExpected.planReturnPermission" in load
    assert "loadedMeta.plan_return_permission =" in load
    assert 'if ((x.permission || "") !== (y.permission || "")) return false;' in app
    assert "x.plan_return_permission" in app


def test_session_permission_controls_are_disabled_while_streaming():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert html.count(':disabled="permissionControlDisabled()"') == 2
    assert html.count(':title="permissionControlTitle()"') == 2
    assert "st._permissionChangePending || st.compacting" in app
    change = app[
        app.index("async onPermissionChange()"):
        app.index("async onEffortChange()")
    ]
    assert "if (this.permissionControlDisabled(sid))" in change
    assert "this.permission = stable;" in change
    assert change.index("permissionControlDisabled(sid)") < change.index(
        'method: "PATCH"')


def test_exit_plan_permission_request_has_dedicated_mode_card():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    card = html[
        html.index("<!-- ExitPlanMode permission_request"):
        html.index("<!-- Generic permission_request")
    ]
    assert "isExitPlanPermission(m)" in card
    assert "m.description || m.summary" in card
    assert "planPermissionMarkdown(m)" in card
    assert 'x-for="suggestion in planModeSuggestions(m)"' in card
    assert "decidePermission(m, 'allow', suggestion.mode)" in card
    assert "decidePermission(m, 'deny', null)" in card
    assert "perm.always" not in card
    assert (
        "m.role === 'permission_request' && !isExitPlanPermission(m)"
        in html
    )

    handler = app[
        app.index('es.addEventListener("permission_request"'):
        app.index('es.addEventListener("permission_mode_changed"')
    ]
    for field in (
        "sessionId: streamSid",
        "kind: d.kind",
        "suggestions:",
        "return_mode:",
        "title:",
        "display_name:",
        "description:",
        "input:",
    ):
        assert field in handler

    decide = app[
        app.index("async decidePermission("):
        app.index("async togglePinSession(", app.index("async decidePermission("))
    ]
    assert "const sid = msg.sessionId || this.currentId;" in decide
    assert "encodeURIComponent(sid)" in decide
    assert "JSON.stringify({ decision, mode })" in decide


def test_permission_mode_changed_updates_origin_session():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert '"permission_request_resolved"' in app
    assert '"permission_mode_changed"' in app
    assert '"permission_mode_change_failed"' in app
    resolved = app[
        app.index('es.addEventListener("permission_request_resolved"'):
        app.index('es.addEventListener("permission_mode_changed"', app.index(
            'es.addEventListener("permission_request_resolved"'))
    ]
    assert "msg.id !== d.id" in resolved
    assert 'd.kind === "exit_plan"' in resolved
    assert 'd.decision === "allow"' in resolved
    assert "msg.awaitingTransition = true" in resolved
    assert "msg.resolved = false" in resolved
    assert "msg.resolved = true" in resolved
    assert "msg._decisionAcknowledged = true" in resolved
    assert "this.currentId" not in resolved

    handler = app[
        app.index('es.addEventListener("permission_mode_changed"'):
        app.index('es.addEventListener("permission_mode_change_failed"', app.index(
            'es.addEventListener("permission_mode_changed"'))
    ]
    assert "_applySessionPermissionMode(streamSid, d.permission" in handler
    assert "previousPermission: d.previous_permission" in handler
    assert 'd.source === "exit_plan"' in handler
    assert "d.tool_use_id !== msg.tool_use_id" in handler
    assert "this.currentId" not in handler

    failed = app[
        app.index('es.addEventListener("permission_mode_change_failed"'):
        app.index("const _stopTimer", app.index(
            'es.addEventListener("permission_mode_change_failed"'))
    ]
    assert 'msg.decision = "failed"' in failed
    assert "msg.awaitingTransition = false" in failed
    assert "d.tool_use_id !== msg.tool_use_id" in failed
    assert failed.index("_applySessionPermissionMode") < failed.index(
        'if (d.source !== "exit_plan") return;')
    assert "_finalizePendingPermissionRequests();" in app


def test_exit_plan_allow_waits_for_transition_commit():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    decide = app[
        app.index("async decidePermission("):
        app.index("async togglePinSession(", app.index("async decidePermission("))
    ]
    assert 'decision === "allow"' in decide
    assert "msg.awaitingTransition = waitsForTransition;" in decide
    assert "if (!waitsForTransition) msg.resolved = true;" in decide
    assert "if (msg._decisionAcknowledged) return;" in decide
    assert "permission_mode_changed SSE is the commit" in decide
    assert "!m.resolved && !m.awaitingTransition" in html
    assert "plan.awaiting_transition" in html
    assert "plan.exit_failed" in html
    assert "plan.approval_expired" in html

    terminal = app[
        app.index("const _finalizePendingPermissionRequests"):
        app.index("const _stopTimer", app.index(
            "const _finalizePendingPermissionRequests"))
    ]
    assert 'msg.role !== "permission_request"' in terminal
    assert 'msg.decision = transitionFailed ? "failed" : "expired"' in terminal
    assert "msg.awaitingTransition = false" in terminal
    assert "msg._decisionAcknowledged = true" in terminal
    assert "msg.resolved = true" in terminal


def test_plan_queue_snapshots_return_permission():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    enqueue = app[
        app.index("async _enqueueMessage("):
        app.index("// Post-turn / on-activate hook", app.index(
            "async _enqueueMessage("))
    ]
    assert 'permission === "plan"' in enqueue
    assert "item.plan_return_permission" in enqueue
    assert "plan_return_permission: planReturnPermission" in enqueue

    send = app[
        app.index("async send("):
        app.index("// ====== permission_request helpers ======")
    ]
    assert 'sendPermission === "plan"' in send
    assert "sendMeta && sendMeta.plan_return_permission" in send
    assert "plan_return_permission: sendPlanReturnPermission" in send
    assert "sendState._permissionChangePending" in send


def test_plan_mode_i18n_keys_are_bilingual():
    strings = (FRONTEND / "i18n" / "index.js").read_text(encoding="utf-8")
    for key in (
        "plan.approval_title",
        "plan.keep_planning",
        "plan.awaiting_transition",
        "plan.exit_failed",
        "plan.approval_expired",
        "plan.approved_mode",
        "plan.approve.default",
        "plan.approve.acceptEdits",
        "perm.switch_wait_stream",
        "perm.decision_expired",
        "perm.mode.plan",
    ):
        assert strings.count(f'"{key}"') == 2
