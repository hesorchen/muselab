import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Body, Depends, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from .auth import require_token
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from .files import router as files_router
from .chat import RuntimeCleanupTimeout, router as chat_router
from .api_settings import router as settings_router
from .api_memory import router as memory_router
from .api_scheduler import router as scheduler_router
from .scheduler import SchedulerPersistenceError
from .api_push import router as push_router
from .workspaces import router as workspaces_router
from .activity_api import router as activity_router
from .terminal import router as terminal_router
from .file_events import router as file_events_router
from .todos_api import router as todos_router
from .settings import ROOT, PORT, HOST
from .version import project_version
from .observability import (
    elapsed_ms as _perf_elapsed_ms,
    is_slow as _perf_is_slow,
    monotonic as _perf_monotonic,
    perf_enabled as _perf_enabled,
    perf_event,
)


class _TokenFilter(logging.Filter):
    """Make Uvicorn access logs low-volume and safe for local archives.

    Uvicorn receives the concrete request target, not FastAPI's route
    template.  Logging it verbatim exposes every query value and stable
    session UUID.  The privacy-safe slow/error ASGI middleware below replaces
    raw access logging, so every Uvicorn access record is dropped.  The target
    sanitizer remains here as a fail-closed regression surface and for any
    future explicitly opted-in access sink.
    """
    _muselab_access_filter = True

    _uuid_re = re.compile(
        r"(?i)(?<![0-9a-f])(?:"
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32}"
        r")(?![0-9a-f])"
    )
    _safe_query_name_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
    _known_query_names = frozenset({
        "around_uuid", "cursor", "direction", "full", "ids", "image_ids",
        "limit", "mobile", "model", "offset", "path", "permission",
        "preview", "q", "root", "session_id", "show_hidden", "tail",
        "ticket", "token", "turn_id", "v", "workspace",
    })

    @classmethod
    def _safe_target(cls, target: str) -> str:
        path, separator, raw_query = target.partition("?")
        safe_path = cls._uuid_re.sub(":id", path)
        if not separator:
            return safe_path
        safe_parts: list[str] = []
        for part in raw_query.split("&"):
            raw_name = part.partition("=")[0]
            name = (
                raw_name
                if (cls._safe_query_name_re.fullmatch(raw_name)
                    and raw_name in cls._known_query_names)
                else "param"
            )
            safe_parts.append(f"{name}=***")
        return f"{safe_path}?{'&'.join(safe_parts)}"

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn's access logger calls info("%s - \"%s %s HTTP/%s\" %d", ...)
        # so the token-bearing URL lives in record.args, NOT record.msg (which
        # is just the format template). The token sits in the URL/path, i.e.
        # args[2].
        #
        # Normalize the structured tuple before suppressing the raw access
        # record. This keeps the filter fail-safe if another handler inspects
        # the record, while privacy-safe slow/error summaries are emitted by
        # the HTTP observability middleware instead.
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            full_path = args[2]
            if isinstance(full_path, str):
                record.args = (
                    args[0], args[1],
                    self._safe_target(full_path),
                    args[3], args[4],
                )
            return False
        # A future Uvicorn format change must fail closed.  Free-form access
        # messages can contain a prompt-bearing legacy URL, so do not attempt a
        # best-effort scrub that might miss an unusual query encoding.
        record.msg = "access event suppressed: unsupported log shape"
        record.args = ()
        return False


def _install_access_log_filter() -> None:
    """Replace MuseLab's process-global filter across module reloads.

    The test app intentionally reloads backend.main for filesystem isolation.
    Logging keeps filters globally, so blindly adding one per import retained
    every previous FastAPI module graph until interpreter shutdown.
    """
    logger = logging.getLogger("uvicorn.access")
    for installed in tuple(logger.filters):
        if getattr(installed, "_muselab_access_filter", False):
            logger.removeFilter(installed)
    logger.addFilter(_TokenFilter())


_install_access_log_filter()
_CLIENT_ERROR_LOG = logging.getLogger("muselab.client")

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

# Strong references to long-lived fire-and-forget startup tasks so the
# event loop's weak task references don't let them be GC'd mid-run.
_BG_TASKS: set = set()
GRACEFUL_SHUTDOWN_TIMEOUT = 3
_LOOP_LAG_INTERVAL_S = 1.0
_LOOP_LAG_THRESHOLD_MS = 250
_LOOP_STALL_THRESHOLD_S = 5.0
_LOOP_STALL_RATE_LIMIT_S = 60.0


def _bounded_env_float(name: str, default: float,
                       minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not minimum <= value <= maximum:
        value = default
    return value


class _EventLoopStallWatchdog:
    """Attribute severe loop stalls from a daemon thread without user data.

    The watchdog records only the blocked frame's module and function names.
    It never formats locals, source lines, absolute paths, exception text, or
    request/session identifiers.
    """

    def __init__(self, loop_thread_id: int, heartbeat_at: float, *,
                 threshold_s: float, rate_limit_s: float, poll_s: float):
        self._loop_thread_id = loop_thread_id
        self._heartbeat_at = heartbeat_at
        self._threshold_s = threshold_s
        self._rate_limit_s = rate_limit_s
        self._poll_s = poll_s
        self._last_report_at: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="muselab-loop-watchdog",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def heartbeat(self, observed_at: float) -> None:
        self._heartbeat_at = observed_at

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=min(1.0, self._poll_s * 2 + 0.05))

    def _blocked_site(self) -> str:
        frame = sys._current_frames().get(self._loop_thread_id)
        if frame is None:
            return "unknown"
        module = str(frame.f_globals.get("__name__", "unknown"))
        function = str(frame.f_code.co_name or "unknown")
        safe_module = re.sub(r"[^A-Za-z0-9_.-]", "_", module)[:80]
        safe_function = re.sub(r"[^A-Za-z0-9_.-]", "_", function)[:60]
        return f"{safe_module}:{safe_function}"

    def _check_once(self, observed_at: float) -> None:
        lag_s = max(0.0, observed_at - self._heartbeat_at)
        if lag_s < self._threshold_s:
            return
        if (self._last_report_at is not None
                and observed_at - self._last_report_at < self._rate_limit_s):
            return
        self._last_report_at = observed_at
        try:
            perf_event(
                "runtime.loop_stall",
                lag_ms=round(lag_s * 1000),
                site=self._blocked_site(),
            )
        except Exception:
            # Diagnostics must never terminate the watchdog thread.
            pass

    def _run(self) -> None:
        while not self._stop.wait(self._poll_s):
            self._check_once(_perf_monotonic())


