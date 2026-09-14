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


## SDK transcript ownership

The CLI owns canonical transcript writes for its entire process lifetime. A
ResultMessage releases a turn's receive lane; it does not close the transcript
writer or prove that all file appends have finished. Turn completion must not
rewrite, replace, or sanitize the canonical JSONL. An atomic replacement can
leave a retained SDK descriptor pointing at an unlinked inode, losing later
assistant output even when it was already delivered over SSE. A subsequent
turn's parent UUID can then point at a missing final record.

Keep provider parser compatibility in memory and MuseLab presentation metadata
in sidecars. Unsigned thinking stays in canonical history. The legacy
`fix-thinking-signatures.py` utility is an explicit offline migration only:
stop every writer, preserve a backup, and inspect its heuristic removals first.
Automatic cross-provider transcript sanitization is not part of turn completion.

`test_transcript_writer_ownership.py` holds the SDK append descriptor across
Result, flushes the final answer late, optionally appends a follow-up turn, then
checks the real history endpoint. Both answers and original thinking must remain
visible in order without replacing the SDK's file. Browser coverage in
`test_live_subagent_updates.py` reloads the page after subagent continuations and
checks that the earlier parent final and the continuation suffix still render.

## SDK output ownership

Every pooled client has exactly one SDK reader. A foreground command/turn or a
background watcher owns a bounded receive queue. Releasing that queue transfers
its pending tail under the same barrier as wire delivery, and joins the transfer
even if the caller is repeatedly cancelled. A watcher reserves its queue before
its coroutine is scheduled; the foreground consumer selects that successor
before acknowledging Result to its producer. Startup metadata reads therefore
cannot create a gap in message ownership. Idle messages are consumed in the
originating session: telemetry is observed without retention, while assistant,
tool, and result output uses a durable continuation broadcast. There is no orphan
mailbox for a later human turn to inherit. Direct observers retain the per-message
byte guard; bounded queues still fail explicitly and retire the exact client.

A continuation is not evidence of a scheduled trigger. Native origin establishes
scheduled attribution; an unannounced continuation never acquires a job ID merely
because the session has native schedules. Both kinds retain terminal assistant
identity, tool results, replay, activity completion, and canonical-history refresh.

Native control commands share receive ownership and cancellation cleanup. Cancel
requests interrupt through the SDK and drain to its actual Result before releasing
the lane. An unconfirmed terminal state retires that exact client before reuse.

## Bounded I/O lifecycle

Concurrent identical catalog probes and memory status reads share one producer.
Cancelling one waiter leaves other waiters intact; the final waiter cancels and
joins the producer. Gateway catalog keys include the route, model, and credential
digest. All compatibility requests share one deadline, and cache age starts when
a response completes. A failed probe preserves a bounded-age last known capacity.
An unknown model's generic fallback is display metadata only: it cannot reject
a prompt, force preflight compaction, or set native SDK context-window overrides.
The browser also requires a positive backend `auto_compact_threshold` before
automatically compacting or warning about the limit after a successful turn.
A displayed percentage alone cannot authorize compaction, and failed/cancelled
turns cannot schedule another command from their completion event.
Unavailable capacity metadata therefore leaves the ordinary query and SDK-native
behavior available. Catalog data, explicit overrides, and known model budgets
still control preflight; actual runtime context rejections retain their recovery
path. Native compaction reports separate SDK, measurement, and history phases.

Memory status reads select pending IDs and bounded job summaries through dedicated
indexes, within one read snapshot. Artifact payload size must not affect status
polling. Queue wait, connection resolution, and SQLite execution share a cancellable
deadline. The memory engine owns reusable HTTP connections; each request owns its
credentials and timeout, cookies are disabled, and shutdown joins active users
before closing the transport. Timings expose phases, never URLs or payloads.

A detached filesystem scan may overlap watcher events. A complete cursor interval
and bounded native mutation journal let the commit preserve newer indexed paths
and apply unrelated scan observations. Directory add/delete protects descendants;
directory modification protects only its own metadata. Missing/pruned history or
a changed lifecycle rejects the scan. One reconciliation call performs one scan;
the lifecycle scheduler owns retry timing instead of immediately repeating walks.

The root SSE mux subscribes at admission but serializes delivery within each
session. It sends the predecessor's terminal before the successor's state and
replay, including turns discovered by periodic reconciliation. A delivered
terminal releases this wire ownership without waiting for slow post-turn
bookkeeping. Watcher-only state obeys the same boundary. Sessions retain
independent pumps, and pending successor subscribers are disposed on disconnect.
Real HTTP/SSE browser coverage keeps one transcript selected across Agent bursts
and rapid continuations, then checks its final suffix against indexed history.

## Evidence and diagnostics

Regressions cover long idle output, cancellation-safe ordered handoff, native
command cancellation, account-isolated catalog sharing, payload-free status reads,
cancelled SQLite execution, HTTP reuse/cleanup, and scans during continuous native
changes. See `test_idle_sdk_delivery.py`, `test_shared_calls.py`,
`test_gateway_catalog_concurrency.py`, `test_memory_status_reads.py`,
`test_memory_http_transport.py`, and `test_reconcile_rebase.py`.

Alpine may rethrow an expression error from its vendor bundle. The vendor line
alone does not locate the original expression. Browser error telemetry includes
a digest of the attached expression, alongside reason/stack digests and owned
asset revision; raw expressions and stacks remain only in the local error ring.
The browser suite exercises a real Alpine `undefined.length` error to verify this
boundary. A historical stack fingerprint alone does not prove its source or fix.
