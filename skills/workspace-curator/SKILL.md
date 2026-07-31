---
name: workspace-curator
description: "USE WHEN the user asks to organize, clean up, audit, or restructure files in the current workspace. Scans first, proposes concrete changes, and requires confirmation before every batch of workspace mutations."
---

# Workspace Curator

Organize files and directories inside the current workspace without imposing a
predefined directory structure.

## Workflow

1. Establish the boundary:
   - Treat the current working directory as the workspace boundary.
   - Do not inspect or modify files outside that workspace.
   - Exclude hidden runtime and version-control directories from broad scans
     unless the user explicitly asks about them.

2. Scan before recommending:
   - Map top-level files and directories, file counts, empty directories,
     unusually large files, and recently modified files.
   - Identify likely duplicates, ambiguous placement, inconsistent naming, and
     stale generated artifacts using evidence available in the workspace.
   - Read file contents only when needed to support a concrete recommendation.
   - Summarize findings without exposing unrelated sensitive content.

3. Propose before mutating:
   - Present concrete moves, renames, directory creation, or deletions.
   - Explain the reason and destination for each operation.
   - Group related operations into small, independently confirmable batches.
   - Use `mcp__muselab__ask_user_question` or the native
     `AskUserQuestion` tool for a structured Do / Skip / Modify decision.
   - Do not execute any workspace mutation until the user confirms that batch.
   - Require separate explicit confirmation before deleting anything.

4. Execute and verify:
   - Apply only confirmed operations.
   - Prefer recoverable moves over deletion.
   - Preserve file contents and metadata where practical.
   - Verify the resulting paths and report failures instead of silently
     changing the plan.
   - Summarize completed changes, skipped items, and remaining decisions.

## Boundaries

- Never create a predefined personal directory taxonomy.
- Never collect personal-profile information or ask the user to fill personal,
  health, financial, relationship, or biographical fields.
- Never create or edit `CLAUDE.md` as a side effect of workspace organization.
  If the user separately requests an instruction-file edit, handle it as a
  distinct task with its own explicit scope.
- Do not reorganize system configuration, Claude memory, Skills, or files
  outside the current workspace.
- Do not turn this workflow into a general chat persona.