_ASSET_VERSION_CANDIDATES = tuple(sorted(
    path for path in FRONTEND.rglob("*")
    if path.is_file()
))
# Cache the computed version by the newest frontend mtime. Every frontend file
# participates so editing a split module, locale, or vendored dependency cannot
# leave stale immutable assets behind.
_asset_cache: dict[str, object] = {"mtime": None, "version": "0",
                                   "index_html": None, "manifest": None}
_asset_cache_lock = threading.Lock()


def _max_asset_mtime() -> int:
    # Missing files can disappear during a deployment; ignore them while the
    # directory is being replaced instead of failing the HTML request.
    mtimes = []
    for p in _ASSET_VERSION_CANDIDATES:
        try:
            mtimes.append(p.stat().st_mtime_ns)
        except OSError:
            continue
    return max(mtimes) if mtimes else 0


def _asset_version() -> str:
    """One version stamp shared across every /static URL the HTML emits.
    Built from the largest mtime among the files most likely to change on a
    deploy (app.js / index.html / styles.css). When ANY of them change the
    stamp bumps, every HTML-emitted /static URL changes, and browsers refetch
    everything fresh — even though we still ask them to cache /static
    aggressively (one year + immutable). Cached by mtime (see _asset_cache)."""
    mt = _max_asset_mtime()
    with _asset_cache_lock:
        if _asset_cache["mtime"] != mt:
            _asset_cache["mtime"] = mt
            _asset_cache["version"] = str(mt // 1_000_000)  # ms granularity
            _asset_cache["index_html"] = None  # invalidate rendered HTML
            _asset_cache["manifest"] = None    # invalidate rendered manifest
        return _asset_cache["version"]  # type: ignore[return-value]


async def _start_optional_services(scheduler, push, memory) -> None:
    """Start peripheral services without making chat availability depend on them."""
    import traceback

    memory.start()
    try:
        push.init()
    except Exception as exc:
        sys.stderr.write(
            f"[muselab] push init failed (continuing without push): "
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}\n"
        )
        sys.stderr.flush()
    try:
        await scheduler.start_scheduler()
    except Exception as exc:
        sys.stderr.write(
            f"[muselab] scheduler start failed (continuing without scheduler): "
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}\n"
        )
        sys.stderr.flush()


async def _start_workspace_index(file_watch_manager) -> bool:
    """Start the optional file index without taking chat/terminal down."""
    try:
        await file_watch_manager.start()
    except Exception as exc:
        sys.stderr.write(
            "[muselab] workspace index start failed "
            f"(continuing with chat/terminal): {exc}\n"
        )
        sys.stderr.flush()
        return False
    return True


def _launch_background_tasks(coroutines) -> None:
    import asyncio

    for coroutine in coroutines:
        task = asyncio.create_task(coroutine)
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)


async def _monitor_event_loop_lag() -> None:
    """Report loop lag and let a thread attribute severe stalls while blocked."""
    if not _perf_enabled():
        return
    import asyncio

    interval_s = _bounded_env_float(
        "MUSELAB_LOOP_HEARTBEAT_MS", _LOOP_LAG_INTERVAL_S * 1000, 50, 60_000
    ) / 1000
    warning_ms = _bounded_env_float(
        "MUSELAB_LOOP_LAG_WARN_MS", _LOOP_LAG_THRESHOLD_MS, 25, 60_000
    )
    stall_s = max(
        interval_s * 2,
        _bounded_env_float(
            "MUSELAB_LOOP_STALL_MS", _LOOP_STALL_THRESHOLD_S * 1000,
            1000, 300_000,
        ) / 1000,
    )
    rate_limit_s = _bounded_env_float(
        "MUSELAB_LOOP_STALL_RATE_LIMIT_S", _LOOP_STALL_RATE_LIMIT_S, 1, 3600
    )
    observed = _perf_monotonic()
    watchdog = _EventLoopStallWatchdog(
        threading.get_ident(),
        observed,
        threshold_s=stall_s,
        rate_limit_s=rate_limit_s,
        poll_s=min(interval_s, max(0.05, stall_s / 4)),
    )
    watchdog.start()
    expected = observed + interval_s
    try:
        while True:
            await asyncio.sleep(interval_s)
            observed = _perf_monotonic()
            watchdog.heartbeat(observed)
            lag_ms = max(0, round((observed - expected) * 1000))
            if lag_ms >= warning_ms:
                try:
                    perf_event(
                        "runtime.loop_lag",
                        site="event_loop",
                        session="none",
                        duration_ms=lag_ms,
                        file_size=0,
                        lag_ms=lag_ms,
                    )
                except Exception:
                    # Diagnostics must never terminate their own long-lived monitor.
                    pass
            # Reset after a stall so one pause creates one event rather than a
            # permanent positive offset on every later healthy tick.
            expected = observed + interval_s
    finally:
        watchdog.stop()


