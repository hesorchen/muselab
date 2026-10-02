# Third-party licenses

muselab itself is [MIT licensed](LICENSE).

The application bundles or depends on the following third-party
components. Each is used under its original license. Inclusion in this
list is not an endorsement, and each upstream project retains its own
copyright.

If you redistribute muselab (a fork, a container image, a Show HN
mirror, etc.), keep this file intact and reproduce the upstream
license texts when required by the corresponding license.

> Inventory refreshed: 2026-09-08. Exact frontend versions, source URLs,
> SHA-256 digests and license files are recorded in
> [`frontend/vendor/manifest.json`](https://github.com/hesorchen/muselab/blob/main/frontend/vendor/manifest.json).

## Frontend — vendored under `frontend/vendor/`

These are shipped as local minified assets so muselab needs **no frontend build
step or runtime CDN**. Model and external-tool calls still require their
configured network access.

| Component | Version | License | Upstream |
|---|---|---|---|
| Alpine.js | v3.14.1 | MIT | <https://github.com/alpinejs/alpine> |
| marked | v13.0.0 | MIT | <https://github.com/markedjs/marked> |
| DOMPurify | v3.4.16 | Apache 2.0 / MPL 2.0 (dual) | <https://github.com/cure53/DOMPurify> |
| highlight.js | v11.10.0 + language extras + theme CSS | BSD 3-Clause | <https://github.com/highlightjs/highlight.js> |
| KaTeX (incl. fonts + `auto-render`) | v0.16.47 | MIT | <https://github.com/KaTeX/KaTeX> |
| CodeMirror (`cm/codemirror.min.{js,css}` + `addon/` + `mode/` + `theme/`) | v5.65.16 | MIT | <https://github.com/codemirror/codemirror5> |
| Mermaid (`mermaid.min.js`, diagram rendering) | v11.17.2 | MIT | <https://github.com/mermaid-js/mermaid> |
| xterm.js + FitAddon | v6.0.0 / v0.11.0 | MIT | <https://github.com/xtermjs/xterm.js> |

### License notes

- **Alpine.js, marked, KaTeX, CodeMirror, xterm.js** — MIT. Permission granted to
  use, copy, modify, distribute, sublicense, and sell, provided the
  original copyright notice + license text are preserved in
  substantial portions. Full license texts are supplied under
  `frontend/vendor/licenses/` (xterm keeps its existing adjacent license
  files); bundled notice comments are preserved. A minified version header
  alone is not a substitute for a full license text.
- **xterm.js** — the terminal renderer and fit addon are vendored under
  `frontend/vendor/xterm/`; their complete MIT texts are included alongside
  the scripts.
- **DOMPurify** — dual-licensed Apache License 2.0 or Mozilla Public
  License 2.0; user picks. muselab uses it under Apache 2.0 (compatible
  with this project's MIT distribution).
- **highlight.js** — BSD 3-Clause. The "neither the name of the
  copyright holder nor the names of its contributors may be used to
  endorse" clause applies; muselab does not use the highlight.js name
  for endorsement.

The Mermaid bundle includes the exact transitive package versions listed in
`manifest.json` under `bundled_packages` and `embedded_packages`. Their full
license and notice texts are in `frontend/vendor/licenses/mermaid/` and
`frontend/vendor/licenses/mermaid-embedded/`, including nested versions of the
same package. An upstream path-browserify source fragment with no identified
npm version has its own preserved MIT notice and source hash. The maintainer build and npm integrity lock are in
[`scripts/vendor/`](https://github.com/hesorchen/muselab/blob/main/scripts/vendor/README.md); ordinary installations use the
committed assets directly.

## Backend — Python dependencies (from `pyproject.toml`)

These are not vendored — they are installed via `uv` / `pip` at install
time. Licenses listed are the upstream-declared license at the version
floor muselab pins.

| Package | License | Upstream |
|---|---|---|
| `claude-agent-sdk` (= 0.2.152) | MIT | <https://github.com/anthropics/claude-agent-sdk-python> |
| `fastapi` (≥ 0.138.1) | MIT | <https://github.com/tiangolo/fastapi> |
| `starlette` (≥ 1.3.1) | BSD 3-Clause | <https://github.com/encode/starlette> |
| `uvicorn[standard]` (≥ 0.49.0) | BSD 3-Clause | <https://github.com/encode/uvicorn> |
| `python-dotenv` (≥ 1.2.2) | BSD 3-Clause | <https://github.com/theskumar/python-dotenv> |
| `python-multipart` (≥ 0.0.32) | Apache 2.0 | <https://github.com/Kludex/python-multipart> |
| `watchfiles` (≥ 1.2.0) | MIT | <https://github.com/samuelcolvin/watchfiles> |
| `httpx` (≥ 0.27) | BSD 3-Clause | <https://github.com/encode/httpx> |
| `openpyxl` (≥ 3.1.5) | MIT | <https://foss.heptapod.net/openpyxl/openpyxl> |
| `pywebpush` (≥ 2.0) | MPL 2.0 | <https://github.com/web-push-libs/pywebpush> |
| `aiohttp` (≥ 3.14.3) | Apache 2.0 | <https://github.com/aio-libs/aiohttp> |
| `Pillow` (≥ 12.3.0) | HPND | <https://github.com/python-pillow/Pillow> |
| `PyJWT[crypto]` (≥ 2.13.0) | MIT | <https://github.com/jpadilla/pyjwt> |
| `psycopg[binary]` (≥ 3.2.9) | LGPL 3.0 | <https://github.com/psycopg/psycopg> |

`fastapi`, `uvicorn`, and `python-multipart` pull transitive deps
(`starlette`, `pydantic`, `h11`, `httptools`, `websockets`,
`cryptography`, etc.). Run `uv tree` against the local lock for the exact
graph; preserve the license and notice files in the installed distributions.
This table covers direct dependencies and does not replace the built image
inventory for transitive packages or operating-system components.

### Dev-only

Not shipped to end users; relevant only if you hack on muselab:

| Package | License |
|---|---|
| `pytest`, `pytest-asyncio`, `pytest-playwright` | MIT |
| `ruff` | MIT |

## Runtime components and container distribution

A native source installation uses locally installed Python, uv, Node.js and
Claude Code. The Docker image also **redistributes runtime components**: its
base image supplies Python, its build installs Node.js and a pinned Claude
Code npm package, and it copies uv plus the Python virtual environment. The
Claude Agent SDK package includes its own bundled CLI. Consult `Dockerfile`,
`scripts/versions.env`, `uv.lock` and the built image's package inventory for
exact runtime versions; these components are not all covered by MuseLab's MIT
license.

| Component | License / source of terms | Distribution facts |
|---|---|---|
| Python | PSF license and bundled third-party notices | Present in the Python base image |
| Node.js | MIT and bundled third-party notices | Installed in the runtime image |
| uv | Apache 2.0 / MIT | Binary copied into the runtime image |
| Claude Agent SDK 0.2.152 | Package's MIT license | Installed in the virtual environment; package includes a bundled CLI |
| Claude Code CLI | Its own version-specific license and Anthropic terms | Pinned npm CLI and SDK-bundled CLI are separate runtime artifacts |

Application and SDK licensing do not by themselves grant rights to redistribute
Claude Code. Image publishers must review the terms accompanying the exact
CLI artifacts they ship; this inventory does not make a legal determination
about those separate terms. Keep upstream package licenses/notices in the
image, and distribute this file, the application `LICENSE`, and the frontend
license directory alongside the application.

## Reporting attribution issues

If something here is wrong (wrong license, wrong version, missing
attribution), open an issue at
<https://github.com/hesorchen/muselab/issues> with the component name
and the upstream URL + license text. We treat attribution bugs as P0.
