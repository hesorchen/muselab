---
name: archive-curator
description: "USE WHEN an older prompt explicitly requests archive-curator. Deprecated compatibility alias for workspace-curator; organizes the current workspace without collecting or creating a personal profile."
---

# Archive Curator — Deprecated Compatibility Alias

New prompts should use `workspace-curator`. For compatibility with saved
sessions and older clients, apply the same generic workspace workflow here.

## Workflow

1. Treat the current working directory as the workspace boundary.
2. Scan its visible file structure and report evidence-backed organization
   issues without exposing unrelated sensitive content.
3. Propose concrete operations in small batches.
4. Require a Do / Skip / Modify confirmation before each mutation batch and
   separate explicit confirmation before deletion.
5. Apply only confirmed operations, verify results, and report skipped items
   and failures.

## Boundaries

- Do not collect personal-profile information.
- Do not create personal category directories.
- Do not create or edit `CLAUDE.md` as part of this compatibility workflow.
- Do not reorganize system configuration, Claude memory, skills, or files
  outside the current workspace.
- Prefer recoverable moves over deletion.
- Never mutate files before confirmation.
