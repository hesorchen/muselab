import os
import json
import secrets
import shutil
import time
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from .auth import require_token, require_token_query
from .settings import ROOT, atomic_write_text

router = APIRouter(prefix="/api/files", tags=["files"])

# ============================================================
# Trash / dustbin — soft delete moves to <ROOT>/.muselab-dustbin/ instead
# of unlink. Restore + permanent-purge are separate endpoints. The dir is
# always excluded from file tree listings, search, and grep (it has its
# own dedicated UI surface in the frontend).
#
# Layout per deletion:
#   <ROOT>/.muselab-dustbin/<trash_id>.json   ← manifest (original path,
#                                                deletion time, kind, size)
#   <ROOT>/.muselab-dustbin/<trash_id>        ← payload (file OR dir, the
#                                                inode rename'd in place)
# trash_id = "<unix_ts>_<8-hex>" — sortable, collision-resistant, opaque
# to the client.
# ============================================================
TRASH_DIR_NAME = ".muselab-dustbin"


def _trash_dir() -> Path:
    return ROOT / TRASH_DIR_NAME


def _ensure_trash_dir() -> Path:
    d = _trash_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _gen_trash_id() -> str:
    return f"{int(time.time())}_{secrets.token_hex(4)}"


def _dir_size(p: Path) -> int:
    """Sum of file sizes (best-effort; OSError on individual files skipped)."""
    total = 0
    try:
        for sub in p.rglob("*"):
            try:
                if sub.is_file():
                    total += sub.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def _move_to_trash(target: Path) -> dict:
    """Move `target` into the trash, write a manifest, return it.
    Caller is responsible for ensuring `target` exists + is inside ROOT.
    Same-filesystem rename, so atomic + cheap regardless of payload size."""
    trash = _ensure_trash_dir()
    tid = _gen_trash_id()
    payload = trash / tid
    original_rel = str(target.relative_to(ROOT))
    target.rename(payload)
    is_dir = payload.is_dir()
    try:
        size = _dir_size(payload) if is_dir else payload.stat().st_size
    except OSError:
        size = 0
    manifest = {
        "trash_id": tid,
        "original_path": original_rel,
        "original_name": target.name,
        "deleted_at": time.time(),
        "kind": "dir" if is_dir else "file",
        "size": size,
    }
    (trash / f"{tid}.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    return manifest


def _read_manifest(tid: str) -> dict | None:
    mf = _trash_dir() / f"{tid}.json"
    if not mf.exists():
        return None
    try:
        return json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _list_trash() -> list[dict]:
    """Manifests of every in-trash item, newest first. Orphans (manifest
    without payload, or vice versa) are skipped — they'd be confusing
    to surface and the user can't usefully act on them anyway."""
    d = _trash_dir()
    if not d.exists():
        return []
    items: list[dict] = []
    try:
        for mf in d.glob("*.json"):
            try:
                data = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            tid = data.get("trash_id")
            if not tid:
                continue
            payload = d / tid
            if not payload.exists():
                continue
            items.append(data)
    except OSError:
        return []
    items.sort(key=lambda x: x.get("deleted_at", 0), reverse=True)
    return items


def _purge_one(tid: str) -> None:
    """Permanently delete one trash item (manifest + payload).
    Silent no-op if neither exists."""
    d = _trash_dir()
    payload = d / tid
    if payload.exists():
        if payload.is_dir():
            shutil.rmtree(payload, ignore_errors=True)
        else:
            try:
                payload.unlink()
            except OSError:
                pass
    mf = d / f"{tid}.json"
    if mf.exists():
        try:
            mf.unlink()
        except OSError:
            pass

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
    # Shell / language history files — frequently contain pasted tokens,
    # one-off commands with secrets. Added when MUSELAB_ROOT=$HOME became
    # supported (2026-05-17) so a token leak doesn't expose them.
    ".bash_history", ".zsh_history", ".python_history", ".node_repl_history",
    ".sqlite_history", ".lesshst", ".viminfo", ".wget-hsts",
    ".npm-debug.log", ".yarn-error.log",
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
    # NUL byte in a path raises ValueError from (ROOT / rel) and FastAPI
    # converts that to a 500 with a traceback that leaks internal module
    # paths. Reject early as 400. Same for any string that Python's path
    # layer refuses (control chars trip OS-level checks downstream).
    if "\x00" in rel:
        raise HTTPException(status_code=400, detail="invalid path")
    # First-pass resolve (follows symlinks → catches symlink escape):
    try:
        target = (ROOT / rel).resolve()
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="invalid path") from None
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
    # Trash dir is always hidden from the file tree (even when
    # show_hidden=true) — it has its own dedicated UI surface; mixing it
    # back into the tree would surface deleted files in a confusing
    # context. Only relevant at the root level since trash dir lives there.
    is_root_listing = (target == ROOT)
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if is_root_listing and child.name == TRASH_DIR_NAME:
            continue
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


