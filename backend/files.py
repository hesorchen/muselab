from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from .auth import require_token, require_token_query
from .settings import ROOT

router = APIRouter(prefix="/api/files", tags=["files"])

# Filenames without extensions that are commonly text (Dockerfile, Makefile, etc.).
# Compared case-insensitively against the full name.
TEXT_NAMES = {
    "dockerfile", "containerfile", "makefile", "justfile", "rakefile",
    "procfile", "gemfile", "vagrantfile", "brewfile", "guardfile",
    "license", "licence", "readme", "changelog", "authors", "contributors",
    "copying", "notice", "manifest", "todo", "version",
}

TEXT_EXT = {
    # markup / docs
    ".md", ".markdown", ".txt", ".rst", ".tex", ".org", ".adoc",
    # web
    ".html", ".htm", ".css", ".scss", ".sass", ".less", ".svg", ".vue", ".svelte",
    # data / config
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".properties", ".csv", ".tsv", ".xml", ".plist",
    # scripts
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    # programming
    ".py", ".pyi", ".pyw", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".rs", ".go", ".java", ".kt", ".kts", ".scala", ".groovy",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".m", ".mm",
    ".rb", ".php", ".pl", ".lua", ".r", ".jl", ".swift", ".dart", ".ex", ".exs",
    ".cs", ".fs", ".vb", ".clj", ".cljs", ".elm", ".hs", ".ml",
    # other text
    ".log", ".sql", ".graphql", ".gql", ".proto", ".sol",
    ".gitignore", ".gitattributes", ".editorconfig", ".npmrc",
    ".dockerignore",
}
MAX_TEXT_SIZE = 2 * 1024 * 1024  # 2MB

# Files whose contents should never be served or overwritten through this API,
# regardless of extension. Matches against the basename (case-insensitive).
SENSITIVE_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    ".netrc", ".pgpass", ".npmrc", ".pypirc", ".dockercfg",
    ".htpasswd", ".htaccess",
    "credentials", "credentials.json", "service-account.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "authorized_keys", "known_hosts",
}
# Extension suffixes treated as sensitive — private keys, cert bundles, and
# `.env`-style files regardless of basename (prod.env, staging.env, etc.).
SENSITIVE_SUFFIX = {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".env"}


def _is_sensitive(p: Path) -> bool:
    name = p.name.lower()
    if name in SENSITIVE_NAMES:
        return True
    if name.startswith(".env."):  # .env.* variants like .env.local
        return True
    if p.suffix.lower() in SENSITIVE_SUFFIX:
        return True
    return False


def safe_resolve(rel: str, allow_sensitive: bool = False) -> Path:
    """Resolve a path relative to ROOT, blocking traversal outside ROOT and,
    by default, blocking access to credential-shaped filenames."""
    rel = (rel or "").lstrip("/")
    target = (ROOT / rel).resolve()
    if ROOT != target and ROOT not in target.parents:
        raise HTTPException(status_code=400, detail="path escapes root")
    # Block by name regardless of whether the file already exists, so the API
    # can neither read nor write `.env` / private-key shaped paths.
    if not allow_sensitive and not target.is_dir() and _is_sensitive(target):
        raise HTTPException(status_code=403, detail="sensitive file blocked")
    return target


class Entry(BaseModel):
    name: str
    path: str  # relative to ROOT
    is_dir: bool
    size: int
    mtime: float


MAX_LIST_ENTRIES = 500  # safety cap so huge dirs (.git/objects) don't freeze the UI


@router.get("/list", dependencies=[Depends(require_token)])
def list_dir(path: str = "", show_hidden: bool = False) -> dict:
    target = safe_resolve(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="not a directory")
    entries: list[dict] = []
    truncated = False
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if not show_hidden and child.name.startswith("."):
            continue
        if len(entries) >= MAX_LIST_ENTRIES:
            truncated = True
            break
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append(Entry(
            name=child.name,
            path=str(child.relative_to(ROOT)),
            is_dir=child.is_dir(),
            size=stat.st_size if not child.is_dir() else 0,
            mtime=stat.st_mtime,
        ).model_dump())
    return {"root": str(ROOT), "path": path, "entries": entries, "truncated": truncated}


