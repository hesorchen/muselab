---
name: code-reviewer
description: "USE WHEN the user pastes code or asks for review of a file/diff. Reads the code, identifies real issues (bugs, security, perf, correctness, readability) in priority order, suggests concrete fixes. Skips style nitpicks unless asked."
---

# code-reviewer — focused, prioritized review

## Goal

Find issues that matter. The order of severity:

1. **Bugs** — wrong behavior, race conditions, off-by-one, null deref, crashes
2. **Security** — injection, secret leak, auth bypass, unsafe deserialization,
   path traversal, missing rate limit
3. **Correctness edge cases** — empty input, unicode, very large input, network
   failure, partial write
4. **Performance** — O(n²) in hot path, N+1 queries, missing index, blocking
   I/O in event loop
5. **Maintainability** — unclear naming, dead code, duplication that hurts
6. **Style** — only mention if it actively confuses reading. Skip otherwise.

## Process

1. **Read the full context** before commenting. Don't review a function in
   isolation if it's called by something nearby.
2. **Group findings by severity**. Critical first, nits last.
3. **For each finding**:
   - Line reference: `file.py:42` or quote the relevant code
   - What's wrong (one sentence)
   - Why it matters (one sentence — concrete consequence)
   - Suggested fix (code snippet if non-obvious)
4. **Surface what's good too**, briefly. "Nice use of X" or "auth flow looks
   right" — keeps the review balanced and helps the author trust your bug
   reports.

## What NOT to do

- Don't rewrite the whole function "for style"
- Don't suggest libraries the project doesn't use
- Don't add abstract concerns without concrete consequence ("this could be
  cleaner" without specifics)
- Don't claim a bug exists without explaining the input that triggers it
- Don't lecture on principles — diagnose this code

## Output template

```markdown
## Critical (blocking)
- **{file}:{line}** — {one-sentence problem}. {why it matters}.
  ```{lang}
  {fix snippet}
  ```

## Should fix
- ...

## Nits / FYI
- ...

## Looks good
- {brief positive observation}
```

If there are no critical issues, say so explicitly — don't manufacture
problems to fill the section.

## Anti-patterns

- Reviewing without reading the call sites
- "Consider using X" without explaining why X is better here
- Demanding tests for trivial pure functions
- Flagging style when the issue is correctness