# xlsx preview caps. Read-only mode + capped per-sheet rows/cols so a
# 1M-cell spreadsheet doesn't OOM the SSE event loop or blow up the JSON
# payload over the wire. Truncation is signaled to the FE so it can hint
# the user instead of silently dropping data.
XLSX_MAX_SHEETS = 20
XLSX_MAX_ROWS = 500
XLSX_MAX_COLS = 50
XLSX_CELL_MAX_CHARS = 500   # one obnoxious cell shouldn't blow the page


@router.get("/xlsx", dependencies=[Depends(require_token)])
def xlsx_preview(path: str) -> dict:
    """Read-only xlsx preview as structured JSON.

    Returns each sheet's first XLSX_MAX_ROWS×XLSX_MAX_COLS cells as
    strings. Formulas are NOT evaluated — `data_only=True` returns the
    cached value the spreadsheet app last wrote. If a file was created
    programmatically without ever being opened in Excel/LibreOffice,
    formula cells will be null and surface as empty strings.
    """
    target = safe_resolve(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not a file")
    if target.suffix.lower() not in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        raise HTTPException(status_code=415, detail="not an xlsx-family file")
    try:
        import openpyxl  # local import — openpyxl is only loaded on demand
    except ImportError:
        raise HTTPException(status_code=500,
                            detail="openpyxl not installed — run `uv sync`")
    try:
        wb = openpyxl.load_workbook(target, read_only=True, data_only=True)
    except Exception as e:
        raise HTTPException(status_code=422,
                            detail=f"failed to parse xlsx: {type(e).__name__}: {e}")
    try:
        sheets: list[dict] = []
        sheet_names = wb.sheetnames
        sheets_truncated = len(sheet_names) > XLSX_MAX_SHEETS
        for sheet_name in sheet_names[:XLSX_MAX_SHEETS]:
            ws = wb[sheet_name]
            rows: list[list[str]] = []
            rows_truncated = False
            cols_truncated = False
            for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
                if r_idx >= XLSX_MAX_ROWS:
                    rows_truncated = True
                    break
                cells: list[str] = []
                for c_idx, val in enumerate(row):
                    if c_idx >= XLSX_MAX_COLS:
                        cols_truncated = True
                        break
                    if val is None:
                        cells.append("")
                    else:
                        s = str(val)
                        if len(s) > XLSX_CELL_MAX_CHARS:
                            s = s[:XLSX_CELL_MAX_CHARS] + "…"
                        cells.append(s)
                rows.append(cells)
            sheets.append({
                "name": sheet_name,
                "rows": rows,
                "rows_truncated": rows_truncated,
                "cols_truncated": cols_truncated,
            })
        return {
            "path": path,
            "sheets": sheets,
            "sheets_truncated": sheets_truncated,
            "limits": {"max_rows": XLSX_MAX_ROWS, "max_cols": XLSX_MAX_COLS,
                       "max_sheets": XLSX_MAX_SHEETS},
        }
    finally:
        wb.close()


# CSV preview caps. Paginated by design — CSV files in the wild can be
# millions of rows, so we never load the whole file into memory. Each
# request returns one window; the UI calls back with offset += limit when
# the user pages forward.
CSV_DEFAULT_LIMIT = 200       # default page size
CSV_MAX_LIMIT = 1000          # hard ceiling the client can request
CSV_MAX_COLS = 50             # per-row column cap
CSV_CELL_MAX_CHARS = 500
CSV_SNIFF_BYTES = 8192        # sample size for delimiter / header detection


@router.get("/csv", dependencies=[Depends(require_token)])
def csv_preview(path: str, offset: int = 0, limit: int = CSV_DEFAULT_LIMIT) -> dict:
    """Read-only paginated CSV / TSV preview as structured JSON.

    Returns rows[offset : offset+limit] from the file, plus the sniffed
    delimiter and a `total_rows` count so the UI can show pagination.
    Header row (if csv.Sniffer flags one) is returned separately.

    Designed to never load more than a window into memory: the file is
    iterated row-by-row, skipping rows below offset and breaking once
    `limit` is filled. The trailing total-rows count is the only full
    scan, and it just discards each row.
    """
    import csv as _csv  # local import — csv is stdlib, but keep import local
                       # so import overhead stays out of every other route.
    target = safe_resolve(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not a file")
    if target.suffix.lower() not in {".csv", ".tsv"}:
        raise HTTPException(status_code=415, detail="not a csv/tsv file")
    if limit < 1:
        limit = CSV_DEFAULT_LIMIT
    if limit > CSV_MAX_LIMIT:
        limit = CSV_MAX_LIMIT
    if offset < 0:
        offset = 0
    # Sniff delimiter + header from a small head sample. Defaults to
    # excel-style comma if Sniffer can't tell (e.g. one-column file).
    try:
        with target.open("r", encoding="utf-8", errors="replace", newline="") as f:
            sample = f.read(CSV_SNIFF_BYTES)
        try:
            dialect = _csv.Sniffer().sniff(sample, delimiters=",\t;|")
            has_header = _csv.Sniffer().has_header(sample)
        except _csv.Error:
            dialect = _csv.excel
            has_header = False
        # Override sniff for explicit .tsv — Sniffer sometimes guesses comma
        # on tab-separated files when the first row has no tabs.
        if target.suffix.lower() == ".tsv":
            dialect = _csv.excel_tab
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"failed to read: {e}")

    header: list[str] = []
    rows: list[list[str]] = []
    cols_truncated = False
    total_rows = 0
    try:
        with target.open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = _csv.reader(f, dialect=dialect)
            # Pull header before any data offset is applied. The user paging
            # to offset=200 still wants column titles at the top of the page.
            if has_header:
                try:
                    header_row = next(reader)
                    header = [_clip_cell(c) for c in header_row[:CSV_MAX_COLS]]
                    if len(header_row) > CSV_MAX_COLS:
                        cols_truncated = True
                except StopIteration:
                    pass
            row_idx = 0
            for raw in reader:
                if row_idx < offset:
                    row_idx += 1
                    continue
                if len(rows) < limit:
                    cells = [_clip_cell(c) for c in raw[:CSV_MAX_COLS]]
                    if len(raw) > CSV_MAX_COLS:
                        cols_truncated = True
                    rows.append(cells)
                row_idx += 1
            total_rows = row_idx
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"failed to read: {e}")

    return {
        "path": path,
        "header": header,
        "rows": rows,
        "offset": offset,
        "limit": limit,
        "total_rows": total_rows,
        "has_header": has_header,
        "delimiter": dialect.delimiter,
        "cols_truncated": cols_truncated,
        "limits": {"max_cols": CSV_MAX_COLS, "max_limit": CSV_MAX_LIMIT},
    }