async def _recover_message_queues_at_startup(session_store) -> int:
    """Restore durable claims and leave every surviving queue paused.

    Kept as a small helper so the startup safety boundary is regression-testable
    without booting schedulers, terminals, or file watchers.
    """
    import asyncio

    recovered = 0
    failures = 0
    for sid in session_store.list_queue_session_ids():
        try:
            queue = await asyncio.to_thread(
                session_store.recover_queue_inflight, sid)
        except Exception as exc:
            # Continue the sweep so one unwritable/corrupt sidecar cannot
            # leave every later queue live.  We still fail startup after the
            # sweep: serving requests with even one queue whose pause was not
            # durably committed would re-open automatic draining.
            failures += 1
            sys.stderr.write(
                f"[muselab] queue recovery failed sid={sid[:8]} "
                f"exc={type(exc).__name__}\n")
            sys.stderr.flush()
            continue
        if queue.get("items"):
            recovered += 1
    if failures:
        raise RuntimeError(
            f"could not safely recover {failures} message queue(s)"
        ) from None
    return recovered


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Boot the in-process scheduler + push subsystem on startup.
    Uses the modern lifespan context manager — `@app.on_event("startup")`
    is deprecated and emits a warning on every server restart.

    Each subsystem is guarded so a single failure (e.g. push VAPID
    generation hitting a disk-quota error) doesn't take down the
    whole web server — the chat UI is the primary capability and
    must come up even if peripheral subsystems are degraded."""
    import asyncio as _asyncio

    from . import chat as _chat
    from . import files as _files
    from . import sessions as _sess
    from .activity import activity as _activity
    from .todos import todos as _todos
    from . import scheduler as _sched
    from . import push as _push
    from . import memory_client as _mem0
    from .workspaces import registry as _workspace_registry
    # Historical releases inherited the process umask for internal state.
    # Repair permissions only once test fixtures and the workspace registry
    # have selected their final runtime paths.
    private_roots = {ROOT, *_workspace_registry.paths()}
    await _asyncio.gather(
        _asyncio.to_thread(_sess.ensure_private_session_storage),
        _asyncio.to_thread(_chat.ensure_private_attachment_storage),
        *(
            _asyncio.to_thread(
                _files.ensure_private_trash_storage, root, create=False)
            for root in private_roots
        ),
        _asyncio.to_thread(_activity.initialize_runtime_state),
        _asyncio.to_thread(_todos.initialize_runtime_state),
    )
    # Older releases allowed a successor CLI's synthetic ``stopped`` record to
    # overwrite the predecessor's real terminal state. Repair each runtime
    # chain from its oldest owner before applying restart recovery, so a true
    # completed task is never relabelled stopped merely because the process
    # restarted later.
    repaired_runtime_tasks = await _asyncio.to_thread(
        _sess.reconcile_runtime_task_overlay_chains)
    if repaired_runtime_tasks:
        sys.stderr.write(
            f"[muselab] repaired {repaired_runtime_tasks} inherited runtime "
            "task overlay(s) on startup\n")
        sys.stderr.flush()

    # Runtime-rollover task cards are durable UI overlays, but their owning
    # CLI process and watcher are intentionally process-local.  After a
    # service restart there is therefore no legitimate way for a persisted
    # ``running`` overlay to still be alive.  Settle those rows before serving
    # requests so a successor tab cannot poll forever for an owner that no
    # longer exists.
    stale_runtime_tasks = await _asyncio.to_thread(
        _sess.stop_stale_runtime_task_overlays)
    if stale_runtime_tasks:
        sys.stderr.write(
            f"[muselab] stopped {stale_runtime_tasks} stale runtime task "
            "overlay(s) on startup\n")
        sys.stderr.flush()
    try:
        # Run the fail-closed queue sweep before starting any optional service.
        # In particular, scheduler catch-up can launch work immediately; it
        # must never overlap an incomplete queue reconciliation.
        recovered = await _recover_message_queues_at_startup(_sess)
        await _asyncio.to_thread(
            _chat.recover_durable_queue_attachments_at_startup,
            _sess,
        )
    except Exception as exc:
        sys.stderr.write(
            "[muselab] queue recovery incomplete; refusing startup "
            f"exc={type(exc).__name__}\n")
        sys.stderr.flush()
        raise RuntimeError(
            "message queue recovery was not durably completed"
        ) from None
    if recovered:
        sys.stderr.write(
            f"[muselab] recovered {recovered} paused message queue(s) "
            "on startup\n")
        sys.stderr.flush()
    # A hidden background-task owner can finish its Agent continuation just
    # before the process exits.  The private READY outbox survives that crash;
    # resume its presentation-only delivery to the latest visible successor.
    # Scheduling is non-blocking, and queue recovery above has already paused
    # every stale claim so no restarted turn can race ahead of the projection.
    from . import chat as _chat
    recovered_continuations = await (
        _chat.recover_runtime_continuation_outboxes_at_startup()
    )
    if recovered_continuations:
        sys.stderr.write(
            f"[muselab] resumed {recovered_continuations} pending runtime "
            "continuation delivery task(s) on startup\n"
        )
        sys.stderr.flush()
    await _start_optional_services(_sched, _push, _mem0)
    # Prune empty sessions + auto-purge expired trash. Both used to block
    # lifespan before yield (50-300 ms total on archives with many
    # sessions / a populated trash dir), pushing first-request TTFB out.
    # Moved to background tasks (2026-05-28) — neither is user-visible at
    # boot: a stray empty session in the list for ~1s, or a couple of
    # >30-day trash items not yet cleaned, are both no-ops from the user's
    # POV. `asyncio.to_thread` runs the sync IO off the event loop so a
    # slow disk doesn't stall concurrent requests either.
    async def _bg_prune_sessions() -> None:
        try:
            from . import sessions as _sess_mod
            pruned = await _asyncio.to_thread(_sess_mod.prune_empty_sessions)
            if pruned:
                sys.stderr.write(
                    f"[muselab] pruned {len(pruned)} empty session(s) on startup\n")
                sys.stderr.flush()
        except Exception as _e:
            sys.stderr.write(f"[muselab] startup prune failed (non-fatal): {_e}\n")
            sys.stderr.flush()

    async def _bg_purge_trash() -> None:
        try:
            from . import files as _files_mod
            from .workspaces import registry as _workspace_registry
            purged = 0
            for root in _workspace_registry.paths():
                purged += await _asyncio.to_thread(
                    _files_mod.auto_purge_expired_trash, root)
            if purged:
                sys.stderr.write(
                    f"[muselab] auto-purged {purged} expired trash item(s) "
                    f"(> {_files_mod._TRASH_TTL_DAYS}d old)\n")
                sys.stderr.flush()
        except Exception as _e:
            sys.stderr.write(
                f"[muselab] trash auto-purge failed (non-fatal): {_e}\n")
            sys.stderr.flush()

    async def _bg_warm_versions() -> None:
        # Version detection runs a `claude --version` subprocess (up to 3s).
        # It used to run at import time (`_VERSIONS = _detect_versions()`),
        # blocking module load — and thus uvicorn cold start — for up to 3s.
        # Now lru_cache'd + warmed here off the event loop, so import is
        # unblocked and the first /api/meta is instant. (perf: RED —
        # main.py _detect_versions import-time block)
        try:
            v = await _asyncio.to_thread(_detect_versions)
            print(f"[muselab] versions: muselab={v['muselab_version']} "
                  f"sdk={v['sdk_version']} cli={v['cli_version']} "
                  f"py={v['python_version']}",
                  file=sys.stderr, flush=True)
        except Exception as _e:
            sys.stderr.write(
                f"[muselab] version detect failed (non-fatal): {_e}\n")
            sys.stderr.flush()

    # Keep strong references to fire-and-forget tasks. asyncio only holds a
    # weak reference to a task, so a bare `create_task(...)` whose result is
    # discarded can be garbage-collected mid-run, silently cancelling the
    # background work. Stash them on a module-level set and drop each one
    # when it finishes so the set doesn't grow unbounded.
    _launch_background_tasks((
        _monitor_event_loop_lag(),
        _bg_prune_sessions(),
        _bg_purge_trash(),
        _bg_warm_versions(),
        _backfill_turn_counts(),
    ))
    # Same fire-and-forget pattern: rewrite turn_count for any session
    # written by the old algorithm. Gated by a sentinel file so reruns
    # are cheap; first run can take a few seconds on archives with
    # hundreds of sessions.
    from .terminal import manager as _terminal_manager
    from .file_events import manager as _file_watch_manager
    await _start_workspace_index(_file_watch_manager)
    await _terminal_manager.start()
    try:
        yield
    finally:
        from .runtime_lifecycle import shutdown_runtime
        await shutdown_runtime(
            _BG_TASKS,
            scheduler=_sched,
            memory=_mem0,
            terminal=_terminal_manager,
            file_watcher=_file_watch_manager,
        )


async def _backfill_turn_counts() -> None:
    """One-shot migration: rewalk each session's JSONL via the SDK and
    rewrite turn_count using the correct (real-prompt-only) filter.

    Gated by a sentinel file under sessions/ so we don't re-scan every
    JSONL on every restart (was adding noticeable boot latency on
    archives with hundreds of sessions). To force a re-run after an SDK
    upgrade that changes `_is_real_user_prompt` semantics, delete
    `sessions/.backfill_done`.
    """
    import asyncio as _asyncio
    from . import sessions as _sess
    from . import chat as _chat
    from .settings import ROOT as _ROOT
    sentinel = _sess.SESS_DIR / ".backfill_done"
    if sentinel.exists():
        return
    try:
        from claude_agent_sdk import get_session_messages as _gsm
    except Exception:
        return
    if _ROOT is None:
        return
    try:
        ss = await _asyncio.to_thread(_sess.list_sessions)
    except Exception as e:
        sys.stderr.write(f"[muselab] backfill list_sessions failed: {e}\n")
        return

    def _load_counts(sid: str) -> tuple[int, int]:
        msgs = _gsm(sid, directory=str(_sess.session_workspace(sid)))
        return len(msgs), sum(
            1 for sm in msgs if _chat._is_real_user_prompt(sm)
        )

    updated = 0
    for s in ss:
        sid = s.get("id")
        if not sid:
            continue
        try:
            message_count, n_turns = await _asyncio.to_thread(_load_counts, sid)
        except Exception:
            continue
        cur = s.get("turn_count")
        if cur == n_turns:
            continue
        try:
            await _asyncio.to_thread(
                _sess.bump_session,
                sid,
                message_count=message_count,
                turn_count=n_turns,
            )
            updated += 1
        except Exception:
            pass
    if updated:
        sys.stderr.write(
            f"[muselab] backfilled turn_count for {updated} sessions\n")
        sys.stderr.flush()
    # Drop sentinel even when 0 sessions needed updating — that just means
    # the archive is already correct; no reason to keep rescanning.
    try:
        sentinel.touch()
    except OSError as e:
        sys.stderr.write(f"[muselab] backfill sentinel write failed: {e}\n")


app = FastAPI(title="muselab", version=project_version(), lifespan=_lifespan)


@app.exception_handler(SchedulerPersistenceError)
async def scheduler_persistence_error_handler(
    _request: Request,
    exc: SchedulerPersistenceError,
) -> JSONResponse:
    """Expose durable-state degradation without leaking filesystem paths."""
    return JSONResponse(
        status_code=503,
        content={
            "detail": str(exc),
            "code": "scheduler_persistence_unavailable",
            "degraded": True,
        },
    )


@app.exception_handler(RuntimeCleanupTimeout)
async def runtime_cleanup_timeout_handler(
    _request: Request,
    _exc: RuntimeCleanupTimeout,
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": "runtime cleanup is still in progress; retry shortly",
            "code": "runtime_cleanup_pending",
        },
    )

# Gzip every response ≥1KB. The frontend ships ~1.2MB of uncompressed text
# assets (app.js / index.html / styles.css) plus JSON-heavy API responses
# (cost-dashboard / settings / skills) — all highly compressible (~75-80%).
# This is the single biggest cold-load TTI win and costs one line.
# SSE bodies are not compressed: chat.py's EventSourceResponse sets
# `Content-Encoding: identity`. Starlette's responder still retains the
# response start until the first body frame, so every SSE generator must emit
# an immediate handshake event; see chat._subscribe_multiplex.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


class _SecurityHeadersMiddleware:
    """Attach defensive headers without BaseHTTPMiddleware task boundaries.

    Why these three and not a full CSP:
    - `X-Content-Type-Options: nosniff` — prevents browsers from MIME-sniffing
      a `.txt` preview as `text/html` and executing inline scripts. Free.
    - `Referrer-Policy: same-origin` — auth token rides in some query strings
      (SSE / file download — see auth.py docstring). Without this, clicking a
      link from muselab to github.com would leak the full URL (token included)
      via the Referer header. `same-origin` strips Referer on any cross-origin
      navigation. Doesn't break in-app routing.
    - `X-Frame-Options: SAMEORIGIN` — the HTML preview iframe is same-origin
      (served via `/api/files/read`), so this doesn't block it; what it DOES
      block is some external site embedding the muselab UI in a frame to
      phish credentials.

    Deliberately NOT setting:
    - `Content-Security-Policy` — the UI relies on Alpine.js inline directives
      (`x-on:`, `@click`, `:class`) and many inline `<script>` tags. Strict
      CSP would require either nonce-per-request rewrites or eval-script
      allowances; not worth the maintenance for a single-user app.
    - `Strict-Transport-Security` — only meaningful over HTTPS. muselab
      typically runs at 127.0.0.1; HSTS on plaintext localhost would just
      confuse reverse-proxy setups.
    """

    _HEADERS = (
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"same-origin"),
        (b"x-frame-options", b"SAMEORIGIN"),
    )

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or ())
                existing = {name.lower() for name, _ in headers}
                headers.extend(header for header in self._HEADERS
                               if header[0] not in existing)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


app.add_middleware(_SecurityHeadersMiddleware)


def _safe_http_route(scope: Scope) -> str:
    """Return only a code-defined route template, never a concrete URL."""
    route = scope.get("route")
    template = getattr(route, "path", "")
    if isinstance(template, str) and template.startswith("/"):
        return template
    # A 404 has no matched route.  Do not derive a fallback from scope["path"]:
    # it may be a private filename or another user-controlled value.
    return "<unmatched>"


def _emit_http_perf(**fields: object) -> None:
    """Performance diagnostics must never be able to fail an HTTP request."""
    try:
        perf_event("http.request", **fields)
    except Exception:
        pass


class _RequestPerformanceMiddleware:
    """Log only slow or failed HTTP requests using privacy-safe dimensions.

    This is a direct ASGI wrapper rather than ``BaseHTTPMiddleware`` so it
    does not add task boundaries or buffer SSE.  Ordinary responses are timed
    through their final body chunk.  For SSE, the only meaningful HTTP latency
    is time-to-response-headers; the long-lived connection is deliberately not
    reported as a slow request.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _perf_enabled():
            await self.app(scope, receive, send)
            return

        started = _perf_monotonic()
        status_code = 500
        response_started = False
        response_complete = False
        is_sse = False
        emitted = False
        response_bytes = 0
        headers_ms: int | None = None

        def emit_if_needed(phase: str, *, error_kind: str = "") -> None:
            nonlocal emitted
            if emitted:
                return
            duration_ms = (
                headers_ms
                if phase == "handshake" and headers_ms is not None
                else _perf_elapsed_ms(started)
            )
            if (status_code < 400 and not error_kind
                    and not _perf_is_slow(duration_ms)):
                return
            emitted = True
            method = str(scope.get("method") or "UNKNOWN").upper()
            if not re.fullmatch(r"[A-Z]{1,16}", method):
                method = "OTHER"
            scope_state = scope.get("state")
            if not isinstance(scope_state, dict):
                scope_state = {}
            correlation = {
                key: value for key, value in {
                    "sid8": scope_state.get("perf_sid8"),
                    "turn8": scope_state.get("perf_turn8"),
                }.items() if value
            }
            _emit_http_perf(
                method=method,
                route=_safe_http_route(scope),
                **correlation,
                status_code=status_code,
                duration_ms=duration_ms,
                headers_ms=headers_ms,
                response_bytes=response_bytes,
                phase=phase,
                error_kind=error_kind or None,
            )

        async def send_with_timing(message: Message) -> None:
            nonlocal status_code, response_started, response_complete, is_sse
            nonlocal response_bytes, headers_ms
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message.get("status") or 500)
                headers_ms = _perf_elapsed_ms(started)
                for name, value in message.get("headers") or ():
                    if name.lower() == b"content-type":
                        is_sse = value.lower().startswith(b"text/event-stream")
                        break
                if is_sse:
                    emit_if_needed("handshake")
            elif message["type"] == "http.response.body":
                if not is_sse:
                    body = message.get("body", b"")
                    if isinstance(body, (bytes, bytearray, memoryview)):
                        response_bytes += len(body)
                if not message.get("more_body", False):
                    response_complete = True
                    if not is_sse:
                        emit_if_needed("complete")
            await send(message)

        try:
            await self.app(scope, receive, send_with_timing)
            # Defensive ASGI fallback for an app that omits the terminal empty
            # body frame.  Never use it for SSE: that would measure connection
            # lifetime instead of handshake latency.
            if response_started and not response_complete and not is_sse:
                emit_if_needed("complete")
        except Exception as exc:
            # Once an SSE handshake is sent, a later disconnect/stream failure
            # belongs to stream diagnostics, not HTTP request duration.
            if not (response_started and is_sse):
                emit_if_needed("handshake" if not response_started else "complete",
                               error_kind=type(exc).__name__)
            raise


