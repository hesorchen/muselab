# Conversation and settings reliability

Implementation contract:

- A background task alone does not veto input into an owned foreground turn.
- Delivery feedback reflects the server's actual admission decision.
- Submitted content has a stable identity across admission, queue and history.
- Idle sends paint one inline user bubble immediately; a private pending receipt
  must not appear as a queue card. Only genuinely waiting work uses the outbox.
- Exact queue ownership follows active/mux state so late admission or thumbnail
  responses cannot restore an already-running item to the outbox.
- Stop/cancel targets an exact request or turn; it never cancels a successor.
- Completed history recovers without a page refresh and preserves user scroll.
- Memory content loads independently from configuration and diagnostics.
- Memory sorting applies to all matching rows before pagination.
- File references retain the absolute path of their originating workspace.
- Settings separate daily preferences, memory browsing, maintenance and upgrades.
- Settings loading/error rows remain inside the scroll clip. Timeouts use an
  actionable message; superseded opens cannot overwrite the current result.
- Diagnostic work must not block stream delivery; durable chat data is not dropped.

Regression fixtures must be synthetic and isolated from live sessions, credentials,
memory registries and workspaces. Measure local feedback, server admission, model
latency and final visibility separately. Preserve existing virtualized history and
stable DOM identity protections when changing presentation.

Delivery receipts store a request fingerprint and acknowledgement identifiers,
not prompt text. Reusing an ID cannot execute a second turn. An HTTP disconnect
does not cancel the admission owner. A cancellation during admission remains
pending until its exact turn can be identified. If delivery cannot be established
after a process failure, the UI keeps the content and requires explicit review
before restoring it as a draft; it never automatically resubmits.

```mermaid
sequenceDiagram
    participant B as Browser
    participant API as HTTP handler
    participant O as Admission owner
    participant R as Receipts

    B->>API: POST turn with message ID
    API->>O: hand off to a detached owner
    O->>R: reserve pending, fingerprint only
    API--xB: connection lost
    Note over O: admission continues
    B->>API: cancel this message ID
    API->>R: mark cancel_requested
    O->>R: admission finishes, recorded as cancelled
    O->>O: interrupt the exact admitted turn
    B->>API: query receipt
    API-->>B: cancelled, with its turn ID
    Note over B,R: If a process failure leaves delivery unknown,<br/>the content waits for explicit review and is never resubmitted.
```

Only diagnostic logging and Hook trace persistence use bounded best-effort
workers. Authoritative chat, queue, attachment and receipt commits stay durable.
Context measurements are reused only for the same unchanged SDK generation,
within 20 seconds, below 50 percent including the next prompt estimate, and without
background/scheduled writers. Post-turn control probes have a three-second bound.

A direct send may wait up to three seconds for an exact result-forwarded owner
to release its slot, without presenting internal cleanup as queue activity.
Revalidate successor ownership, background writers, deletion and older FIFO work
after waiting. A still-running reply is not eligible for this cleanup grace.

```mermaid
flowchart TD
    S["Direct send"] --> A{"Current turn<br/>in this session?"}
    A -->|"none"| W{"Background<br/>writer active?"}
    A -->|"reply still running"| BUSY["409 busy"]
    A -->|"answer forwarded,<br/>owner cleaning up"| G["Wait up to 3 s for<br/>that exact owner"]
    G --> F{"Older FIFO work<br/>pending or inflight?"}
    F -->|"yes"| BUSY
    F -->|"no"| RE{"Recheck successor,<br/>writers and deletion"}
    RE -->|"successor or writer"| BUSY
    RE -->|"session deleted"| GONE["404"]
    RE -->|"still clear"| ADMIT["Admit the new turn"]
    W -->|"no"| ADMIT
    W -->|"yes"| BUSY
    BUSY --> CL["Client queues the message;<br/>UI shows the delivery the server chose"]
```