def _clip_cell(value: str) -> str:
    """Cap a single CSV cell so one runaway value can't blow up the page."""
    s = "" if value is None else str(value)
    if len(s) > CSV_CELL_MAX_CHARS:
        s = s[:CSV_CELL_MAX_CHARS] + "…"
    return s


@router.get("/read", dependencies=[Depends(require_token)])
def read_file(path: str) -> PlainTextResponse:
    target = safe_resolve(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not a file")
    suffix = target.suffix.lower()
    # Fast reject for known binary extensions.
    if suffix in BINARY_EXT:
        raise HTTPException(status_code=415, detail="binary file — not previewable as text")
    if target.stat().st_size > MAX_TEXT_SIZE:
        raise HTTPException(status_code=413, detail="file too large for preview")
    # Empty extension + not a known text name? Sniff content. Empty files OK.
    # This is the path that picks up .tmpl, .conf.j2, .env.staging, etc.
    if target.stat().st_size > 0 and _looks_binary(target):
        raise HTTPException(status_code=415, detail="binary content — not previewable as text")
    content = target.read_text(encoding="utf-8", errors="replace")
    if len(content) > MAX_TEXT_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large to read as text ({len(content)} bytes > {MAX_TEXT_SIZE})",
        )
    return PlainTextResponse(
        content,
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
    # `no-cache` (NOT no-store) — let browsers cache but force a conditional
    # GET (If-None-Match / If-Modified-Since) every time. FileResponse still
    # sends ETag + Last-Modified, so unchanged files return 304 cheaply; the
    # moment mtime changes, the etag flips and the browser pulls the new
    # body. Without this, browsers happily served the disk-cached version on
    # every page reload (URLs identical) and edits never showed until users
    # hit the manual reload button — see 2026-05-18 dark-mode HTML report bug.
    base_headers = {
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-cache",
    }
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
                "script-src 'self' 'unsafe-inline' https:; "
                "style-src 'self' 'unsafe-inline' https:; "
                "img-src 'self' data: https:; "
                "font-src https: data:; "
                "connect-src 'self'; "
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


# Upper bound on a single editor-save payload. Generous enough for real-world
# documents (a 10 MB Markdown file is ~3 million words) but stops a runaway
# script from filling the disk via this endpoint. Matches the spirit of
# MAX_TEXT_SIZE on the read path.
MAX_WRITE_BYTES = 10 * 1024 * 1024


@router.put("/write", dependencies=[Depends(require_token)])
def write_file(req: WriteReq) -> dict:
    """Overwrite a file at `path` with `content`. Atomic (tmpfile + rename),
    so a crash mid-write leaves the previous content intact instead of a
    truncated half-file. Capped at MAX_WRITE_BYTES to prevent the editor
    from accidentally serving as an unbounded ingest path."""
    target = safe_resolve(req.path)
    if target.exists() and target.is_dir():
        raise HTTPException(status_code=400, detail="path is a directory")
    # Two-stage size gate. Each char is 1-4 UTF-8 bytes, so the upper
    # bound `len(s) * 4` is cheap (no encoding); if it already exceeds
    # the limit we reject without materializing the encoded bytes at
    # all. Only the borderline case (str length close to limit) needs
    # the precise encode. Saves ~10 MB transient RSS on a max-size
    # payload that was previously rejected anyway.
    char_len = len(req.content)
    if char_len * 4 > MAX_WRITE_BYTES:
        if char_len > MAX_WRITE_BYTES \
                or len(req.content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"content exceeds {MAX_WRITE_BYTES // (1024 * 1024)} MB limit",
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, req.content)
    return {"ok": True, "size": target.stat().st_size}


# Default 100 MB cap per uploaded file. Override via MUSELAB_MAX_UPLOAD_MB.
MAX_UPLOAD_BYTES = int(os.environ.get("MUSELAB_MAX_UPLOAD_MB", "100")) * 1024 * 1024
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
    # Stream + enforce size cap. Write to a temporary file first, then
    # atomically rename to dest so a crash or size-exceeded abort never
    # leaves a partial file at the intended path.
    import uuid as _uuid
    tmp_path = dest.parent / f".~{dest.name}.{_uuid.uuid4().hex[:8]}.uploading"
    written = 0
    try:
        with tmp_path.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    f.close()
                    tmp_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"upload exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB cap",
                    )
                f.write(chunk)
        tmp_path.rename(dest)
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return {"ok": True, "path": str(dest.relative_to(ROOT)), "size": dest.stat().st_size}


