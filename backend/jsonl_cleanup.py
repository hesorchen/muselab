"""Offline migration utility for transcripts with unsigned thinking blocks.

Some providers omit signatures or emit placeholders that another provider may
reject on resume. This utility removes suspect blocks using a length heuristic;
it does not verify signatures and cannot guarantee cross-provider compatibility.
Removed thinking is user-visible history and cannot be reconstructed from the
remaining transcript. Preserve a backup before applying this migration.

Only operate on a private copy or a transcript whose CLI writers have all stopped.
Atomic replacement protects readers from partial files, but does not coordinate
with appenders: late SDK writes can target the old, unlinked inode and be lost.
A ResultMessage or an idle pooled client does not establish exclusive file access.
MuseLab's live turn lifecycle must never call this utility.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


# Legacy length heuristic, not cryptographic signature validation. Even a
# legitimate signature could be classified as suspect; use only for an explicit,
# backed-up offline migration after inspecting the dry-run report.
MIN_SIG_LEN = 40

# Placeholder content block we insert when stripping all content from a
# message would leave it empty (Anthropic API rejects empty content[]).
_PLACEHOLDER_TEXT = "(internal reasoning omitted — original vendor did not produce a verifiable signature)"


@dataclass
class CleanupReport:
    path: Path
    lines_total: int = 0
    lines_changed: int = 0
    blocks_dropped: int = 0
    error: str | None = None

    @property
    def dirty(self) -> bool:
        return self.lines_changed > 0

    def summary(self) -> str:
        if self.error:
            return f"{self.path}: ERROR {self.error}"
        if not self.dirty:
            return f"{self.path}: clean ({self.lines_total} lines)"
        return (
            f"{self.path}: fixed {self.lines_changed} message(s), "
            f"dropped {self.blocks_dropped} thinking block(s) "
            f"({self.lines_total} lines total)"
        )


def is_invalid_thinking(block: object) -> bool:
    """True if a thinking signature is missing, empty, or below the heuristic."""
    if not isinstance(block, dict):
        return False
    if block.get("type") != "thinking":
        return False
    sig = block.get("signature", "")
    if not isinstance(sig, str):
        return True
    return len(sig) < MIN_SIG_LEN


def _clean_message_obj(msg: dict) -> tuple[bool, int]:
    """Mutate `msg` in place, return (changed, num_blocks_dropped)."""
    content = msg.get("content")
    if not isinstance(content, list):
        return False, 0
    kept: list = []
    dropped = 0
    for blk in content:
        if is_invalid_thinking(blk):
            dropped += 1
            continue
        kept.append(blk)
    if dropped == 0:
        return False, 0
    if not kept:
        # Don't leave an empty content[] — Anthropic rejects that.
        # A minimal text block keeps the message structurally valid
        # without inventing a fake answer.
        kept = [{"type": "text", "text": _PLACEHOLDER_TEXT}]
    msg["content"] = kept
    return True, dropped


def clean_jsonl(path: Path) -> CleanupReport:
    """Rewrite one offline .jsonl; the caller must stop all writers first.

    Uses atomic replacement and does nothing when no blocks need removal.
    This operation deletes history; retain a backup of the original file.
    """
    report = CleanupReport(path=path)
    try:
        # newline="" disables universal-newline translation so we can SEE the
        # file's real terminators (read_text() would have already collapsed
        # \r\n → \n, making CRLF undetectable). We translate to \n ourselves
        # for parsing but remember the original ending for re-emit.
        with open(path, "r", encoding="utf-8", newline="") as f:
            raw = f.read()
    except (OSError, UnicodeDecodeError) as e:
        report.error = f"read failed: {e}"
        return report

    # Preserve the file's original line ending. `splitlines()` strips both
    # \n and \r\n, and naively re-joining with "\n" would silently rewrite a
    # CRLF file to LF (audit O/403). Treat the file as CRLF if it contains
    # any \r\n; otherwise LF. (Anthropic's CLI tolerates either, but
    # rewriting line endings is gratuitous and noisy in diffs / git.)
    _newline = "\r\n" if "\r\n" in raw else "\n"

    new_lines: list[str] = []
    any_change = False
    for line in raw.splitlines():
        report.lines_total += 1
        if not line.strip():
            new_lines.append(line)
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            # Malformed line — leave it. Anthropic CLI is tolerant of
            # bad lines, and we don't want to silently delete data.
            new_lines.append(line)
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            new_lines.append(line)
            continue
        changed, dropped = _clean_message_obj(msg)
        if changed:
            report.lines_changed += 1
            report.blocks_dropped += dropped
            any_change = True
            new_lines.append(json.dumps(d, ensure_ascii=False))
        else:
            new_lines.append(line)

    if not any_change:
        return report

    # Atomic write — same dir as target so rename is atomic. newline=""
    # so our explicit _newline (which may be \r\n) is written verbatim and
    # not re-translated by text-mode newline handling.
    fd, tmp = tempfile.mkstemp(prefix=".clean.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(_newline.join(new_lines))
            # Re-emit a trailing terminator iff the original had one.
            if raw.endswith("\n"):   # covers both \n and \r\n
                f.write(_newline)
        os.replace(tmp, path)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        report.error = f"write failed: {e}"
    return report


def clean_all_under(root: Path) -> list[CleanupReport]:
    """Recursive sweep — call clean_jsonl on every *.jsonl under `root`.
    Returns one report per file, in path order."""
    out: list[CleanupReport] = []
    for p in sorted(root.rglob("*.jsonl")):
        out.append(clean_jsonl(p))
    return out


def clean_session(
    session_id: str,
    claude_projects_root: Path | None = None,
) -> CleanupReport | None:
    """Clean one offline session by id across native and vendor stores.

    An explicit ``claude_projects_root`` preserves the original single-root
    behavior for callers/tests. By default we search both Claude's normal
    store and muselab's durable vendor-isolated store; third-party sessions
    never live under ``~/.claude/projects``.
    """
    if claude_projects_root is not None:
        project_roots = [claude_projects_root]
    else:
        state_home = os.environ.get("XDG_STATE_HOME", "").strip()
        state_root = (
            Path(state_home).expanduser()
            if state_home
            else Path.home() / ".local" / "state"
        )
        project_roots = [
            Path.home() / ".claude" / "projects",
            state_root / "muselab" / "vendor-cli" / "projects",
        ]

    for projects_root in project_roots:
        if not projects_root.exists():
            continue
        for proj in projects_root.iterdir():
            if not proj.is_dir():
                continue
            target = proj / f"{session_id}.jsonl"
            if target.exists():
                return clean_jsonl(target)
    return None
