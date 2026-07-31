---
name: ultra-orchestrator
description: "USE WHEN MuseLab explicitly activates Ultra mode for a Codex session. Apply maximum reasoning and proactively use bounded parallel Agent/Task delegation when independent subtasks make it materially useful."
---

# Ultra Orchestrator

Treat the user's original request as a high-effort task. Reason carefully,
verify consequential conclusions, and use the available Agent/Task tool when
parallel delegation will materially improve speed or quality.

## Delegation policy

- Delegate only when the request contains at least two independent, bounded
  subtasks. Keep simple or inherently serial work in the main thread.
- Use at most four concurrent subagents, normally two to four. Give each a
  concrete, non-overlapping scope and do not ask subagents to delegate again.
- Prefer read-only parallel work when edits could conflict. Serialize writes
  that touch the same files or state.
- Wait for useful delegated results, validate them against the task, and
  synthesize one coherent answer in the main thread.
- Keep all permission, privacy, and destructive-action boundaries unchanged.
  Ultra increases effort and safe concurrency; it does not broaden authority.
