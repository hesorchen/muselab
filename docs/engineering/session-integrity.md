# Session integrity and latency checks

A quiet history refresh is still a destructive UI replacement. Before installing
it, check the server's completion stability, the last observed update time, and
the committed final message boundary within the same history generation. Compare
counts against installed canonical history, not provisional streaming bubbles.
Compaction and explicit full-history navigation may legitimately change the window.
A rejected response leaves the displayed messages intact and pending reconciliation.

Native schedules follow the Claude Agent SDK contract. MuseLab stores observation
receipts; it does not interpret cron expressions, recreate tasks, redirect fires,
or change native persistence and expiration. Successful creation establishes the
receipt owner. A later CronList result or replayed creation cannot transfer an
existing job to another session. Startup loads all receipts before deciding which
session to resume, removing legacy list-only copies before recovery starts.

SDK `origin.kind=task-notification` with `subkind=scheduled-trigger` remains
valid even when MuseLab missed the original creation. Do not reject a native fire
merely because its local receipt is absent. `senderTaskId` identifies peer agents,
not cron jobs. For older runtimes that omit origin, exact prompt matching is only
a fallback for a live task in the receiving session outside a foreground turn.
The public contract is described in [Claude Code scheduled tasks](https://code.claude.com/docs/en/scheduled-tasks)
and the pinned Python SDK's `MessageOrigin` type documentation.

For startup latency, prepare full memory recall concurrently with native context
preflight. Both must finish before query commit; cancellation or early failure
must join the recall producer before clearing its receipt. Keep the configured
recall timeout, result limit, and native context/compaction behavior intact.
Capability reads must not prepare CLI configuration directories. Bounded memory
extraction uses the native output-token budget and disabled thinking.

Repeated task observations that only change `updated_at` should not rewrite a
sidecar. Failed expensive filesystem scans use a bounded retry delay at least as
large as the preceding pass, capped at 30 seconds; healthy partial continuations
keep their short yield. The logged retry duration is the actual retry deadline.
`sessions.rename_index` separates index lock, read, and write time without logging
session names, paths, or contents. These timings distinguish local filesystem
latency from SDK/network latency in a deployment that cannot be inspected directly.

Focused regressions are in `test_history_snapshot_integrity.py`,
`test_completion_visibility.py`, `test_native_cron_reliability.py`, and
`test_turn_startup_overlap.py`. Browser coverage requires `RUN_E2E=1`; always use a
temporary `MUSELAB_ROOT` and a synthetic authentication token for local tests.