class DeleteReq(BaseModel):
    path: str


@router.delete("/delete", dependencies=[Depends(require_token)])
def delete(req: DeleteReq, permanent: bool = Query(default=False)) -> dict:
    """Soft delete by default: move into <ROOT>/.muselab-dustbin/. The
    previous "must be empty for dirs" guard is dropped because the
    operation is now reversible — Restore moves the payload back to its
    original path. Set ?permanent=true for hard delete (skips trash).

    Refuses to delete the trash dir itself or anything inside it via this
    route — those operations have dedicated /trash/* endpoints with a
    different mental model."""
    target = safe_resolve(req.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="not found")
    trash_root = _trash_dir()
    if target == trash_root or trash_root in target.parents:
        raise HTTPException(
            status_code=400,
            detail="cannot delete trash via /delete — use /trash/purge or /trash/empty",
        )
    if permanent:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return {"ok": True, "permanent": True}
    manifest = _move_to_trash(target)
    return {"ok": True, "permanent": False,
            "trash_id": manifest["trash_id"], "manifest": manifest}


# ============================================================
# Trash management endpoints
# ============================================================
@router.get("/trash/list", dependencies=[Depends(require_token)])
def trash_list() -> dict:
    """All trash items, newest first. Each item: trash_id, original_path,
    original_name, deleted_at (unix sec, float), kind ('file'|'dir'), size."""
    return {"items": _list_trash()}


