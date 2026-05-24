"""Frontend static lint — narrow but high-value checks for bug classes that
already shipped once. These read frontend/ as plain text; no JS runtime
needed.

Why this exists: JS object literals silently shadow earlier definitions when
the same key appears twice. We hit this in the multi-tab sprint
(2026-05-17) — a second `closeChatTab(...)` was added below the first one
and the upper definition was lost without any warning. The duplicate sat
undiscovered until a button stopped working. Pytest is the cheapest
guard."""
from __future__ import annotations
import re
from collections import Counter
from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


# Match top-level method definitions inside the Alpine x-data object:
#     methodName(args) {
#     async methodName(args) {
#     *gen(args) {
# - Exactly 4 spaces of indent (the component's outer indent level).
# - Strips optional `async ` / `static ` / `*` prefix so it doesn't capture
#   the keyword as the name. Without this, `async closeChatTab` matched as
#   `async` and missed the real collision.
# - Excludes arrow assignments (`const foo = () =>`) and `function ` decls.
# `(?!\{)` negative lookahead excludes calls like `_report({ ... })` where
# the open paren is immediately followed by a `{` (object literal arg). A
# real method def starts with `name(arg…)` or `name()`, never `name({`.
_METHOD_DEF = re.compile(
    r"^    (?:async\s+|static\s+|\*\s*)?([a-zA-Z_][a-zA-Z0-9_]*)\s*\((?!\{)"
)


def test_app_js_has_no_duplicate_method_definitions():
    """Guard against silently shadowed methods in app.js.

    Real bug, 2026-05-17: two `closeChatTab(id)` definitions coexisted —
    JS kept only the second, so the toolbar's close button (wired to the
    first) silently broke. This test would have caught it instantly."""
    text = (FRONTEND / "app.js").read_text(encoding="utf-8")

    names = []
    for line in text.splitlines():
        m = _METHOD_DEF.match(line)
        if not m:
            continue
        name = m.group(1)
        # Skip JS keywords that legitimately appear in the same column shape
        # (if/for/while/switch/return/etc.) — not method defs.
        if name in {
            "if", "for", "while", "switch", "return", "throw", "catch",
            "do", "else", "function", "case",
        }:
            continue
        names.append(name)

    dupes = [n for n, c in Counter(names).items() if c > 1]
    assert not dupes, (
        f"Duplicate method definitions in app.js: {dupes}. "
        "JS keeps only the LAST one — the earlier definitions are dead "
        "code and any caller wired to them silently breaks. Rename or "
        "merge the duplicates."
    )


def test_i18n_zh_en_key_parity():
    """Both language sections in i18n/index.js must define the same set of
    keys. A missing translation causes `t('foo.bar')` to fall back to the
    key literal — exposed to users as 'foo.bar' on screen. We hit this
    historically when a quick zh-only addition landed without the en
    mirror; the English UI showed raw keys until a user reported it."""
    text = (FRONTEND / "i18n" / "index.js").read_text(encoding="utf-8")
    # The file has shape `window.MUSELAB_STRINGS = { zh: {...}, en: {...} };`
    # — split it at the top-level "zh:" / "en:" labels. The blocks are
    # several hundred lines but contain no nested object literals that look
    # like another language label, so a greedy "until next label" works.
    zh_match = re.search(r"\bzh:\s*\{(.*?)\n  \},\s*en:", text, re.S)
    en_match = re.search(r"\ben:\s*\{(.*?)\n  \},?\s*\};", text, re.S)
    assert zh_match, "couldn't find zh: { ... } block in i18n/index.js"
    assert en_match, "couldn't find en: { ... } block in i18n/index.js"
    zh_keys = set(re.findall(r'"([\w.]+)"\s*:', zh_match.group(1)))
    en_keys = set(re.findall(r'"([\w.]+)"\s*:', en_match.group(1)))
    only_zh = zh_keys - en_keys
    only_en = en_keys - zh_keys
    assert not only_zh and not only_en, (
        f"i18n key drift between zh and en. "
        f"only in zh: {sorted(only_zh)[:8]}; "
        f"only in en: {sorted(only_en)[:8]}. "
        f"Add the missing translations or `t()` will leak raw keys to "
        f"users on the side that's missing them."
    )
