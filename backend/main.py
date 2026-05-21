import os
import re
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from .files import router as files_router
from .chat import router as chat_router
from .api_settings import router as settings_router
from .api_scheduler import router as scheduler_router
from .api_push import router as push_router
from .settings import ROOT, PORT, HOST

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def _asset_version() -> str:
    """One version stamp shared across every /static URL the HTML emits.
    Built from the largest mtime among the files most likely to change on a
    deploy (app.js / index.html / styles.css). When ANY of them change the
    stamp bumps, every HTML-emitted /static URL changes, and browsers refetch
    everything fresh — even though we still ask them to cache /static
    aggressively (one year + immutable)."""
    candidates = [FRONTEND / n for n in ("app.js", "styles.css", "index.html")]
    # Include split-out modules so editing only translations / data still bumps
    # the stamp and forces clients to refetch.
    for sub in ("i18n/index.js", "data/constants.js"):
        p = FRONTEND / sub
        if p.exists():
            candidates.append(p)
    try:
        latest = max(p.stat().st_mtime_ns for p in candidates if p.exists())
        return str(latest // 1_000_000)  # ms granularity, short enough
    except Exception:
        return "0"

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
    # Fire-and-forget: rewrite turn_count for any session whose value was
    # written by the old algorithm (which counted every type="user" SDK
    # frame — including tool_result sidechain echoes — so values were
    # 5-10× too high). Runs in a background task so it doesn't delay the
    # event loop or the first user request.
    import asyncio as _asyncio
    _asyncio.create_task(_backfill_turn_counts())
    yield


async def _backfill_turn_counts() -> None:
    """One-shot migration: rewalk each session's JSONL via the SDK and
    rewrite turn_count using the correct (real-prompt-only) filter.

    Idempotent. Safe to run on every startup — sessions whose turn_count
    is already within tolerance of the recomputed value skip the write.
    """
    import asyncio as _asyncio
    from . import sessions as _sess
    from . import chat as _chat
    from .settings import ROOT as _ROOT
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


app = FastAPI(title="muselab", version="0.1.0", lifespan=_lifespan)


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
    claude_bin = shutil.which("claude")
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


# Capture once at startup — these don't change for the lifetime of the process.
_VERSIONS = _detect_versions()
print(f"[muselab] versions: muselab={_VERSIONS['muselab_version']} "
      f"sdk={_VERSIONS['sdk_version']} cli={_VERSIONS['cli_version']} "
      f"py={_VERSIONS['python_version']}",
      file=sys.stderr, flush=True)

# Surface the resolved config so ops can confirm what the running process
# is actually using — host / port / root / which third-party vendors are
# enabled. Helps diagnose "I added DEEPSEEK_API_KEY but the model picker
# still doesn't show it" by making the env-var-vs-process state explicit.
def _startup_config_banner() -> None:
    from . import endpoints as _ep
    host = os.environ.get("MUSELAB_HOST", "127.0.0.1")
    port = os.environ.get("MUSELAB_PORT", "8765")
    enabled = [p.display for p in _ep.CATALOG
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
    raw = (FRONTEND / "index.html").read_text(encoding="utf-8")
    ver = _asset_version()
    html = _STATIC_REF_RE.sub(lambda m: f'{m.group(1)}{m.group(2)}?v={ver}{m.group(3)}', raw)
    # The HTML itself must never be cached — it embeds the per-deploy
    # version stamps that point at the cacheable static assets.
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


class _VersionedStaticFiles(StaticFiles):
    """When the request URL carries ?v=… (added by index() above), the asset
    can be treated as content-addressed and cached for a year. Otherwise we
    fall back to no-cache so a direct hit during development still picks up
    fresh content.

    The query-string presence is the marker — its value doesn't matter, since
    a stale ?v=… points at the same on-disk file anyway."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        query = scope.get("query_string", b"")
        if b"v=" in query:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


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


@app.get("/api/meta")
def meta() -> dict:
    return {"root": str(ROOT), **_VERSIONS}


@app.get("/api/health")
def health() -> dict:
    """Liveness probe — no auth required. Used by Docker HEALTHCHECK,
    Caddy `health_uri`, k8s readiness probes, and uptime monitors. Stays
    minimal on purpose: any heavier check (e.g. SDK client, archive
    write probe) could itself fail intermittently and cause restarts."""
    return {"status": "ok"}


@app.post("/api/log/client-error")
async def client_error_log(request: Request) -> dict:
    """Capture browser-side JS errors that the user can't easily extract
    themselves (e.g. iOS Safari with no devtools attached). Intentionally
    unauthenticated — the page that emits these may not be authed yet
    (errors during boot), and the only side-effect is a stderr line.

    Body is opaque JSON, size-capped, written verbatim to stderr so it
    lands in systemd/docker logs alongside server-side tracebacks. No
    storage, no parsing — keep the surface tiny on purpose."""
    import json as _json
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
        line = raw.decode("utf-8", errors="replace")[:8192]
    sys.stderr.write(f"[client-error] {line}\n")
    sys.stderr.flush()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host=HOST, port=PORT, reload=False)
