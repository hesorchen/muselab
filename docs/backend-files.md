# Files API

> [中文](backend-files_zh.md)

The Files API is the constrained browser surface for file management and preview. It is not muselab's only filesystem surface: Agent tools and real terminals have separate authority boundaries. See [Backend security model](backend-security.md).

## Workspace selection

Every request is bound to one registered workspace:

- `MUSELAB_ROOT` is the default;
- the `workspace=<absolute-path>` query parameter selects another root;
- normal fetch calls may instead use the URL-encoded `X-Muselab-Workspace` header.

Unregistered, removed, or disallowed directories are rejected. Returned paths are always relative to the selected workspace.

## Endpoints

There are currently 19 Files endpoints, all token-authenticated.

### Read and preview

| Method and path | Purpose |
|---|---|
| `GET /api/files/list` | List a directory, up to 500 entries |
| `GET /api/files/stat` | Read type, size, and modification time for one path |
| `GET /api/files/read` | Read text, up to 2 MiB |
| `GET /api/files/raw` | Inline supported image, media, PDF, HTML, and SVG; force-download other types |
| `GET /api/files/download` | Download as an attachment |
| `GET /api/files/xlsx` | Structured workbook preview |
| `GET /api/files/csv` | Paginated CSV/TSV preview |
| `GET /api/files/grep` | Bounded full-text search |
| `GET /api/files/search` | Filename and directory-name search |

`raw` and `download` serve iframes, images, or browser navigation and therefore accept a query token. HTML and SVG previews use a sandbox CSP. The HTML preview bridge for scroll and image interaction is injected only for files up to 12 MiB.

Preview limits:

- XLSX: 20 sheets, 500 rows and 50 columns per sheet, 500 characters per cell.
- CSV: 200 rows by default, up to 1,000 rows and 50 columns, 500 characters per cell; total rows are cached by file signature.
- grep: minimum 2-character query, 1 MB per file, about 8 seconds per scan, and at most two concurrent scans.

### Write and organize

| Method and path | Purpose |
|---|---|
| `PUT /api/files/write` | Atomically create or replace text, up to 10 MiB |
| `POST /api/files/upload` | Multipart upload, 100 MiB per file by default |
| `POST /api/files/mkdir` | Create a directory |
| `POST /api/files/rename` | Move or rename |
| `POST /api/files/copy-bak` | Create `.bak`, `.bak.2`, and later backup copies |
| `DELETE /api/files/delete` | Soft-delete by default; `permanent=true` deletes permanently |

A same-name upload first moves the old file into the dustbin and then atomically replaces it, preserving recovery. Dangerous executable extensions and sensitive filenames are rejected by default.

### Dustbin

| Method and path | Purpose |
|---|---|
| `GET /api/files/trash/list` | List the selected workspace's dustbin |
| `POST /api/files/trash/restore` | Restore one entry |
| `DELETE /api/files/trash/purge` | Permanently remove one entry |
| `DELETE /api/files/trash/empty` | Empty the selected workspace's dustbin |

Each workspace has its own `.muselab-dustbin/`:

```text
<workspace>/.muselab-dustbin/
├── <timestamp>_<8hex>
└── <timestamp>_<8hex>.json
```

Payload and manifest share one opaque ID. The default retention is 30 days, configurable through `MUSELAB_TRASH_TTL_DAYS`; `0` disables automatic cleanup.

## Path and sensitive-file defenses

Every path goes through normalization:

1. Reject NUL bytes and malformed paths.
2. Resolve the relative path under the selected workspace.
3. Follow symlinks and verify the real target still stays inside the root.
4. Apply sensitive-name checks to both the presented path and resolved target.

Typical blocked content includes `.env`, private keys, certificates, SSH credentials, cloud credentials, and common token files. Write, upload, rename, and backup operations cannot directly modify `.muselab-dustbin/`; only dedicated dustbin endpoints may do so.

These rules protect the browser Files interface. They are not a substitute for OS-level isolation.
