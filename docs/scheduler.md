# Scheduled tasks

> [简体中文](scheduler_zh.md)

muselab can run a saved prompt on a schedule — a lightweight cron that
lives inside the backend's asyncio loop, no external scheduler needed.
Each run dispatches the full agent loop (tools, MCP, skills) just like an
interactive turn, and the result lands in a bell-badge drawer in the top
bar.

Typical uses: a recurring digest ("summarize anything new in `notes/`
and list open action items"), a periodic check, or any prompt you'd
otherwise re-type on a fixed cadence.

## How it works

- Tasks persist in `<archive>/.muselab/scheduler.json` alongside
  muselab's other sidecar metadata, so they survive a restart.
- On startup the next fire time is recomputed. If the process was down
  through a fire window, the missed run is caught up (staggered to avoid
  a thundering herd when several tasks were missed).
- A finished run increments an **unread** counter shown as a bell badge;
  opening the drawer acknowledges it back to zero.
- If Web Push is configured, a long run also sends a push notification on
  completion, even with the tab closed (see [Mobile](mobile.md)).

## Schedule kinds

| Kind | Fires |
|------|-------|
| `daily` | every day at `hh:mm` — or several times a day via a `times` list |
| `weekly` | on the chosen weekdays (0 = Monday) at `hh:mm` |
| `monthly` | on a day-of-month (1–31) at `hh:mm` |
| `once` | a single `year / month / day` at `hh:mm`, then auto-disables |

The browser supplies its UTC offset (`tz_offset_minutes`) so a task fires
at *your* local time. Legacy tasks saved without an offset fall back to
the server's local timezone.

## Session mode

- **`fresh`** (default) — every run gets a brand-new session, so runs
  never cross-contaminate. Best for digests and one-shot reports.
- **`reuse`** — one session is pre-allocated at task creation and every
  run appends to it, so context accumulates across runs.

## API

All endpoints require the bearer token.

| Method & path | Purpose |
|---|---|
| `GET /api/scheduler/tasks` | list tasks + current unread count |
| `POST /api/scheduler/tasks` | create a task |
| `PATCH /api/scheduler/tasks/{id}` | rename / change time / toggle enabled |
| `DELETE /api/scheduler/tasks/{id}` | remove a task (does **not** delete the bound session) |
| `POST /api/scheduler/tasks/{id}/run` | trigger an out-of-schedule run (retry / smoke-test) |
| `GET /api/scheduler/history` | run log, newest first (`?limit=`, 1–500) |
| `GET /api/scheduler/tasks/{id}/history` | run log for one task |
| `DELETE /api/scheduler/history` | clear all history entries |
| `DELETE /api/scheduler/history/{ts}` | delete one history row by timestamp |
| `POST /api/scheduler/ack` | reset the unread badge to zero |

## Security note

Scheduled runs execute **unattended** with the agent's full permission
set — there is no human present to approve a tool call. Treat a scheduled
prompt with the same caution as an unattended cron job: be deliberate
about prompts that fetch external content (a web page, an inbox), since
injected instructions in that content would run without confirmation.