app.add_middleware(_RequestPerformanceMiddleware)


app.include_router(files_router)
app.include_router(file_events_router)
app.include_router(chat_router)
app.include_router(settings_router)
app.include_router(memory_router)
app.include_router(scheduler_router)
app.include_router(push_router)
app.include_router(workspaces_router)
app.include_router(activity_router)
app.include_router(terminal_router)
app.include_router(todos_router)


@functools.lru_cache(maxsize=1)
def _detect_versions() -> dict:
    """Capture muselab + Python + claude-agent-sdk + claude CLI versions
    so the UI can surface "what's actually running" and the upgrade flow has
    something to diff against. Best-effort — missing pieces return None."""
    sdk_version = None
    try:
        from claude_agent_sdk import __version__ as _v
        sdk_version = _v
    except Exception:
        pass
    cli_version = None
    # locate_executable falls back past systemd's minimal PATH (nvm /
    # Volta / ~/.npm-global). shutil.which("claude") alone would miss
    # the most common install locations.
    from .settings import locate_executable
    claude_bin = locate_executable("claude")
    if claude_bin:
        try:
            out = subprocess.run([claude_bin, "--version"], capture_output=True,
                                  text=True, timeout=3)
            cli_version = (out.stdout.strip().splitlines() or [""])[0] or None
        except Exception:
            pass
    return {
        "muselab_version": app.version,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "sdk_version": sdk_version,
        "cli_version": cli_version,
        "cli_present": cli_version is not None,
    }