class TrashIdReq(BaseModel):
    trash_id: str


@router.post("/trash/restore", dependencies=[Depends(require_token)])
def trash_restore(req: TrashIdReq) -> dict:
    """Move the payload back to its original path. Fails 409 if that path
    is now occupied — user has to rename / clear it before restoring.
    Manifest is removed on success."""
    data = _read_manifest(req.trash_id)
    if not data:
        raise HTTPException(status_code=404, detail="trash item not found")
    payload = _trash_dir() / req.trash_id
    if not payload.exists():
        raise HTTPException(status_code=404, detail="trash payload missing")
    orig_rel = data.get("original_path") or ""
    if not orig_rel:
        raise HTTPException(status_code=500, detail="manifest missing original_path")
    # Reuse safe_resolve so the same anti-traversal + sensitive-name guards
    # apply to restoration as to any other write. allow_sensitive=True
    # because the user already had this file in-place before deletion;
    # blocking restore would leave their data stranded in trash.
    orig = safe_resolve(orig_rel, allow_sensitive=True)
    if orig.exists():
        raise HTTPException(
            status_code=409,
            detail="original path is occupied; rename or clear it first",
        )
    orig.parent.mkdir(parents=True, exist_ok=True)
    payload.rename(orig)
    mf = _trash_dir() / f"{req.trash_id}.json"
    if mf.exists():
        try:
            mf.unlink()
        except OSError:
            pass
    return {"ok": True, "restored_path": orig_rel}


@router.delete("/trash/purge", dependencies=[Depends(require_token)])
def trash_purge(req: TrashIdReq) -> dict:
    """Permanently delete one trash item. Irreversible."""
    d = _trash_dir()
    if not (d / f"{req.trash_id}.json").exists() and not (d / req.trash_id).exists():
        raise HTTPException(status_code=404, detail="trash item not found")
    _purge_one(req.trash_id)
    return {"ok": True}


@router.delete("/trash/empty", dependencies=[Depends(require_token)])
def trash_empty() -> dict:
    """Permanently delete every trash item. Irreversible."""
    d = _trash_dir()
    if not d.exists():
        return {"ok": True, "purged": 0}
    count = 0
    for mf in list(d.glob("*.json")):
        tid = mf.stem
        _purge_one(tid)
        count += 1
    return {"ok": True, "purged": count}


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
                 ".cache", ".pytest_cache", ".mypy_cache", "dist", "build",
                 # Trash always excluded from search/grep regardless of
                 # show_hidden — otherwise a search for "foo" surfaces every
                 # version of foo.md the user has ever deleted, which the
                 # trash UI is purpose-built to present separately.
                 TRASH_DIR_NAME}

GREP_EXTS = {".md", ".markdown", ".txt", ".html", ".htm", ".json", ".yaml", ".yml",
             ".py", ".js", ".ts", ".css", ".sh", ".toml", ".ini", ".csv", ".sql",
             ".log", ".xml", ".rst", ".tex"}


MAX_GREP_FILE_SIZE = 1_000_000   # 1MB per file — skip large files
MAX_GREP_TIME_SEC = 8            # soft time budget


@router.get("/grep", dependencies=[Depends(require_token)])
def grep(q: str, limit: int = 50, show_hidden: bool = False) -> dict:
    """Cross-platform full-text search (pure Python, no grep dependency)."""
    q_lower = q.strip().lower()
    if not q_lower:
        return {"hits": []}
    hits: list[dict] = []
    started = time.monotonic()
    timed_out = False
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # prune ignored dirs; hidden only if not requested
        dirnames[:] = [d for d in dirnames
                       if d not in SEARCH_IGNORE
                       and (show_hidden or not d.startswith("."))]
        if time.monotonic() - started > MAX_GREP_TIME_SEC:
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
                if _is_sensitive(Path(full)):
                    continue
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
            if time.monotonic() - started > MAX_GREP_TIME_SEC:
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
    for dirpath, dirnames, filenames in os.walk(ROOT):
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
