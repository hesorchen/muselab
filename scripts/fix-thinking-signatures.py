#!/usr/bin/env python3
"""One-shot fixer for `claude --resume` failing with:

    API Error: 400 messages.N.content.0: Invalid `signature` in `thinking` block

Backstory: any chat session created against a third-party
Anthropic-compat vendor (DeepSeek / GLM / MiniMax / Kimi / Qwen /
Baidu Qianfan / Xiaomi MiMo, etc.) ends up with thinking blocks whose
`signature` field is missing or empty. Anthropic's real API verifies
those signatures on resume and rejects the request. This script walks
your local Claude Code session store (default: ~/.claude/projects/)
and rewrites those bad thinking blocks so resume works.

Default behaviour: dry-run. Pass --apply to actually rewrite files.

Usage:

    # Preview every project's needed changes
    python scripts/fix-thinking-signatures.py

    # Only scan one project (path matches the path-mangled dirname
    # Claude uses, e.g. -home-user-claude-space-projects-muselab)
    python scripts/fix-thinking-signatures.py --project muselab

    # Actually rewrite (atomic write, no backup needed — original
    # contents are deterministically reproducible by re-adding the
    # dropped thinking blocks if you ever want them back)
    python scripts/fix-thinking-signatures.py --apply

The script is idempotent: running it again on already-clean files
is a no-op.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow this script to be run directly (not as a module) from anywhere
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
try:
    from backend import jsonl_cleanup  # noqa: E402
except ImportError as e:
    print(f"✗ could not import backend.jsonl_cleanup: {e}")
    print("  Run this from the muselab repo after dependencies are installed:")
    print("    uv sync && uv run python scripts/fix-thinking-signatures.py")
    sys.exit(1)


def _default_projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _matches_filter(proj_dir: Path, name_filter: str | None) -> bool:
    if not name_filter:
        return True
    return name_filter.lower() in proj_dir.name.lower()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", type=Path, default=_default_projects_root(),
                    help="Claude projects root (default: ~/.claude/projects)")
    p.add_argument("--project", default="",
                    help="Only scan project dirs whose name contains this substring")
    p.add_argument("--apply", action="store_true",
                    help="Actually rewrite files (default is dry-run preview)")
    p.add_argument("-v", "--verbose", action="store_true",
                    help="Print every file scanned, including clean ones")
    args = p.parse_args()

    root: Path = args.root
    if not root.exists():
        print(f"✗ {root} does not exist — no Claude session store found")
        return 1

    proj_dirs = sorted([d for d in root.iterdir() if d.is_dir() and _matches_filter(d, args.project)])
    if not proj_dirs:
        print(f"✗ no project dirs match {args.project!r} under {root}")
        return 1

    print(f"{'DRY-RUN' if not args.apply else 'APPLYING'} — scanning {len(proj_dirs)} project(s) under {root}\n")

    total_files = 0
    total_dirty = 0
    total_blocks = 0
    for proj in proj_dirs:
        jsonls = sorted(proj.rglob("*.jsonl"))
        if not jsonls:
            continue
        proj_dirty = 0
        proj_blocks = 0
        for f in jsonls:
            total_files += 1
            if args.apply:
                rep = jsonl_cleanup.clean_jsonl(f)
            else:
                # Dry-run: scan without writing. We still call the
                # cleaner — but on a temp copy — so the report reflects
                # exactly what --apply would do.
                import shutil
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
                    tmp_path = Path(tf.name)
                try:
                    shutil.copy(f, tmp_path)
                    rep = jsonl_cleanup.clean_jsonl(tmp_path)
                    rep.path = f  # report the real path, not the temp
                finally:
                    # delete=False means we own cleanup — unlink even if
                    # copy/clean raised, so we don't leak temps in /tmp.
                    tmp_path.unlink(missing_ok=True)

            if rep.error:
                print(f"  ✗ {rep.summary()}")
            elif rep.dirty:
                proj_dirty += 1
                proj_blocks += rep.blocks_dropped
                print(f"  {'✓' if args.apply else '·'} {rep.summary()}")
            elif args.verbose:
                print(f"    {rep.summary()}")

        total_dirty += proj_dirty
        total_blocks += proj_blocks
        if proj_dirty or args.verbose:
            print(f"  [{proj.name}] {proj_dirty} file(s) needed fix, {proj_blocks} thinking block(s)\n")

    print("─" * 60)
    if args.apply:
        print(f"DONE. Rewrote {total_dirty}/{total_files} files, dropped {total_blocks} thinking block(s).")
    else:
        print(f"PREVIEW. {total_dirty}/{total_files} files need fix, {total_blocks} thinking block(s) total.")
        if total_dirty:
            print("Re-run with --apply to write the changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