# Versions are captured lazily via the lru_cache on _detect_versions (warmed
# off the event loop in lifespan startup — see _bg_warm_versions). Capturing
# at import time used to spawn a `claude --version` subprocess that blocked
# module load for up to 3s, delaying uvicorn cold start.

# Surface a privacy-bounded config summary so ops can confirm what the running
# process is actually using — host / port / whether a root override exists /
# which third-party vendors are
# enabled. Helps diagnose "I added DEEPSEEK_API_KEY but the model picker
# still doesn't show it" by making the env-var-vs-process state explicit.
def _startup_config_banner() -> None:
    from . import endpoints as _ep
    host = os.environ.get("MUSELAB_HOST", "127.0.0.1")
    port = os.environ.get("MUSELAB_PORT", "8765")
    enabled = [p.display for p in _ep.catalog()
                 if os.environ.get(p.env_key)]
    enabled_s = ", ".join(enabled) if enabled else "(none — Claude only)"
    root_configured = bool(os.environ.get("MUSELAB_ROOT", "").strip())
    print(f"[muselab] config: host={host} port={port} "
          f"root_configured={str(root_configured).lower()} "
          f"third_party={enabled_s}",
          file=sys.stderr, flush=True)
_startup_config_banner()


# `/static/foo` ↔ `/static/foo?v=N` rewrite. The HTML is generated per-request
# (cheap — one file read) and we append ?v=<asset_version> to every static
# URL so cache-busting happens automatically on each deploy.
_STATIC_REF_RE = re.compile(r'((?:href|src)=")(/static/[^"?#]+)(")')


