"""Strip thinking blocks with invalid signatures from Claude Code
session JSONLs, so the session can be resumed via the official Claude
API (or `claude --resume`).

Why this exists:

  When muselab routes a chat through a third-party Anthropic-compat
  endpoint (DeepSeek / GLM / MiniMax / Kimi / Qwen / Baidu / Xiaomi
  MiMo, etc.), those vendors' responses include `thinking` content
  blocks but the `signature` field is either missing, empty, or a
  short non-cryptographic placeholder. Anthropic's real API verifies
  the signature on every assistant message it ingests during resume,
  and 400s with `Invalid signature in thinking block`. End result:
  any session created against a third-party vendor cannot be resumed
  with `claude --resume` once the model is later switched to Claude.

  The fix is to drop the bad thinking blocks from the JSONL. We keep
  the surrounding text blocks (the actual visible answer) untouched —
  thinking blocks are model-internal scratchpad that the user has
  never seen and that future turns don't depend on. If a message
  ends up with zero content blocks (rare: vendor returned only
  thinking + no text), we insert a one-line placeholder so the
  message stays structurally valid (Anthropic rejects empty content
  arrays).

Public API:

  clean_jsonl(path)            — clean a single .jsonl file in place,
                                 atomic write, returns CleanupReport
  clean_all_under(root)        — recursive sweep, returns
                                 list[CleanupReport]
  is_invalid_thinking(block)   — True if a content block looks like
                                 the unverifiable kind
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


# A thinking block written by Claude itself carries a long base64 ed25519
# signature (typically ~88 chars). Third-party vendors either omit the
# field entirely, ship an empty string, or use a placeholder shorter
# than ~40 chars. We treat anything under MIN_SIG_LEN as suspect.
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
    """True if `block` is a thinking content-block whose signature
    Anthropic's resume API would reject (missing / empty / too short)."""
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
    """In-place clean of a single .jsonl. Atomic write (tmp + rename).
    Idempotent: running again on a clean file is a no-op (no rewrite)."""
    report = CleanupReport(path=path)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        report.error = f"read failed: {e}"
        return report

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

    # Atomic write — same dir as target so rename is atomic.
    fd, tmp = tempfile.mkstemp(prefix=".clean.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines))
            if raw.endswith("\n"):
                f.write("\n")
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


def clean_session(session_id: str, claude_projects_root: Path | None = None) -> CleanupReport | None:
    """Clean exactly one session by id. We don't know which project
    dir it belongs to, so we search every project under
    ~/.claude/projects/ and clean the first match. Used by chat.py
    in the turn-done hook so each session stays resumable immediately
    after the assistant finishes a reply that included a stripped
    thinking block."""
    if claude_projects_root is None:
        claude_projects_root = Path.home() / ".claude" / "projects"
    if not claude_projects_root.exists():
        return None
    for proj in claude_projects_root.iterdir():
        if not proj.is_dir():
            continue
        target = proj / f"{session_id}.jsonl"
        if target.exists():
            return clean_jsonl(target)
    return None
