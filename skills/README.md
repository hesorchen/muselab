# skills/

This directory is muselab's empty extension slot for repository-local Skills.
Muselab exposes the repository as a local Claude Agent SDK plugin and enables
all discoverable Skills by default. The default checkout does not bundle any
`SKILL.md` payloads.

本目录是 muselab 为仓库级 Skill 保留的空扩展槽。muselab 将仓库作为本地
Claude Agent SDK plugin 暴露，并默认启用所有可发现的 Skills。默认 checkout
不预装任何 `SKILL.md` payload。

## Add a repository-local Skill / 添加仓库级 Skill

1. Create `skills/your-skill/`.
2. Add `SKILL.md` with YAML frontmatter:

   ```yaml
   ---
   name: your-skill
   description: "USE WHEN ... — describe the trigger and capability"
   ---
   ```

3. Put the reusable workflow and safety boundaries in the body. Optional scripts
   or references may live beside it.
4. Restart a native installation so the SDK can discover the new Skill. Docker
   deployments must rebuild and recreate the image; see `docs/skills.md`.

1. 新建 `skills/your-skill/`。
2. 添加带上述 YAML frontmatter 的 `SKILL.md`。
3. 在正文中写明可复用流程与安全边界；可选脚本或参考资料可放在同一目录。
4. 原生安装重启 muselab；Docker 部署需重新构建并创建镜像，详见
   `docs/skills_zh.md`。

## Other supported sources / 其他支持来源

Muselab also preserves SDK discovery from workspace/project scopes,
`~/.claude/skills/`, installed plugins, and reviewed generated Skills. The
Settings and chat Skills views dynamically list repository-extension,
user-global, and installed-plugin Skills; active-workspace Skills remain
runtime-only discovery. Neither path depends on repository presets.

muselab 同时保留 workspace/project scope、`~/.claude/skills/`、已安装 plugin
以及经审核生成的 Skills。Settings 与对话区的 Skills 界面动态展示仓库扩展、用户
全局和已安装 plugin Skills；当前 workspace Skills 仍只由运行时发现。两条路径都
不依赖仓库预设。

Set `MUSELAB_DISABLE_SKILLS=1` to pass an explicit empty Skill list to the SDK
and disable discovery for muselab sessions.

设置 `MUSELAB_DISABLE_SKILLS=1` 后，muselab 会向 SDK 显式传入空 Skill 列表，
从而关闭会话中的 Skill 发现。