@app.get("/")
def index() -> HTMLResponse:
    # _asset_version() refreshes the cache (incl. invalidating the rendered
    # HTML) when any frontend file's mtime changed. On the common case
    # (nothing changed) we reuse the memoized render — no disk read, no
    # regex sub — collapsing the per-"/" cost to a single max-mtime stat.
    ver = _asset_version()
    with _asset_cache_lock:
        html = _asset_cache.get("index_html")
    if html is None:
        raw = (FRONTEND / "index.html").read_text(encoding="utf-8")
        html = _STATIC_REF_RE.sub(
            lambda m: f'{m.group(1)}{m.group(2)}?v={ver}{m.group(3)}', raw)
        # Substitute the asset-version placeholder so the loaded HTML can
        # tell, via the <meta name="muselab-asset-version"> tag, which JS
        # bundle it was bootstrapped with. The app.js client compares this
        # against /api/meta.asset_version on visibilitychange and reloads
        # when out-of-date.
        html = html.replace("__MUSELAB_ASSET_VERSION__", ver)
        with _asset_cache_lock:
            _asset_cache["index_html"] = html
    # The HTML itself must never be cached — it embeds the per-deploy
    # version stamps that point at the cacheable static assets.
    return HTMLResponse(
        html,  # type: ignore[arg-type]
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


class _VersionedStaticFiles(StaticFiles):
    """When the request URL carries ?v=… (added by index() above), the asset
    can be treated as content-addressed and cached for a year. Otherwise we
    fall back to no-cache so a direct hit during development still picks up
    fresh content.

    The query-string presence is the marker — its value doesn't matter, since
    a stale ?v=… points at the same on-disk file anyway.

    Large compressible assets (app.js ~900KB, mermaid.min.js ~3.3MB) are
    additionally served from an in-memory gzip cache: GZipMiddleware would
    otherwise re-deflate the same multi-MB file from scratch on every cold
    client (each PWA install / cache-evicted reload), burning tens of ms of
    CPU per request. Compressed once per (path, mtime), capped small — only
    a handful of assets qualify."""

    _GZ_MIN_SIZE = 256 * 1024
    _GZ_EXTS = (".js", ".css", ".json", ".svg", ".webmanifest", ".map")
    _gz_cache: dict[str, tuple[int, int, bytes]] = {}
    _gz_cache_max = 8
    _gz_cache_lock = threading.Lock()
    _gz_locks_guard = threading.Lock()
    _gz_locks_by_loop: weakref.WeakKeyDictionary[
        asyncio.AbstractEventLoop,
        weakref.WeakValueDictionary[str, asyncio.Lock],
    ] = weakref.WeakKeyDictionary()

    @classmethod
    def _gzip_lock(cls, key: str) -> asyncio.Lock:
        """Return a per-asset lock bound to the current request loop.

        Uvicorn normally has one event loop, while TestClient may create a new
        loop per fixture.  Keeping locks per loop avoids sharing an asyncio
        primitive across loops and still coalesces every production cold miss.
        """
        loop = asyncio.get_running_loop()
        with cls._gz_locks_guard:
            locks = cls._gz_locks_by_loop.get(loop)
            if locks is None:
                locks = weakref.WeakValueDictionary()
                cls._gz_locks_by_loop[loop] = locks
            lock = locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                locks[key] = lock
            return lock

    @classmethod
    def _gzip_cache_hit(cls, key: str, mtime_ns: int, size: int):
        with cls._gz_cache_lock:
            hit = cls._gz_cache.get(key)
            if hit is not None and hit[0] == mtime_ns and hit[1] == size:
                return hit
        return None

    @classmethod
    def _store_gzip_cache(
        cls, key: str, mtime_ns: int, size: int, data: bytes,
    ) -> tuple[int, int, bytes]:
        with cls._gz_cache_lock:
            # Another loop may have completed while this loop compressed.  In
            # that rare test/server topology, prefer the already-published
            # value when it represents the same file generation.
            hit = cls._gz_cache.get(key)
            if hit is not None and hit[0] == mtime_ns and hit[1] == size:
                return hit
            if len(cls._gz_cache) >= cls._gz_cache_max and key not in cls._gz_cache:
                cls._gz_cache.pop(next(iter(cls._gz_cache)), None)
            hit = (mtime_ns, size, data)
            cls._gz_cache[key] = hit
            return hit

    @staticmethod
    def _read_and_gzip(
        full: str, expected_mtime_ns: int, expected_size: int,
    ) -> bytes | None:
        """Read and compress one stable file generation off the event loop."""
        import gzip as _gzip

        try:
            before = os.stat(full)
            if (before.st_mtime_ns != expected_mtime_ns
                    or before.st_size != expected_size):
                return None
            raw = Path(full).read_bytes()
            after = os.stat(full)
        except OSError:
            return None
        if (after.st_mtime_ns != before.st_mtime_ns
                or after.st_size != before.st_size):
            return None
        return _gzip.compress(raw, compresslevel=6)

    async def get_response(self, path, scope):
        gz = await self._try_gzip_response(path, scope)
        resp = gz if gz is not None else await super().get_response(path, scope)
        query = scope.get("query_string", b"")
        if b"v=" in query:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    async def _try_gzip_response(self, path, scope):
        from starlette.responses import Response as _Resp
        headers = dict(scope.get("headers") or [])
        ae = headers.get(b"accept-encoding", b"").decode("latin-1")
        if "gzip" not in ae:
            return None
        if not path.lower().endswith(self._GZ_EXTS):
            return None
        try:
            full, st = self.lookup_path(path)
        except Exception:
            return None
        if st is None or not full or st.st_size < self._GZ_MIN_SIZE:
            return None
        # Use the concrete file path, not the request-relative path: multiple
        # app/TestClient instances can otherwise cross-contaminate `app.js`.
        key = full
        hit = self._gzip_cache_hit(key, st.st_mtime_ns, st.st_size)
        if hit is None:
            # Single-flight the whole cold path.  The previous implementation
            # read on the event loop and let every concurrent miss compress the
            # same multi-megabyte asset independently.
            async with self._gzip_lock(key):
                hit = self._gzip_cache_hit(key, st.st_mtime_ns, st.st_size)
                if hit is None:
                    import anyio
                    data = await anyio.to_thread.run_sync(
                        self._read_and_gzip,
                        full,
                        st.st_mtime_ns,
                        st.st_size,
                    )
                    if data is None:
                        return None
                    hit = self._store_gzip_cache(
                        key, st.st_mtime_ns, st.st_size, data,
                    )
        import mimetypes
        mt = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return _Resp(
            content=hit[2],
            media_type=mt,
            headers={
                "Content-Encoding": "gzip",
                "Vary": "Accept-Encoding",
                # Pre-encoded → GZipMiddleware sees Content-Encoding set and
                # skips double compression.
            },
        )


# Dynamic manifest handler — MUST be registered before the /static mount
# so the route matches first. Without this, manifest.webmanifest is served
# as a flat static file with hard-coded icon paths like
# `/static/assets/icon.svg`. PWA installers (Chrome, Edge, Safari) fetch
# icons via those literal URLs and the browser hits whatever it has in
# the favicon / image cache — which on a long-running install can be
# weeks-stale. Injecting `?v=<asset_version>` into every icon src here
# forces a fresh fetch whenever any frontend file changes mtime.
@app.get("/static/assets/manifest.webmanifest")
def manifest_webmanifest():
    import json as _json
    from fastapi.responses import JSONResponse
    # _asset_version() refreshes the cache (incl. invalidating the rendered
    # manifest) when any frontend file's mtime changed. On the common case
    # we reuse the memoized dict — no disk read, no json.loads, no icon loop.
    ver = _asset_version()
    with _asset_cache_lock:
        data = _asset_cache.get("manifest")
    if data is None:
        raw = (FRONTEND / "assets" / "manifest.webmanifest").read_text(encoding="utf-8")
        data = _json.loads(raw)
        for icon in data.get("icons", []) or []:
            src = icon.get("src", "")
            if src and "?" not in src:
                icon["src"] = f"{src}?v={ver}"
        with _asset_cache_lock:
            _asset_cache["manifest"] = data
    return JSONResponse(
        data,
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


app.mount("/static", _VersionedStaticFiles(directory=FRONTEND), name="static")


@app.get("/sw.js")
def service_worker():
    """Service Worker must be served from the same path it controls — if
    we left it at /static/sw.js, the browser would scope it to /static/*
    only and Web Push events for the main app (/) wouldn't fire. Serving
    at /sw.js gives it whole-origin scope automatically."""
    from fastapi.responses import FileResponse
    return FileResponse(
        FRONTEND / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache",
                 "Service-Worker-Allowed": "/"},
    )


@app.get("/robots.txt")
def robots():
    """Tell crawlers to stay out. muselab instances aren't meant to be public;
    if one accidentally is, this is the second line of defense after the
    `<meta name=robots>` tag in index.html."""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        "User-agent: *\nDisallow: /\n",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/meta", dependencies=[Depends(require_token)])
def meta() -> dict:
    # Auth-gated: ROOT is the user's actual filesystem path on disk,
    # which is useful to any attacker on the LAN trying to recon a
    # muselab instance. Defence-in-depth — token is already required
    # for every meaningful endpoint, this just stops drive-by probes
    # from getting the path + SDK / CLI versions for free.
    # `asset_version` matches the ?v=… stamp the index() handler embeds
    # in <link>/<script src> URLs. Clients poll /api/meta (visibilitychange
    # + 10s heartbeat) and compare against the version their HTML was
    # served with — a mismatch means the user's PWA / Safari tab is
    # running stale JS (common when "restart" only resumed a backgrounded
    # tab without re-fetching HTML), and the client should hard-reload.
    from .terminal import ENABLED as terminal_enabled
    return {
        "root": str(ROOT),
        "asset_version": _asset_version(),
        "terminal_enabled": terminal_enabled,
        **_detect_versions(),
    }


@app.get("/api/health")
def health() -> dict:
    """Liveness probe — no auth required. Used by Docker HEALTHCHECK,
    Caddy `health_uri`, k8s readiness probes, and uptime monitors. Stays
    minimal on purpose: any heavier check (e.g. SDK client, archive
    write probe) could itself fail intermittently and cause restarts."""
    return {"status": "ok"}


@app.post("/api/presence", dependencies=[Depends(require_token)])
def presence_heartbeat(payload: dict | None = Body(default=None)) -> dict:
    """Frontend visibility reports. Body (optional, JSON):
      device_id — stable per-device UUID minted by the frontend into
                  localStorage; only used to tell devices apart
      visible   — true on init / 15s keep-alive / refocus,
                  false the moment the page hides (the "I left" signal
                  that lets the push gate fire without waiting out the
                  grace window)
    No body → legacy v1 client → treated as visible on the shared
    "default" device. Gate logic lives in backend/presence.py."""
    from . import presence as _presence
    p = payload if isinstance(payload, dict) else {}
    device_id = str(p.get("device_id") or "default")[:64]
    visible = bool(p.get("visible", True))
    _presence.mark_seen(device_id, visible)
    age = _presence.last_seen_age()
    return {"ok": True, "last_seen_age_sec": age, "grace_sec": _presence.GRACE_SECONDS}


# Per-IP rate limiter for /api/log/client-error. The endpoint is
# intentionally unauthenticated (errors fire before auth is established),
# which means a misbehaving page or a hostile client could flood
# stderr / journald. Cap each IP to 30 errors / minute; over-budget
# requests are silently accepted (return ok) but not logged. State is a
# plain dict — the endpoint is single-process; a multi-worker deployment
# would want Redis here, but muselab is single-user / single-worker.
_CLIENT_ERR_BUCKETS: dict[str, tuple[float, int]] = {}
_CLIENT_ERR_WINDOW_SEC = 60.0
_CLIENT_ERR_PER_WINDOW = 30
_CLIENT_ERR_BODY_LIMIT = 8 * 1024
_CLIENT_ERR_FINGERPRINT_KEY = os.urandom(16)
_CLIENT_ERR_KINDS = frozenset({
    "error",
    "unhandledrejection",
    "resource",
})
_CLIENT_ERR_NAMES = frozenset({
    "AbortError", "AggregateError", "DataError", "Error", "EvalError",
    "InvalidStateError", "NetworkError", "NotAllowedError", "NotFoundError",
    "NotReadableError", "OperationError", "QuotaExceededError", "RangeError",
    "ReferenceError", "SecurityError", "SyntaxError", "TimeoutError",
    "TypeError", "URIError", "UnknownError",
})
_CLIENT_ERR_METHODS = frozenset({
    "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT",
})
_CLIENT_ERR_RESOURCE_TAGS = frozenset({
    "AUDIO", "IFRAME", "IMG", "LINK", "SCRIPT", "SOURCE", "VIDEO",
})


def _client_err_allow(ip: str) -> bool:
    import time
    now = time.monotonic()
    win, count = _CLIENT_ERR_BUCKETS.get(ip, (now, 0))
    if now - win >= _CLIENT_ERR_WINDOW_SEC:
        _CLIENT_ERR_BUCKETS[ip] = (now, 1)
        # Opportunistic GC: if the table grows past 1024 entries (hostile
        # spray from many IPs), drop everything older than a window.
        if len(_CLIENT_ERR_BUCKETS) > 1024:
            cutoff = now - _CLIENT_ERR_WINDOW_SEC
            stale = [k for k, (w, _) in _CLIENT_ERR_BUCKETS.items() if w < cutoff]
            for k in stale:
                _CLIENT_ERR_BUCKETS.pop(k, None)
        return True
    if count >= _CLIENT_ERR_PER_WINDOW:
        return False
    _CLIENT_ERR_BUCKETS[ip] = (win, count + 1)
    return True


def _client_error_fingerprint(value: object) -> str:
    """Return a process-local correlation token, never reversible log text."""
    if not isinstance(value, str) or not value:
        return ""
    return hashlib.blake2s(
        value.encode("utf-8", errors="replace"),
        key=_CLIENT_ERR_FINGERPRINT_KEY,
        digest_size=8,
    ).hexdigest()


def _client_error_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(10_000_000, max(0, value))


def _safe_client_error_record(payload: object) -> dict[str, object] | None:
    """Validate one browser diagnostic and return its strict safe projection."""
    if not isinstance(payload, dict):
        return None
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in _CLIENT_ERR_KINDS:
        return None

    raw_name = payload.get("name")
    error_name = (
        raw_name
        if isinstance(raw_name, str) and raw_name in _CLIENT_ERR_NAMES
        else "OtherError"
    )
    record: dict[str, object] = {
        "kind": kind,
        "error_name": error_name,
        "line": _client_error_int(payload.get("lineno")),
        "column": _client_error_int(payload.get("colno")),
    }
    reason_fp = _client_error_fingerprint(payload.get("message"))
    trace_fp = _client_error_fingerprint(payload.get("stack"))
    if reason_fp:
        record["reason_fp"] = reason_fp
    if trace_fp:
        record["trace_fp"] = trace_fp
    last_fetch = payload.get("lastFetch")
    if isinstance(last_fetch, dict):
        method = str(last_fetch.get("method") or "").upper()
        if method in _CLIENT_ERR_METHODS:
            record["last_method"] = method
    if kind == "resource":
        tag = str(payload.get("tagName") or "").upper()
        record["resource_tag"] = (
            tag if tag in _CLIENT_ERR_RESOURCE_TAGS else "OTHER"
        )
    return record


@app.post("/api/log/client-perf", dependencies=[Depends(require_token)])
async def client_performance_log(payload: dict = Body(...)) -> dict:
    """Persist one strict, content-free browser history-load timing summary."""
    allowed_status = {"ok", "cancelled", "error"}
    allowed_mode = {"cold", "quiet", "prefetch"}
    status = payload.get("status")
    mode = payload.get("mode")
    if status not in allowed_status or mode not in allowed_mode:
        return JSONResponse(
            {"ok": False, "error": "invalid_payload"}, status_code=422)

    def bounded_int(name: str) -> int:
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return min(100_000_000, max(0, value))

    allowed_fields = {
        "status", "mode", "foreground", "total_ms", "fetch_ms", "parse_ms",
        "shape_ms", "markdown_ms", "install_ms", "response_bytes",
        "block_count", "assistant_blocks", "long_task_count", "longest_task_ms",
    }
    if any(name not in allowed_fields for name in payload):
        return JSONResponse(
            {"ok": False, "error": "invalid_payload"}, status_code=422)
    try:
        perf_event(
            "client.history_load",
            status=status,
            mode=mode,
            foreground=bool(payload.get("foreground")),
            total_ms=bounded_int("total_ms"),
            fetch_ms=bounded_int("fetch_ms"),
            parse_ms=bounded_int("parse_ms"),
            shape_ms=bounded_int("shape_ms"),
            markdown_ms=bounded_int("markdown_ms"),
            install_ms=bounded_int("install_ms"),
            response_bytes=bounded_int("response_bytes"),
            block_count=bounded_int("block_count"),
            assistant_blocks=bounded_int("assistant_blocks"),
            long_task_count=bounded_int("long_task_count"),
            longest_task_ms=bounded_int("longest_task_ms"),
        )
    except Exception:
        # Diagnostics must never turn a successful history load into an error.
        pass
    return {"ok": True}


@app.post("/api/log/session-rename", dependencies=[Depends(require_token)])
async def client_session_rename_log(payload: dict = Body(...)) -> dict:
    """Persist one strict, title-free browser session-rename timing summary."""
    allowed_fields = {
        "surface", "status", "asset_version", "optimistic_apply_ms",
        "optimistic_paint_ms", "request_ms", "total_ms",
        "long_task_count", "longest_task_ms",
    }
    if any(name not in allowed_fields for name in payload):
        return JSONResponse(
            {"ok": False, "error": "invalid_payload"}, status_code=422)
    surface = payload.get("surface")
    status = payload.get("status")
    asset_version = payload.get("asset_version")
    if surface not in {"tab", "picker", "modal"}:
        return JSONResponse(
            {"ok": False, "error": "invalid_payload"}, status_code=422)
    if status not in {"ok", "error", "rollback"}:
        return JSONResponse(
            {"ok": False, "error": "invalid_payload"}, status_code=422)
    if (not isinstance(asset_version, str)
            or len(asset_version) > 32
            or not re.fullmatch(r"[A-Za-z0-9._-]*", asset_version)):
        return JSONResponse(
            {"ok": False, "error": "invalid_payload"}, status_code=422)

    def bounded_int(name: str) -> int:
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return min(100_000_000, max(0, value))

    try:
        perf_event(
            "client.session_rename",
            surface=surface,
            status=status,
            asset_version=asset_version,
            optimistic_apply_ms=bounded_int("optimistic_apply_ms"),
            optimistic_paint_ms=bounded_int("optimistic_paint_ms"),
            request_ms=bounded_int("request_ms"),
            total_ms=bounded_int("total_ms"),
            long_task_count=bounded_int("long_task_count"),
            longest_task_ms=bounded_int("longest_task_ms"),
        )
    except Exception:
        # Diagnostics must never affect the rename interaction.
        pass
    return {"ok": True}


@app.post("/api/log/client-error")
async def client_error_log(request: Request) -> dict:
    """Capture browser-side JS errors that the user can't easily extract
    themselves (e.g. iOS Safari with no devtools attached). Intentionally
    unauthenticated — the page that emits these may not be authed yet
    (errors during boot), and the only side-effect is a stderr line.

    The input is size-capped and projected through a strict allowlist. Error
    text, stack content, URLs, fetch targets, filenames, UA strings, session
    IDs, and arbitrary extra fields are never persisted. Message/stack hashes
    use a process-local keyed digest solely for duplicate correlation.

    Rate-limited per IP (30 / minute) so a runaway error loop in the
    browser can't fill journald / docker logs."""
    ip = (request.client.host if request.client else "?") or "?"
    if not _client_err_allow(ip):
        return {"ok": True, "rate_limited": True}
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "invalid_content_length"},
                status_code=400,
            )
        if declared_size < 0 or declared_size > _CLIENT_ERR_BODY_LIMIT:
            return JSONResponse(
                {"ok": False, "error": "body_too_large"},
                status_code=413,
            )
    try:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > _CLIENT_ERR_BODY_LIMIT:
                return JSONResponse(
                    {"ok": False, "error": "body_too_large"},
                    status_code=413,
                )
            body.extend(chunk)
        raw = bytes(body)
    except Exception as e:
        _CLIENT_ERROR_LOG.warning(
            "browser diagnostic body read failed exc=%s", type(e).__name__)
        return {"ok": False}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Never mirror malformed/raw input into logs.
        return JSONResponse(
            {"ok": False, "error": "invalid_json"}, status_code=400)
    safe_record = _safe_client_error_record(payload)
    if safe_record is None:
        return JSONResponse(
            {"ok": False, "error": "invalid_payload"}, status_code=422)
    encoded = json.dumps(
        safe_record, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    _CLIENT_ERROR_LOG.error("browser error %s", encoded)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=HOST,
        port=PORT,
        reload=False,
        # SSE and WebSocket connections are intentionally long-lived. Without
        # a deadline Uvicorn waits forever before entering lifespan shutdown,
        # so systemd reaches TimeoutStopSec and SIGKILLs the process.
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT,
    )
