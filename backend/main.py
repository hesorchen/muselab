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


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse((FRONTEND / "index.html").read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/api/meta")
def meta() -> dict:
    return {"root": str(ROOT)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host=HOST, port=PORT, reload=False)
