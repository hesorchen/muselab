"""Short starter messages for muselab's built-in workflows.

Detailed reusable instructions belong in SDK-native Skills, not in a
muselab-owned system prompt. The workspace workflow lives in
``skills/workspace-curator/SKILL.md`` and is invoked explicitly below.
"""

CURATOR_INITIAL_MESSAGE = {
    "zh": (
        "请使用 workspace-curator skill 扫描当前工作区并提出整理方案，"
        "先分析，再让我确认后执行。"
    ),
    "en": (
        "Use the workspace-curator skill to scan the current workspace and "
        "propose an organization plan. Analyze first, then ask for confirmation "
        "before making changes."
    ),
}
