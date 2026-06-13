import functools
import logging
import os
import re
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Body, Depends, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from .auth import require_token
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from .files import router as files_router
from .chat import router as chat_router
from .api_settings import router as settings_router
from .api_scheduler import router as scheduler_router
from .api_push import router as push_router
from .settings import ROOT, PORT, HOST


class _TokenFilter(logging.Filter):
    """Strip token= query param from uvicorn access log URLs."""
    _re = re.compile(r'token=[^&\s"]+')

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn's access logger calls info("%s - \"%s %s HTTP/%s\" %d", ...)
        # so the token-bearing URL lives in record.args, NOT record.msg (which
        # is just the format template). The token sits in the URL/path, i.e.
        # args[2].
        #
        # 2026-06-08 fix: the previous version rendered the message, scrubbed
        # it, then set `record.args = ()`. That blanked the 5-tuple uvicorn's
        # AccessFormatter unpacks — `(client_addr, method, full_path,
        # http_version, status) = record.args` — so EVERY authed request
        # (token= is in nearly all of them) spammed journald with
        # "--- Logging error --- ValueError: not enough values to unpack
        # (expected 5, got 0)". Redact INSIDE the args tuple instead, leaving
        # the 5-tuple shape intact so the formatter still works.
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            full_path = args[2]
            if isinstance(full_path, str) and "token=" in full_path:
                record.args = (
                    args[0], args[1],
                    self._re.sub("token=***", full_path),
                    args[3], args[4],
                )
            return True
        # Fallback for any non-access-log record routed through this filter
        # (different arg shape → not consumed by AccessFormatter's 5-tuple
        # unpack, so collapsing to a scrubbed literal is safe here).
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = self._re.sub("token=***", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


# Apply to uvicorn access logger
logging.getLogger("uvicorn.access").addFilter(_TokenFilter())

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

# Strong references to long-lived fire-and-forget startup tasks so the
# event loop's weak task references don't let them be GC'd mid-run.
_BG_TASKS: set = set()


_ASSET_VERSION_CANDIDATES = (
    FRONTEND / "app.js", FRONTEND / "styles.css", FRONTEND / "index.html",
    # Split-out modules so editing only translations / data still bumps the
    # stamp and forces clients to refetch.
    FRONTEND / "i18n" / "index.js", FRONTEND / "data" / "constants.js",
)
# Cache: {computed_version_string} keyed by the max-mtime we last saw. index()
# / manifest / meta each called _asset_version() (5 stat()s + a max()) on
# EVERY "/" request; the value only changes on deploy. We still stat the
# candidate files each call (cheap, sub-µs, and the only reliable change
# signal) but skip recomputation when the mtime is unchanged. Also memoize
# the fully-rendered index HTML keyed by the same mtime so the per-request
# file read + double regex sub disappears on the hot path.
_asset_cache: dict[str, object] = {"mtime": None, "version": "0",
                                   "index_html": None, "manifest": None}
_asset_cache_lock = threading.Lock()


def _max_asset_mtime() -> int:
    # Single stat() per candidate instead of exists()+stat() (was 2 syscalls
    # × 5 files = 10 per static request). Missing files raise OSError, which
    # we swallow per-file so a deleted optional asset doesn't zero the stamp.
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

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Boot the in-process scheduler + push subsystem on startup.
    Uses the modern lifespan context manager — `@app.on_event("startup")`
    is deprecated and emits a warning on every server restart.

    The scheduler task continues until interpreter exit; no graceful
    shutdown handling needed (systemctl SIGTERMs the whole process).

    Each subsystem is guarded so a single failure (e.g. push VAPID
    generation hitting a disk-quota error) doesn't take down the
    whole web server — the chat UI is the primary capability and
    must come up even if peripheral subsystems are degraded."""
    from . import scheduler as _sched
    from . import push as _push
    import traceback
    try:
        _push.init()
    except Exception as e:
        sys.stderr.write(
            f"[muselab] push init failed (continuing without push): "
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}\n")
        sys.stderr.flush()
    try:
        await _sched.start_scheduler()
    except Exception as e:
        sys.stderr.write(
            f"[muselab] scheduler start failed (continuing without scheduler): "
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}\n")
        sys.stderr.flush()
    # Prune empty sessions + auto-purge expired trash. Both used to block
    # lifespan before yield (50-300 ms total on archives with many
    # sessions / a populated trash dir), pushing first-request TTFB out.
    # Moved to background tasks (2026-05-28) — neither is user-visible at
    # boot: a stray empty session in the list for ~1s, or a couple of
    # >30-day trash items not yet cleaned, are both no-ops from the user's
    # POV. `asyncio.to_thread` runs the sync IO off the event loop so a
    # slow disk doesn't stall concurrent requests either.
    import asyncio as _asyncio

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
            purged = await _asyncio.to_thread(_files_mod.auto_purge_expired_trash)
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
    for _coro in (_bg_prune_sessions(), _bg_purge_trash(),
                  _bg_warm_versions(), _backfill_turn_counts()):
        _t = _asyncio.create_task(_coro)
        _BG_TASKS.add(_t)
        _t.add_done_callback(_BG_TASKS.discard)
    # Same fire-and-forget pattern: rewrite turn_count for any session
    # written by the old algorithm. Gated by a sentinel file so reruns
    # are cheap; first run can take a few seconds on archives with
    # hundreds of sessions.
    yield


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
        ss = _sess.list_sessions()
    except Exception as e:
        sys.stderr.write(f"[muselab] backfill list_sessions failed: {e}\n")
        return
    updated = 0
    for s in ss:
        sid = s.get("id")
        if not sid:
            continue
        try:
            msgs = _gsm(sid, directory=str(_ROOT))
        except Exception:
            continue
        n_turns = sum(1 for sm in msgs if _chat._is_real_user_prompt(sm))
        cur = s.get("turn_count")
        if cur == n_turns:
            continue
        try:
            _sess.bump_session(sid, message_count=len(msgs),
                                turn_count=n_turns)
            updated += 1
        except Exception:
            pass
        # Yield to the event loop periodically so we don't starve the web
        # server on a large archive (~200+ sessions).
        if updated % 20 == 0:
            await _asyncio.sleep(0)
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


app = FastAPI(title="muselab", version="1.0.0", lifespan=_lifespan)

# Gzip every response ≥1KB. The frontend ships ~1.2MB of uncompressed text
# assets (app.js / index.html / styles.css) plus JSON-heavy API responses
# (cost-dashboard / settings / skills) — all highly compressible (~75-80%).
# This is the single biggest cold-load TTI win and costs one line.
# SSE streams are NOT touched: chat.py's EventSourceResponse sets
# `Content-Encoding: identity`, and Starlette's GZipMiddleware skips any
# response that already carries a Content-Encoding header (gzip.py:55-57),
# so live token streaming is never buffered/compressed.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Attach defensive headers to every response.

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
    response = await call_next(request)
    # Don't clobber explicit headers set by the endpoint (e.g. iframe
    # preview that needs different X-Frame-Options).
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return response


app.include_router(files_router)
app.include_router(chat_router)
app.include_router(settings_router)
app.include_router(scheduler_router)
app.include_router(push_router)


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

# Surface the resolved config so ops can confirm what the running process
# is actually using — host / port / root / which third-party vendors are
# enabled. Helps diagnose "I added DEEPSEEK_API_KEY but the model picker
# still doesn't show it" by making the env-var-vs-process state explicit.
def _startup_config_banner() -> None:
    from . import endpoints as _ep
    host = os.environ.get("MUSELAB_HOST", "127.0.0.1")
    port = os.environ.get("MUSELAB_PORT", "8765")
    enabled = [p.display for p in _ep.catalog()
                 if os.environ.get(p.env_key)]
    enabled_s = ", ".join(enabled) if enabled else "(none — Claude only)"
    print(f"[muselab] config: host={host} port={port} root={ROOT} "
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
    _gz_cache: dict[str, tuple[float, int, bytes]] = {}
    _gz_cache_max = 8

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
        import gzip as _gzip
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
        key = path
        hit = self._gz_cache.get(key)
        if hit is None or hit[0] != st.st_mtime or hit[1] != st.st_size:
            try:
                raw = Path(full).read_bytes()
            except OSError:
                return None
            # Run the (CPU-bound, GIL-releasing) compress off the event loop.
            import anyio
            data = await anyio.to_thread.run_sync(
                lambda: _gzip.compress(raw, compresslevel=6))
            if len(self._gz_cache) >= self._gz_cache_max and key not in self._gz_cache:
                self._gz_cache.pop(next(iter(self._gz_cache)), None)
            self._gz_cache[key] = (st.st_mtime, st.st_size, data)
            hit = self._gz_cache[key]
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
    return {"root": str(ROOT), "asset_version": _asset_version(), **_detect_versions()}


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


@app.post("/api/log/client-error")
async def client_error_log(request: Request) -> dict:
    """Capture browser-side JS errors that the user can't easily extract
    themselves (e.g. iOS Safari with no devtools attached). Intentionally
    unauthenticated — the page that emits these may not be authed yet
    (errors during boot), and the only side-effect is a stderr line.

    Body is opaque JSON, size-capped, written verbatim to stderr so it
    lands in systemd/docker logs alongside server-side tracebacks. No
    storage, no parsing — keep the surface tiny on purpose.

    Rate-limited per IP (30 / minute) so a runaway error loop in the
    browser can't fill journald / docker logs."""
    import json as _json
    ip = (request.client.host if request.client else "?") or "?"
    if not _client_err_allow(ip):
        return {"ok": True, "rate_limited": True}
    try:
        raw = await request.body()
    except Exception as e:
        sys.stderr.write(f"[client-error] body read failed: {type(e).__name__}: {e}\n")
        sys.stderr.flush()
        return {"ok": False}
    # Cap at 8 KiB — a real stack trace is well under 2 KiB; anything
    # bigger is either pathological or hostile.
    if len(raw) > 8192:
        raw = raw[:8192] + b"...[truncated]"
    try:
        payload = _json.loads(raw.decode("utf-8", errors="replace"))
        line = _json.dumps(payload, ensure_ascii=False)[:8192]
    except Exception:
        # Invalid-JSON fallback writes the raw body. json.dumps above
        # escapes embedded newlines, but this path doesn't — a body with
        # CR/LF would forge extra "[client-error] …" log lines (log
        # injection). Collapse CR/LF to spaces so one request stays one
        # log line.
        line = raw.decode("utf-8", errors="replace")[:8192]
        line = line.replace("\r", " ").replace("\n", " ")
    sys.stderr.write(f"[client-error] {line}\n")
    sys.stderr.flush()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host=HOST, port=PORT, reload=False)