@router.get("/read", dependencies=[Depends(require_token)])
def read_file(path: str) -> PlainTextResponse:
    target = safe_resolve(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not a file")
    suffix = target.suffix.lower()
    if suffix not in TEXT_EXT and target.name.lower() not in TEXT_NAMES:
        raise HTTPException(status_code=415, detail="not previewable as text")
    if target.stat().st_size > MAX_TEXT_SIZE:
        raise HTTPException(status_code=413, detail="file too large for preview")
    return PlainTextResponse(
        target.read_text(encoding="utf-8", errors="replace"),
        headers={
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )


# Types we serve inline (images / PDF / media render natively in browser).
INLINE_OK_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
                    ".pdf", ".mp4", ".webm", ".mp3", ".ogg", ".wav"}
# Types we serve inline INSIDE A SANDBOXED IFRAME (HTML / SVG can render but
# the strong CSP + sandbox attribute on the iframe blocks JS execution and
# same-origin token theft).
SANDBOXED_INLINE_SUFFIX = {".html", ".htm", ".svg"}


@router.get("/raw", dependencies=[Depends(require_token_query)])
def raw_file(path: str = Query(...)) -> FileResponse:
    """Stream raw file (images, PDF, sandboxed HTML, etc.). Token via query.
    Everything outside the whitelists is forced to download as octet-stream."""
    target = safe_resolve(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not a file")
    suffix = target.suffix.lower()
    base_headers = {"X-Content-Type-Options": "nosniff"}
    # RFC 5987: non-ASCII filenames must be URL-encoded in filename* attribute.
    # HTTP headers are latin-1 only; Chinese / emoji filenames break encode().
    from urllib.parse import quote
    disp_filename = f'filename="file{suffix}"; filename*=UTF-8\'\'{quote(target.name)}'

    if suffix in INLINE_OK_SUFFIX:
        return FileResponse(target, headers={
            **base_headers,
            "Content-Disposition": f"inline; {disp_filename}",
        })
    if suffix in SANDBOXED_INLINE_SUFFIX:
        return FileResponse(target, headers={
            **base_headers,
            "Content-Disposition": f"inline; {disp_filename}",
            "Content-Security-Policy":
                "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
                "base-uri 'none'; form-action 'none'; sandbox",
        })
    # FileResponse(filename=) sets Content-Disposition itself; use our safe one.
    return FileResponse(target, media_type="application/octet-stream", headers={
        **base_headers,
        "Content-Disposition": f"attachment; {disp_filename}",
    })


@router.get("/download", dependencies=[Depends(require_token_query)])
def download_file(path: str = Query(...)) -> FileResponse:
    target = safe_resolve(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not a file")
    from urllib.parse import quote
    suffix = target.suffix.lower()
    disp = f'attachment; filename="file{suffix}"; filename*=UTF-8\'\'{quote(target.name)}'
    return FileResponse(target, media_type="application/octet-stream",
                        headers={"Content-Disposition": disp})


class WriteReq(BaseModel):
    path: str
    content: str


@router.put("/write", dependencies=[Depends(require_token)])
def write_file(req: WriteReq) -> dict:
    target = safe_resolve(req.path)
    if target.exists() and target.is_dir():
        raise HTTPException(status_code=400, detail="path is a directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(req.content, encoding="utf-8")
    return {"ok": True, "size": target.stat().st_size}


@router.post("/upload", dependencies=[Depends(require_token)])
async def upload(path: str = Form(""), file: UploadFile = File(...)) -> dict:
    target_dir = safe_resolve(path)
    if not target_dir.exists() or not target_dir.is_dir():
        raise HTTPException(status_code=400, detail="target dir invalid")
    safe_name = Path(file.filename or "upload.bin").name
    dest = target_dir / safe_name
    with dest.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    return {"ok": True, "path": str(dest.relative_to(ROOT)), "size": dest.stat().st_size}


class DeleteReq(BaseModel):
    path: str


@router.delete("/delete", dependencies=[Depends(require_token)])
def delete(req: DeleteReq) -> dict:
    target = safe_resolve(req.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="not found")
    if target.is_dir():
        # require empty dir for safety
        if any(target.iterdir()):
            raise HTTPException(status_code=400, detail="dir not empty")
        target.rmdir()
    else:
        target.unlink()
    return {"ok": True}


class MkdirReq(BaseModel):
    path: str


@router.post("/mkdir", dependencies=[Depends(require_token)])
def mkdir(req: MkdirReq) -> dict:
    target = safe_resolve(req.path)
    target.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": str(target.relative_to(ROOT))}


class RenameReq(BaseModel):
    src: str
    dst: str   # relative to ROOT


@router.post("/rename", dependencies=[Depends(require_token)])
def rename(req: RenameReq) -> dict:
    src = safe_resolve(req.src)
    dst = safe_resolve(req.dst)
    if not src.exists():
        raise HTTPException(status_code=404, detail="source not found")
    if dst.exists():
        raise HTTPException(status_code=409, detail="destination already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return {"ok": True, "path": str(dst.relative_to(ROOT))}


SEARCH_IGNORE = {".git", "node_modules", "__pycache__", ".venv", "venv",
                 ".cache", ".pytest_cache", ".mypy_cache", "dist", "build"}

GREP_EXTS = {".md", ".markdown", ".txt", ".html", ".htm", ".json", ".yaml", ".yml",
             ".py", ".js", ".ts", ".css", ".sh", ".toml", ".ini", ".csv", ".sql",
             ".log", ".xml", ".rst", ".tex"}


import os as _os
import time as _time

MAX_GREP_FILE_SIZE = 1_000_000   # 1MB per file — skip large files
MAX_GREP_TIME_SEC = 8            # soft time budget


@router.get("/grep", dependencies=[Depends(require_token)])
def grep(q: str, limit: int = 50, show_hidden: bool = False) -> dict:
    """Cross-platform full-text search (pure Python, no grep dependency)."""
    q_lower = q.strip().lower()
    if not q_lower:
        return {"hits": []}
    hits: list[dict] = []
    started = _time.monotonic()
    timed_out = False
    for dirpath, dirnames, filenames in _os.walk(ROOT):
        # prune ignored dirs; hidden only if not requested
        dirnames[:] = [d for d in dirnames
                       if d not in SEARCH_IGNORE
                       and (show_hidden or not d.startswith("."))]
        if _time.monotonic() - started > MAX_GREP_TIME_SEC:
            timed_out = True
            break
        for fname in filenames:
            if not show_hidden and fname.startswith("."):
                continue
            # 隐藏文件即使没扩展名也允许 grep（用户主动开了 show_hidden 说明想看）
            if Path(fname).suffix.lower() not in GREP_EXTS and not (show_hidden and fname.startswith(".")):
                continue
            full = Path(dirpath) / fname
            try:
                if full.stat().st_size > MAX_GREP_FILE_SIZE:
                    continue
                with full.open("r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if q_lower in line.lower():
                            try:
                                rel = str(full.relative_to(ROOT))
                            except ValueError:
                                continue
                            hits.append({
                                "path": rel,
                                "name": fname,
                                "line": i,
                                "snippet": line.strip()[:200],
                            })
                            if len(hits) >= limit:
                                return {"hits": hits, "truncated": True}
            except OSError:
                continue
            if _time.monotonic() - started > MAX_GREP_TIME_SEC:
                timed_out = True
                break
        if timed_out:
            break
    return {"hits": hits, "truncated": timed_out}


@router.get("/search", dependencies=[Depends(require_token)])
def search(q: str, limit: int = 100, show_hidden: bool = False) -> dict:
    q_lower = q.strip().lower()
    if not q_lower:
        return {"entries": []}
    hits: list[dict] = []
    for dirpath, dirnames, filenames in __import__("os").walk(ROOT):
        # prune ignored dirs; hidden only when not requested
        dirnames[:] = [d for d in dirnames
                       if d not in SEARCH_IGNORE
                       and (show_hidden or not d.startswith("."))]
        for name in dirnames + filenames:
            if not show_hidden and name.startswith("."):
                continue
            if q_lower in name.lower():
                full = Path(dirpath) / name
                try:
                    stat = full.stat()
                except OSError:
                    continue
                hits.append({
                    "name": name,
                    "path": str(full.relative_to(ROOT)),
                    "is_dir": full.is_dir(),
                    "size": stat.st_size if not full.is_dir() else 0,
                    "mtime": stat.st_mtime,
                })
                if len(hits) >= limit:
                    return {"entries": hits, "truncated": True}
    return {"entries": hits, "truncated": False}
