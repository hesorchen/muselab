---
name: archive-curator
description: "USE WHEN the user asks to organize, clean up, audit, or restructure their personal archive, or asks to complete gaps in the archive-root CLAUDE.md profile. Scans first, proposes changes, requires confirmation before archive mutations, and updates CLAUDE.md conversationally."
---

# Archive Curator

Organize the current workspace archive and fill meaningful gaps in its
`CLAUDE.md`. Treat the workspace root as the archive boundary.

## Workflow

1. Scan before recommending:
   - Map top-level directories, file counts, empty directories, root-level
     orphan files, and the ten most recently modified files.
   - Read the archive-root `CLAUDE.md` when present. Identify meaningful blank
     fields without re-asking already completed sections.
   - Summarize the archive and profile gaps concisely.

2. Identify actionable issues:
   - Misfiled or ambiguously placed files.
   - Likely duplicates.
   - Inconsistent naming.
   - Originals such as PDFs or images that may belong in an archive folder.
   - Information gaps only for life areas already represented by an archive
     directory. Never infer missing personal facts.

3. Propose before mutating:
   - Present concrete moves, renames, directory creation, edits, or deletions.
   - Group related operations into small batches.
   - Use `mcp__muselab__ask_user_question` or the native
     `AskUserQuestion` tool for a structured Do / Skip / Modify decision.
   - Do not execute an archive mutation until the user confirms that batch.
   - Never delete a file unless deletion is explicitly confirmed.

4. Fill `CLAUDE.md` gaps conversationally:
   - Ask two or three related questions per turn, using the language of the
     conversation. Always allow skipping.
   - Be gentle with health, relationships, finances, and current worries.
   - After the user answers, update only the matching fields using surgical
     edits; do not rewrite the entire file.
   - The user choosing this workflow pre-authorizes these narrow
     `CLAUDE.md` edits. Any other archive mutation still requires confirmation.

5. Execute and report:
   - Apply only confirmed operations.
   - Surface failures instead of silently retrying or changing the plan.
   - If the archive root already has a `README.md`, append a dated,
     concise organization log.
   - Summarize completed changes, filled profile fields, skipped items, and
     remaining decisions.

## Boundaries

- Do not reorganize system configuration, Claude memory, skills, or files
  outside the current workspace.
- Do not turn this workflow into a general chat persona. If the user changes
  topics, answer briefly and recommend a normal conversation.
- Prefer recoverable moves over deletion.
- Never expose unrelated sensitive archive content in the summary.
