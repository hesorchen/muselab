# Third-party licenses

muselab itself is [MIT licensed](LICENSE).

The application bundles or depends on the following third-party
components. Each is used under its original license. Inclusion in this
list is not an endorsement, and each upstream project retains its own
copyright.

If you redistribute muselab (a fork, a container image, a Show HN
mirror, etc.), keep this file intact and reproduce the upstream
license texts when required by the corresponding license.

> Verified: 2026-07-23. Spot-check the upstream repository if you need
> the canonical license text for a specific version.

## Frontend — vendored under `frontend/vendor/`

These are shipped verbatim (minified) so muselab needs **no frontend build
step or runtime CDN**. Model and external-tool calls still require their
configured network access.

| Component | Version family | License | Upstream |
|---|---|---|---|
| Alpine.js | v3 | MIT | <https://github.com/alpinejs/alpine> |
| marked | v15.x | MIT | <https://github.com/markedjs/marked> |
| DOMPurify | v3.x | Apache 2.0 / MPL 2.0 (dual) | <https://github.com/cure53/DOMPurify> |
| highlight.js | v11.x + language extras + theme CSS | BSD 3-Clause | <https://github.com/highlightjs/highlight.js> |
| KaTeX (incl. fonts + `auto-render`) | v0.16.x | MIT | <https://github.com/KaTeX/KaTeX> |
| CodeMirror (`cm/codemirror.min.{js,css}` + `addon/` + `mode/` + `theme/`) | v5.65.16 | MIT | <https://github.com/codemirror/codemirror5> |
| Mermaid (`mermaid.min.js`, diagram rendering) | v11.x | MIT | <https://github.com/mermaid-js/mermaid> |
| xterm.js + FitAddon | v6.0.0 / v0.11.0 | MIT | <https://github.com/xtermjs/xterm.js> |

### License notes

- **Alpine.js, marked, KaTeX, CodeMirror, xterm.js** — MIT. Permission granted to
  use, copy, modify, distribute, sublicense, and sell, provided the
  original copyright notice + license text are preserved in
  substantial portions. The notices live inside the minified files
  themselves (header comments) and are not stripped by muselab.
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

## Skills — bundled under `skills/`

These are SKILL.md instruction files (plain text, no compiled code)
shipped with muselab to give Muse out-of-the-box capabilities.
The first eight are muselab-native; the remaining four are community-authored
and included here with attribution.

| Skill | Author / Repo | License | Upstream |
|---|---|---|---|
| `citation-formatter` | muselab contributors | MIT | this repo |
| `code-reviewer` | muselab contributors | MIT | this repo |
| `markdown-formatter` | muselab contributors | MIT | this repo |
| `mermaid-helper` | muselab contributors | MIT | this repo |
| `summary-distiller` | muselab contributors | MIT | this repo |
| `task-decomposer` | muselab contributors | MIT | this repo |
| `web-search` | muselab contributors | MIT | this repo |
| `archive-curator` | muselab contributors | MIT | this repo |
| `pptx` | tfriedel / claude-office-skills | not specified† | <https://github.com/tfriedel/claude-office-skills> |
| `csv-analyzer` | coffeefuelbump | not specified† | <https://github.com/coffeefuelbump/csv-data-summarizer-claude-skill> |
| `translate` | feiskyer | MIT | <https://github.com/feiskyer/claude-code-settings> |
| `meeting-notes` | claude-office-skills contributors | MIT | <https://github.com/claude-office-skills/skills> |

† These repositories do not include an explicit `LICENSE` file at the
time of inclusion (2026-05-23). The SKILL.md files are plain natural-
language instructions with no executable code. If you are a copyright
holder of either upstream repository and wish to add a license, please
open an issue at the upstream URL above. muselab will update attribution
accordingly.

## Backend — Python dependencies (from `pyproject.toml`)

These are not vendored — they are installed via `uv` / `pip` at install
time. Licenses listed are the upstream-declared license at the version
floor muselab pins.

| Package | License | Upstream |
|---|---|---|
| `claude-agent-sdk` (≥ 0.2.120, < 0.3) | MIT | <https://github.com/anthropics/claude-agent-sdk-python> |
| `fastapi` (≥ 0.138.1) | MIT | <https://github.com/tiangolo/fastapi> |
| `starlette` (≥ 1.3.1) | BSD 3-Clause | <https://github.com/encode/starlette> |
| `uvicorn[standard]` (≥ 0.49.0) | BSD 3-Clause | <https://github.com/encode/uvicorn> |
| `python-dotenv` (≥ 1.2.2) | BSD 3-Clause | <https://github.com/theskumar/python-dotenv> |
| `python-multipart` (≥ 0.0.32) | Apache 2.0 | <https://github.com/Kludex/python-multipart> |
| `openpyxl` (≥ 3.1.5) | MIT | <https://foss.heptapod.net/openpyxl/openpyxl> |
| `pywebpush` (≥ 2.0) | MPL 2.0 | <https://github.com/web-push-libs/pywebpush> |
| `aiohttp` (≥ 3.14.0) | Apache 2.0 | <https://github.com/aio-libs/aiohttp> |
| `Pillow` (≥ 12.3.0) | HPND | <https://github.com/python-pillow/Pillow> |
| `PyJWT[crypto]` (≥ 2.13.0) | MIT | <https://github.com/jpadilla/pyjwt> |

`fastapi`, `uvicorn`, and `python-multipart` pull transitive deps
(`starlette`, `pydantic`, `h11`, `httptools`, `websockets`,
`cryptography`, etc.) — all permissively licensed (MIT / BSD / Apache
2.0 / MPL). Run `uv tree` against your local lock for the exact graph.

### Dev-only

Not shipped to end users; relevant only if you hack on muselab:

| Package | License |
|---|---|
| `pytest`, `pytest-asyncio`, `pytest-playwright` | MIT |
| `ruff` | MIT |

## External runtime — required at the install boundary

These run **alongside** muselab; they are not bundled, not statically
linked, and not redistributed. muselab calls them as subprocesses /
HTTP services. They have their own license terms that bind the user,
not this project.

| Component | Role | Where it comes from |
|---|---|---|
| Claude Code CLI (`claude`) | Drives the agent loop muselab dispatches against | Anthropic, installed by the user (`npm install -g @anthropic-ai/claude-code`) |
| Node.js | Runtime for the CLI above | <https://nodejs.org/> |
| Python 3.12+ | muselab itself runs on it | <https://www.python.org/> |
| `uv` | Python tooling, used by installer | <https://github.com/astral-sh/uv> (Apache 2.0 / MIT) |

## Reporting attribution issues

If something here is wrong (wrong license, wrong version, missing
attribution), open an issue at
<https://github.com/hesorchen/muselab/issues> with the component name
and the upstream URL + license text. We treat attribution bugs as P0.
