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
from .settings import ROOT, PORT, HOST

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="muselab", version="0.1.0")
app.include_router(files_router)
app.include_router(chat_router)
app.include_router(settings_router)


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


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse((FRONTEND / "index.html").read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/api/meta")
def meta() -> dict:
    return {"root": str(ROOT), **_VERSIONS}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host=HOST, port=PORT, reload=False)
