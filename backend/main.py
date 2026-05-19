import re
import shutil
import subprocess
import sys
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from .files import router as files_router
from .chat import router as chat_router
from .api_settings import router as settings_router
from .api_scheduler import router as scheduler_router
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

app = FastAPI(title="muselab", version="0.1.0")
app.include_router(files_router)
app.include_router(chat_router)
app.include_router(settings_router)
app.include_router(scheduler_router)


@app.on_event("startup")
async def _startup_scheduler() -> None:
    """Boot the in-process task scheduler so any persisted scheduled
    prompts start firing on their schedule. No-op when scheduler.json
    is empty / missing — the daemon just idles."""
    from . import scheduler as _sched
    await _sched.start_scheduler()


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


@app.get("/api/meta")
def meta() -> dict:
    return {"root": str(ROOT), **_VERSIONS}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host=HOST, port=PORT, reload=False)
