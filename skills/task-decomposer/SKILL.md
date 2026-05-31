---
name: task-decomposer
description: "USE WHEN the user describes a vague goal ('build X', 'plan Y', 'figure out how to Z') that needs to be broken into concrete steps before any work can start. Produces an ordered task list with rough estimates, dependencies, and unknowns flagged."
---

# task-decomposer — turn vague goals into actionable plans

## When to use

Trigger phrases:

- "How do I…"
- "Help me plan…"
- "I want to build/launch/migrate/refactor…"
- "Where do I even start?"

Do NOT use when:

- The task is already concrete and atomic ("fix this typo")
- The user explicitly says "just do it" — they don't want a plan, they want execution
- The decomposition is trivial (≤2 steps)

## Process

### 1. Clarify the goal

Restate it in one sentence. If anything is ambiguous, ask one targeted
question before decomposing. Avoid 5-question intake forms.

### 2. Identify the **DoD** (Definition of Done)

What does success look like, concretely? Examples:

- "✅ when the test suite passes on CI"
- "✅ when 10 real users have used it without crashes"
- "✅ when the doc is approved by manager"

Without a DoD, the plan is open-ended.

### 3. Decompose

Break into tasks that are:

- **Atomic enough** that an estimate is meaningful (~15 min to ~4 hr each)
- **Verifiable** — each step has a clear "done" signal
- **Ordered** — note explicit dependencies (`#3 blocked by #1`)
- **Sized** — give a rough estimate: XS (<30m) / S (1-2h) / M (half-day) /
  L (full day) / XL (>1 day, recommend splitting further)

### 4. Flag unknowns

Mark anything you couldn't decompose because of missing info. These are
research tasks: "Investigate X (S) — required before deciding between
approach A or B".

### 5. Suggest a critical path

Identify the 2-3 steps that gate everything else. The user should know what
to do first.

## Output template

```markdown
**Goal**: {one-sentence restatement}
**Done when**: {concrete success signal}

## Critical path
1. {task} (S) — blocks 3, 4
2. {task} (M) — needed for 5

## Backlog (no dependencies)
- {task} (XS)
- {task} (S)

## Unknowns to research first
- {what} — {why it matters}

## Risks
- {what could derail this}
```

## Anti-patterns

- A plan with 30 tasks of "research X" without ever proposing concrete work
- Tasks the size of "build the backend" — too big, will rot
- Imaginary dependencies (#3 doesn't actually block #4)
- Skipping the DoD because "we'll know it when we see it"
- Adding "write tests" / "deploy" as afterthoughts — they're real tasks, size them
