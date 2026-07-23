"""Short starter messages for muselab's built-in workflows.

Detailed reusable instructions belong in SDK-native Skills, not in a
muselab-owned system prompt. The archive workflow lives in
``skills/archive-curator/SKILL.md`` and is invoked explicitly below.
"""

CURATOR_INITIAL_MESSAGE = {
    "zh": "请使用 archive-curator skill 扫描并整理我的 archive，按工作流先分析、再让我确认后执行。",
    "en": "Use the archive-curator skill to scan and organize my archive. Analyze first, then ask for confirmation before making changes.",
}
