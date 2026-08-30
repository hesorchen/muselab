# Skills

> [简体中文](skills_zh.md)

Skills are `SKILL.md` instruction packs discovered by the Claude Agent SDK.
When a task matches a Skill's description, the model can load its reusable
workflow. The same mechanism is available in interactive chat,
[scheduled tasks](scheduler.md), and other contexts that run the full agent
loop.

Muselab's default checkout does **not** bundle any Skill payloads. The
repository keeps an empty `skills/` extension slot and the general discovery,
listing, and reviewed-generation mechanisms.

Upgrades do not preserve the former preset names. Saved prompts, scheduled
tasks, or external clients that explicitly invoke names such as
`archive-curator`, `workspace-curator`, or `web-search` must be rewritten to
describe the task directly or supplied with a user/plugin replacement Skill.

## Supported sources

Muselab preserves Skills from these sources:

- user-global Skills under `~/.claude/skills/`;
- project and local Skills discovered from the active workspace;
- installed Claude plugins;
- reviewed generated Skills;
- optional repository-local Skills added under `<muselab-repo>/skills/`.

The Settings and chat Skills views dynamically enumerate repository-extension,
user-global, and installed-plugin Skills. Active-workspace project/local Skills
remain SDK-native runtime discovery and are not represented by that management
list. Neither path relies on a fixed preset catalog.

## How discovery works

Muselab passes SDK-native discovery options to `ClaudeAgentOptions`:

```python
setting_sources=["user", "project", "local"]
cwd=str(workspace_root)
plugins=[{"type": "local", "path": "<muselab-repo>"}]
skills="all"
```

`cwd` is the active workspace, so project and local configuration follow the
workspace selected for the session. The local plugin keeps the repository's
empty `skills/` extension slot available without copying or symlinking files.
User and plugin Skills continue to use the SDK's normal discovery paths.

Third-party providers use an isolated `CLAUDE_CONFIG_DIR` to prevent Claude
OAuth credentials from leaking or being used as a fallback. Muselab maps only
`~/.claude/skills/` into that isolated user scope; active-workspace project/local
Skills and the explicit repository-extension plugin remain available, while
installed user plugins, settings, hooks, credentials, and transcripts stay
isolated. A Skill supplied only by an installed user plugin is therefore not
available on an isolated third-party route.

`GET /api/settings/skills` independently enumerates repository-extension,
user-global, and installed-plugin Skills for the read-only frontend list. Both
`SKILL.md` and `skill.md` filenames are accepted. The UI listing does not include
active-workspace project/local Skills and does not control runtime activation.

## Adding a Skill

Common locations are:

| Location | Scope |
|---|---|
| `<workspace>/.claude/skills/your-skill/SKILL.md` | active workspace |
| `~/.claude/skills/your-skill/SKILL.md` | user-global |
| `<muselab-repo>/skills/your-skill/SKILL.md` | muselab repository extension |

A minimal Skill looks like this:

```yaml
---
name: your-skill
description: "USE WHEN ... — describe the trigger and capability"
---
```

Put the reusable workflow and its safety boundaries in the Markdown body. Keep
it concise, include non-trigger examples where useful, and place optional
scripts or references beside `SKILL.md`. Restart a native installation after
adding or editing a Skill so new SDK clients can discover it. Docker deployments
copy `skills/` into the image, so rebuild and recreate the service instead of
only restarting it:

```bash
docker compose up -d --build --force-recreate
```

## Kill switch

Skills are enabled for every full agent runtime by default. To disable them
for muselab sessions, set:

```text
MUSELAB_DISABLE_SKILLS=1
```

Accepted values are `1`, `true`, and `yes` (case-insensitive). Muselab then
passes an explicit empty Skill list (`skills=[]`) to the SDK so SDK defaults
cannot re-enable discovery.

*Related: [architecture.md](architecture.md) · [routing.md](routing.md) · [providers.md](providers.md)*
