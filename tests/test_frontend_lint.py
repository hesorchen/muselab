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
