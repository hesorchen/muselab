# Non-blocking message delivery

The composer accepts new input independently of HTTP acknowledgement, a running
turn, or an in-flight Stop request. A local submission is not presented as
server-accepted until its acknowledgement is known.

## Contract

- While another submission is settling, additional input is snapshotted into a
  persistent browser outbox before the composer is cleared. Storage failure
  leaves the draft untouched.
- Recovery uses the original session, message ID and payload. An authoritative
  missing receipt permits replay of that same idempotent request; an unresolved
  reservation does not authorize another execution with a fresh ID.
- Unknown delivery remains an item-level state. It cannot lock the composer.
  An existing inline message stays in place during receipt recovery.
- Cancel targets one submission identity. A cancellation tombstone fences a
  delayed POST; if admission already won, only its immutable turn is stopped.
- Stop holds the pre-click queue snapshot together with interruption. Held items
  survive restart but do not block later manual input or consume runnable queue
  capacity. Resume releases explicit holds without replaying failed/uncertain
  execution records.
- Late Stop requests cannot pause or interrupt a successor turn.
- Browser outbox records keep their receipt owner across runtime handoff.

## Regression gates

tests/test_nonblocking_delivery.py covers explicit holds, restart, late POSTs,
idempotent cancellation and stale Stop ownership.
tests/e2e/test_nonblocking_delivery.py covers repeated send, lost acknowledgement,
local cancellation, Stop overlap, persisted recovery and storage failure.
The existing queue, submission, multi-tab and feedback suites remain required.
tests/test_docs.py checks documentation consistency, including the required
English and Chinese document pairs.

Transport receipt recovery is an implementation detail, not a user approval step.
