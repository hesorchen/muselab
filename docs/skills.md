# Skills

> [简体中文](skills_zh.md)

Skills are SKILL.md instruction packs that the Claude Agent SDK loads at
startup and makes available to Muse. When a task matches a skill's trigger,
the model reads the skill's body and follows its protocol — no extra wiring
required on your end. Skills work the same way in interactive chat,
[scheduled tasks](scheduler.md), and any other context that runs the full
agent loop.

**Example.** A skill called `changelog-formatter` might have a
`description` starting with `"USE WHEN the user asks to format or generate
a CHANGELOG entry"`. Whenever you ask Muse to write a changelog, the SDK
surfaces that skill and the model adopts its output conventions
automatically.

---

## Bundled skills

Muselab ships 12 skills out of the box. The first eight are muselab-native
(MIT); the last four are community-contributed and included with
attribution — see `THIRD_PARTY_LICENSES.md` for upstream URLs and license
details.

| Skill | What it does | Origin | External deps |
|---|---|---|---|
| `web-search` | Translates vague queries into targeted searches, opens at least one source to confirm recency, returns a cited answer with dates | muselab-native | `WebSearch` / `WebFetch` tool or `mcp__fetch__fetch` |
| `markdown-formatter` | Normalises heading hierarchy, lists, tables, code fences, math delimiters, and Chinese full-width punctuation; returns the rewritten doc only | muselab-native | none |
| `mermaid-helper` | Picks the right Mermaid diagram type, writes validated syntax, returns a fenced block plus a short explanation | muselab-native | none |
| `code-reviewer` | Reviews code by severity order (bugs → security → correctness → performance → maintainability), with line references and fix snippets | muselab-native | none |
| `citation-formatter` | Converts DOIs, arXiv IDs, PubMed IDs, and raw text into APA 7 / IEEE / GB/T 7714 / BibTeX; fetches authoritative metadata when possible | muselab-native | `WebFetch` or `mcp__fetch__fetch` (optional) |
| `task-decomposer` | Turns a vague goal into an ordered task list with size estimates, a Definition of Done, critical-path steps, and flagged unknowns | muselab-native | none |
| `summary-distiller` | Picks the right summary shape (TL;DR, key points, structured, action items) based on source type; preserves numbers, names, and dates verbatim | muselab-native | none |
| `archive-curator` | Scans and organizes the personal archive, proposes changes before mutation, and fills meaningful `CLAUDE.md` gaps conversationally | muselab-native | none |
| `pptx` | Generates PowerPoint files by writing and running inline Python with `python-pptx` via the Bash tool | community | `python-pptx` (`pip install python-pptx`) |
| `csv-analyzer` | Loads a CSV with `pandas`, profiles column types, generates conditional charts (PNG), outputs a complete analysis in one response | community | `pandas`; `matplotlib`/`seaborn` optional |
| `translate` | Three-stage internal pipeline (literal → issue identification → polished reinterpretation); outputs final Chinese text only, preserving technical terms | community | none |
| `meeting-notes` | Extracts decisions, action items (with owners and due dates), and next steps from raw notes or transcripts using four ready-made templates | community | none |

---

## How discovery works

Skill discovery is controlled by SDK-native options passed to
`ClaudeAgentOptions`:

**`setting_sources`:**

```python
setting_sources=["user", "project", "local"]
```

This tells the SDK to load `CLAUDE.md` and Claude configuration from three
scopes:

| Scope | Resolves to |
|---|---|
| `user` | `~/.claude/` — user-global config shared with Claude Code CLI |
| `project` | the archive `cwd` (see below) |
| `local` | `.claude/` inside `cwd` |

**`cwd` is the active workspace:**

```python
cwd=str(workspace_root)
```

Because the active workspace is not the muselab checkout, muselab also passes
its repository as a local SDK plugin:

```python
plugins=[{"type": "local", "path": "<muselab-repo>"}]
```

That plugin exposes the bundled `skills/` directory in every workspace.
Output files produced by skills such as `pptx` or `csv-analyzer` still land
in the active workspace unless you specify an explicit path.

**`skills="all"`:**

```python
if not skills_off:
    opts_kwargs["skills"] = "all"
```

When this flag is set, the SDK loads discoverable `SKILL.md` files for every
provider. There is no copy or symlink step: bundled skills are exposed by the
local plugin.

**UI listing.** The `GET /api/settings/skills` endpoint independently
enumerates bundled, user-global, and installed-plugin skills for the frontend.
Both `SKILL.md` and `skill.md` filenames are accepted. This listing is
read-only and has no effect on what the model uses at runtime.

---

## Adding your own skill

### Where to put it

| Location | Scope | Visible to |
|---|---|---|
| `<muselab-repo>/skills/your-skill/SKILL.md` | project | muselab only |
| `~/.claude/skills/your-skill/SKILL.md` | user | muselab + all Claude Code projects |

Repository skills are plugin-qualified internally, so they can coexist with a
user-global skill of the same short name.

### Required structure

```
skills/your-skill/
└── SKILL.md          ← must contain YAML frontmatter
```

The frontmatter block must include at minimum `name` and `description`:

```yaml
---
name: your-skill
description: "USE WHEN ... — one sentence describing the trigger and capability"
---
```

The body is free-form Markdown that the model reads on every invocation —
keep it concise. Recommended practices are listed in `skills/README.md`:

- Start `description` with `"USE WHEN ..."` — this is the primary signal
  the model uses to select a skill.
- Use a table to map scenarios to actions.
- Include a `NOT use when` section to prevent overuse.
- Optional: place reference scripts (`*.py`) or config (`config.yaml`) in
  the same subdirectory and reference them from the SKILL.md body.

### Restart required

Skills are loaded during SDK initialisation. After adding or editing a
skill, restart the muselab service:

**Linux (systemd):**
```bash
systemctl --user restart muselab
```

**macOS (launchd):**
```bash
launchctl kickstart -k "gui/$(id -u)/com.muselab"
```

---

## Kill switch

Skills are enabled for every provider by default. To disable them globally,
set the following in your `.env`:

```
MUSELAB_DISABLE_SKILLS=1
```

Accepted values: `1`, `true`, `yes` (case-insensitive).

---

*Related: [architecture.md](architecture.md) · [routing.md](routing.md) · [providers.md](providers.md)*
