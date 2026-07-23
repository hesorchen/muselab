# Scheduled tasks

> [中文](scheduler_zh.md)

muselab can run a saved prompt on a schedule. The scheduler lives in the backend asyncio loop and does not require system cron. Each run has the same Agent capabilities as an interactive turn, including tools, MCP servers, and Skills.

Typical uses include periodic note cleanup, recurring reports, and external-state checks. Scheduled prompts run unattended, so save only prompts you are comfortable executing automatically.

## Persistence and notifications

- Tasks, the latest 200 history rows, and the unread count are stored in `$MUSELAB_ROOT/.muselab/scheduler.json`.
- Startup recomputes the next run. The most recent run missed while the service was down is caught up; multiple catch-up jobs are staggered.
- A completed run writes history and increments the unread count. With Web Push configured, completion can also be delivered while the page is closed, without reply content in the notification body.
- Deleting a task does not delete sessions it created or reused.

## Schedule kinds

| Kind | Rule |
|---|---|
| `daily` | Once per day, or up to 24 daily slots through `times` |
| `weekly` | Selected weekdays, where `0` is Monday and `6` is Sunday |
| `monthly` | A day of month; months without that day use their last day |
| `once` | One calendar date, disabled automatically after firing |

## Time zones

New tasks store the browser's IANA time-zone name, such as `Asia/Shanghai` or `America/New_York`. The backend evaluates wall-clock time with the IANA zone, so a “09:00 daily” task stays at 09:00 through daylight-saving transitions.

Compatibility order:

1. Prefer the IANA name in `schedule.tz`.
2. Legacy tasks use the fixed `schedule.tz_offset_minutes`; this is not DST-aware.
3. If neither value is usable, fall back to the server-local time zone.

## Session modes

- `fresh` (default): create a new session for every run; context is isolated.
- `reuse`: bind one session and append every run to its context.

Only one run of a task may execute at a time. A manual run that overlaps a scheduled run is serialized to prevent interleaved output on a shared SDK session.

## API

Every endpoint requires token authentication.

| Method and path | Purpose |
|---|---|
| `GET /api/scheduler/tasks` | List tasks and unread count |
| `POST /api/scheduler/tasks` | Create a task |
| `PATCH /api/scheduler/tasks/{id}` | Edit task, model, schedule, or enabled state |
| `DELETE /api/scheduler/tasks/{id}` | Delete a task without deleting its session |
| `POST /api/scheduler/tasks/{id}/run` | Run once now without advancing the schedule |
| `GET /api/scheduler/history` | Read recent run history |
| `GET /api/scheduler/tasks/{id}/history` | Read one task's history |
| `DELETE /api/scheduler/history` | Clear all history |
| `DELETE /api/scheduler/history/{ts}` | Delete one row; `task_id` can disambiguate equal timestamps |
| `POST /api/scheduler/ack` | Reset the unread count |

## Security boundary

Scheduled tasks run unattended with the muselab service user's authority; nobody is present to approve tool calls in real time. External pages, mail, and files can contain prompt injection. Use stricter permissions and isolation for unattended tasks that write, delete, publish, or spend money.
