from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from .auth import require_token, require_token_query
from .settings import ROOT

router = APIRouter(prefix="/api/files", tags=["files"])

# Filenames without extensions that are commonly text (Dockerfile, Makefile, etc.).
# Compared case-insensitively against the full name.
# Known-binary extensions — fast reject, don't even try to sniff.
# Everything NOT in this set + not containing NUL bytes in the sniff window
# is treated as text-previewable. This lets .tmpl / .vue.bak / random custom
# extensions all preview without us maintaining a whitelist.
BINARY_EXT = {
    # archives / packages
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tgz", ".tbz",
    ".whl", ".jar", ".war", ".ear", ".deb", ".rpm", ".pkg", ".dmg", ".iso",
    ".apk", ".ipa", ".xpi", ".crx",
    # images (have their own img preview)
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".tif",
    ".heic", ".heif", ".raw", ".psd", ".ai", ".sketch", ".fig",
    # audio / video
    ".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac", ".wma",
    ".mp4", ".webm", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v",
    # binary docs (PDF has its own preview; office formats need conversion)
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt",
    ".ods", ".odp", ".rtf", ".epub", ".mobi",
    # executables / libs
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".lib", ".obj",
    ".class", ".pyc", ".pyo", ".elc", ".wasm",
    # fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # databases
    ".db", ".sqlite", ".sqlite3", ".mdb",
}
MAX_TEXT_SIZE = 2 * 1024 * 1024  # 2 MB — bigger files refuse with 413
SNIFF_BYTES = 4096                # how much we read to detect NUL bytes


def _looks_binary(p: Path) -> bool:
    """Heuristic: read up to 4 KB, presence of NUL byte → binary. Otherwise
    decode with `errors="replace"` and check how many bytes turned into the
    Unicode replacement character (U+FFFD). High ratio → binary / garbage.

    Important: we must NOT use plain `decode("utf-8")` here. The sniff window
    cuts at a fixed byte offset, which routinely splits a multi-byte UTF-8
    character (CJK chars are 3 bytes). A clean text file would then raise
    UnicodeDecodeError purely because of the chunk boundary — wrongly tagged
    binary. `errors="replace"` decodes whatever can be decoded and only the
    truly invalid bytes become U+FFFD."""
    try:
        with p.open("rb") as f:
            chunk = f.read(SNIFF_BYTES)
    except OSError:
        return True
    if b"\x00" in chunk:
        return True
    # decode with replacement; count how many chars are the replacement marker
    decoded = chunk.decode("utf-8", errors="replace")
    if not decoded:
        return False
    bad = decoded.count("�")
    # >5% replacement chars across a 4 KB window strongly suggests non-UTF-8
    # binary. A clean text file boundary-split mid-char contributes ≤1 bad char.
    return (bad / len(decoded)) > 0.05

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
    by default, blocking access to credential-shaped filenames.

    Defends against:
      - `../../etc/passwd` style traversal (resolve() + ROOT-prefix check)
      - **Symlink escape**: a symlink inside ROOT pointing to /etc/shadow would
        previously slip through because `.resolve()` follows symlinks. We
        explicitly check the resolved target is still under ROOT.
      - `.env`, `id_rsa`, `*.pem` etc. (SENSITIVE_SUFFIX / SENSITIVE_NAMES)."""
    rel = (rel or "").lstrip("/")
    # First-pass resolve (follows symlinks → catches symlink escape):
    target = (ROOT / rel).resolve()
    # ROOT itself must also be resolved (it might itself be a symlink target).
    root_real = ROOT.resolve()
    if root_real != target and root_real not in target.parents:
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
    name_lower = target.name.lower()
    # Fast reject for known binary extensions.
    if suffix in BINARY_EXT:
        raise HTTPException(status_code=415, detail="binary file — not previewable as text")
    if target.stat().st_size > MAX_TEXT_SIZE:
        raise HTTPException(status_code=413, detail="file too large for preview")
    # Empty extension + not a known text name? Sniff content. Empty files OK.
    # This is the path that picks up .tmpl, .conf.j2, .env.staging, etc.
    if target.stat().st_size > 0 and _looks_binary(target):
        raise HTTPException(status_code=415, detail="binary content — not previewable as text")
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
        # CSP relaxed enough for academic HTML reports (MathJax / KaTeX / highlight.js
        # from CDN, inline <script>window.MathJax = {...}</script> config blocks).
        # The iframe `sandbox="allow-scripts"` attribute (set in frontend/index.html)
        # still puts JS in a unique opaque origin: it cannot read MUSELAB_TOKEN, cannot
        # fetch /api/* (CORS blocks), cannot use cookies. The CSP `sandbox` directive
        # is intentionally omitted — iframe's sandbox attribute is the source of truth.
        return FileResponse(target, headers={
            **base_headers,
            "Content-Disposition": f"inline; {disp_filename}",
            "Content-Security-Policy": (
                "default-src 'none'; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; "
                "style-src 'self' 'unsafe-inline' https:; "
                "img-src 'self' data: https:; "
                "font-src https: data:; "
                "connect-src https:; "
                "base-uri 'none'; form-action 'none'"
            ),
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


import os as _os

# Default 100 MB cap per uploaded file. Override via MUSELAB_MAX_UPLOAD_MB.
MAX_UPLOAD_BYTES = int(_os.environ.get("MUSELAB_MAX_UPLOAD_MB", "100")) * 1024 * 1024
# Filename extensions that are likely to be hostile or pointless to host in
# a personal archive. Block at upload (cleaner than after-the-fact cleanup).
UPLOAD_BLOCKED_SUFFIX = {
    ".exe", ".dll", ".so", ".dylib", ".scr", ".com", ".bat", ".cmd",
    ".ps1",  # PowerShell scripts — block by default; allow via .env override later
    ".msi", ".app",
}


@router.post("/upload", dependencies=[Depends(require_token)])
async def upload(path: str = Form(""), file: UploadFile = File(...)) -> dict:
    target_dir = safe_resolve(path)
    if not target_dir.exists() or not target_dir.is_dir():
        raise HTTPException(status_code=400, detail="target dir invalid")
    safe_name = Path(file.filename or "upload.bin").name
    # Block dangerous extensions early.
    suffix = Path(safe_name).suffix.lower()
    if suffix in UPLOAD_BLOCKED_SUFFIX:
        raise HTTPException(status_code=400,
                             detail=f"upload blocked by extension: {suffix}")
    # Also block uploads with sensitive filenames (.env, id_rsa etc.).
    if _is_sensitive(Path(safe_name)):
        raise HTTPException(status_code=403,
                             detail="sensitive filename blocked")
    dest = target_dir / safe_name
    # Stream + enforce size cap. If the user posts a 10 GB file, we abort
    # mid-write so the disk doesn't fill.
    written = 0
    try:
        with dest.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    f.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"upload exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB cap",
                    )
                f.write(chunk)
    except HTTPException:
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        raise
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
