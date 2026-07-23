# Model routing and conversation turn loop

> [中文](routing_zh.md)

This page covers model selection, the SDK client pool, third-party provider environment isolation, and how an SSE turn starts, survives in the background, and reconnects.

## Model resolution and locking

A new session resolves its model in this order:

1. A requested model that is currently available.
2. The model in `MUSELAB_MODEL`, if currently available.
3. The first model of the first available provider.
4. Empty when no provider is configured, allowing the UI to guide setup.

After the first turn starts, the session stores the actual model. Later turns prefer that locked model over the current picker value so thinking signatures produced by one provider are not replayed to an incompatible endpoint.

An empty session with no CLI JSONL may heal to the current default when its original provider is unavailable. A session with real history never moves across providers automatically.

## SDK client pool

Clients are cached by `(session_id, model, effort)`. The default live-client cap is 3 and can be changed with `MUSELAB_CLIENT_POOL_CAP`.

- A per-key lock collapses concurrent cache misses.
- Cache hits refresh LRU order.
- Permission mode is an SDK-process launch contract. A mismatch rebuilds the runtime at a safe boundary instead of silently retaining old authority.
- Clients with active turns or SDK background tasks are not evicted; the pool may temporarily exceed its cap.
- Model, effort, thinking, or permission changes rebuild the session runtime at the next safe point.
- With external MCP servers configured, the first connection waits for a stable tool set before starting.

Interactive turns, scheduled tasks, native compact, and other SDK operations for one session share a serialization lock so they cannot consume the same CLI stream concurrently.

## Third-party provider environment

Anthropic uses the normal Claude CLI login or key environment. Other providers receive a minimal full environment replacement:

- process, locale, proxy, and TLS variables required for execution;
- the selected provider's base URL and key;
- cleared Claude OAuth fallback variables;
- a per-OS-user isolated temporary `CLAUDE_CONFIG_DIR`;
- no `MUSELAB_TOKEN` and no keys for other providers.

This supports Anthropic-compatible endpoints while reducing credential crossover and silent fallback to Anthropic.

## Starting an SSE turn

New clients should use a one-time ticket:

```text
POST /api/chat/stream/start
X-Auth-Token: <token>
Content-Type: application/json

{
  "prompt": "...",
  "session_id": "...",
  "model": "...",
  "permission": "default",
  "image_ids": "a1,b2",
  "mobile": false
}
```

After receiving `{"ticket":"..."}`, connect to:

```text
GET /api/chat/stream?ticket=<single-use-ticket>
```

The ticket expires after 60 seconds and is destroyed on first use. The prompt, attachment parameters, and long-lived token stay out of the SSE URL. Query-token streaming remains only as a legacy compatibility path.

An empty prompt without attachments subscribes to an existing turn. An empty prompt with attachments still starts a new turn.

## Turn broadcast and replay spool

An independent background task drives each turn; the HTTP SSE connection is only a subscriber. Closing the browser does not cancel the Agent. The backend continues consuming SDK output, writing the session, and updating the Activity Center.

Each `TurnBroadcast` appends events to a JSONL replay spool in the operating system's temporary directory. Subscribers hold independent file cursors, so a slow connection does not create an unbounded Python queue or duplicate the full event list per browser.

On connection or reconnection:

1. Replay events already written to the spool.
2. Tail the same file for new events.
3. Stop at the turn-completion marker.

Desktop subscribers retain full replay behavior. If a mobile replay exceeds `MUSELAB_STREAM_REPLAY_MAX_EVENTS` (512 by default) or `MUSELAB_STREAM_REPLAY_MAX_BYTES` (2 MiB by default), it receives `resync` and reloads the persisted session instead of processing an expensive replay.

Active turns live in an in-memory registry. A just-finished turn remains available for 60 seconds by default, configurable with `MUSELAB_RECENT_TURN_TTL`, so a browser can still attach to a fast queue-drained turn. Expiry closes and deletes its temporary spool.

A background turn has a hard 30-minute timeout. Explicit interruption marks it cancelled and pauses a non-empty server queue.

## Reconnection and cross-device state

- `GET /api/chat/sessions/{sid}/active` reports the current turn, event count, and continuation information.
- The session list's `active` field exposes real server state to other devices.
- After reload, the frontend polls for activity and reconnects using an empty-prompt ticket to receive replay plus the live tail.
- If the service process is killed, a disk sentinel produces an interrupted-turn notice after restart. Temporary replay spools are not cross-process recovery storage.

The Activity Center persists each conversation's current state in `$MUSELAB_ROOT/.muselab/activity.json`. It provides cross-workspace running, waiting, and completion status; it does not replace the CLI transcript.

## Main SSE events

| Event | Meaning |
|---|---|
| `text`, `thinking` | Text and reasoning deltas |
| `tool_use`, `tool_result` | Tool request and result |
| `task_started`, `task_progress`, `task_notification` | SDK background-task lifecycle |
| `rate_limit` | Provider or subscription quota state |
| `ask_user_question` | Agent is waiting for an answer |
| `permission_request` | Agent is waiting for tool approval |
| `resync` | Mobile replay is too large; reload persisted session |
| `done` | Success with timing, cost, and token summary |
| `cancelled` | Explicit user interruption |
| `error` | Authentication, quota, network, cross-provider, session, or SDK failure |

## Effort and thinking

- `effort` is stored per session and participates in the client cache key. Values are `low`, `medium`, `high`, `xhigh`, and `max`; empty uses the SDK default.
- The extended-thinking budget defaults to 10,000 tokens and is configurable with `MUSELAB_THINKING_BUDGET`.
- Provider capabilities for effort and thinking come from `/api/chat/providers`.

```mermaid
sequenceDiagram
    participant B as Browser
    participant API as muselab
    participant SDK as Agent SDK / CLI
    participant V as Provider

    B->>API: POST /stream/start (header + JSON)
    API-->>B: single-use ticket
    B->>API: GET /stream?ticket=...
    API->>SDK: start or resume session turn
    API->>API: background task writes replay spool
    SDK->>V: model and tool requests
    V-->>SDK: streaming response
    SDK-->>API: SDK messages
    API-->>B: SSE events
    Note over B,API: browser disconnects; turn continues
    B->>API: new ticket + empty-prompt reconnect
    API-->>B: spool replay + live tail
    API-->>B: done / cancelled / error
```
