"""Source contracts for native mobile text-selection actions."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
STYLES = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")


def test_selection_actions_run_on_pointerdown_and_keyboard_click_only():
    start = APP.index("    activatePreviewSelectionAction(ev, action) {")
    end = APP.index("\n    quotePreviewSelection()", start)
    handler = APP[start:end]

    assert 'ev.type === "pointerdown"' in handler
    assert 'ev.type === "click" && ev.detail === 0' in handler
    assert "if (!pointerDown && !keyboardClick) return false" in handler
    assert 'if (action === "quote") return this.quotePreviewSelection()' in handler
    assert 'if (action === "ask")' in handler

    assert "@pointerdown=\"activatePreviewSelectionAction($event, 'quote')\"" in INDEX
    assert "@click=\"activatePreviewSelectionAction($event, 'quote')\"" in INDEX
    assert "@pointerdown=\"activatePreviewSelectionAction($event, 'ask')\"" in INDEX
    assert "@click=\"activatePreviewSelectionAction($event, 'ask')\"" in INDEX
    assert "@pointerdown.prevent @click=\"quotePreviewSelection()\"" not in INDEX
    assert "@pointerdown.prevent @click=\"openPreviewSelectionAsk()\"" not in INDEX


def test_composer_focus_closes_portalled_activity_menu_before_keyboard_resize():
    start = APP.index("    onChatInputFocus() {")
    end = APP.index("\n    // Paired teardown", start)
    focus = APP[start:end]

    assert "if (this.activity.moveMenu.show) this.closeActivityMoveMenu();" in focus
    assert "if (this.activity.show) this.closeActivityCenter();" in focus
    assert focus.index("this.closeActivityMoveMenu()") < focus.index(
        'document.body.classList.add("kb-open")'
    )
    assert focus.index("this.closeActivityCenter()") < focus.index(
        'document.body.classList.add("kb-open")'
    )


def test_selection_action_buttons_disable_touch_double_tap_delay():
    start = STYLES.index(".preview-selection-actions button {")
    end = STYLES.index("\n}", start)
    rule = STYLES[start:end]
    assert "touch-action: manipulation;" in rule
